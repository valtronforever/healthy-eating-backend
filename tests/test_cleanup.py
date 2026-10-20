import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import (
    AccessTokenRecord,
    AuthorizationCodeRecord,
    Grant,
    LoginRequest,
    OAuthClient,
    RefreshTokenRecord,
)
from healthy_eating.services.cleanup import (
    CleanupResult,
    delete_expired,
    run_cleanup_periodically,
)
from tests.oauth_helpers import (
    bearer,
    dev_login,
    pkce_pair,
    refresh,
    register_client,
    sign_in,
    start_authorization,
)

# The clock the cleanup runs at, relative to the sign-ins below.
LATER = timedelta(hours=2)  # login requests, codes and access tokens have expired
MUCH_LATER = timedelta(days=91)  # refresh tokens (90 days) have expired too

MODELS = (LoginRequest, AuthorizationCodeRecord, AccessTokenRecord, RefreshTokenRecord, Grant)


async def row_counts(app: FastAPI) -> dict[str, int]:
    db: Database = app.state.db
    async with db.session() as session:
        return {
            model.__tablename__: await session.scalar(select(func.count()).select_from(model)) or 0
            for model in MODELS
        }


async def cleanup_at(app: FastAPI, now: datetime) -> CleanupResult:
    db: Database = app.state.db
    async with db.session() as session:
        return await delete_expired(session, now=now)


async def signed_in(http: httpx.AsyncClient) -> tuple[Any, dict[str, Any]]:
    client = await register_client(http, name="Claude")
    return client, await sign_in(http, client, email="olga@example.com")


async def test_keeps_everything_that_is_still_valid(app: FastAPI, http: httpx.AsyncClient) -> None:
    _, tokens = await signed_in(http)

    result = await cleanup_at(app, utcnow())

    assert result.total == 0
    response = await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))
    assert response.status_code == 200


async def test_deletes_expired_login_requests_codes_and_access_tokens(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    started = utcnow()
    await signed_in(http)
    # An abandoned sign-in (login page never completed) and an unused authorization code.
    client = await register_client(http, name="ChatGPT")
    _, challenge = pkce_pair()
    await start_authorization(http, client, challenge)
    await start_authorization(http, client, challenge)
    await dev_login(http, "olga@example.com")

    result = await cleanup_at(app, started + LATER)

    assert result == CleanupResult(
        login_requests=1, authorization_codes=1, access_tokens=1, refresh_tokens=0, grants=0
    )
    counts = await row_counts(app)
    assert counts["refresh_tokens"] == 1
    assert counts["grants"] == 1


async def test_deletes_grants_once_their_tokens_expired(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    started = utcnow()
    await signed_in(http)

    result = await cleanup_at(app, started + MUCH_LATER)

    assert result.refresh_tokens == 1
    assert result.grants == 1
    assert set((await row_counts(app)).values()) == {0}
    # Clients stay: meals refer to the assistant that saved them.
    db: Database = app.state.db
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(OAuthClient)) == 1


async def test_rotated_refresh_token_still_revokes_the_grant(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    started = utcnow()
    client, tokens = await signed_in(http)
    rotated = (await refresh(http, client, tokens["refresh_token"])).json()

    await cleanup_at(app, started + LATER)

    # The old refresh token is kept until it expires, so presenting it again is still
    # detected as reuse and ends the whole grant.
    reused = await refresh(http, client, tokens["refresh_token"])
    assert reused.status_code in (400, 401)
    assert (await refresh(http, client, rotated["refresh_token"])).status_code in (400, 401)


async def test_revoked_grant_is_deleted_with_its_last_token(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    started = utcnow()
    _, tokens = await signed_in(http)
    [connection] = (
        await http.get("/api/v1/connections", headers=bearer(tokens["access_token"]))
    ).json()["connections"]
    grant_id = connection["grants"][0]["grant_id"]
    response = await http.delete(
        f"/api/v1/connections/{grant_id}", headers=bearer(tokens["access_token"])
    )
    assert response.status_code == 200

    # Revoked tokens are kept until they expire; then the grant goes with them.
    assert (await cleanup_at(app, started + LATER)).grants == 0
    assert (await cleanup_at(app, started + MUCH_LATER)).grants == 1


async def test_runs_periodically_inside_the_app(
    app: FastAPI, http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    client = await register_client(http, name="Claude")
    _, challenge = pkce_pair()
    await start_authorization(http, client, challenge)
    db: Database = app.state.db
    async with db.session() as session:
        login = await session.scalar(select(LoginRequest))
        assert login is not None
        login.expires_at = utcnow() - timedelta(seconds=1)

    with caplog.at_level(logging.INFO, logger="healthy_eating.services.cleanup"):
        task = asyncio.create_task(run_cleanup_periodically(db, interval_seconds=0.01))
        try:
            for _ in range(200):
                if (await row_counts(app))["login_requests"] == 0:
                    break
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert (await row_counts(app))["login_requests"] == 0
    [line] = [r.getMessage() for r in caplog.records if r.name.endswith("cleanup")]
    assert line.startswith("Cleanup deleted 1 login requests, 0 authorization codes")
