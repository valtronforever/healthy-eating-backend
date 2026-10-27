"""Earlier meals with known values, for logging the same food again (owner decision 2026-10-27).

A packaged product or a dish whose values the user had from a label, their own calculation or
their workflow should not be estimated anew the next time, usually at another weight. Only
such meals are found here: `source` is `from_conversation`, none of the four main values is
estimated, and the portion weight is known, so the values can be given per 100 g. Meals the
assistant estimated (from a photo or a description) are never offered: reusing an estimate
would make it look like a known value.

Search: the words of the query (case ignored, any script) are compared with the words of the
meal's name and of its items' names; a query word matches a word that starts with it, so
"йогурт" also finds "йогурта". Meals with the same name (case and spacing ignored) are one
result, represented by the latest. Results with more matching words come first, then the
ones eaten more often, then the latest. Only the last year is searched.

The hint after saving (`earlier_known_meal`) compares a new meal whose values were estimated
with the latest known meal whose name contains every word of the new meal's name. It is only
given when the values per 100 g clearly differ; see `_clearly_different`.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, TRACKED_NUTRIENT_FIELDS
from healthy_eating.services.meals import LocalMeal, local_meal, with_client_name
from healthy_eating.services.settings import get_time_zone
from healthy_eating.services.usual_meals import dish_key

SEARCH_DAYS = 365
MAX_RESULTS = 10
WORD = re.compile(r"\w+")


def has_known_values(meal: Meal) -> bool:
    """The four main values came from the user's side, and the portion weight is known."""
    return (
        meal.source == "from_conversation"
        and meal.portion_grams is not None
        and not set(meal.estimated_fields) & set(NUTRIENT_FIELDS)
    )


def values_per_100g(meal: Meal) -> dict[str, float]:
    """The meal's values per 100 g, for the values it has (portion weight needed)."""
    assert meal.portion_grams is not None
    factor = 100 / meal.portion_grams
    values: dict[str, float] = {}
    for field in TRACKED_NUTRIENT_FIELDS:
        value = getattr(meal, field)
        if value is not None:
            values[field] = round(value * factor, 1)
    return values


@dataclass(frozen=True)
class KnownMeal:
    # The latest meal with this name: its values, portion and provenance.
    template: LocalMeal
    # How often a meal with this name and known values was eaten in the searched year.
    times: int
    per_100g: dict[str, float]
    # How many words of the query the name or the item names match.
    matched_words: int


def _words(text: str) -> list[str]:
    return WORD.findall(text.casefold())


def _matched(query: list[str], words: set[str]) -> int:
    return sum(1 for wanted in query if any(word.startswith(wanted) for word in words))


async def find_known_meals(
    session: AsyncSession,
    user_id: uuid.UUID,
    query: str,
    *,
    now: datetime,
    limit: int = MAX_RESULTS,
    all_words: bool = False,
    exclude: uuid.UUID | None = None,
) -> list[KnownMeal]:
    """Earlier meals with known values whose names match the query, best first.

    With `all_words`, only meals that match every word of the query are returned.
    """
    wanted = list(dict.fromkeys(_words(query)))
    if not wanted:
        return []
    statement = (
        select(Meal)
        .where(Meal.user_id == user_id, Meal.eaten_at >= now - timedelta(days=SEARCH_DAYS))
        .where(Meal.source == "from_conversation", Meal.portion_grams.is_not(None))
        .where(~Meal.estimated_fields.overlap(list(NUTRIENT_FIELDS)))
        .order_by(Meal.eaten_at.desc(), Meal.id.desc())
    )
    if exclude is not None:
        statement = statement.where(Meal.id != exclude)
    rows = list(await session.execute(with_client_name(statement)))

    latest: dict[str, tuple[Meal, str | None, int]] = {}
    times: dict[str, int] = {}
    for meal, client_name in rows:
        key = dish_key(meal.dish_name)
        times[key] = times.get(key, 0) + 1
        if key in latest:
            continue
        words = set(_words(meal.dish_name))
        for item in meal.items:
            words.update(_words(item.name))
        matched = _matched(wanted, words)
        if matched and (not all_words or matched == len(wanted)):
            latest[key] = (meal, client_name, matched)
        else:
            # Older meals with this name are not looked at: the latest one decides.
            latest[key] = (meal, client_name, 0)

    found = [(key, entry) for key, entry in latest.items() if entry[2] > 0]
    found.sort(key=lambda pair: (-pair[1][2], -times[pair[0]], -pair[1][0].eaten_at.timestamp()))
    time_zone = await get_time_zone(session, user_id)
    return [
        KnownMeal(
            template=local_meal(meal, time_zone, client_name),
            times=times[key],
            per_100g=values_per_100g(meal),
            matched_words=matched,
        )
        for key, (meal, client_name, matched) in found[:limit]
    ]


def _clearly_different(new: dict[str, float], known: dict[str, float]) -> bool:
    """Calories per 100 g differ by more than 10 % (and 5 kcal), or a macronutrient by more than
    20 % (and 1 g); smaller differences are rounding or a slightly different product."""
    for field in NUTRIENT_FIELDS:
        a, b = new.get(field), known.get(field)
        if a is None or b is None:
            continue
        share, least = (0.10, 5.0) if field == "calories_kcal" else (0.20, 1.0)
        if abs(a - b) > max(share * max(a, b), least):
            return True
    return False


async def earlier_known_meal(
    session: AsyncSession, user_id: uuid.UUID, meal: Meal, *, now: datetime
) -> KnownMeal | None:
    """An earlier meal with known values to offer instead of the new meal's estimate."""
    if meal.portion_grams is None or has_known_values(meal):
        return None
    found = await find_known_meals(
        session, user_id, meal.dish_name, now=now, limit=1, all_words=True, exclude=meal.id
    )
    if not found or not _clearly_different(values_per_100g(meal), found[0].per_100g):
        return None
    return found[0]
