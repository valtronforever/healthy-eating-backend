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
from evals.personas import CALORIE_PROJECT, CHAT
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal

HEALTHY_EATING = re.compile(r"healthy ?eating", re.IGNORECASE)
POSITIVE = re.compile(r"\b(saved|updated|logged|recorded|added)\b", re.IGNORECASE)
NEGATIVE = re.compile(
    r"\b(not|never|unable|failed|couldn['\u2019]?t|wasn['\u2019]?t|didn['\u2019]?t|isn['\u2019]?t)\b",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"[^.!?\n]+")


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
            "nothing marked as estimated",
            meal.estimated_fields == [],
            str(meal.estimated_fields),
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
    Scenario("server_error", "When saving fails, the user is told it was not saved", server_error),
    Scenario("not_food", "A food question that is not a meal logs nothing", not_food),
]
