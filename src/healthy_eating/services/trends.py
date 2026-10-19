"""Trends: averages per counted day over a window of local days, compared with the window before.

The window is the N local days ending today. Today counts only once it is marked complete:
an unfinished day would pull every average down. Otherwise days count as in
services/days.py. Averages are taken over counted days only, never over calendar days.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.nutrition.types import NUTRIENT_FIELDS, NutrientField
from healthy_eating.services.days import user_today
from healthy_eating.services.summaries import CountingRule, DaySummary, daily_summaries

WINDOWS = (7, 30, 90)


@dataclass(frozen=True)
class Period:
    first_day: date
    last_day: date
    counted_days: int
    # Days with at least one meal, counted or not.
    logged_days: int
    # Average per counted day; None when no day counts.
    averages: dict[NutrientField, float] | None
    # Share of the counted days' calories that the assistant estimated.
    estimated_calories_share: float | None


@dataclass(frozen=True)
class Trends:
    window_days: int
    counting_rule: CountingRule
    current: Period
    previous: Period
    # current minus previous averages; None unless both periods have counted days.
    change: dict[NutrientField, float] | None


def _period(days: list[DaySummary], today: date) -> Period:
    counted = [
        day for day in days if day.counted and (day.local_date != today or day.status == "complete")
    ]
    averages = None
    share = None
    if counted:
        averages = {
            name: round(sum(getattr(day, name) for day in counted) / len(counted), 1)
            for name in NUTRIENT_FIELDS
        }
        calories = sum(day.calories_kcal for day in counted)
        if calories > 0:
            share = round(sum(day.estimated_calories_kcal for day in counted) / calories, 3)
    return Period(
        first_day=days[0].local_date,
        last_day=days[-1].local_date,
        counted_days=len(counted),
        logged_days=sum(1 for day in days if day.meal_count > 0),
        averages=averages,
        estimated_calories_share=share,
    )


async def trends(
    session: AsyncSession, user_id: uuid.UUID, window_days: int, *, now: datetime
) -> Trends:
    if window_days not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    today = await user_today(session, user_id, now)
    first_day = today - timedelta(days=2 * window_days - 1)
    summaries = await daily_summaries(session, user_id, first_day, today)
    previous = _period(summaries.days[:window_days], today)
    current = _period(summaries.days[window_days:], today)
    change = None
    if current.averages is not None and previous.averages is not None:
        change = {
            name: round(current.averages[name] - previous.averages[name], 1)
            for name in NUTRIENT_FIELDS
        }
    return Trends(
        window_days=window_days,
        counting_rule=summaries.counting_rule,
        current=current,
        previous=previous,
        change=change,
    )
