import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from healthy_eating.db import Base, Database
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, refresh, register_client, sign_in

MEAL: dict[str, Any] = {
    "dish_name": "Porridge",
    "source": "from_conversation",
    "calories_kcal": 350,
    "protein_g": 12,
    "fat_g": 8,
    "carbohydrates_g": 55,
}
CONFIRM = {"confirm": "delete-account"}


async def rows_of(app: FastAPI, user_id: uuid.UUID) -> dict[str, int]:
    """Rows in every table that belong to the user (by users.id or a user_id column)."""
    db: Database = app.state.db
    counts = {}
    async with db.session() as session:
        for table in Base.metadata.sorted_tables:
            column = table.c.id if table.name == "users" else table.c.get("user_id")
            if column is None:
                continue
            counts[table.name] = (
                await session.scalar(
                    select(func.count()).select_from(table).where(column == user_id)
                )
                or 0
            )
    return counts


async def delete_account(http: httpx.AsyncClient, token: str, body: Any) -> httpx.Response:
    return await http.request("DELETE", "/api/v1/me", json=body, headers=bearer(token))


@pytest.fixture
async def account(app: FastAPI, http: httpx.AsyncClient) -> dict[str, Any]:
    """A user with data in every table: meals, a day status, settings, two sign-ins."""
    claude = await register_client(http, name="Claude")
    claude_tokens = await sign_in(http, claude, email="nina@example.com")
    app_client = await register_client(http, redirect_uri="healthyeating://oauth/callback")
    tokens = await sign_in(http, app_client, email="nina@example.com")
    headers = bearer(tokens["access_token"])
    await http.patch("/api/v1/me/settings", json={"timezone": "Europe/Berlin"}, headers=headers)
    await http.put("/api/v1/days/2026-10-18/status", json={"status": "complete"}, headers=headers)
    async with mcp_client(app, claude_tokens["access_token"]) as mcp:
        await mcp.call_tool("log_meal", MEAL)
    me = (await http.get("/api/v1/me", headers=headers)).json()
    return {
        "user_id": uuid.UUID(me["id"]),
        "token": tokens["access_token"],
        "claude": claude,
        "claude_tokens": claude_tokens,
    }


async def test_deleting_the_account_removes_everything(
    app: FastAPI, http: httpx.AsyncClient, account: dict[str, Any]
) -> None:
    before = await rows_of(app, account["user_id"])
    assert all(
        before[name] > 0 for name in ("users", "identities", "meals", "day_statuses", "grants")
    )
    other_client = await register_client(http)
    other = await sign_in(http, other_client, email="omar@example.com")

    response = await delete_account(http, account["token"], CONFIRM)

    assert response.status_code == 204
    assert set((await rows_of(app, account["user_id"])).values()) == {0}
    assert (await http.get("/api/v1/me", headers=bearer(account["token"]))).status_code == 401
    claude_access = account["claude_tokens"]["access_token"]
    assert (await http.get("/api/v1/me", headers=bearer(claude_access))).status_code == 401
    claude_refresh = await refresh(
        http, account["claude"], account["claude_tokens"]["refresh_token"]
    )
    assert claude_refresh.status_code != 200
    # Other users are untouched.
    assert (await http.get("/api/v1/me", headers=bearer(other["access_token"]))).status_code == 200


async def test_signing_in_again_creates_a_new_empty_account(
    app: FastAPI, http: httpx.AsyncClient, account: dict[str, Any]
) -> None:
    await delete_account(http, account["token"], CONFIRM)
    client = await register_client(http)
    tokens = await sign_in(http, client, email="nina@example.com")
    me = (await http.get("/api/v1/me", headers=bearer(tokens["access_token"]))).json()
    assert uuid.UUID(me["id"]) != account["user_id"]
    meals = await http.get("/api/v1/meals", headers=bearer(tokens["access_token"]))
    assert meals.json()["meals"] == []


@pytest.mark.parametrize(
    "body", [None, {}, {"confirm": "yes"}, {"confirm": True}, CONFIRM | {"extra": 1}]
)
async def test_deletion_needs_explicit_confirmation(
    app: FastAPI, http: httpx.AsyncClient, account: dict[str, Any], body: Any
) -> None:
    response = await delete_account(http, account["token"], body)
    assert response.status_code == 422
    assert (await rows_of(app, account["user_id"]))["meals"] == 1


async def test_deletion_needs_a_token(http: httpx.AsyncClient) -> None:
    response = await http.request("DELETE", "/api/v1/me", json=CONFIRM)
    assert response.status_code == 401
