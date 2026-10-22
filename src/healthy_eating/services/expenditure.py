"""Energy expenditure estimated from the user's own log and weight trend.

Per-meal AI estimates carry a steady bias. Instead of trusting a formula based on age and
height, the estimate compares what the log says was eaten with what the weight trend did over
the same days. A bias in the log is then part of both sides, so a target set from this estimate
stays consistent with the log (decision in healthy-eating-ios#14; the MacroFactor approach).

Method, in plain words (shown in the app's "How this is calculated"):

1. Window: the last WINDOW_DAYS local days. Days count as in Trends (services/trends.py):
   complete days only once the user marks any day complete, today only once it is complete.
2. Intake: the average of the counted days' calories.
3. Weight: the smoothed trend (services/weights.py) at the start and at the end of the window.
   The change is spread over the days between those two trend values.
4. Expenditure = average intake - trend change x KCAL_PER_KG / days. Losing weight means you
   used more than you logged; gaining means less.
5. It needs at least MIN_COUNTED_DAYS counted days and weigh-ins on at least MIN_WEIGH_IN_DAYS
   days spanning MIN_SPAN_DAYS days; otherwise there is no estimate, with a reason.
6. Confidence grows with counted days, weigh-in days and the span (see `_confidence`).

KCAL_PER_KG is the usual approximation for body tissue; it is rough, which is one more reason
the app shows the result as "about" and rounds it to 10 kcal.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.nutrition.types import Confidence
from healthy_eating.services.days import user_today
from healthy_eating.services.summaries import CountingRule, daily_summaries
from healthy_eating.services.weights import TrendPoint, weight_history

WINDOW_DAYS = 28
KCAL_PER_KG = 7700.0
MIN_COUNTED_DAYS = 14
MIN_WEIGH_IN_DAYS = 6
MIN_SPAN_DAYS = 14

NoEstimateReason = Literal["not_enough_logged_days", "not_enough_weigh_ins"]


@dataclass(frozen=True)
class Expenditure:
    first_day: date
    last_day: date
    counting_rule: CountingRule
    counted_days: int
    weigh_ins: int
    weigh_in_days: int
    average_intake_kcal: float | None
    trend_change_kg: float | None
    span_days: int | None
    # None when there is not enough data; `reason` says what is missing.
    maintenance_kcal: float | None
    confidence: Confidence | None
    reason: NoEstimateReason | None


def _confidence(counted_days: int, weigh_in_days: int, span_days: int) -> Confidence:
    if counted_days >= 24 and weigh_in_days >= 16 and span_days >= 24:
        return "high"
    if counted_days >= 18 and weigh_in_days >= 10 and span_days >= 21:
        return "medium"
    return "low"


def _span(trend: list[TrendPoint], first_day: date) -> tuple[TrendPoint, TrendPoint] | None:
    """The trend at the window's start (or its first weigh-in) and at its last weigh-in."""
    if not trend:
        return None
    before = [point for point in trend if point.local_date <= first_day]
    start = before[-1] if before else trend[0]
    return start, trend[-1]


def estimate(
    *,
    first_day: date,
    last_day: date,
    counting_rule: CountingRule,
    counted_calories: list[float],
    weigh_in_count: int,
    trend: list[TrendPoint],
) -> Expenditure:
    """The estimate from counted days' calories and the trend (pure; see the module doc)."""
    in_window = [point for point in trend if point.local_date >= first_day]
    weigh_in_days = len(in_window)
    average = sum(counted_calories) / len(counted_calories) if counted_calories else None
    span = _span(trend, first_day)
    change = days = None
    if span is not None:
        start, end = span
        days = (end.local_date - start.local_date).days
        change = end.trend_kg - start.trend_kg

    reason: NoEstimateReason | None = None
    if len(counted_calories) < MIN_COUNTED_DAYS:
        reason = "not_enough_logged_days"
    elif weigh_in_days < MIN_WEIGH_IN_DAYS or days is None or days < MIN_SPAN_DAYS:
        reason = "not_enough_weigh_ins"

    maintenance = confidence = None
    if reason is None and average is not None and change is not None and days:
        maintenance = round((average - change * KCAL_PER_KG / days) / 10) * 10.0
        confidence = _confidence(len(counted_calories), weigh_in_days, days)
    return Expenditure(
        first_day=first_day,
        last_day=last_day,
        counting_rule=counting_rule,
        counted_days=len(counted_calories),
        weigh_ins=weigh_in_count,
        weigh_in_days=weigh_in_days,
        average_intake_kcal=None if average is None else round(average, 1),
        trend_change_kg=None if change is None else round(change, 2),
        span_days=days,
        maintenance_kcal=maintenance,
        confidence=confidence,
        reason=reason,
    )


async def expenditure(session: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> Expenditure:
    today = await user_today(session, user_id, now)
    first_day = today - timedelta(days=WINDOW_DAYS - 1)
    summaries = await daily_summaries(session, user_id, first_day, today)
    counted = [
        day.calories_kcal
        for day in summaries.days
        if day.counted and (day.local_date != today or day.status == "complete")
    ]
    # The whole history, so the window's start can use the last weigh-in before it.
    history = await weight_history(session, user_id, date.min, today)
    return estimate(
        first_day=first_day,
        last_day=today,
        counting_rule=summaries.counting_rule,
        counted_calories=counted,
        weigh_in_count=sum(1 for item in history.entries if item.measured_at.date() >= first_day),
        trend=history.trend,
    )
