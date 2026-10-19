"""Integration status: does the AI assistant reach Healthy Eating? No meal contents."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal, OAuthClient
from healthy_eating.services.days import local_today
from healthy_eating.services.local_time import local_date_column, utc_window
from healthy_eating.services.settings import get_time_zone


@dataclass(frozen=True)
class IntegrationStatus:
    # When the most recent meal reached Healthy Eating (not when it was eaten).
    last_meal_saved_at: datetime | None
    last_client_name: str | None
    meals_today: int


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
    return IntegrationStatus(
        last_meal_saved_at=None if last is None else last[0],
        last_client_name=None if last is None else last[1],
        meals_today=meals_today or 0,
    )
