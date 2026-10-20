"""Integration status: does the AI assistant reach Healthy Eating? No meal contents.

`arrival_status` tells whether meals still arrive as usual. A broken integration (an expired
connector, a disabled tool) loses meals silently; this is about the integration, never about
the user eating or logging less. It looks at when meals reached the server (`created_at`), not
when they were eaten, so a one-off import of older meals does not count as a usual day.

- "unknown": meals were saved on fewer than MIN_ACTIVE_DAYS of the last HISTORY_DAYS local days
  (before today). New users and irregular patterns never get a status.
- "ok": a meal was saved today, or it is not yet later than usual.
- "quiet": nothing was saved all of yesterday, or nothing today although the usual time has
  passed: the USUAL_TIME_PERCENTILE of the local times of each day's first save, plus GRACE.
"""

import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal, OAuthClient
from healthy_eating.services.days import local_today
from healthy_eating.services.local_time import local_date_column, utc_window, zone_info
from healthy_eating.services.settings import get_time_zone

ArrivalStatus = Literal["ok", "quiet", "unknown"]

HISTORY_DAYS = 28
MIN_ACTIVE_DAYS = 23
USUAL_TIME_PERCENTILE = 0.8
GRACE = timedelta(hours=2)


def arrival_status(
    saved_at: Iterable[datetime], time_zone: str | None, *, now: datetime
) -> ArrivalStatus:
    """Whether meals still arrive as usual, from the times meals were saved (see module doc)."""
    zone = zone_info(time_zone)
    local_now = now.astimezone(zone)
    today = local_now.date()
    first_saves: dict[date, time] = {}
    for moment in saved_at:
        local = moment.astimezone(zone)
        first = first_saves.get(local.date())
        if first is None or local.time() < first:
            first_saves[local.date()] = local.time()

    history = [today - timedelta(days=n) for n in range(1, HISTORY_DAYS + 1)]
    usual_days = [first_saves[day] for day in history if day in first_saves]
    if len(usual_days) < MIN_ACTIVE_DAYS:
        return "unknown"
    if today in first_saves:
        return "ok"
    if today - timedelta(days=1) not in first_saves:
        return "quiet"
    first_times = sorted(usual_days)
    usual = first_times[math.ceil(USUAL_TIME_PERCENTILE * len(first_times)) - 1]
    expected_by = datetime.combine(today, usual, tzinfo=zone) + GRACE
    return "quiet" if local_now >= expected_by else "ok"


@dataclass(frozen=True)
class IntegrationStatus:
    # When the most recent meal reached Healthy Eating (not when it was eaten).
    last_meal_saved_at: datetime | None
    last_client_name: str | None
    meals_today: int
    arrival_status: ArrivalStatus


async def integration_status(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime
) -> IntegrationStatus:
    last = (
        await session.execute(
            select(Meal.created_at, OAuthClient.client_info["client_name"].astext)
            .outerjoin(OAuthClient, OAuthClient.client_id == Meal.client_id)
            .where(Meal.user_id == user_id)
            .order_by(Meal.created_at.desc())
            .limit(1)
        )
    ).first()

    time_zone = await get_time_zone(session, user_id)
    today = local_today(time_zone, now)
    start, end = utc_window(today, today)
    assert start is not None and end is not None
    local_date = local_date_column(time_zone)
    meals_today = await session.scalar(
        select(func.count()).where(
            Meal.user_id == user_id,
            Meal.eaten_at >= start,
            Meal.eaten_at < end,
            local_date == today,
        )
    )
    # A day more than the history, so every local day of it is complete in any time zone.
    saved_at = await session.scalars(
        select(Meal.created_at).where(
            Meal.user_id == user_id,
            Meal.created_at >= now - timedelta(days=HISTORY_DAYS + 2),
        )
    )
    return IntegrationStatus(
        last_meal_saved_at=None if last is None else last[0],
        last_client_name=None if last is None else last[1],
        meals_today=meals_today or 0,
        arrival_status=arrival_status(saved_at, time_zone, now=now),
    )
