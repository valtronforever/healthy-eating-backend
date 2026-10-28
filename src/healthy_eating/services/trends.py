"""Trends: averages per counted day over a window of local days, compared with the window before.

The window is the N local days ending today. Today counts only once it is marked complete:
an unfinished day would pull every average down. Otherwise days count as in
services/days.py. Averages are taken over counted days only, never over calendar days.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS, Micronutrient
from healthy_eating.nutrition.types import (
    NUTRIENT_FIELDS,
    OPTIONAL_NUTRIENT_FIELDS,
    NutrientField,
    OptionalNutrientField,
)
from healthy_eating.services.days import user_today
from healthy_eating.services.summaries import CountingRule, DaySummary, daily_summaries

WINDOWS = (7, 30, 90)


@dataclass(frozen=True)
class OptionalAverage:
    """An optional nutrient's average over the counted days that have a value for it."""

    average: float
    days: int


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
    # Fiber, sugar and sodium; a nutrient is missing when no counted day has a value.
    optional_averages: dict[OptionalNutrientField, OptionalAverage]
    # Vitamins and minerals (#25) over the counted days on which every meal has a value, so a
    # day where only some meals have food items does not pull the average down.
    micronutrient_averages: dict[Micronutrient, OptionalAverage]


@dataclass(frozen=True)
class Trends:
    window_days: int
    counting_rule: CountingRule
    current: Period
    previous: Period
    # current minus previous averages; None unless both periods have counted days.
    change: dict[NutrientField, float] | None


def counted_days(days: list[DaySummary], today: date) -> list[DaySummary]:
    """The days averages use: counted ones, and today only once it is marked complete."""
    return [
        day for day in days if day.counted and (day.local_date != today or day.status == "complete")
    ]


def covered_totals(counted: list[DaySummary], code: Micronutrient) -> list[tuple[date, float]]:
    """A vitamin's or mineral's day totals on the counted days on which every meal has a value.

    A day where only some meals have food items would pull an average down, so it is left out.
    """
    return [
        (day.local_date, day.micronutrients[code].amount)
        for day in counted
        if code in day.micronutrients and day.micronutrients[code].meal_count == day.meal_count
    ]


def micronutrient_averages(counted: list[DaySummary]) -> dict[Micronutrient, OptionalAverage]:
    """Vitamins and minerals over the counted days on which every meal has a value.

    `days` says how many days each average uses. Shared with the weekly review and the insights.
    """
    averages = {}
    for code in MICRONUTRIENTS:
        amounts = [amount for _, amount in covered_totals(counted, code)]
        if amounts:
            averages[code] = OptionalAverage(
                average=round(sum(amounts) / len(amounts), 2), days=len(amounts)
            )
    return averages


def _period(days: list[DaySummary], today: date) -> Period:
    counted = counted_days(days, today)
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
    optional_averages = {}
    for name in OPTIONAL_NUTRIENT_FIELDS:
        amounts = [day.optional[name].amount for day in counted if name in day.optional]
        if amounts:
            optional_averages[name] = OptionalAverage(
                average=round(sum(amounts) / len(amounts), 1), days=len(amounts)
            )
    return Period(
        first_day=days[0].local_date,
        last_day=days[-1].local_date,
        counted_days=len(counted),
        logged_days=sum(1 for day in days if day.meal_count > 0),
        averages=averages,
        estimated_calories_share=share,
        optional_averages=optional_averages,
        micronutrient_averages=micronutrient_averages(counted),
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
