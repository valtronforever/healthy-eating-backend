from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account, add_meal

HISTORY = [
    {
        "dish_name": "Oatmeal with berries",
        "eaten_at": "2026-09-01T08:00:00",
        "calories_kcal": 380,
        "protein_g": 14,
        "fat_g": 9,
        "carbohydrates_g": 58,
    },
    {
        "dish_name": "Chicken salad",
        "eaten_at": "2026-09-01T13:00:00",
        "calories_kcal": 520,
        "protein_g": 40,
        "fat_g": 30,
        "carbohydrates_g": 18,
        "estimated_fields": ["fat_g"],
    },
    {
        "dish_name": "Day total",
        "eaten_at": "2026-09-02T12:00:00",
        "calories_kcal": 2100,
        "protein_g": 110,
        "fat_g": 80,
        "carbohydrates_g": 230,
        "estimated_fields": ["protein_g", "fat_g", "carbohydrates_g"],
        "confidence": "low",
    },
]


async def import_history(
    app: FastAPI,
    owner: Account,
    meals: list[dict[str, Any]],
    imported_from: str = "ChatGPT project",
) -> dict[str, Any]:
    async with mcp_client(app, owner.token) as mcp:
        result = await mcp.call_tool(
            "import_meals", {"meals": meals, "imported_from": imported_from}
        )
    content: dict[str, Any] = result.structured_content or {}
    return content


async def meals_between(
    http: httpx.AsyncClient, owner: Account, first: str, last: str
) -> list[dict[str, Any]]:
    response = await http.get(
        "/api/v1/meals", params={"from": first, "to": last}, headers=owner.headers
    )
    assert response.status_code == 200, response.text
    meals: list[dict[str, Any]] = response.json()["meals"]
    return meals


async def test_history_is_imported_unchanged_and_marked(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ida.import@example.com", "Europe/Berlin")

    result = await import_history(app, owner, HISTORY)

    assert result["saved_to_healthy_eating"] is True
    assert result["imported"] == 3
    assert (result["first_day"], result["last_day"]) == ("2026-09-01", "2026-09-02")
    assert result["user_message"] == (
        "Imported 3 meals from ChatGPT project into HealthyEating (2026-09-01 to 2026-09-02)."
    )
    meals = {
        m["dish_name"]: m for m in await meals_between(http, owner, "2026-09-01", "2026-09-02")
    }
    salad = meals["Chicken salad"]
    assert (salad["calories_kcal"], salad["protein_g"], salad["fat_g"]) == (520, 40, 30)
    assert salad["source"] == "from_conversation"
    assert salad["estimated_fields"] == ["fat_g"]
    assert salad["imported_from"] == salad["source_note"] == "ChatGPT project"
    # A time without an offset is read in the user's time zone, like every other meal.
    assert meals["Oatmeal with berries"]["eaten_at"] == "2026-09-01T08:00:00+02:00"
    assert meals["Day total"]["confidence"] == "low"


async def test_importing_again_counts_nothing_twice(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "jon.import@example.com", "Europe/Berlin")
    await import_history(app, owner, HISTORY[:2])

    # A retry that overlaps the first part: only the new day is added.
    result = await import_history(app, owner, HISTORY)

    assert result["imported"] == 1
    assert result["skipped_duplicates"] == 2
    assert result["user_message"] == (
        "Imported 1 meal from ChatGPT project into HealthyEating (2026-09-02); "
        "2 entries already there."
    )
    assert len(await meals_between(http, owner, "2026-09-01", "2026-09-02")) == 3


async def test_days_with_meals_logged_live_are_left_as_they_were(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "kai.import@example.com", "Europe/Berlin")
    await add_meal(app, owner, "2026-09-02T09:00:00+00:00", 120, dish_name="Porridge")

    result = await import_history(app, owner, HISTORY)

    assert result["imported"] == 2
    assert result["days_already_logged"] == ["2026-09-02"]
    assert result["user_message"].endswith(
        "; 1 day that already had meals logged left as they were."
    )
    assert [
        m["dish_name"] for m in await meals_between(http, owner, "2026-09-02", "2026-09-02")
    ] == ["Porridge"]


async def test_incomplete_and_future_entries_are_not_imported(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "lea.import@example.com")
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    entries = [
        {"dish_name": "Soup", "eaten_at": "2026-09-03T12:00:00+00:00", "calories_kcal": 300},
        {
            "dish_name": "Rice bowl",
            "eaten_at": "2026-09-03T19:00:00+00:00",
            "protein_g": 20,
            "fat_g": 10,
            "carbohydrates_g": 70,
        },
        {**HISTORY[0], "eaten_at": tomorrow},
    ]

    result = await import_history(app, owner, entries)

    assert result["imported"] == 1
    assert [(r["index"], r["reason"]) for r in result["rejected"]] == [
        (0, "missing values: protein_g, fat_g, carbohydrates_g"),
        (2, "eaten_at is in the future"),
    ]
    [bowl] = await meals_between(http, owner, "2026-09-03", "2026-09-03")
    # Calories derived from the macros are marked as estimated.
    assert bowl["calories_kcal"] == 450
    assert "calories_kcal" in bowl["estimated_fields"]


async def test_nothing_new_is_reported_as_not_saved(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "max.import@example.com")
    await import_history(app, owner, HISTORY[:1])

    result = await import_history(app, owner, HISTORY[:1])

    assert result["saved_to_healthy_eating"] is False
    assert result["user_message"] == (
        "Nothing new was imported into HealthyEating; 1 entry already there."
    )


async def test_imported_meals_are_not_usual_meals_and_are_exported(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "nia.import@example.com")
    recent = datetime.now(UTC) - timedelta(days=3)
    entries = [
        {**HISTORY[2], "eaten_at": (recent - timedelta(days=day)).isoformat()} for day in range(3)
    ]

    await import_history(app, owner, entries, imported_from="Google Sheets")

    usual = await http.get("/api/v1/usual-meals", headers=owner.headers)
    assert usual.json()["meals"] == []
    export = await http.get("/api/v1/export", params={"format": "json"}, headers=owner.headers)
    assert {meal["imported_from"] for meal in export.json()["meals"]} == {"Google Sheets"}


async def test_each_user_imports_only_into_their_own_account(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    one = await account(http, "ola.import@example.com")
    two = await account(http, "pia.import@example.com")
    await import_history(app, one, HISTORY)

    # The same history is new for another user.
    result = await import_history(app, two, HISTORY)

    assert result["imported"] == 3
    assert len(await meals_between(http, one, "2026-09-01", "2026-09-02")) == 3
    assert len(await meals_between(http, two, "2026-09-01", "2026-09-02")) == 3
