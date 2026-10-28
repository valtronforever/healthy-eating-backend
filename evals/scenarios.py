"""Evaluation scenarios for the HealthyEating MCP tools.

Each scenario talks to a real model through opencode and then checks what reached the
database, which tool calls were made (recorded by the proxy) and what the user was told.
Required checks decide pass or fail; notes are reported but never fail a run.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from evals.environment import FIXTURES_DIR, EvalEnvironment, Turn
from evals.personas import CALORIE_PROJECT, CHAT, CHAT_WITH_MEMORY
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, OPTIONAL_NUTRIENT_FIELDS
from healthy_eating.services.settings import SettingsChanges, TargetRange

HEALTHY_EATING = re.compile(r"healthy ?eating", re.IGNORECASE)
POSITIVE = re.compile(
    r"\b(saved|updated|logged|recorded|added|imported)\b"
    # "They're in HealthyEating", "it is now in HealthyEating"
    r"|(?:\b(?:are|is)|['\u2019](?:re|s))\s+(?:all\s+|now\s+)?in\s+healthy ?eating",
    re.IGNORECASE,
)
NEGATIVE = re.compile(
    r"\b(not|never|unable|failed|couldn['\u2019]?t|wasn['\u2019]?t|didn['\u2019]?t|isn['\u2019]?t)\b",
    re.IGNORECASE,
)
# A sentence ends at . ! ? or a line break, but not at a decimal point ("177.4 lb").
SENTENCE = re.compile(r"(?:[^.!?\n]|(?<=\d)\.(?=\d))+")


def _healthy_eating_sentences(reply: str) -> list[str]:
    return [m.group(0) for m in SENTENCE.finditer(reply) if HEALTHY_EATING.search(m.group(0))]


def confirms_saved(reply: str) -> bool:
    """The reply tells the user the meal reached HealthyEating (saved or updated)."""
    return any(
        POSITIVE.search(sentence) and not NEGATIVE.search(sentence)
        for sentence in _healthy_eating_sentences(reply)
    )


def reports_not_saved(reply: str) -> bool:
    """The reply tells the user the meal did NOT reach HealthyEating."""
    return any(
        NEGATIVE.search(sentence) and re.search(r"sav|log|record", sentence, re.IGNORECASE)
        for sentence in _healthy_eating_sentences(reply)
    )


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    required: bool = True


@dataclass
class Checks:
    items: list[Check] = field(default_factory=list)

    def require(self, name: str, passed: bool, detail: str = "") -> bool:
        self.items.append(Check(name, passed, detail))
        return passed

    def note(self, name: str, passed: bool, detail: str = "") -> None:
        self.items.append(Check(name, passed, detail, required=False))

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.items if check.required)


Run = Callable[[EvalEnvironment, Checks, list[Turn]], Awaitable[None]]


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    run: Run


def _values(meal: Meal) -> tuple[float, float, float, float]:
    return (meal.calories_kcal, meal.protein_g, meal.fat_g, meal.carbohydrates_g)


def _describe_meals(meals: list[Meal]) -> str:
    if not meals:
        return "no meals saved"
    return "; ".join(
        f"{m.dish_name}: {_values(m)} source={m.source} estimated={m.estimated_fields}"
        for m in meals
    )


def _require_turn_ok(checks: Checks, turn: Turn, label: str) -> bool:
    ok = turn.exit_code == 0 and bool(turn.reply)
    return checks.require(
        f"{label}: opencode returned a reply",
        ok,
        f"exit code {turn.exit_code}; stderr: {turn.stderr[-300:]}" if not ok else "",
    )


def _log_meal_results(env: EvalEnvironment) -> list[dict[str, Any]]:
    assert env.proxy is not None
    return [call.structured_result or {} for call in env.proxy.tool_calls("log_meal")]


def _require_confirmation(env: EvalEnvironment, checks: Checks, reply: str) -> None:
    checks.require("user told it was saved", confirms_saved(reply), reply[-300:])
    results = _log_meal_results(env)
    message = str(results[-1].get("user_message", "")) if results else ""
    checks.note(
        "repeated the tool's user_message word for word",
        bool(message) and message.rstrip(".") in reply,
        message,
    )


async def numbers_from_conversation(
    env: EvalEnvironment, checks: Checks, turns: list[Turn]
) -> None:
    turn = await env.chat(
        CHAT.name,
        "For lunch I had grilled chicken breast (200 g) with boiled rice (150 g). "
        "I already worked it out: 520 kcal, 66 g protein, 8 g fat, 45 g carbs.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    checks.require("exactly one meal saved", len(meals) == 1, _describe_meals(meals))
    if meals:
        meal = meals[0]
        checks.require(
            "user's numbers stored unchanged",
            _values(meal) == (520, 66, 8, 45),
            f"stored {_values(meal)}",
        )
        checks.require(
            "source is from_conversation", meal.source == "from_conversation", meal.source
        )
        checks.require(
            "none of the user's four numbers marked as estimated",
            not set(meal.estimated_fields) & set(NUTRIENT_FIELDS),
            str(meal.estimated_fields),
        )
        added = [name for name in OPTIONAL_NUTRIENT_FIELDS if getattr(meal, name) is not None]
        checks.require(
            "fiber, sugar or sodium the assistant added are marked as estimated",
            set(added) <= set(meal.estimated_fields),
            f"added {added}, estimated {meal.estimated_fields}",
        )
    checks.require(
        "never asks for the user's weight",
        not WEIGHT_QUESTION.search(turn.reply),
        turn.reply[-300:],
    )
    _require_confirmation(env, checks, turn.reply)


async def photo(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    first = await env.chat(
        CHAT.name, "Here's my breakfast.", files=[FIXTURES_DIR / "breakfast.jpg"]
    )
    turns.append(first)
    if not _require_turn_ok(checks, first, "reply"):
        return
    checks.require(
        "answer includes an estimate (not only a question)",
        "kcal" in first.reply.lower() or "calorie" in first.reply.lower(),
        first.reply[:300],
    )
    last = first
    meals = await env.meals()
    asked = not meals and "?" in first.reply
    checks.note("saved without a follow-up question", bool(meals), first.reply[-200:])
    if asked:
        # The assistant may ask about something it cannot see (as our tool allows). Answer it
        # the way a user would and expect the meal to be saved then.
        last = await env.chat(
            CHAT.name,
            "Just what you see on the plate, plus water and black coffee. Use your best estimate.",
            session_id=first.session_id,
        )
        turns.append(last)
        if not _require_turn_ok(checks, last, "reply to the follow-up"):
            return
        meals = await env.meals()
    checks.require("exactly one meal saved", len(meals) == 1, _describe_meals(meals))
    if meals:
        meal = meals[0]
        checks.require(
            "source is estimated_by_assistant",
            meal.source == "estimated_by_assistant",
            meal.source,
        )
        checks.require(
            "all four values present and plausible",
            100 <= meal.calories_kcal <= 2500 and all(v >= 0 for v in _values(meal)),
            f"stored {_values(meal)}",
        )
        checks.note("confidence", True, meal.confidence)
        given = {name: getattr(meal, name) for name in OPTIONAL_NUTRIENT_FIELDS}
        checks.note(
            "fiber, sugar and sodium estimated",
            all(value is not None for value in given.values()),
            str(given),
        )
    _require_confirmation(env, checks, last.reply)


async def existing_workflow(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CALORIE_PROJECT.name, "Breakfast today: two fried eggs and a slice of buttered toast."
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    reply = turn.reply
    checks.require(
        "answer keeps the project's format",
        all(label in reply for label in ("Calories:", "Protein:", "Fat:", "Carbs:")),
        reply[:300],
    )
    rows = env.sheet_rows()
    checks.require("Google Sheets row still added (exactly one)", len(rows) == 1, str(rows))
    meals = await env.meals()
    checks.require(
        "exactly one meal saved to HealthyEating", len(meals) == 1, _describe_meals(meals)
    )
    if rows and meals:
        row, meal = rows[0], meals[0]
        sheet_values = (row["calories_kcal"], row["protein_g"], row["fat_g"], row["carbs_g"])
        checks.require(
            "same numbers in Sheets and HealthyEating",
            all(abs(a - b) <= 0.01 for a, b in zip(sheet_values, _values(meal), strict=True)),
            f"sheets {sheet_values}, healthyeating {_values(meal)}",
        )
        order = [use.tool for use in turn.tool_uses]
        sheets_at = order.index("sheets_append_row") if "sheets_append_row" in order else None
        ours_at = (
            order.index("healthyeating_log_meal") if "healthyeating_log_meal" in order else None
        )
        checks.require(
            "existing workflow not pushed behind HealthyEating (Sheets called first or together)",
            sheets_at is not None and ours_at is not None and sheets_at <= ours_at,
            f"tool order: {order}",
        )
        checks.note(
            "source chosen for the project's own estimate",
            True,
            f"source={meal.source}, estimated={meal.estimated_fields}, note={meal.source_note}",
        )
    _require_confirmation(env, checks, reply)


async def missing_portion(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    first = await env.chat(CHAT.name, "I just had some soup.")
    turns.append(first)
    if not _require_turn_ok(checks, first, "first reply"):
        return
    checks.require("assistant asks a follow-up question", "?" in first.reply, first.reply[-300:])
    early = await env.meals()
    checks.require(
        "at most one meal saved before the answer", len(early) <= 1, _describe_meals(early)
    )
    if early:
        # Allowed: a rough estimate is saved now and refined after the answer.
        _require_confirmation(env, checks, first.reply)
    else:
        checks.require(
            "user told it is not saved yet", reports_not_saved(first.reply), first.reply[-300:]
        )
    checks.note("saved a rough estimate before the answer", bool(early), _describe_meals(early))

    second = await env.chat(
        CHAT.name,
        "It was a big bowl of homemade chicken noodle soup, about 500 ml.",
        session_id=first.session_id,
    )
    turns.append(second)
    if not _require_turn_ok(checks, second, "second reply"):
        return
    checks.require(
        "no new question after the user answered", "?" not in second.reply, second.reply[-300:]
    )
    meals = await env.meals()
    checks.require(
        "exactly one meal after the answer (no duplicate)", len(meals) == 1, _describe_meals(meals)
    )
    if early and meals:
        checks.require("the early estimate was updated in place", meals[0].id == early[0].id, "")
    assert env.proxy is not None
    if not early or len(env.proxy.tool_calls("log_meal")) > 1:
        _require_confirmation(env, checks, second.reply)


async def earlier_meal(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "Yesterday evening I had a cheeseburger with fries. "
        "I worked it out: 980 kcal, 45 g protein, 52 g fat, 85 g carbs.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    checks.require("exactly one meal saved", len(meals) == 1, _describe_meals(meals))
    if meals:
        meal = meals[0]
        checks.require(
            "user's numbers stored unchanged",
            _values(meal) == (980, 45, 52, 85),
            f"stored {_values(meal)}",
        )
        hours_ago = (utcnow() - meal.eaten_at).total_seconds() / 3600
        checks.require(
            "dated yesterday, not 'now' (time of the meal kept)",
            hours_ago >= 3,
            f"eaten_at={meal.eaten_at.isoformat()} ({hours_ago:.1f} h ago)",
        )
        checks.note(
            "UTC offset given",
            meal.utc_offset_minutes is not None,
            f"offset={meal.utc_offset_minutes}",
        )
    checks.require(
        "does not ask the user for a UTC offset or time zone",
        not re.search(r"offset|time ?zone", turn.reply, re.IGNORECASE),
        turn.reply[-300:],
    )
    _require_confirmation(env, checks, turn.reply)


async def time_zone(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "I live in Berlin. Breakfast this morning at 8: muesli with yogurt. "
        "I worked it out: 420 kcal, 18 g protein, 12 g fat, 58 g carbs.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    stored = await env.time_zone()
    checks.require("time zone stored as Europe/Berlin", stored == "Europe/Berlin", str(stored))
    meals = await env.meals()
    checks.require("exactly one meal saved", len(meals) == 1, _describe_meals(meals))
    if meals:
        meal = meals[0]
        checks.require(
            "user's numbers stored unchanged",
            _values(meal) == (420, 18, 12, 58),
            f"stored {_values(meal)}",
        )
        local = meal.eaten_at.astimezone(ZoneInfo("Europe/Berlin"))
        checks.note(
            "eaten at 08:00 Berlin time",
            (local.hour, local.minute) == (8, 0),
            f"eaten_at={local.isoformat()} offset={meal.utc_offset_minutes}",
        )
    assert env.proxy is not None
    order = [call.name for call in env.proxy.calls]
    checks.note(
        "set_timezone called before log_meal",
        "set_timezone" in order
        and "log_meal" in order
        and order.index("set_timezone") < order.index("log_meal"),
        str(order),
    )
    checks.require(
        "does not ask the user for a time zone",
        not re.search(r"time ?zone\?|which time ?zone|what time ?zone", turn.reply, re.IGNORECASE),
        turn.reply[-300:],
    )
    _require_confirmation(env, checks, turn.reply)


async def day_complete(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "Dinner was a bowl of lentil soup, I worked it out: 380 kcal, 22 g protein, "
        "6 g fat, 58 g carbs. That's all I'm eating today, I'm done for the day.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    checks.require("exactly one meal saved", len(meals) == 1, _describe_meals(meals))
    statuses = await env.day_statuses()
    checks.require(
        "today marked complete by the assistant",
        [(s.status, s.set_by) for s in statuses] == [("complete", "assistant")],
        str([(s.local_date, s.status, s.set_by) for s in statuses]),
    )
    checks.require(
        "does not ask whether the day is complete",
        not re.search(r"(is|was) (that|this) (all|everything)\?", turn.reply, re.IGNORECASE),
        turn.reply[-300:],
    )
    _require_confirmation(env, checks, turn.reply)


JUDGMENT = re.compile(
    r"\b(too (much|many|little)|went over|over your|under your|good job|well done|bad|unhealthy)\b",
    re.IGNORECASE,
)


async def targets(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "From now on my daily targets are 1800 to 2100 kcal and at least 120 g protein.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    stored = await env.targets()
    checks.require(
        "calorie target stored as 1800 to 2100 kcal",
        stored["calories_kcal"] == TargetRange(min=1800, max=2100),
        str(stored),
    )
    checks.require(
        "protein target stored as at least 120 g",
        stored["protein_g"] == TargetRange(min=120, max=None),
        str(stored),
    )
    checks.require(
        "no other target set",
        stored["fat_g"] is None and stored["carbohydrates_g"] is None,
        str(stored),
    )
    assert env.proxy is not None
    meals = env.proxy.tool_calls("log_meal")
    checks.require("no meal logged", not meals, str([c.arguments for c in meals]))
    checks.require(
        "user told the targets were saved", confirms_saved(turn.reply), turn.reply[-300:]
    )
    checks.require("no judgment of the targets", not JUDGMENT.search(turn.reply), turn.reply[-300:])


async def single_number_target(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(CHAT.name, "Please save my calorie target: 2000 kcal a day.")
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    stored = await env.targets()
    checks.require("no target stored", all(t is None for t in stored.values()), str(stored))
    checks.require(
        "asks the user for a range",
        "?" in turn.reply and bool(re.search(r"range|at least|at most|between", turn.reply, re.I)),
        turn.reply[-300:],
    )
    checks.require("no judgment of the target", not JUDGMENT.search(turn.reply), turn.reply[-300:])


async def vitamin_goal(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "My doctor told me to get 50 µg of vitamin D a day. Please save that as my goal.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    goals = await env.micronutrient_goals()
    checks.require(
        "vitamin D goal stored as from 50 µg",
        goals.get("vitamin_d_ug") == TargetRange(min=50, max=None),
        str(goals),
    )
    checks.require("no other goal set", set(goals) <= {"vitamin_d_ug"}, str(goals))
    stored = await env.targets()
    checks.require("no target set", all(t is None for t in stored.values()), str(stored))
    checks.require("user told the goal was saved", confirms_saved(turn.reply), turn.reply[-300:])
    checks.require("no judgment of the goal", not JUDGMENT.search(turn.reply), turn.reply[-300:])


# The tools that change data (server INSTRUCTIONS list them the same way).
WRITE_TOOLS = frozenset(
    {
        "log_meal",
        "relog_meal",
        "import_meals",
        "delete_meal",
        "set_day_status",
        "set_timezone",
        "set_targets",
        "log_weight",
        "delete_weight",
    }
)
ADVICE = re.compile(
    r"\b(you should|try (eating|adding|to eat)|consider (eating|adding|taking)|"
    r"eat more|take a supplement)\b",
    re.IGNORECASE,
)


async def iron_this_month(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    await env.change_settings(
        SettingsChanges(micronutrient_goals={"iron_mg": TargetRange(min=18, max=45)})
    )
    for days, iron in ((3, 12.0), (6, 9.5), (10, 15.0), (13, 11.0), (17, 8.0), (20, 14.5)):
        await env.add_meal(
            "Lentil soup with bread",
            (520, 24, 12, 78),
            minutes_ago=days * 24 * 60,
            micronutrients={"iron_mg": iron},
        )
    # Two meals without food items: no vitamin or mineral values.
    for days in (4, 11):
        await env.add_meal("Pizza", (800, 30, 30, 95), minutes_ago=days * 24 * 60)
    turn = await env.chat(CHAT.name, "How was my iron over the last four weeks?")
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    assert env.proxy is not None
    reads = env.proxy.tool_calls("get_micronutrient_insights")
    checks.require("read the insights", bool(reads), str([call.name for call in env.proxy.calls]))
    writes = [call for call in env.proxy.calls if call.name in WRITE_TOOLS]
    checks.require("nothing changed", not writes, str([call.name for call in writes]))
    checks.require("no judgment of the intake", not JUDGMENT.search(turn.reply), turn.reply[-400:])
    checks.require("no advice what to eat", not ADVICE.search(turn.reply), turn.reply[-400:])
    checks.note(
        "says the values cover only some meals",
        bool(
            re.search(
                r"\b(8|eight)\b[^.]{0,30}\bmeals\b|\bof (the )?(8|eight)\b",
                turn.reply,
                re.IGNORECASE,
            )
        ),
        turn.reply[-400:],
    )


CALORIES = re.compile(r"kcal|calorie|\bkj\b|kilojoule", re.IGNORECASE)


async def calm_mode_targets(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    calories = TargetRange(min=1800, max=2200)
    await env.change_settings(SettingsChanges(calm_mode=True, targets={"calories_kcal": calories}))
    turn = await env.chat(
        CHAT.name,
        "Please save my new daily targets: 1900 to 2300 kcal and 100 to 140 g protein.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    stored = await env.targets()
    checks.require(
        "protein target stored as 100 to 140 g",
        stored["protein_g"] == TargetRange(min=100, max=140),
        str(stored),
    )
    checks.require("calorie target unchanged", stored["calories_kcal"] == calories, str(stored))
    checks.require(
        "user told the targets were saved", confirms_saved(turn.reply), turn.reply[-300:]
    )
    checks.note("reply does not mention calories", not CALORIES.search(turn.reply), turn.reply)


# The assistant never asks for weight: a question that mentions weighing or weight.
WEIGHT_QUESTION = re.compile(r"\b(weigh|weight)\b[^.!?\n]*\?", re.IGNORECASE)


async def weight_logged(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(CHAT.name, "I weighed 80.4 kg this morning.")
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    weights = await env.weights()
    checks.require(
        "exactly one weigh-in of 80.4 kg saved",
        [w.weight_kg for w in weights] == [80.4],
        str([(w.weight_kg, w.measured_at) for w in weights]),
    )
    assert env.proxy is not None
    meals = env.proxy.tool_calls("log_meal")
    checks.require("no meal logged", not meals, str([c.arguments for c in meals]))
    checks.require("user told it was saved", confirms_saved(turn.reply), turn.reply[-300:])
    checks.require("no judgment of the weight", not JUDGMENT.search(turn.reply), turn.reply[-300:])
    calls = env.proxy.tool_calls("log_weight")
    checks.note(
        "measured_at given for 'this morning'",
        bool(calls) and calls[-1].arguments.get("measured_at") is not None,
        str([c.arguments for c in calls]),
    )


async def weight_in_pounds(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(CHAT.name, "Scale says 177.4 lb today. Please save it.")
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    weights = await env.weights()
    checks.require(
        "one weigh-in of 177.4 lb (80.47 kg) saved",
        len(weights) == 1 and abs(weights[0].weight_kg - 80.47) <= 0.05,
        str([w.weight_kg for w in weights]),
    )
    checks.require("user told it was saved", confirms_saved(turn.reply), turn.reply[-300:])
    checks.require("no judgment of the weight", not JUDGMENT.search(turn.reply), turn.reply[-300:])


async def weight_update_request(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    # A real ChatGPT message that only updated its own memory: short, Ukrainian, "update".
    turn = await env.chat(CHAT_WITH_MEMORY.name, "Онови вагу 56кг")  # noqa: RUF001 (Ukrainian on purpose)
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    weights = await env.weights()
    checks.require(
        "exactly one weigh-in of 56 kg saved",
        [w.weight_kg for w in weights] == [56],
        str([(w.weight_kg, w.measured_at) for w in weights]),
    )
    checks.require(
        "reply names HealthyEating", bool(HEALTHY_EATING.search(turn.reply)), turn.reply[-300:]
    )
    checks.require("no judgment of the weight", not JUDGMENT.search(turn.reply), turn.reply[-300:])


async def what_did_i_eat(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    await env.add_meal("Oatmeal with berries", (350, 12, 8, 55), minutes_ago=120)
    await env.add_meal("Chicken salad", (480, 35, 30, 18), minutes_ago=30)
    turn = await env.chat(
        CHAT.name, "What have I eaten today so far, and how many calories is that?"
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    assert env.proxy is not None
    reads = env.proxy.tool_calls("get_daily_summary") + env.proxy.tool_calls("list_meals")
    checks.require("read the saved meals", bool(reads), str([c.name for c in env.proxy.calls]))
    writes = env.proxy.tool_calls("log_meal") + env.proxy.tool_calls("delete_meal")
    checks.require("nothing logged or deleted", not writes, str([c.name for c in writes]))
    meals = await env.meals()
    checks.require("meals unchanged", len(meals) == 2, _describe_meals(meals))
    checks.require(
        "reply gives the total (830 kcal)",
        bool(re.search(r"\b830\b", turn.reply)),
        turn.reply[-300:],
    )
    checks.require("no judgment of the intake", not JUDGMENT.search(turn.reply), turn.reply[-300:])


async def same_as_yesterday(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    for days in (1, 2):
        await env.add_meal("Oatmeal with berries", (350, 12, 8, 55), minutes_ago=days * 24 * 60)
    turn = await env.chat(CHAT.name, "Same breakfast as yesterday.")
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    newest = max(meals, key=lambda meal: meal.created_at) if meals else None
    checks.require("exactly one new meal", len(meals) == 3, _describe_meals(meals))
    checks.require(
        "the same oatmeal with the same values",
        newest is not None
        and newest.dish_name.casefold() == "oatmeal with berries"
        and (newest.calories_kcal, newest.protein_g, newest.fat_g, newest.carbohydrates_g)
        == (350, 12, 8, 55),
        _describe_meals(meals),
    )
    checks.require("user told it was saved", confirms_saved(turn.reply), turn.reply[-300:])
    assert env.proxy is not None
    checks.note("used relog_meal", bool(env.proxy.tool_calls("relog_meal")), "")


async def known_product(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    # Logged two days ago from its label: 150 g with 105 kcal, 4.5 g protein, 3.75 g fat, 13.5 g
    # carbohydrates, so 200 g has 140 kcal, 6 g protein, 5 g fat and 18 g carbohydrates.
    await env.add_meal(
        "Йогурт Danone 2,5%",
        (105, 4.5, 3.75, 13.5),
        minutes_ago=2 * 24 * 60,
        portion_grams=150,
        source_note="nutrition label",
    )
    turn = await env.chat(CHAT.name, "Just had a Danone yogurt, 200 g.")
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    checks.require("exactly one new meal", len(meals) == 2, _describe_meals(meals))
    newest = max(meals, key=lambda meal: meal.created_at) if meals else None
    checks.require(
        "the label values scaled to 200 g",
        newest is not None
        and abs(newest.calories_kcal - 140) <= 7
        and abs(newest.protein_g - 6) <= 0.5
        and abs(newest.fat_g - 5) <= 0.5
        and abs(newest.carbohydrates_g - 18) <= 1,
        _describe_meals(meals),
    )
    checks.require(
        "not marked as estimated",
        newest is not None and not newest.estimated_fields,
        _describe_meals(meals),
    )
    checks.require("user told it was saved", confirms_saved(turn.reply), turn.reply[-300:])
    assert env.proxy is not None
    checks.note("used find_meals", bool(env.proxy.tool_calls("find_meals")), "")
    checks.note("used relog_meal", bool(env.proxy.tool_calls("relog_meal")), "")


async def meal_items(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name, "Lunch: 150 g of boiled rice and a baked salmon fillet, about 120 g."
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    checks.require("one meal saved", len(meals) == 1, _describe_meals(meals))
    if len(meals) != 1:
        return
    async with env.db.session() as session:
        meal = await session.get(Meal, meals[0].id)
        assert meal is not None
        items = [(item.name, item.grams, item.status) for item in meal.items]
        micronutrients = meal.micronutrients
    checks.require("items with grams sent", len(items) >= 2, str(items))
    checks.require(
        "rice and salmon matched",
        sum(1 for *_, status in items if status == "matched") >= 2,
        str(items),
    )
    checks.require("vitamins and minerals stored", bool(micronutrients), str(micronutrients))
    checks.require("user told it was saved", confirms_saved(turn.reply), turn.reply[-300:])
    checks.note(
        "reply does not bring up vitamins or minerals",
        not re.search(r"vitamin|mineral|iron|calcium", turn.reply, re.IGNORECASE),
        turn.reply[-300:],
    )


async def supplements(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "I just took my daily supplements, one capsule each: vitamin D3 2000 IU, magnesium "
        "200 mg and zinc 15 mg.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    meals = await env.meals()
    checks.require("one meal saved", len(meals) == 1, _describe_meals(meals))
    if len(meals) != 1:
        return
    async with env.db.session() as session:
        meal = await session.get(Meal, meals[0].id)
        assert meal is not None
        labels = [item.label_values for item in meal.items if item.status == "label"]
        micronutrients = meal.micronutrients or {}
    checks.require("label values sent", len(labels) >= 1, str(labels))
    expected = {"vitamin_d_ug": 50, "magnesium_mg": 200, "zinc_mg": 15}
    checks.require(
        "label values stored, IU converted",
        all(abs(micronutrients.get(code, 0) - value) < 0.5 for code, value in expected.items()),
        str(micronutrients),
    )
    checks.require("user told it was saved", confirms_saved(turn.reply), turn.reply[-300:])


# Four meals over three days, as a ChatGPT calorie project would list them.
HISTORY_VALUES = {
    (1, (350, 12, 8, 55)),
    (1, (640, 42, 22, 60)),
    (2, (310, 18, 9, 38)),
    (3, (520, 35, 18, 50)),
}


async def import_history(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name,
        "Here is my food log from my ChatGPT calorie project for this month. Please add it to "
        "HealthyEating.\n"
        "Oct 1: breakfast oatmeal 350 kcal (P 12 g, F 8 g, C 55 g); dinner salmon with rice "
        "640 kcal (P 42 g, F 22 g, C 60 g)\n"
        "Oct 2: breakfast yogurt with granola 310 kcal (P 18 g, F 9 g, C 38 g)\n"
        "Oct 3: lunch chicken wrap 520 kcal (P 35 g, F 18 g, C 50 g)",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    assert env.proxy is not None
    checks.require("import_meals used", bool(env.proxy.tool_calls("import_meals")), "")
    meals = await env.meals()
    checks.require(
        "four imported meals, nothing else",
        len(meals) == 4 and all(meal.imported_from for meal in meals),
        _describe_meals(meals),
    )
    saved = {(meal.eaten_at.day, _values(meal)) for meal in meals}
    checks.require(
        "values unchanged, on their days", saved == HISTORY_VALUES, _describe_meals(meals)
    )
    checks.require("user told it was imported", confirms_saved(turn.reply), turn.reply[-300:])
    checks.note("log_meal not used", not env.proxy.tool_calls("log_meal"), "")


async def delete_duplicate(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    await env.add_meal("Oatmeal", (300, 10, 6, 50), minutes_ago=60)
    await env.add_meal("Oatmeal", (300, 10, 6, 50), minutes_ago=58)
    turn = await env.chat(
        CHAT.name, "Oops, I logged my oatmeal twice today by mistake. Please remove the extra one."
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    assert env.proxy is not None
    deletes = env.proxy.tool_calls("delete_meal")
    checks.require("delete_meal called once", len(deletes) == 1, f"{len(deletes)} calls")
    checks.require("log_meal not called", not env.proxy.tool_calls("log_meal"), "")
    meals = await env.meals()
    checks.require("exactly one oatmeal left", len(meals) == 1, _describe_meals(meals))
    told = any(
        re.search(r"delet|remov", sentence, re.IGNORECASE) and not NEGATIVE.search(sentence)
        for sentence in _healthy_eating_sentences(turn.reply)
    )
    checks.require("user told it was removed from HealthyEating", told, turn.reply[-300:])


async def correction(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    first = await env.chat(
        CHAT.name,
        "Save my dinner: margherita pizza, 2 slices. 570 kcal, 24 g protein, 20 g fat, 70 g carbs.",
    )
    turns.append(first)
    if not _require_turn_ok(checks, first, "first reply"):
        return
    meals = await env.meals()
    if not checks.require("first meal saved", len(meals) == 1, _describe_meals(meals)):
        return
    first_id = meals[0].id

    second = await env.chat(
        CHAT.name,
        "Sorry, it was actually 3 slices: 855 kcal, 36 g protein, 30 g fat, 105 g carbs.",
        session_id=first.session_id,
    )
    turns.append(second)
    if not _require_turn_ok(checks, second, "second reply"):
        return
    meals = await env.meals()
    checks.require("still exactly one meal (no duplicate)", len(meals) == 1, _describe_meals(meals))
    if meals:
        checks.require("the same meal was updated", meals[0].id == first_id, str(meals[0].id))
        checks.require(
            "corrected values stored",
            _values(meals[0]) == (855, 36, 30, 105),
            f"stored {_values(meals[0])}",
        )
    assert env.proxy is not None
    calls = env.proxy.tool_calls("log_meal")
    checks.require(
        "correction passed replaces_meal_id",
        bool(calls) and calls[-1].arguments.get("replaces_meal_id") == str(first_id),
        str(calls[-1].arguments.get("replaces_meal_id")) if calls else "no calls",
    )
    _require_confirmation(env, checks, second.reply)


async def server_error(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    assert env.proxy is not None
    env.proxy.fail_tool_calls = True
    turn = await env.chat(
        CHAT.name,
        "Save my snack: an apple. 95 kcal, 0.5 g protein, 0.3 g fat, 25 g carbs.",
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    calls = env.proxy.tool_calls("log_meal")
    checks.require("log_meal was attempted", bool(calls), f"{len(calls)} calls")
    meals = await env.meals()
    checks.require("nothing saved", not meals, _describe_meals(meals))
    reply = turn.reply
    checks.require(
        "user told it was NOT saved to HealthyEating", reports_not_saved(reply), reply[-300:]
    )
    checks.require("no false 'saved' claim", not confirms_saved(reply), reply[-300:])


async def not_food(env: EvalEnvironment, checks: Checks, turns: list[Turn]) -> None:
    turn = await env.chat(
        CHAT.name, "How much protein is in a boiled egg? Just curious, I haven't eaten one."
    )
    turns.append(turn)
    if not _require_turn_ok(checks, turn, "reply"):
        return
    assert env.proxy is not None
    calls = env.proxy.tool_calls("log_meal")
    checks.require("log_meal not called", not calls, f"{len(calls)} calls")
    meals = await env.meals()
    checks.require("nothing saved", not meals, _describe_meals(meals))


SCENARIOS = [
    Scenario(
        "numbers_from_conversation",
        "Numbers the user already calculated are stored unchanged",
        numbers_from_conversation,
    ),
    Scenario("photo", "A meal photo: the assistant estimates everything and saves it", photo),
    Scenario(
        "existing_workflow",
        "A calorie project with its own format and a Sheets log keeps working unchanged",
        existing_workflow,
    ),
    Scenario(
        "missing_portion",
        "Unclear portion: answer, ask a follow-up, end with exactly one meal",
        missing_portion,
    ),
    Scenario("correction", "A correction updates the same meal", correction),
    Scenario(
        "earlier_meal",
        "A meal eaten earlier is saved (with a valid time or none)",
        earlier_meal,
    ),
    Scenario(
        "time_zone",
        "The user says where they live: the time zone is stored and the meal saved",
        time_zone,
    ),
    Scenario(
        "day_complete",
        "The user says they are done for the day: meal saved and today marked complete",
        day_complete,
    ),
    Scenario(
        "targets",
        "The user states daily targets as ranges: they are stored and the user told so",
        targets,
    ),
    Scenario(
        "single_number_target",
        "A single-number target: nothing stored, the assistant asks for a range",
        single_number_target,
    ),
    Scenario(
        "vitamin_goal",
        "A daily vitamin D amount from the user's doctor: stored as a goal from 50 µg",
        vitamin_goal,
    ),
    Scenario(
        "iron_this_month",
        "Iron over four weeks with a goal: read with get_micronutrient_insights, neutrally",
        iron_this_month,
    ),
    Scenario(
        "calm_mode_targets",
        "Calm mode: a calorie and a protein target; protein saved, the calorie target unchanged",
        calm_mode_targets,
    ),
    Scenario(
        "weight_logged",
        "The user gives their weight in kg: one weigh-in saved, confirmed neutrally",
        weight_logged,
    ),
    Scenario(
        "weight_in_pounds",
        "The user gives their weight in lb: stored in kg, confirmed neutrally",
        weight_in_pounds,
    ),
    Scenario(
        "weight_update_request",
        "Short Ukrainian request to update the weight, chat with memory: one weigh-in saved",
        weight_update_request,
    ),
    Scenario(
        "what_did_i_eat",
        "Two saved meals: the assistant reads them back and gives the total, neutrally",
        what_did_i_eat,
    ),
    Scenario(
        "same_as_yesterday",
        "Same breakfast as yesterday: one new meal with the same values",
        same_as_yesterday,
    ),
    Scenario(
        "known_product",
        "A product logged before from its label, now another weight: the label values scaled",
        known_product,
    ),
    Scenario(
        "delete_duplicate",
        "A meal saved twice: the assistant deletes exactly one copy",
        delete_duplicate,
    ),
    Scenario(
        "meal_items",
        "A described meal is sent with its foods and grams, so it gets vitamins and minerals",
        meal_items,
    ),
    Scenario(
        "supplements",
        "Supplements get the vitamins and minerals from their label, IU converted",
        supplements,
    ),
    Scenario(
        "import_history",
        "History pasted from a ChatGPT project is imported unchanged on its days",
        import_history,
    ),
    Scenario("server_error", "When saving fails, the user is told it was not saved", server_error),
    Scenario("not_food", "A food question that is not a meal logs nothing", not_food),
]
