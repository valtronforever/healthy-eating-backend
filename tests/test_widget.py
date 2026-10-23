from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from tests.oauth_helpers import bearer
from tests.test_meals_api import Account, account, add_meal


async def widget_token(http: httpx.AsyncClient, owner: Account) -> str:
    response = await http.post("/api/v1/widget-token", headers=owner.headers)
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


async def test_widget_reads_todays_totals_with_its_own_token(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "wen@example.com")
    await http.patch(
        "/api/v1/me/settings",
        json={"targets": {"calories_kcal": {"min": 1900, "max": 2200}}, "calm_mode": True},
        headers=owner.headers,
    )
    await add_meal(app, owner, datetime.now(UTC).isoformat())
    token = await widget_token(http, owner)

    response = await http.get("/api/v1/widget/today", headers=bearer(token))

    assert response.status_code == 200, response.text
    today = response.json()
    assert today["meal_count"] == 1
    assert today["calories_kcal"] == 200
    assert today["protein_g"] == 6
    assert today["targets"]["calories_kcal"] == {"min": 1900, "max": 2200}
    assert today["targets"]["protein_g"] is None
    assert today["calm_mode"] is True
    assert today["energy_unit"] == "kcal"
    assert today["last_meal_saved_at"] is not None


async def test_widget_and_app_tokens_are_not_interchangeable(http: httpx.AsyncClient) -> None:
    owner = await account(http, "xia@example.com")
    token = await widget_token(http, owner)

    assert (await http.get("/api/v1/me", headers=bearer(token))).status_code == 401
    assert (await http.post("/api/v1/widget-token", headers=bearer(token))).status_code == 401
    app_token = await http.get("/api/v1/widget/today", headers=owner.headers)
    assert app_token.status_code == 401
    assert (await http.get("/api/v1/widget/today")).status_code == 401


async def test_a_new_widget_token_replaces_the_old_one(http: httpx.AsyncClient) -> None:
    owner = await account(http, "yan@example.com")
    first = await widget_token(http, owner)
    second = await widget_token(http, owner)

    assert (await http.get("/api/v1/widget/today", headers=bearer(first))).status_code == 401
    assert (await http.get("/api/v1/widget/today", headers=bearer(second))).status_code == 200


async def test_revoking_the_app_sign_in_ends_the_widget_token(http: httpx.AsyncClient) -> None:
    owner = await account(http, "zed@example.com")
    token = await widget_token(http, owner)
    connections = (await http.get("/api/v1/connections", headers=owner.headers)).json()
    grants = [grant for client in connections["connections"] for grant in client["grants"]]
    [grant_id] = [grant["grant_id"] for grant in grants if grant["current"]]

    revoked = await http.delete(f"/api/v1/connections/{grant_id}", headers=owner.headers)

    assert revoked.status_code == 200, revoked.text
    assert (await http.get("/api/v1/widget/today", headers=bearer(token))).status_code == 401


async def test_each_widget_token_reads_only_its_own_account(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    one = await account(http, "amy@example.com")
    two = await account(http, "bob@example.com")
    await add_meal(app, one, datetime.now(UTC).isoformat())
    token = await widget_token(http, two)

    today = (await http.get("/api/v1/widget/today", headers=bearer(token))).json()

    assert today["meal_count"] == 0
