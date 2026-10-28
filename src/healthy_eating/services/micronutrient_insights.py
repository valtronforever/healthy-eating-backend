"""Vitamin and mineral insights (#36): how intake went over weeks and where it came from.

Values exist only for meals with food items, so every number says what it covers:
- Averages (the whole period and each week) use the counted days on which every meal has a
  value, as in Trends (`covered_totals`); today counts only once it is marked complete.
- On those days the day total is below, within or above the range that applies now (the
  user's goal, else the life stage reference). The three counts are facts of equal weight.
- Sources use every meal of the period, counted day or not: each item's amount (label values,
  or the USDA food's values for its weight) and its share of all items' amounts. Items whose
  USDA food is no longer in the bundled data have no amount here.
- Coverage: the period's meals, and how many of them have vitamin and mineral values.

The period before has the same length and ends the day before `first_day`.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal, MealItem, ReferenceFood
from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS, Micronutrient
from healthy_eating.services.days import user_today
from healthy_eating.services.local_time import local_date_column, utc_window
from healthy_eating.services.settings import (
    MicronutrientRange,
    get_settings,
    get_time_zone,
    micronutrient_ranges,
)
from healthy_eating.services.summaries import (
    MAX_SUMMARY_DAYS,
    CountingRule,
    RangeTooLongError,
    daily_summaries,
)
from healthy_eating.services.trends import OptionalAverage, counted_days, covered_totals

# Sources listed per kind (foods, label items); the rest are in no list.
TOP_SOURCES = 5


@dataclass(frozen=True)
class WeekAverage:
    # A local week, Monday to Sunday, cut to the period at its ends.
    first_day: date
    last_day: date
    # Over the week's counted days on which every meal has a value; None when there are none.
    average: float | None
    days: int


@dataclass(frozen=True)
class RangeDays:
    below: int
    within: int
    above: int


@dataclass(frozen=True)
class Source:
    # The item's name as the assistant wrote it last; items are grouped ignoring case.
    name: str
    amount: float
    # Of all items' amounts of this nutrient in the period (0 to 1).
    share: float
    meals: int


@dataclass(frozen=True)
class NutrientInsight:
    average: OptionalAverage | None
    # None when no range applies.
    range_days: RangeDays | None
    weeks: list[WeekAverage]
    food_sources: list[Source]
    # Items with label values: supplements and packaged foods.
    label_sources: list[Source]


@dataclass(frozen=True)
class InsightsPeriod:
    first_day: date
    last_day: date
    counted_days: int
    meal_count: int
    meals_with_values: int
    # Nutrients without any value in the period are left out.
    nutrients: dict[Micronutrient, NutrientInsight]


@dataclass(frozen=True)
class Insights:
    counting_rule: CountingRule
    ranges: dict[Micronutrient, MicronutrientRange]
    current: InsightsPeriod
    previous: InsightsPeriod


@dataclass
class _Source:
    name: str
    amount: float = 0.0
    meals: set[uuid.UUID] = field(default_factory=set)


async def _coverage(
    session: AsyncSession, user_id: uuid.UUID, time_zone: str | None, first: date, last: date
) -> tuple[int, int]:
    local_date = local_date_column(time_zone)
    start, end = utc_window(first, last)
    # A meal without items may hold SQL NULL or JSON null (a relogged one); {} matched nothing.
    has_values = and_(
        func.jsonb_typeof(Meal.micronutrients) == "object",
        Meal.micronutrients != literal_column("'{}'::jsonb"),
    )
    row = (
        await session.execute(
            select(func.count(), func.count().filter(has_values)).where(
                Meal.user_id == user_id,
                Meal.eaten_at >= start,
                Meal.eaten_at < end,
                local_date >= first,
                local_date <= last,
            )
        )
    ).one()
    return row[0], row[1]


async def _sources(
    session: AsyncSession,
    user_id: uuid.UUID,
    time_zone: str | None,
    first: date,
    last: date,
    codes: list[Micronutrient],
) -> dict[Micronutrient, tuple[list[Source], list[Source]]]:
    local_date = local_date_column(time_zone)
    start, end = utc_window(first, last)
    rows = await session.execute(
        select(
            MealItem.meal_id,
            MealItem.name,
            MealItem.status,
            MealItem.grams,
            MealItem.label_values,
            ReferenceFood.nutrients,
        )
        .join(Meal, Meal.id == MealItem.meal_id)
        .outerjoin(
            ReferenceFood, and_(MealItem.status == "matched", ReferenceFood.id == MealItem.food_id)
        )
        .where(
            Meal.user_id == user_id,
            Meal.eaten_at >= start,
            Meal.eaten_at < end,
            local_date >= first,
            local_date <= last,
            MealItem.status.in_(("matched", "label")),
        )
        .order_by(Meal.eaten_at, MealItem.position)
    )
    found: dict[Micronutrient, dict[tuple[bool, str], _Source]] = defaultdict(dict)
    for meal_id, name, status, grams, label_values, per_100_g in rows:
        is_label = status == "label"
        # Label values are for the amount taken; USDA values per 100 g (matched items have grams).
        if is_label:
            values, scale = label_values, 1.0
        elif grams is not None:
            values, scale = per_100_g, grams / 100
        else:
            continue
        if not values:
            continue
        for code in codes:
            if code not in values:
                continue
            amount = values[code] * scale
            key = (is_label, name.strip().casefold())
            source = found[code].setdefault(key, _Source(name))
            # Later meals come later: keep the latest spelling.
            source.name = name
            source.amount += amount
            source.meals.add(meal_id)

    result = {}
    for code, by_key in found.items():
        total = sum(source.amount for source in by_key.values())
        lists: tuple[list[Source], list[Source]] = ([], [])
        for (is_label, _), source in sorted(by_key.items(), key=lambda pair: -pair[1].amount):
            listed = lists[1] if is_label else lists[0]
            if len(listed) == TOP_SOURCES or source.amount <= 0:
                continue
            listed.append(
                Source(
                    name=source.name,
                    amount=round(source.amount, 2),
                    share=round(source.amount / total, 3),
                    meals=len(source.meals),
                )
            )
        result[code] = lists
    return result


def _weeks(first: date, last: date) -> list[tuple[date, date]]:
    weeks = []
    start = first
    while start <= last:
        end = min(start + timedelta(days=6 - start.weekday()), last)
        weeks.append((start, end))
        start = end + timedelta(days=1)
    return weeks


def _range_days(totals: list[float], found: MicronutrientRange | None) -> RangeDays | None:
    if found is None:
        return None
    below = sum(1 for amount in totals if found.min is not None and amount < found.min)
    above = sum(1 for amount in totals if found.max is not None and amount > found.max)
    return RangeDays(below=below, within=len(totals) - below - above, above=above)


def _average(totals: list[float]) -> float | None:
    return round(sum(totals) / len(totals), 2) if totals else None


async def _period(
    session: AsyncSession,
    user_id: uuid.UUID,
    first: date,
    last: date,
    *,
    today: date,
    codes: list[Micronutrient],
    ranges: dict[Micronutrient, MicronutrientRange],
) -> tuple[InsightsPeriod, CountingRule]:
    summaries = await daily_summaries(session, user_id, first, last)
    counted = counted_days(summaries.days, today)
    time_zone = await get_time_zone(session, user_id)
    meal_count, meals_with_values = await _coverage(session, user_id, time_zone, first, last)
    sources = await _sources(session, user_id, time_zone, first, last, codes)

    nutrients = {}
    for code in codes:
        covered = covered_totals(counted, code)
        food_sources, label_sources = sources.get(code, ([], []))
        if not covered and not food_sources and not label_sources:
            continue
        totals = [amount for _, amount in covered]
        weeks = []
        for week_first, week_last in _weeks(first, last):
            in_week = [amount for day, amount in covered if week_first <= day <= week_last]
            weeks.append(WeekAverage(week_first, week_last, _average(in_week), len(in_week)))
        average = _average(totals)
        nutrients[code] = NutrientInsight(
            average=None if average is None else OptionalAverage(average, len(totals)),
            range_days=_range_days(totals, ranges.get(code)) if totals else None,
            weeks=weeks,
            food_sources=food_sources,
            label_sources=label_sources,
        )
    period = InsightsPeriod(
        first_day=first,
        last_day=last,
        counted_days=len(counted),
        meal_count=meal_count,
        meals_with_values=meals_with_values,
        nutrients=nutrients,
    )
    return period, summaries.counting_rule


async def micronutrient_insights(
    session: AsyncSession,
    user_id: uuid.UUID,
    first_day: date,
    last_day: date,
    *,
    now: datetime,
    nutrients: list[Micronutrient] | None = None,
) -> Insights | None:
    """Insights for the local days first_day..last_day and the period before; None without the
    user. Raises RangeTooLongError beyond MAX_SUMMARY_DAYS and ValueError for a reversed range."""
    if first_day > last_day:
        raise ValueError("first_day is after last_day")
    length = (last_day - first_day).days + 1
    if length > MAX_SUMMARY_DAYS:
        raise RangeTooLongError(f"Ask for at most {MAX_SUMMARY_DAYS} days at a time")
    settings = await get_settings(session, user_id)
    if settings is None:
        return None
    codes = [code for code in MICRONUTRIENTS if nutrients is None or code in nutrients]
    ranges = {
        code: found for code, found in micronutrient_ranges(settings).items() if code in codes
    }
    today = await user_today(session, user_id, now)
    current, counting_rule = await _period(
        session, user_id, first_day, last_day, today=today, codes=codes, ranges=ranges
    )
    previous_last = first_day - timedelta(days=1)
    previous, _ = await _period(
        session,
        user_id,
        previous_last - timedelta(days=length - 1),
        previous_last,
        today=today,
        codes=codes,
        ranges=ranges,
    )
    return Insights(counting_rule=counting_rule, ranges=ranges, current=current, previous=previous)
