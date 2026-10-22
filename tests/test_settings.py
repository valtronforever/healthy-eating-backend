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
    assert profile["calm_mode"] is False
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


async def stored_targets(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    settings = (await http.get("/api/v1/me/settings", headers=bearer(token))).json()
    targets: dict[str, Any] = settings["targets"]
    return targets


async def test_set_targets_stores_ranges_and_keeps_the_others(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    await patch(http, token, {"targets": {"fat_g": {"min": 50, "max": 80}}})
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool(
            "set_targets",
            {"calories_kcal": {"min": 1800, "max": 2100}, "protein_g": {"min": 120}},
        )
        later = await mcp.call_tool("set_targets", {"carbohydrates_g": {"max": 250.5}})
    assert result.structured_content is not None
    assert result.structured_content["saved_to_healthy_eating"] is True
    assert result.structured_content["user_message"] == (
        "Saved targets to HealthyEating: 1800 to 2100 kcal, protein from 120 g."
    )
    assert later.structured_content is not None
    assert later.structured_content["user_message"] == (
        "Saved targets to HealthyEating: carbohydrates up to 250.5 g."
    )
    expected = {
        "calories_kcal": {"min": 1800, "max": 2100},
        "protein_g": {"min": 120, "max": None},
        "fat_g": {"min": 50, "max": 80},
        "carbohydrates_g": {"min": None, "max": 250.5},
    }
    assert later.structured_content["targets"] == expected
    assert await stored_targets(http, token) == expected


async def test_set_targets_clears_only_the_listed_targets(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    await patch(
        http,
        token,
        {"targets": {"fat_g": {"min": 50, "max": 80}, "protein_g": {"min": 100, "max": 150}}},
    )
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool(
            "set_targets", {"protein_g": {"min": 110, "max": 160}, "clear": ["fat_g"]}
        )
    assert result.structured_content is not None
    assert result.structured_content["user_message"] == (
        "Saved targets to HealthyEating: protein 110 to 160 g, no fat target."
    )
    assert await stored_targets(http, token) == NO_TARGETS | {"protein_g": {"min": 110, "max": 160}}


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"calories_kcal": {"min": 2000, "max": 2000}}, "never a single number"),
        ({"protein_g": {}}, "needs a lower bound, an upper bound or both"),
        ({"fat_g": {"min": 80, "max": 50}}, "cannot exceed the upper bound"),
        ({"fat_g": {"min": 50}, "clear": ["fat_g"]}, "both set and cleared"),
        ({}, "No target given"),
        # A valid target is not saved when another one is rejected.
        (
            {"protein_g": {"min": 120}, "carbohydrates_g": {"min": 200, "max": 200}},
            "never a single number",
        ),
    ],
)
async def test_set_targets_rejects_invalid_targets_and_saves_nothing(
    app: FastAPI, http: httpx.AsyncClient, token: str, arguments: dict[str, Any], message: str
) -> None:
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool("set_targets", arguments, raise_on_error=False)
    assert result.is_error
    assert "Targets not saved to HealthyEating" in str(result.content)
    assert message in str(result.content)
    assert await stored_targets(http, token) == NO_TARGETS


async def test_set_targets_rejects_negative_bounds(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool(
            "set_targets", {"protein_g": {"min": -5}}, raise_on_error=False
        )
    assert result.is_error
    assert await stored_targets(http, token) == NO_TARGETS


async def test_set_targets_in_calm_mode_leaves_calories_alone(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    calories = {"min": 1800, "max": 2200}
    await patch(http, token, {"calm_mode": True, "targets": {"calories_kcal": calories}})
    async with mcp_client(app, token) as mcp:
        profile = await mcp.call_tool("get_profile", {})
        set_calories = await mcp.call_tool(
            "set_targets",
            {"calories_kcal": {"min": 1900, "max": 2300}, "protein_g": {"min": 120}},
            raise_on_error=False,
        )
        clear_calories = await mcp.call_tool(
            "set_targets", {"clear": ["calories_kcal"]}, raise_on_error=False
        )
        macros = await mcp.call_tool("set_targets", {"protein_g": {"min": 120}})
    assert profile.structured_content is not None
    assert profile.structured_content["calm_mode"] is True
    for rejected in (set_calories, clear_calories):
        assert rejected.is_error
        assert "Calm mode is on" in str(rejected.content)
    assert macros.structured_content is not None
    assert macros.structured_content["user_message"] == (
        "Saved targets to HealthyEating: protein from 120 g."
    )
    assert "calories_kcal" not in macros.structured_content["targets"]
    assert await stored_targets(http, token) == NO_TARGETS | {
        "calories_kcal": calories,
        "protein_g": {"min": 120, "max": None},
    }


async def test_set_targets_changes_only_the_signed_in_user(
    app: FastAPI, http: httpx.AsyncClient, token: str
) -> None:
    client = await register_client(http, name="Claude")
    other = (await sign_in(http, client, email="jon@example.com"))["access_token"]
    async with mcp_client(app, other) as mcp:
        await mcp.call_tool("set_targets", {"protein_g": {"min": 120}})
    assert await stored_targets(http, token) == NO_TARGETS
    assert (await stored_targets(http, other))["protein_g"] == {"min": 120, "max": None}


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
