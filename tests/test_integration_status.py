from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from healthy_eating.db.base import utcnow
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, register_client, sign_in
from tests.test_meals_api import account, add_meal

MEAL: dict[str, Any] = {
    "dish_name": "Porridge",
    "source": "from_conversation",
    "calories_kcal": 350,
    "protein_g": 12,
    "fat_g": 8,
    "carbohydrates_g": 55,
}


async def tokens_for(http: httpx.AsyncClient, client_name: str, email: str) -> str:
    client = await register_client(http, name=client_name)
    token: str = (await sign_in(http, client, email=email))["access_token"]
    return token


async def log_meal(app: FastAPI, token: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool("log_meal", arguments)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def test_meals_record_the_assistant_that_saved_them(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    claude = await tokens_for(http, "Claude", "nina@example.com")
    chatgpt = await tokens_for(http, "ChatGPT", "nina@example.com")
    saved = await log_meal(app, claude, MEAL)
    meal_id = saved["meal"]["meal_id"]

    meal = (await http.get(f"/api/v1/meals/{meal_id}", headers=bearer(claude))).json()
    assert meal["client_name"] == "Claude"
    listed = (await http.get("/api/v1/meals", headers=bearer(claude))).json()
    assert listed["meals"][0]["client_name"] == "Claude"

    # A correction from another assistant, or from the app, keeps the original assistant.
    await log_meal(app, chatgpt, MEAL | {"calories_kcal": 400, "replaces_meal_id": meal_id})
    edited = await http.patch(f"/api/v1/meals/{meal_id}", json={"fat_g": 9}, headers=bearer(claude))
    assert edited.json()["meal"]["client_name"] == "Claude"
    assert edited.json()["meal"]["calories_kcal"] == 400


async def test_integration_status_reports_the_last_meal_without_contents(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    token = await tokens_for(http, "Claude", "nina@example.com")
    await log_meal(app, token, MEAL)

    response = await http.get("/api/v1/integration-status", headers=bearer(token))

    assert response.status_code == 200
    status = response.json()
    assert set(status) == {"last_meal_saved_at", "last_client_name", "meals_today"}
    assert status["last_client_name"] == "Claude"
    assert status["meals_today"] == 1
    saved_at = datetime.fromisoformat(status["last_meal_saved_at"])
    assert abs((utcnow() - saved_at).total_seconds()) < 60


async def test_meals_today_uses_the_users_local_day(app: FastAPI, http: httpx.AsyncClient) -> None:
    lee = await account(http, "lee@example.com", "Pacific/Kiritimati")
    await add_meal(app, lee, (utcnow() - timedelta(days=2)).isoformat())
    await add_meal(app, lee, utcnow().isoformat())

    status = (await http.get("/api/v1/integration-status", headers=lee.headers)).json()

    assert status["meals_today"] == 1
    # Meals added outside MCP have no assistant.
    assert status["last_client_name"] is None
    assert status["last_meal_saved_at"] is not None


async def test_integration_status_for_a_new_user_and_isolation(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    token = await tokens_for(http, "Claude", "nina@example.com")
    await log_meal(app, token, MEAL)
    other = await account(http, "omar@example.com")

    status = (await http.get("/api/v1/integration-status", headers=other.headers)).json()

    assert status == {"last_meal_saved_at": None, "last_client_name": None, "meals_today": 0}
    assert (await http.get("/api/v1/integration-status")).status_code == 401
