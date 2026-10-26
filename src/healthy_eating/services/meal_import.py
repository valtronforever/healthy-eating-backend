"""Importing meal history from the user's earlier tracking (healthy-eating-backend#27).

The assistant reads the history the user pasted (a ChatGPT project, a spreadsheet, another
app's export) and sends it in parts. Each meal follows the rules of `log_meal`: numbers from
the history are kept unchanged, values the assistant fills in are marked as estimated, and all
four values are needed. Imported meals keep where they came from in `imported_from`.

Nothing is counted twice:
- a meal with the same name (case and spacing ignored), minute and calories as a stored meal is
  skipped, so a retried or overlapping part changes nothing;
- a local day that already has meals logged as they were eaten (not imported) is skipped as a
  whole, because the history and the live log would describe the same day.

Imported meals do not count for usual meals (services/usual_meals.py): those come from what
the user logs now. An entry may list its food items, which get vitamins and minerals from the
reference data exactly as in `log_meal`.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal
from healthy_eating.services.local_time import (
    local_date_column,
    local_eaten_at,
    resolve_eaten_at,
    utc_window,
)
from healthy_eating.services.meals import MealEstimate, evaluate_meal, set_items
from healthy_eating.services.settings import get_time_zone
from healthy_eating.services.usual_meals import dish_key

MAX_IMPORT_MEALS = 100
MAX_IMPORTED_FROM_LENGTH = 100
# A meal may be stamped a little ahead of the server clock; anything later is refused.
FUTURE_TOLERANCE = timedelta(hours=1)


@dataclass(frozen=True)
class RejectedMeal:
    # Position of the meal in the request, from 0.
    index: int
    reason: str


@dataclass(frozen=True)
class ImportResult:
    imported: list[Meal] = field(default_factory=list)
    # Same name, minute and calories as a meal already stored (or earlier in this request).
    duplicates: int = 0
    # Local days skipped because meals were already logged there as they were eaten.
    days_already_logged: list[date] = field(default_factory=list)
    rejected: list[RejectedMeal] = field(default_factory=list)
    first_day: date | None = None
    last_day: date | None = None


def _key(name: str, eaten_at: datetime, calories_kcal: float) -> tuple[str, datetime, int]:
    return dish_key(name), eaten_at.replace(second=0, microsecond=0), round(calories_kcal)


async def import_meals(
    session: AsyncSession,
    user_id: uuid.UUID,
    meals: Sequence[MealEstimate],
    *,
    imported_from: str,
    client_id: str | None,
    now: datetime,
) -> ImportResult:
    """Store the meals that are new; see the module docstring for what is skipped."""
    time_zone = await get_time_zone(session, user_id)
    rejected: list[RejectedMeal] = []
    # (index, meal with normalized values, eaten_at in UTC, its offset, its local date)
    candidates: list[tuple[int, MealEstimate, datetime, int | None, date]] = []
    for index, estimate in enumerate(meals):
        if estimate.eaten_at is None:
            rejected.append(RejectedMeal(index, "eaten_at is missing"))
            continue
        evaluation = evaluate_meal(replace(estimate, open_questions=[], replaces_meal_id=None))
        if not evaluation.complete:
            missing = ", ".join(evaluation.missing_fields)
            rejected.append(RejectedMeal(index, f"missing values: {missing}"))
            continue
        when, offset = resolve_eaten_at(estimate.eaten_at, time_zone, now=now)
        if when > now + FUTURE_TOLERANCE:
            rejected.append(RejectedMeal(index, "eaten_at is in the future"))
            continue
        day = local_eaten_at(when, offset, time_zone).date()
        candidates.append((index, evaluation.meal, when, offset, day))

    if not candidates:
        return ImportResult(rejected=rejected)

    days = {day for *_, day in candidates}
    start, end = utc_window(min(days), max(days))
    local_day = local_date_column(time_zone)
    stored = await session.execute(
        select(Meal.dish_name, Meal.eaten_at, Meal.calories_kcal, Meal.imported_from, local_day)
        .where(Meal.user_id == user_id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .where(local_day.in_(days))
    )
    seen: set[tuple[str, datetime, int]] = set()
    logged_days: set[date] = set()
    for name, eaten_at, calories, from_import, day in stored:
        seen.add(_key(name, eaten_at, calories))
        if from_import is None:
            logged_days.add(day)

    imported: list[Meal] = []
    imported_days: set[date] = set()
    duplicates = 0
    for _index, meal, when, offset, day in candidates:
        if day in logged_days:
            continue
        assert meal.calories_kcal is not None
        key = _key(meal.dish_name, when, meal.calories_kcal)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        row = Meal(
            user_id=user_id,
            client_id=client_id,
            dish_name=meal.dish_name,
            description=meal.description,
            source=meal.source,
            source_note=meal.source_note or imported_from,
            eaten_at=when,
            utc_offset_minutes=offset,
            portion_grams=meal.portion_grams,
            calories_kcal=meal.calories_kcal,
            protein_g=meal.protein_g,
            fat_g=meal.fat_g,
            carbohydrates_g=meal.carbohydrates_g,
            fiber_g=meal.fiber_g,
            sugar_g=meal.sugar_g,
            sodium_mg=meal.sodium_mg,
            estimated_fields=list(meal.estimated_fields),
            confidence=meal.confidence,
            imported_from=imported_from,
            # Loaded and empty, so set_items needs no lazy load (see save_meal).
            items=[],
        )
        session.add(row)
        if meal.items is not None:
            await set_items(session, row, list(meal.items))
        imported.append(row)
        imported_days.add(day)
    await session.flush()

    return ImportResult(
        imported=imported,
        duplicates=duplicates,
        days_already_logged=sorted(days & logged_days),
        rejected=rejected,
        first_day=min(imported_days, default=None),
        last_day=max(imported_days, default=None),
    )
