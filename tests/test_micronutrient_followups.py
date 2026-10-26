"""Vitamins and minerals in the weekly review, the MCP reading tools and imports (#25)."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account, add_meal


def days_ago(days: int, hour: int = 12) -> str:
    moment = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (moment - timedelta(days=days)).isoformat()


async def call(app: FastAPI, owner: Account, tool: str, arguments: dict[str, Any]) -> Any:
    async with mcp_client(app, owner.token) as mcp:
        result = await mcp.call_tool(tool, arguments)
    return result.structured_content


async def test_the_weekly_review_averages_fully_covered_days(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ari.week@example.com")
    # A past week, so no day of it is today.
    monday = date.today() - timedelta(days=date.today().weekday() + 7)
    tuesday, wednesday = monday + timedelta(days=1), monday + timedelta(days=2)
    await add_meal(app, owner, f"{monday}T12:00:00+00:00", micronutrients={"iron_mg": 6})
    await add_meal(app, owner, f"{tuesday}T12:00:00+00:00", micronutrients={"iron_mg": 10})
    # Only one of two meals has items: left out.
    await add_meal(app, owner, f"{wednesday}T09:00:00+00:00", micronutrients={"iron_mg": 1})
    await add_meal(app, owner, f"{wednesday}T13:00:00+00:00")

    response = await http.get(
        "/api/v1/weekly-review", params={"week": str(monday)}, headers=owner.headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["micronutrient_averages"] == {"iron_mg": {"average": 8.0, "days": 2}}


async def test_the_assistant_can_read_vitamins_and_minerals(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "bo.read@example.com")
    when = days_ago(1)
    await add_meal(app, owner, when, micronutrients={"iron_mg": 2.5, "zinc_mg": 1})
    await add_meal(app, owner, when)
    day = when[:10]

    summary = await call(app, owner, "get_daily_summary", {"first_day": day, "last_day": day})
    meals = await call(app, owner, "list_meals", {"first_day": day, "last_day": day})

    [totals] = summary["days"]
    assert totals["micronutrients"]["iron_mg"] == {"amount": 2.5, "meal_count": 1}
    assert sorted([meal["micronutrients"] for meal in meals["meals"]], key=lambda m: m is None) == [
        {"iron_mg": 2.5, "zinc_mg": 1},
        None,
    ]


async def test_imported_meals_can_carry_food_items(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "cy.import@example.com")
    entry = {
        "dish_name": "Rice with salmon",
        "eaten_at": "2026-09-05T13:00:00+00:00",
        "calories_kcal": 610,
        "protein_g": 38,
        "fat_g": 18,
        "carbohydrates_g": 70,
        "items": [{"name": "boiled rice", "grams": 150}, {"name": "salmon, baked", "grams": 120}],
    }

    result = await call(
        app, owner, "import_meals", {"meals": [entry], "imported_from": "Google Sheets"}
    )

    assert result["imported"] == 1
    response = await http.get(
        "/api/v1/meals", params={"from": "2026-09-05", "to": "2026-09-05"}, headers=owner.headers
    )
    [meal] = response.json()["meals"]
    assert meal["imported_from"] == "Google Sheets"
    assert [item["food_description"] for item in meal["items"]] == [
        "Rice, cooked, NFS",
        "Fish, salmon, baked or broiled",
    ]
    assert meal["micronutrients"]["iron_mg"] > 0
    assert (meal["calories_kcal"], meal["protein_g"]) == (610, 38)
