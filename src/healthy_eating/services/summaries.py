"""Daily summaries: totals per local day, how much of them is estimated, and completeness."""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal
from healthy_eating.nutrition.types import DayStatus, StatusSetter
from healthy_eating.services.days import counts_complete_days_only, day_counts, day_statuses
from healthy_eating.services.local_time import local_date_column, utc_window
from healthy_eating.services.settings import get_time_zone

# Longest range of days one request may ask for.
MAX_SUMMARY_DAYS = 366

CountingRule = Literal["complete_days", "logged_days"]


class RangeTooLongError(ValueError):
    """More days were requested than MAX_SUMMARY_DAYS."""


@dataclass(frozen=True)
class DaySummary:
    local_date: date
    meal_count: int
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    # Calories from meals whose calories the assistant estimated, and their share (0 to 1;
    # None without calories).
    estimated_calories_kcal: float
    estimated_calories_share: float | None
    status: DayStatus | None
    status_set_by: StatusSetter | None
    # Whether the day counts for averages (services/days.py).
    counted: bool


@dataclass(frozen=True)
class DailySummaries:
    days: list[DaySummary]
    counting_rule: CountingRule


@dataclass(frozen=True)
class _Totals:
    meal_count: int
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    estimated_kcal: float


_NO_MEALS = _Totals(0, 0.0, 0.0, 0.0, 0.0, 0.0)


async def _totals_by_day(
    session: AsyncSession, user_id: uuid.UUID, first_day: date, last_day: date
) -> dict[date, _Totals]:
    local_date = local_date_column(await get_time_zone(session, user_id)).label("local_date")
    start, end = utc_window(first_day, last_day)
    assert start is not None and end is not None
    estimated_kcal = case(
        (Meal.estimated_fields.contains(["calories_kcal"]), Meal.calories_kcal), else_=0.0
    )
    rows = await session.execute(
        select(
            local_date,
            func.count(),
            func.sum(Meal.calories_kcal),
            func.sum(Meal.protein_g),
            func.sum(Meal.fat_g),
            func.sum(Meal.carbohydrates_g),
            func.sum(estimated_kcal),
        )
        .where(
            Meal.user_id == user_id,
            Meal.eaten_at >= start,
            Meal.eaten_at < end,
            local_date >= first_day,
            local_date <= last_day,
        )
        .group_by(local_date)
    )
    return {
        day: _Totals(count, kcal, protein, fat, carbs, estimated)
        for day, count, kcal, protein, fat, carbs, estimated in rows
    }


async def daily_summaries(
    session: AsyncSession, user_id: uuid.UUID, first_day: date, last_day: date
) -> DailySummaries:
    """One summary per local day first_day..last_day (oldest first), days without meals too."""
    length = (last_day - first_day).days + 1
    if length > MAX_SUMMARY_DAYS:
        raise RangeTooLongError(f"Ask for at most {MAX_SUMMARY_DAYS} days at a time")
    totals = await _totals_by_day(session, user_id, first_day, last_day)
    statuses = await day_statuses(session, user_id, first_day, last_day)
    complete_days_only = await counts_complete_days_only(session, user_id)

    days = []
    for offset in range(length):
        day = first_day + timedelta(days=offset)
        total = totals.get(day, _NO_MEALS)
        status = statuses.get(day)
        days.append(
            DaySummary(
                local_date=day,
                meal_count=total.meal_count,
                calories_kcal=round(total.calories_kcal, 1),
                protein_g=round(total.protein_g, 1),
                fat_g=round(total.fat_g, 1),
                carbohydrates_g=round(total.carbohydrates_g, 1),
                estimated_calories_kcal=round(total.estimated_kcal, 1),
                estimated_calories_share=(
                    round(total.estimated_kcal / total.calories_kcal, 3)
                    if total.calories_kcal > 0
                    else None
                ),
                status=None if status is None else status.status,
                status_set_by=None if status is None else status.set_by,
                counted=day_counts(
                    None if status is None else status.status,
                    total.meal_count,
                    complete_days_only=complete_days_only,
                ),
            )
        )
    return DailySummaries(
        days=days, counting_rule="complete_days" if complete_days_only else "logged_days"
    )
