"""Weekly review: one local week (Monday to Sunday) in neutral facts, never scores.

- Averages count days exactly like Trends (services/trends.py): counted days only, and today
  only once it is marked complete. Every day left out is named with its reason, so the app can
  say which days the averages use.
- Weekdays and weekend: the same averages, for counted Monday to Friday and Saturday/Sunday.
- Vitamins and minerals: averaged over the counted days on which every meal has a value, as in
  Trends (`micronutrient_averages`).
- Per-meal patterns use every meal of the week, counted day or not (a partial day's meals are
  still real meals): protein per meal, late meals, where the values came from, low confidence.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, OPTIONAL_NUTRIENT_FIELDS, NutrientField
from healthy_eating.services.days import local_today
from healthy_eating.services.local_time import local_date_column, local_eaten_at, utc_window
from healthy_eating.services.settings import get_time_zone
from healthy_eating.services.summaries import CountingRule, DaySummary, daily_summaries
from healthy_eating.services.trends import OptionalAverage, micronutrient_averages

# A meal eaten from 21:00 until 04:00 local time is a late meal.
LATE_FROM = time(21, 0)
LATE_UNTIL = time(4, 0)
# Edges of the protein-per-meal buckets in grams; the last bucket has no upper edge.
PROTEIN_BUCKET_EDGES = (0, 10, 20, 30, 40)

LeftOutReason = Literal["future", "in_progress", "not_logged", "partial", "not_marked_complete"]


@dataclass(frozen=True)
class LeftOutDay:
    local_date: date
    reason: LeftOutReason


@dataclass(frozen=True)
class DayGroup:
    counted_days: int
    averages: dict[NutrientField, float] | None


@dataclass(frozen=True)
class ProteinBucket:
    min_g: float
    # None for the last, open bucket.
    max_g: float | None
    meals: int


@dataclass(frozen=True)
class WeeklyReview:
    first_day: date
    last_day: date
    counting_rule: CountingRule
    logged_days: int
    complete_days: int
    counted_days: int
    averages: dict[NutrientField, float] | None
    left_out: list[LeftOutDay]
    weekdays: DayGroup
    weekend: DayGroup
    meal_count: int
    protein_per_meal_g: float | None
    protein_buckets: list[ProteinBucket]
    late_meals: int
    # Share of the meals' values (four per meal plus the optional ones it has) the assistant
    # estimated; None without meals.
    estimated_values_share: float | None
    low_confidence_meals: int
    micronutrient_averages: dict[Micronutrient, OptionalAverage]


def week_of(day: date) -> tuple[date, date]:
    """Monday and Sunday of the week that contains `day`."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def _counts(day: DaySummary, today: date) -> bool:
    if day.local_date > today:
        return False
    return day.counted and (day.local_date != today or day.status == "complete")


def _left_out_reason(day: DaySummary, today: date) -> LeftOutReason:
    if day.local_date > today:
        return "future"
    if day.local_date == today:
        return "in_progress"
    if day.meal_count == 0:
        return "not_logged"
    if day.status == "partial":
        return "partial"
    return "not_marked_complete"


def _averages(days: list[DaySummary]) -> dict[NutrientField, float] | None:
    if not days:
        return None
    return {
        name: round(sum(getattr(day, name) for day in days) / len(days), 1)
        for name in NUTRIENT_FIELDS
    }


def _is_late(moment: datetime) -> bool:
    return moment.time() >= LATE_FROM or moment.time() < LATE_UNTIL


def _protein_buckets(protein: list[float]) -> list[ProteinBucket]:
    buckets = []
    for index, low in enumerate(PROTEIN_BUCKET_EDGES):
        high = PROTEIN_BUCKET_EDGES[index + 1] if index + 1 < len(PROTEIN_BUCKET_EDGES) else None
        meals = sum(1 for grams in protein if grams >= low and (high is None or grams < high))
        buckets.append(ProteinBucket(min_g=low, max_g=high, meals=meals))
    return buckets


async def weekly_review(
    session: AsyncSession, user_id: uuid.UUID, day: date | None, *, now: datetime
) -> WeeklyReview:
    """The local week containing `day` (default: this week)."""
    time_zone = await get_time_zone(session, user_id)
    today = local_today(time_zone, now)
    first_day, last_day = week_of(day or today)

    summaries = await daily_summaries(session, user_id, first_day, last_day)
    counted = [summary for summary in summaries.days if _counts(summary, today)]
    left_out = [
        LeftOutDay(summary.local_date, _left_out_reason(summary, today))
        for summary in summaries.days
        if not _counts(summary, today)
    ]

    start, end = utc_window(first_day, last_day)
    assert start is not None and end is not None
    local_date = local_date_column(time_zone)
    meals = (
        await session.execute(
            select(
                Meal.eaten_at,
                Meal.utc_offset_minutes,
                Meal.protein_g,
                Meal.fiber_g,
                Meal.sugar_g,
                Meal.sodium_mg,
                Meal.estimated_fields,
                Meal.confidence,
            ).where(
                Meal.user_id == user_id,
                Meal.eaten_at >= start,
                Meal.eaten_at < end,
                local_date >= first_day,
                local_date <= last_day,
            )
        )
    ).all()
    protein = [meal.protein_g for meal in meals]
    estimated_values = sum(len(meal.estimated_fields) for meal in meals)
    # Four values per meal, plus the optional ones it has.
    values = sum(
        len(NUTRIENT_FIELDS)
        + sum(1 for name in OPTIONAL_NUTRIENT_FIELDS if getattr(meal, name) is not None)
        for meal in meals
    )

    return WeeklyReview(
        first_day=first_day,
        last_day=last_day,
        counting_rule=summaries.counting_rule,
        logged_days=sum(1 for summary in summaries.days if summary.meal_count > 0),
        complete_days=sum(1 for summary in summaries.days if summary.status == "complete"),
        counted_days=len(counted),
        averages=_averages(counted),
        left_out=left_out,
        weekdays=DayGroup(
            counted_days=sum(1 for d in counted if d.local_date.weekday() < 5),
            averages=_averages([d for d in counted if d.local_date.weekday() < 5]),
        ),
        weekend=DayGroup(
            counted_days=sum(1 for d in counted if d.local_date.weekday() >= 5),
            averages=_averages([d for d in counted if d.local_date.weekday() >= 5]),
        ),
        meal_count=len(meals),
        protein_per_meal_g=round(sum(protein) / len(protein), 1) if protein else None,
        protein_buckets=_protein_buckets(protein),
        late_meals=sum(
            1
            for meal in meals
            if _is_late(local_eaten_at(meal.eaten_at, meal.utc_offset_minutes, time_zone))
        ),
        estimated_values_share=(round(estimated_values / values, 3) if meals else None),
        low_confidence_meals=sum(1 for meal in meals if meal.confidence == "low"),
        micronutrient_averages=micronutrient_averages(counted),
    )
