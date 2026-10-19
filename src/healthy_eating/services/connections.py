"""Connections: the AI assistants and app sessions signed in to a user's account.

Each sign-in is a grant (db.models.Grant). Listing shows active grants grouped by client;
revoking a grant ends all of its tokens at once.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.auth.tokens import hash_token
from healthy_eating.db.models import AccessTokenRecord, Grant, OAuthClient, RefreshTokenRecord

# The iOS app signs in with redirect URI healthyeating://oauth/callback.
IOS_APP_SCHEME = "healthyeating"
# A grant's last use is recorded at most this often, to keep token checks cheap.
LAST_USED_RESOLUTION = timedelta(minutes=1)

ConnectionKind = Literal["ai_assistant", "ios_app"]


@dataclass(frozen=True)
class GrantInfo:
    grant_id: uuid.UUID
    created_at: datetime
    last_used_at: datetime | None
    # Whether this is the grant of the request asking.
    current: bool


@dataclass(frozen=True)
class ClientConnection:
    client_id: str
    client_name: str | None
    kind: ConnectionKind
    grants: list[GrantInfo]


def connection_kind(client_info: dict[str, Any]) -> ConnectionKind:
    redirect_uris = client_info.get("redirect_uris") or []
    if any(str(uri).startswith(f"{IOS_APP_SCHEME}:") for uri in redirect_uris):
        return "ios_app"
    return "ai_assistant"


async def revoke_grant(session: AsyncSession, grant_id: uuid.UUID, *, now: datetime) -> None:
    """End a grant and every token issued for it."""
    for model in (AccessTokenRecord, RefreshTokenRecord):
        await session.execute(
            update(model)
            .where(model.grant_id == grant_id, model.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    await session.execute(
        update(Grant).where(Grant.id == grant_id, Grant.revoked_at.is_(None)).values(revoked_at=now)
    )


async def record_grant_use(session: AsyncSession, grant_id: uuid.UUID, *, now: datetime) -> None:
    await session.execute(
        update(Grant)
        .where(
            Grant.id == grant_id,
            or_(Grant.last_used_at.is_(None), Grant.last_used_at < now - LAST_USED_RESOLUTION),
        )
        .values(last_used_at=now)
    )


async def grant_of_access_token(session: AsyncSession, token: str) -> uuid.UUID | None:
    grant_id: uuid.UUID | None = await session.scalar(
        select(AccessTokenRecord.grant_id).where(AccessTokenRecord.token_hash == hash_token(token))
    )
    return grant_id


def _active(now: datetime) -> Any:
    """Grants that can still be used: not revoked, with a live refresh or access token."""
    live_refresh = exists().where(
        RefreshTokenRecord.grant_id == Grant.id,
        RefreshTokenRecord.revoked_at.is_(None),
        RefreshTokenRecord.expires_at > now,
    )
    live_access = exists().where(
        AccessTokenRecord.grant_id == Grant.id,
        AccessTokenRecord.revoked_at.is_(None),
        AccessTokenRecord.expires_at > now,
    )
    return Grant.revoked_at.is_(None) & (live_refresh | live_access)


async def list_connections(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    current_grant_id: uuid.UUID | None,
    now: datetime,
) -> list[ClientConnection]:
    """Active grants grouped by client, the most recently used client first."""
    rows = await session.execute(
        select(Grant, OAuthClient.client_info)
        .join(OAuthClient, OAuthClient.client_id == Grant.client_id)
        .where(Grant.user_id == user_id, _active(now))
        .order_by(Grant.created_at)
    )
    clients: dict[str, ClientConnection] = {}
    for grant, client_info in rows:
        client = clients.setdefault(
            grant.client_id,
            ClientConnection(
                client_id=grant.client_id,
                client_name=client_info.get("client_name"),
                kind=connection_kind(client_info),
                grants=[],
            ),
        )
        client.grants.append(
            GrantInfo(
                grant_id=grant.id,
                created_at=grant.created_at,
                last_used_at=grant.last_used_at,
                current=grant.id == current_grant_id,
            )
        )

    def last_activity(client: ClientConnection) -> datetime:
        return max(grant.last_used_at or grant.created_at for grant in client.grants)

    return sorted(clients.values(), key=last_activity, reverse=True)


async def revoke_connection(
    session: AsyncSession, user_id: uuid.UUID, grant_id: uuid.UUID, *, now: datetime
) -> bool:
    """Revoke one of the user's active grants. False if there is no such grant."""
    found = await session.scalar(
        select(Grant.id).where(Grant.id == grant_id, Grant.user_id == user_id, _active(now))
    )
    if found is None:
        return False
    await revoke_grant(session, grant_id, now=now)
    return True
