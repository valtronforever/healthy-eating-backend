from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, register_client, sign_in

NO_TARGETS = {"calories_kcal": None, "protein_g": None, "fat_g": None, "carbohydrates_g": None}
MEAL: dict[str, Any] = {
    "dish_name": "Porridge",
    "source": "from_conversation",
    "calories_kcal": 350,
    "protein_g": 12,
    "fat_g": 8,
    "carbohydrates_g": 55,
}


@pytest.fixture
async def token(http: httpx.AsyncClient) -> str:
    client = await register_client(http, redirect_uri="healthyeating://oauth/callback")
    tokens = await sign_in(http, client, email="ines@example.com")
    access_token: str = tokens["access_token"]
    return access_token


async def patch(http: httpx.AsyncClient, token: str, body: dict[str, Any]) -> httpx.Response:
    return await http.patch("/api/v1/me/settings", json=body, headers=bearer(token))


async def test_new_user_has_default_settings(http: httpx.AsyncClient, token: str) -> None:
    response = await http.get("/api/v1/me/settings", headers=bearer(token))
    assert response.status_code == 200
    assert response.json() == {
        "timezone": None,
        "energy_unit": "kcal",
        "calm_mode": False,
        "targets": NO_TARGETS,
    }


async def test_settings_require_a_token(http: httpx.AsyncClient) -> None:
    assert (await http.get("/api/v1/me/settings")).status_code == 401
    assert (await http.patch("/api/v1/me/settings", json={})).status_code == 401


async def test_patch_changes_only_the_given_settings(http: httpx.AsyncClient, token: str) -> None:
    response = await patch(
        http,
        token,
        {
            "timezone": "Europe/Berlin",
            "energy_unit": "kj",
            "targets": {"calories_kcal": {"min": 1800, "max": 2200}, "protein_g": {"min": 90}},
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "timezone": "Europe/Berlin",
        "energy_unit": "kj",
        "calm_mode": False,
        "targets": NO_TARGETS
        | {
            "calories_kcal": {"min": 1800, "max": 2200},
            "protein_g": {"min": 90, "max": None},
        },
    }

    response = await patch(http, token, {"calm_mode": True, "targets": {"protein_g": None}})
    settings = response.json()
    assert settings["timezone"] == "Europe/Berlin"
    assert settings["energy_unit"] == "kj"
    assert settings["calm_mode"] is True
    assert settings["targets"]["calories_kcal"] == {"min": 1800, "max": 2200}
    assert settings["targets"]["protein_g"] is None

    response = await patch(http, token, {"timezone": None})
    assert response.json()["timezone"] is None
    stored = await http.get("/api/v1/me/settings", headers=bearer(token))
    assert stored.json() == response.json()


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Olympus_Mons"},
        {"timezone": ""},
        {"energy_unit": "cal"},
        {"energy_unit": None},
        {"calm_mode": None},
        {"targets": {"fat_g": {"min": 90, "max": 60}}},
        {"targets": {"fat_g": {"min": -1}}},
        {"targets": {"fat_g": {}}},
    ],
)
async def test_invalid_settings_are_rejected(
    http: httpx.AsyncClient, token: str, body: dict[str, Any]
) -> None:
    response = await patch(http, token, body)
    assert response.status_code == 422
    stored = await http.get("/api/v1/me/settings", headers=bearer(token))
    assert stored.json()["timezone"] is None
    assert stored.json()["targets"] == NO_TARGETS


async def test_settings_belong_to_one_user(http: httpx.AsyncClient, token: str) -> None:
    other_client = await register_client(http)
    other = (await sign_in(http, other_client, email="jon@example.com"))["access_token"]
    await patch(http, token, {"timezone": "Asia/Tokyo"})
    response = await http.get("/api/v1/me/settings", headers=bearer(other))
    assert response.json()["timezone"] is None


# MCP


async def test_get_profile_returns_time_zone_and_targets(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    await patch(
        http,
        token,
        {"timezone": "Europe/Berlin", "targets": {"calories_kcal": {"min": 1800, "max": 2200}}},
    )
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool("get_profile", {})
    assert result.structured_content is not None
    profile = result.structured_content
    assert profile["timezone"] == "Europe/Berlin"
    assert profile["energy_unit"] == "kcal"
    assert profile["targets"] == NO_TARGETS | {"calories_kcal": {"min": 1800, "max": 2200}}


async def test_set_timezone_stores_the_time_zone(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool("set_timezone", {"timezone": "America/New_York"})
        invalid = await mcp.call_tool("set_timezone", {"timezone": "Eastern"}, raise_on_error=False)
    assert result.structured_content is not None
    assert result.structured_content["saved_to_healthy_eating"] is True
    assert invalid.is_error
    assert "Time zone not saved to HealthyEating" in str(invalid.content)
    settings = (await http.get("/api/v1/me/settings", headers=bearer(token))).json()
    assert settings["timezone"] == "America/New_York"


async def stored_meal(app: FastAPI) -> Meal:
    db: Database = app.state.db
    async with db.session() as session:
        meal: Meal | None = await session.scalar(select(Meal))
    assert meal is not None
    return meal


async def test_log_meal_reads_a_time_without_offset_in_the_users_time_zone(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    await patch(http, token, {"timezone": "Europe/Berlin"})
    async with mcp_client(app, token) as mcp:
        await mcp.call_tool("log_meal", MEAL | {"eaten_at": "2026-10-17T19:00:00"})
    meal = await stored_meal(app)
    assert meal.eaten_at == datetime(2026, 10, 17, 17, 0, tzinfo=UTC)
    assert meal.utc_offset_minutes == 120


async def test_log_meal_records_the_time_zone_offset_for_meals_eaten_now(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    await patch(http, token, {"timezone": "Asia/Kolkata"})
    async with mcp_client(app, token) as mcp:
        await mcp.call_tool("log_meal", MEAL)
    meal = await stored_meal(app)
    assert meal.utc_offset_minutes == 330
    assert abs((utcnow() - meal.eaten_at).total_seconds()) < 60
