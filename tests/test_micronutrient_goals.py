"""The user's own ranges for vitamins and minerals (#35), from the app and from the chat."""

import csv
import io
import zipfile
from typing import Any

import httpx
from fastapi import FastAPI

from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS
from tests.mcp_helpers import mcp_client
from tests.test_meal_items import call
from tests.test_meals_api import account
from tests.test_reference_group import settings


async def test_a_goal_replaces_the_reference_of_its_nutrient(http: httpx.AsyncClient) -> None:
    owner = await account(http, "amy.goals@example.com")
    await settings(http, owner, reference_group="women_31_50")

    body = await settings(http, owner, micronutrient_goals={"vitamin_d_ug": {"min": 50}})

    assert body["micronutrient_goals"] == {"vitamin_d_ug": {"min": 50, "max": None}}
    ranges = body["micronutrient_ranges"]
    assert ranges["vitamin_d_ug"] == {"min": 50, "max": None, "source": "personal"}
    assert ranges["iron_mg"] == {"min": 18, "max": 45, "source": "reference"}
    assert len(ranges) == len(MICRONUTRIENTS)
    # The reference itself stays as it is.
    assert body["micronutrient_references"]["vitamin_d_ug"] == {"min": 15, "max": 100}


async def test_a_goal_applies_without_a_life_stage_and_null_removes_it(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "ben.goals@example.com")

    body = await settings(
        http, owner, micronutrient_goals={"iron_mg": {"min": 10, "max": 20}, "zinc_mg": {"max": 30}}
    )
    assert body["micronutrient_ranges"] == {
        "iron_mg": {"min": 10, "max": 20, "source": "personal"},
        "zinc_mg": {"min": None, "max": 30, "source": "personal"},
    }

    # Only the nutrients listed change; other settings stay.
    body = await settings(http, owner, micronutrient_goals={"iron_mg": None}, energy_unit="kj")
    assert body["micronutrient_goals"] == {"zinc_mg": {"min": None, "max": 30}}
    body = await settings(http, owner, reference_group="men_19_30")
    assert body["micronutrient_ranges"]["iron_mg"]["source"] == "reference"
    assert body["micronutrient_ranges"]["zinc_mg"]["source"] == "personal"


async def test_invalid_goals_are_refused(http: httpx.AsyncClient) -> None:
    owner = await account(http, "cat.goals@example.com")
    invalid: list[dict[str, Any] | None] = [
        {"iron_mg": {"min": 20, "max": 10}},
        {"iron_mg": {"min": -1}},
        {"iron_mg": {}},
        {"omega_3_g": {"min": 1}},
        None,
    ]
    for goals in invalid:
        response = await http.patch(
            "/api/v1/me/settings", json={"micronutrient_goals": goals}, headers=owner.headers
        )
        assert response.status_code == 422, goals

    assert (await settings(http, owner))["micronutrient_goals"] == {}


async def test_each_user_has_their_own_goals(http: httpx.AsyncClient) -> None:
    one = await account(http, "dee.goals@example.com")
    two = await account(http, "eli.goals@example.com")

    await settings(http, one, micronutrient_goals={"calcium_mg": {"min": 1200}})

    assert (await settings(http, two))["micronutrient_goals"] == {}


async def test_goals_from_the_chat(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "fay.goals@example.com")
    await settings(http, owner, reference_group="women_31_50")

    saved = await call(
        app, owner, "set_targets", {"vitamins_and_minerals": {"vitamin_d_ug": {"min": 50}}}
    )

    assert saved["saved_to_healthy_eating"] is True
    assert saved["user_message"] == "Saved goals to HealthyEating: vitamin D from 50 µg."
    assert saved["vitamin_and_mineral_goals"] == {"vitamin_d_ug": {"min": 50, "max": None}}
    profile = await call(app, owner, "get_profile", {})
    assert profile["reference_group"] == "women_31_50"
    ranges = profile["vitamin_and_mineral_ranges"]
    assert ranges["vitamin_d_ug"] == {"min": 50, "max": None, "source": "personal"}
    assert ranges["iron_mg"] == {"min": 18, "max": 45, "source": "reference"}

    both = await call(
        app,
        owner,
        "set_targets",
        {
            "protein_g": {"min": 120},
            "vitamins_and_minerals": {"vitamin_b12_ug": {"min": 2.4, "max": 4.75}},
            "clear": ["vitamin_d_ug"],
        },
    )
    assert both["user_message"] == (
        "Saved targets to HealthyEating: protein from 120 g, no own vitamin D goal, "
        "vitamin B12 2.4 to 4.75 µg."
    )
    assert both["vitamin_and_mineral_goals"] == {"vitamin_b12_ug": {"min": 2.4, "max": 4.75}}
    rest = await settings(http, owner)
    assert rest["micronutrient_ranges"]["vitamin_d_ug"]["source"] == "reference"


async def test_a_single_number_goal_from_the_chat_is_refused(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "gil.goals@example.com")
    await settings(http, owner, calm_mode=True)

    async with mcp_client(app, owner.token) as mcp:
        result = await mcp.call_tool(
            "set_targets",
            {"vitamins_and_minerals": {"iron_mg": {"min": 18, "max": 18}}},
            raise_on_error=False,
        )
        # Calm mode hides calories only: goals for vitamins and minerals can be set.
        saved = await mcp.call_tool(
            "set_targets", {"vitamins_and_minerals": {"iron_mg": {"min": 18, "max": 30}}}
        )

    assert result.is_error
    assert "never a single number" in str(result.content)
    assert saved.structured_content is not None
    assert saved.structured_content["user_message"] == (
        "Saved goals to HealthyEating: iron 18 to 30 mg."
    )


async def test_goals_are_exported(http: httpx.AsyncClient) -> None:
    owner = await account(http, "hal.goals@example.com")
    await settings(http, owner, micronutrient_goals={"selenium_ug": {"min": 55, "max": 200}})

    exported = await http.get("/api/v1/export", params={"format": "json"}, headers=owner.headers)
    archive = await http.get("/api/v1/export", params={"format": "csv"}, headers=owner.headers)

    assert exported.json()["settings"]["micronutrient_goals"] == {
        "selenium_ug": {"min": 55, "max": 200}
    }
    rows: list[dict[str, Any]] = list(
        csv.DictReader(
            io.StringIO(zipfile.ZipFile(io.BytesIO(archive.content)).read("settings.csv").decode())
        )
    )
    values = {row["setting"]: row["value"] for row in rows}
    assert (values["goal_selenium_ug_min"], values["goal_selenium_ug_max"]) == ("55.0", "200.0")
