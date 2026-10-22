"""Weigh-ins and the smoothed weight trend.

Weight is opt-in (decision in healthy-eating-ios#14): `users.weight_tracking` is off until the
first weigh-in from the chat (`assistant`) or the app (`app`), or until the user turns it on.
Apple Health entries are refused while it is off, so imports only start after the user opts in.
Turning it off hides weight but keeps the entries; deleting them is a separate action.

Trend method (documented for users in the app's "How this is calculated"):

1. Each local day with weigh-ins gets one value: the mean of that day's weigh-ins.
2. The trend starts at the first day's value. Every later day with a value moves the trend
   toward it by `1 - (1 - TREND_SMOOTHING) ** gap`, where `gap` is the number of days since the
   previous weigh-in. With TREND_SMOOTHING = 0.1 this is the exponentially smoothed average of
   The Hacker's Diet: a single high or low weigh-in moves the trend by a tenth, and a gap of
   several days counts like several days of the same weight.
3. The weekly change compares the latest trend with the trend CHANGE_WINDOW_DAYS earlier (or the
   first trend value, when the history is shorter), per 7 days. It needs at least
   MIN_CHANGE_SPAN_DAYS between the two, otherwise there is none.

Weight is stored in kg; the API speaks kg and the app converts for display.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import User, WeightEntry
from healthy_eating.nutrition.types import WeightSource
from healthy_eating.services.local_time import local_eaten_at, resolve_eaten_at, utc_window
from healthy_eating.services.unset import UNSET, Unset

MIN_WEIGHT_KG = 20.0
MAX_WEIGHT_KG = 400.0
MAX_BATCH = 1000
TREND_SMOOTHING = 0.1
CHANGE_WINDOW_DAYS = 28
MIN_CHANGE_SPAN_DAYS = 14


class WeightTrackingOffError(Exception):
    """Apple Health entries arrive although the user has not turned weight tracking on."""


class InvalidWeightError(ValueError):
    """A weight outside the plausible range."""


def check_weight(weight_kg: float) -> float:
    if not MIN_WEIGHT_KG <= weight_kg <= MAX_WEIGHT_KG:
        raise InvalidWeightError(
            f"Weight must be between {MIN_WEIGHT_KG:g} and {MAX_WEIGHT_KG:g} kg"
        )
    return weight_kg


@dataclass(frozen=True)
class WeightInput:
    weight_kg: float
    # None means now; a time without an offset is read in the user's time zone, as for meals.
    measured_at: datetime | None = None
    # Apple Health sample id: the same sample is stored once.
    external_id: str | None = None


@dataclass(frozen=True)
class LocalWeight:
    entry: WeightEntry
    # When it was measured, in its local time (see services/local_time.py).
    measured_at: datetime


@dataclass(frozen=True)
class TrendPoint:
    local_date: date
    # Mean of the day's weigh-ins, and the smoothed trend after that day.
    weight_kg: float
    trend_kg: float


@dataclass(frozen=True)
class WeightHistory:
    entries: list[LocalWeight]
    trend: list[TrendPoint]
    latest_trend_kg: float | None
    # Change of the trend per 7 days over the last CHANGE_WINDOW_DAYS; None when too short.
    weekly_change_kg: float | None
    change_days: int | None


def _local(entry: WeightEntry, time_zone: str | None) -> LocalWeight:
    return LocalWeight(
        entry=entry,
        measured_at=local_eaten_at(entry.measured_at, entry.utc_offset_minutes, time_zone),
    )


def daily_trend(daily: list[tuple[date, float]]) -> list[TrendPoint]:
    """The trend for days with weigh-ins, oldest first (steps 1 and 2 of the module doc)."""
    points: list[TrendPoint] = []
    for day, weight in sorted(daily):
        if not points:
            trend = weight
        else:
            previous = points[-1]
            gap = (day - previous.local_date).days
            factor = 1 - (1 - TREND_SMOOTHING) ** gap
            trend = previous.trend_kg + factor * (weight - previous.trend_kg)
        points.append(TrendPoint(local_date=day, weight_kg=weight, trend_kg=trend))
    return points


def weekly_change(points: list[TrendPoint]) -> tuple[float | None, int | None]:
    """Change of the trend per week over the last CHANGE_WINDOW_DAYS (step 3)."""
    if not points:
        return None, None
    last = points[-1]
    start_day = last.local_date - timedelta(days=CHANGE_WINDOW_DAYS)
    earlier = [point for point in points if point.local_date <= start_day]
    start = earlier[-1] if earlier else points[0]
    span = (last.local_date - start.local_date).days
    if span < MIN_CHANGE_SPAN_DAYS:
        return None, None
    return (last.trend_kg - start.trend_kg) / span * 7, span


async def _user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        raise LookupError("user not found")
    return user


async def add_weights(
    session: AsyncSession,
    user_id: uuid.UUID,
    inputs: list[WeightInput],
    source: WeightSource,
    *,
    now: datetime,
) -> list[LocalWeight]:
    """Store weigh-ins. A weigh-in from the chat or the app turns weight tracking on."""
    user = await _user(session, user_id)
    if source == "apple_health" and not user.weight_tracking:
        raise WeightTrackingOffError
    for item in inputs:
        check_weight(item.weight_kg)
    if source != "apple_health":
        user.weight_tracking = True

    saved = []
    for item in inputs:
        measured_at, offset = resolve_eaten_at(item.measured_at, user.timezone, now=now)
        entry = None
        if item.external_id is not None:
            entry = await session.scalar(
                select(WeightEntry).where(
                    WeightEntry.user_id == user_id, WeightEntry.external_id == item.external_id
                )
            )
        if entry is None:
            entry = WeightEntry(user_id=user_id, source=source, external_id=item.external_id)
            session.add(entry)
        entry.weight_kg = item.weight_kg
        entry.measured_at = measured_at
        entry.utc_offset_minutes = offset
        saved.append(entry)
    await session.flush()
    return [_local(entry, user.timezone) for entry in saved]


async def edit_weight(
    session: AsyncSession,
    user_id: uuid.UUID,
    weight_id: uuid.UUID,
    *,
    weight_kg: float | Unset = UNSET,
    measured_at: datetime | Unset = UNSET,
    now: datetime,
) -> LocalWeight | None:
    """Correct one of the user's weigh-ins. None if it does not exist or is someone else's."""
    entry = await session.scalar(
        select(WeightEntry)
        .where(WeightEntry.id == weight_id, WeightEntry.user_id == user_id)
        .with_for_update()
    )
    if entry is None:
        return None
    user = await _user(session, user_id)
    if not isinstance(weight_kg, Unset):
        entry.weight_kg = check_weight(weight_kg)
    if not isinstance(measured_at, Unset):
        entry.measured_at, entry.utc_offset_minutes = resolve_eaten_at(
            measured_at, user.timezone, now=now
        )
    await session.flush()
    return _local(entry, user.timezone)


async def delete_weight(session: AsyncSession, user_id: uuid.UUID, weight_id: uuid.UUID) -> bool:
    deleted = await session.scalar(
        delete(WeightEntry)
        .where(WeightEntry.id == weight_id, WeightEntry.user_id == user_id)
        .returning(WeightEntry.id)
    )
    return deleted is not None


async def delete_all_weights(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Delete every weigh-in of the user. Weight tracking keeps its setting."""
    count = await session.scalar(
        select(func.count()).select_from(WeightEntry).where(WeightEntry.user_id == user_id)
    )
    await session.execute(delete(WeightEntry).where(WeightEntry.user_id == user_id))
    return count or 0


