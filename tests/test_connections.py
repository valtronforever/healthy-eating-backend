from datetime import timedelta
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Grant
from healthy_eating.services.connections import record_grant_use
from tests.oauth_helpers import Client, bearer, refresh, register_client, sign_in

IOS_REDIRECT = "healthyeating://oauth/callback"


async def connections(http: httpx.AsyncClient, token: str) -> list[dict[str, Any]]:
    response = await http.get("/api/v1/connections", headers=bearer(token))
    assert response.status_code == 200, response.text
    result: list[dict[str, Any]] = response.json()["connections"]
    return result


async def signed_in(
    http: httpx.AsyncClient, name: str, email: str, redirect_uri: str | None = None
) -> tuple[Client, dict[str, Any]]:
    if redirect_uri is None:
        client = await register_client(http, name=name)
    else:
        client = await register_client(http, name=name, redirect_uri=redirect_uri)
    return client, await sign_in(http, client, email=email)


async def test_lists_assistants_and_app_sessions(http: httpx.AsyncClient) -> None:
    await signed_in(http, "Claude", "nina@example.com")
    _, app = await signed_in(http, "Healthy Eating", "nina@example.com", IOS_REDIRECT)
    await signed_in(http, "ChatGPT", "someone-else@example.com")

    listed = await connections(http, app["access_token"])

    by_name = {client["name"]: client for client in listed}
    assert set(by_name) == {"Claude", "Healthy Eating"}
    assert by_name["Claude"]["kind"] == "ai_assistant"
    assert by_name["Healthy Eating"]["kind"] == "ios_app"
    assert by_name["Healthy Eating"]["grants"][0]["current"] is True
    assert by_name["Claude"]["grants"][0]["current"] is False
    # Using a token records the grant's last use.
    assert by_name["Claude"]["grants"][0]["last_used_at"] is None
    assert by_name["Healthy Eating"]["grants"][0]["last_used_at"] is not None
    assert listed[0]["name"] == "Healthy Eating"


async def test_refreshing_keeps_one_grant(http: httpx.AsyncClient) -> None:
    client, tokens = await signed_in(http, "Claude", "nina@example.com")
    refreshed = (await refresh(http, client, tokens["refresh_token"])).json()

    [claude] = await connections(http, refreshed["access_token"])

    assert len(claude["grants"]) == 1


async def test_last_use_is_recorded_at_most_once_a_minute(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    _, tokens = await signed_in(http, "Claude", "nina@example.com")
    await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))
    db: Database = app.state.db
    async with db.session() as session:
        grant = await session.scalar(select(Grant))
        assert grant is not None
        recent = utcnow() - timedelta(seconds=30)
        await session.execute(update(Grant).values(last_used_at=recent))
        await record_grant_use(session, grant.id, now=utcnow())
        assert await session.scalar(select(Grant.last_used_at)) == recent
        stale = utcnow() - timedelta(minutes=2)
        await session.execute(update(Grant).values(last_used_at=stale))
        now = utcnow()
        await record_grant_use(session, grant.id, now=now)
        assert await session.scalar(select(Grant.last_used_at)) == now


async def test_revoking_an_assistant_ends_its_tokens(http: httpx.AsyncClient) -> None:
    claude_client, claude = await signed_in(http, "Claude", "nina@example.com")
    _, app = await signed_in(http, "Healthy Eating", "nina@example.com", IOS_REDIRECT)
    listed = await connections(http, app["access_token"])
    grant_id = next(c for c in listed if c["name"] == "Claude")["grants"][0]["grant_id"]

    response = await http.delete(
        f"/api/v1/connections/{grant_id}", headers=bearer(app["access_token"])
    )

    assert response.status_code == 200
    assert response.json() == {"grant_id": grant_id, "signed_out": False}
    assert (await http.get("/api/v1/me", headers=bearer(claude["access_token"]))).status_code == 401
    assert (await refresh(http, claude_client, claude["refresh_token"])).status_code != 200
    assert [c["name"] for c in await connections(http, app["access_token"])] == ["Healthy Eating"]
    again = await http.delete(
        f"/api/v1/connections/{grant_id}", headers=bearer(app["access_token"])
    )
    assert again.status_code == 404


async def test_revoking_the_current_app_session_signs_out(http: httpx.AsyncClient) -> None:
    _, app = await signed_in(http, "Healthy Eating", "nina@example.com", IOS_REDIRECT)
    [client] = await connections(http, app["access_token"])
    grant_id = client["grants"][0]["grant_id"]

    response = await http.delete(
        f"/api/v1/connections/{grant_id}", headers=bearer(app["access_token"])
    )

    assert response.json() == {"grant_id": grant_id, "signed_out": True}
    assert (await http.get("/api/v1/me", headers=bearer(app["access_token"]))).status_code == 401


async def test_oauth_revoke_removes_the_connection(http: httpx.AsyncClient) -> None:
    claude_client, claude = await signed_in(http, "Claude", "nina@example.com")
    _, app = await signed_in(http, "Healthy Eating", "nina@example.com", IOS_REDIRECT)
    await http.post(
        "/revoke",
        data={
            "token": claude["access_token"],
            "client_id": claude_client.client_id,
            "client_secret": "",
        },
    )
    assert [c["name"] for c in await connections(http, app["access_token"])] == ["Healthy Eating"]


async def test_users_cannot_revoke_other_users_connections(http: httpx.AsyncClient) -> None:
    _, nina = await signed_in(http, "Claude", "nina@example.com")
    _, omar = await signed_in(http, "Claude", "omar@example.com")
    [ninas] = await connections(http, nina["access_token"])
    grant_id = ninas["grants"][0]["grant_id"]

    response = await http.delete(
        f"/api/v1/connections/{grant_id}", headers=bearer(omar["access_token"])
    )

    assert response.status_code == 404
    assert (await http.get("/api/v1/me", headers=bearer(nina["access_token"]))).status_code == 200
    assert (await http.get("/api/v1/connections")).status_code == 401
