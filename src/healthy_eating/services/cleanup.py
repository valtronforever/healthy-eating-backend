"""Delete expired sign-in data: login requests, authorization codes, tokens and dead grants.

Runs inside the app process (see `run_cleanup_periodically`), so a deployment needs no
scheduler. Rotated and revoked refresh tokens are kept until their own expiry: presenting one
again before then still revokes its grant (reuse detection). After its expiry a token is
useless anyway. OAuth clients are never deleted: meals refer to the assistant that saved them.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, exists
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import (
    AccessTokenRecord,
    AuthorizationCodeRecord,
    Grant,
    LinkRequest,
    LoginRequest,
    RefreshTokenRecord,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupResult:
    login_requests: int
    authorization_codes: int
    access_tokens: int
    refresh_tokens: int
    grants: int

    @property
    def total(self) -> int:
        return (
            self.login_requests
            + self.authorization_codes
            + self.access_tokens
            + self.refresh_tokens
            + self.grants
        )


async def _count_deleted(session: AsyncSession, statement: Delete) -> int:
    result = cast(CursorResult[Any], await session.execute(statement))
    return result.rowcount


async def delete_expired(session: AsyncSession, *, now: datetime) -> CleanupResult:
    """Delete everything that expired at or before `now`, then grants without any token.

    A grant without tokens can never be used again, revoked or not.
    """
    expired = {
        model: await _count_deleted(session, delete(model).where(model.expires_at <= now))
        for model in (
            LoginRequest,
            LinkRequest,
            AuthorizationCodeRecord,
            AccessTokenRecord,
            RefreshTokenRecord,
        )
    }
    grants = await _count_deleted(
        session,
        delete(Grant).where(
            ~exists().where(AccessTokenRecord.grant_id == Grant.id),
            ~exists().where(RefreshTokenRecord.grant_id == Grant.id),
        ),
    )
    return CleanupResult(
        # Requests to add a login count with sign-in requests.
        login_requests=expired[LoginRequest] + expired[LinkRequest],
        authorization_codes=expired[AuthorizationCodeRecord],
        access_tokens=expired[AccessTokenRecord],
        refresh_tokens=expired[RefreshTokenRecord],
        grants=grants,
    )


async def run_cleanup(db: Database) -> CleanupResult:
    async with db.session() as session:
        result = await delete_expired(session, now=utcnow())
    if result.total:
        logger.info(
            "Cleanup deleted %d login requests, %d authorization codes, %d access tokens, "
            "%d refresh tokens and %d grants",
            result.login_requests,
            result.authorization_codes,
            result.access_tokens,
            result.refresh_tokens,
            result.grants,
        )
    return result


async def run_cleanup_periodically(db: Database, interval_seconds: float) -> None:
    """Run the cleanup every `interval_seconds` until cancelled; errors never stop the loop."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await run_cleanup(db)
        except Exception:
            logger.exception("Cleanup of expired sign-in data failed; retrying next time")
