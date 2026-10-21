"""Fiber, sugar and sodium: optional on every meal, through MCP, the REST API and the export."""

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from sqlalchemy import select

from healthy_eating.db import Database
from healthy_eating.db.models import Meal
from healthy_eating.services.meals import MealEstimate, evaluate_meal
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import register_client, sign_in
from tests.test_meals_api import account, add_meal

MEAL: dict[str, Any] = {
    "dish_name": "Lentil soup",
    "source": "estimated_by_assistant",
    "calories_kcal": 380,
    "protein_g": 22,
    "fat_g": 8,
    "carbohydrates_g": 54,
}


@pytest.fixture
async def token(http: httpx.AsyncClient) -> str:
    client = await register_client(http, name="Claude")
    tokens = await sign_in(http, client, email="ola@example.com")
    access: str = tokens["access_token"]
    return access


@pytest.fixture
def mcp(app: FastAPI, token: str) -> Client[StreamableHttpTransport]:
    return mcp_client(app, token)


async def log(mcp: Client[StreamableHttpTransport], **arguments: Any) -> dict[str, Any]:
    async with mcp:
        result = await mcp.call_tool("log_meal", arguments)
    assert result.structured_content is not None
    content: dict[str, Any] = result.structured_content
    return content


async def stored(app: FastAPI) -> Meal:
    db: Database = app.state.db
    async with db.session() as session:
        meal: Meal | None = await session.scalar(select(Meal))
    assert meal is not None
    return meal


async def test_assistant_estimates_count_as_estimated(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await log(mcp, **MEAL, fiber_g=11, sugar_g=6, sodium_mg=900)

    meal = await stored(app)
    assert (meal.fiber_g, meal.sugar_g, meal.sodium_mg) == (11, 6, 900)
    assert set(meal.estimated_fields) == {
        "calories_kcal",
        "protein_g",
        "fat_g",
        "carbohydrates_g",
        "fiber_g",
        "sugar_g",
        "sodium_mg",
    }
    assert content["meal"]["fiber_g"] == 11
    assert content["meal"]["sodium_mg"] == 900


async def test_completed_calculation_marks_only_the_added_values(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    # The user's label had sodium; the assistant added fiber.
    content = await log(
        mcp,
        **(MEAL | {"source": "from_conversation"}),
        sodium_mg=750,
        fiber_g=9,
        estimated_fields=["fiber_g"],
    )

    meal = await stored(app)
    assert meal.estimated_fields == ["fiber_g"]
    assert meal.sugar_g is None
    # The one-line message names only the values it shows.
    assert "estimated" not in content["user_message"]


async def test_meals_without_them_keep_null(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await log(mcp, **MEAL)

    meal = await stored(app)
    assert (meal.fiber_g, meal.sugar_g, meal.sodium_mg) == (None, None, None)
    assert "fiber_g" not in meal.estimated_fields
    assert content["meal"]["sugar_g"] is None


def test_sugar_above_carbohydrates_is_a_warning() -> None:
    evaluation = evaluate_meal(MealEstimate(**MEAL, sugar_g=60))

    assert evaluation.complete
    assert evaluation.warnings == ["Sugar (60 g) is higher than the carbohydrates (54 g)."]


async def test_impossible_values_are_rejected(mcp: Client[StreamableHttpTransport]) -> None:
    async with mcp:
        with pytest.raises(ToolError):
            await mcp.call_tool("log_meal", MEAL | {"sodium_mg": -5})


async def test_app_edits_set_and_clear_them(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "pia@example.com", "Europe/Berlin")
    meal_id = await add_meal(
        app,
        owner,
        datetime.now(UTC).isoformat(),
        sugar_g=4.0,
        estimated_fields=["calories_kcal", "protein_g", "fat_g", "carbohydrates_g", "sugar_g"],
    )

    response = await http.patch(
        f"/api/v1/meals/{meal_id}", json={"fiber_g": 7, "sugar_g": None}, headers=owner.headers
    )

    assert response.status_code == 200, response.text
    meal = response.json()["meal"]
    assert (meal["fiber_g"], meal["sugar_g"]) == (7, None)
    # The person's own fiber value is not estimated; the cleared sugar is gone from the marks.
    assert "fiber_g" not in meal["estimated_fields"]
    assert "sugar_g" not in meal["estimated_fields"]
    # The four required values still cannot be cleared.
    cleared = await http.patch(
        f"/api/v1/meals/{meal_id}", json={"protein_g": None}, headers=owner.headers
    )
    assert cleared.status_code == 422


async def test_days_and_trends_say_how_many_meals_and_days_have_them(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "quinn@example.com", "Europe/Berlin")
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    two_days_ago = now - timedelta(days=2)
    await add_meal(app, owner, yesterday.isoformat(), fiber_g=5.0, sodium_mg=400.0)
    await add_meal(app, owner, (yesterday - timedelta(hours=1)).isoformat(), fiber_g=3.0)
    await add_meal(app, owner, (yesterday - timedelta(hours=2)).isoformat())
    await add_meal(app, owner, two_days_ago.isoformat(), fiber_g=10.0)
    day = yesterday.astimezone().date().isoformat()

    days = (
        await http.get("/api/v1/days", params={"from": day, "to": day}, headers=owner.headers)
    ).json()["days"]

    assert days[0]["fiber_g"] == {"amount": 8.0, "meal_count": 2}
    assert days[0]["sodium_mg"] == {"amount": 400.0, "meal_count": 1}
    assert days[0]["sugar_g"] is None

    trends = (await http.get("/api/v1/trends", params={"window": 7}, headers=owner.headers)).json()
    assert trends["current"]["fiber_g"] == {"average": 9.0, "days": 2}
    assert trends["current"]["sodium_mg"] == {"average": 400.0, "days": 1}
    assert trends["current"]["sugar_g"] is None


async def test_export_contains_them(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "rae@example.com", "Europe/Berlin")
    await add_meal(app, owner, datetime.now(UTC).isoformat(), fiber_g=6.0)

    data = (await http.get("/api/v1/export", headers=owner.headers)).json()
    [meal] = data["meals"]
    assert (meal["fiber_g"], meal["sugar_g"], meal["sodium_mg"]) == (6.0, None, None)

    response = await http.get("/api/v1/export", params={"format": "csv"}, headers=owner.headers)
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    [row] = list(csv.DictReader(io.StringIO(archive.read("meals.csv").decode())))
    assert (row["fiber_g"], row["sugar_g"]) == ("6.0", "")


async def test_weekly_review_share_counts_the_values_meals_have(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "sol@example.com", "Europe/Berlin")
    monday = datetime(2026, 3, 2, 12, tzinfo=UTC)
    # Five estimated values (four plus fiber), and four of the person's own.
    await add_meal(
        app,
        owner,
        monday.isoformat(),
        fiber_g=8.0,
        estimated_fields=["calories_kcal", "protein_g", "fat_g", "carbohydrates_g", "fiber_g"],
    )
    await add_meal(app, owner, (monday + timedelta(hours=3)).isoformat(), estimated_fields=[])

    review = (
        await http.get(
            "/api/v1/weekly-review", params={"week": "2026-03-02"}, headers=owner.headers
        )
    ).json()

    assert review["estimated_values_share"] == round(5 / 9, 3)


async def test_estimated_names_are_listed_in_the_contract(http: httpx.AsyncClient) -> None:
    schema = (await http.get("/api/v1/openapi.json")).json()
    fields = schema["components"]["schemas"]["MealResponse"]["properties"]["estimated_fields"]
    assert "fiber_g" in fields["items"]["enum"]
