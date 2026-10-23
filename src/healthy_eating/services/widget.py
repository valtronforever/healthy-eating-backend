"""Read-only tokens for the iOS widgets (healthy-eating-ios#35).

The iOS widgets fetch today's totals on their own schedule, without the app. They get their own
token instead of the app's: sharing the app's refresh token would let the widget and the app
rotate it at the same moment, and a reused refresh token revokes the whole sign-in. A widget
token reads only `/api/v1/widget/today`, belongs to one app sign-in (grant) and ends with it.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.auth.tokens import generate_token, hash_token
from healthy_eating.db.models import Grant, WidgetToken
from healthy_eating.services.connections import LAST_USED_RESOLUTION
from healthy_eating.services.days import user_today
from healthy_eating.services.integration import integration_status
from healthy_eating.services.settings import UserSettings, get_settings
from healthy_eating.services.summaries import DaySummary, daily_summaries


async def issue_widget_token(session: AsyncSession, user_id: uuid.UUID, grant_id: uuid.UUID) -> str:
    """A new widget token for this sign-in; an earlier one of the same sign-in stops working."""
    await session.execute(delete(WidgetToken).where(WidgetToken.grant_id == grant_id))
    token = generate_token()
    session.add(WidgetToken(token_hash=hash_token(token), grant_id=grant_id, user_id=user_id))
    await session.flush()
    return token


async def widget_user(session: AsyncSession, token: str, *, now: datetime) -> uuid.UUID | None:
    """The user of a widget token whose sign-in is still active, or None."""
    row = (
        await session.execute(
            select(WidgetToken.user_id, WidgetToken.token_hash)
            .join(Grant, Grant.id == WidgetToken.grant_id)
            .where(WidgetToken.token_hash == hash_token(token), Grant.revoked_at.is_(None))
        )
    ).first()
    if row is None:
        return None
    await session.execute(
        update(WidgetToken)
        .where(
            WidgetToken.token_hash == row.token_hash,
            or_(
                WidgetToken.last_used_at.is_(None),
                WidgetToken.last_used_at < now - LAST_USED_RESOLUTION,
            ),
        )
        .values(last_used_at=now)
    )
    user_id: uuid.UUID = row.user_id
    return user_id


@dataclass(frozen=True)
class WidgetToday:
    day: DaySummary
    settings: UserSettings
    last_meal_saved_at: datetime | None
    last_client_name: str | None


async def widget_today(session: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> WidgetToday:
    """What the widgets show: today's totals in the user's local day, settings, last saved meal."""
    today = await user_today(session, user_id, now)
    summaries = await daily_summaries(session, user_id, today, today)
    settings = await get_settings(session, user_id)
    assert settings is not None, "a valid widget token always has a user"
    integration = await integration_status(session, user_id, now=now)
    return WidgetToday(
        day=summaries.days[0],
        settings=settings,
        last_meal_saved_at=integration.last_meal_saved_at,
        last_client_name=integration.last_client_name,
    )