async def weight_history(
    session: AsyncSession, user_id: uuid.UUID, first_day: date, last_day: date
) -> WeightHistory:
    """Weigh-ins and trend for the local days first_day..last_day (oldest first).

    The trend is computed from the very first weigh-in, so its value on first_day does not
    depend on the range asked for.
    """
    user = await session.get(User, user_id)
    time_zone = None if user is None else user.timezone
    _, end = utc_window(first_day, last_day)
    assert end is not None
    rows = await session.scalars(
        select(WeightEntry)
        .where(WeightEntry.user_id == user_id, WeightEntry.measured_at < end)
        .order_by(WeightEntry.measured_at, WeightEntry.id)
    )
    local = [_local(entry, time_zone) for entry in rows]
    local = [item for item in local if item.measured_at.date() <= last_day]

    by_day: dict[date, list[float]] = {}
    for item in local:
        by_day.setdefault(item.measured_at.date(), []).append(item.entry.weight_kg)
    points = daily_trend([(day, sum(values) / len(values)) for day, values in by_day.items()])
    change, span = weekly_change(points)
    in_range = [point for point in points if point.local_date >= first_day]
    return WeightHistory(
        entries=[item for item in local if item.measured_at.date() >= first_day],
        trend=[
            TrendPoint(
                local_date=point.local_date,
                weight_kg=round(point.weight_kg, 2),
                trend_kg=round(point.trend_kg, 2),
            )
            for point in in_range
        ],
        latest_trend_kg=round(points[-1].trend_kg, 2) if points else None,
        weekly_change_kg=None if change is None else round(change, 2),
        change_days=span,
    )
