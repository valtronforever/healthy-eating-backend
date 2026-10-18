import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy import delete, select

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal, User
from healthy_eating.services.meals import (
    COMPLETE_DETAILS_QUESTION,
    INGREDIENTS_QUESTION,
    PORTION_QUESTION,
    MealEstimate,
    energy_from_macros,
    evaluate_meal,
)
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import register_client, sign_in

COMPLETE_MEAL: dict[str, Any] = {
    "dish_name": "Chicken Caesar salad",
    "source": "estimated_by_assistant",
    "confidence": "medium",
    "portion_grams": 350,
    "calories_kcal": 480,
    "protein_g": 35,
    "fat_g": 30,
    "carbohydrates_g": 18,
}


# Service


def test_complete_estimate_can_be_saved() -> None:
    evaluation = evaluate_meal(MealEstimate(**COMPLETE_MEAL))
    assert evaluation.complete
    assert evaluation.missing_fields == []
    assert evaluation.warnings == []


def test_assistant_estimate_needs_all_values() -> None:
    evaluation = evaluate_meal(MealEstimate(dish_name="Pasta", calories_kcal=600))
    assert not evaluation.complete
    assert evaluation.missing_fields == ["protein_g", "fat_g", "carbohydrates_g"]
    assert evaluation.questions_for_user == [PORTION_QUESTION, INGREDIENTS_QUESTION]


def test_existing_calculation_with_calories_only_needs_completion() -> None:
    evaluation = evaluate_meal(
        MealEstimate(dish_name="Pasta", source="from_conversation", calories_kcal=600)
    )
    assert not evaluation.complete
    assert evaluation.missing_fields == ["protein_g", "fat_g", "carbohydrates_g"]
    assert evaluation.questions_for_user == [COMPLETE_DETAILS_QUESTION]


def test_existing_calculation_completed_by_assistant_keeps_provenance() -> None:
    evaluation = evaluate_meal(
        MealEstimate(
            dish_name="Pasta",
            source="from_conversation",
            calories_kcal=600,
            protein_g=20,
            fat_g=18,
            carbohydrates_g=85,
            estimated_fields=("protein_g", "fat_g", "carbohydrates_g"),
        )
    )
    assert evaluation.complete
    assert evaluation.meal.calories_kcal == 600
    assert evaluation.meal.estimated_fields == ("protein_g", "fat_g", "carbohydrates_g")


def test_assistant_estimate_marks_all_values_estimated() -> None:
    evaluation = evaluate_meal(MealEstimate(**COMPLETE_MEAL))
    assert evaluation.meal.estimated_fields == (
        "calories_kcal",
        "protein_g",
        "fat_g",
        "carbohydrates_g",
    )


def test_existing_calculation_with_macros_only_derives_calories() -> None:
    evaluation = evaluate_meal(
        MealEstimate(
            dish_name="Oatmeal",
            source="from_conversation",
            protein_g=10,
            fat_g=5,
            carbohydrates_g=50,
        )
    )
    assert evaluation.complete
    assert evaluation.meal.calories_kcal == 285
    assert evaluation.meal.estimated_fields == ("calories_kcal",)
    assert evaluation.warnings == [
        "Calories were derived from the macronutrients (4/9/4 kcal per g)."
    ]


def test_open_questions_do_not_block_a_complete_estimate() -> None:
    questions = ["Was the dressing on the side?"]
    evaluation = evaluate_meal(MealEstimate(**COMPLETE_MEAL, open_questions=questions))
    assert evaluation.complete
    assert evaluation.questions_for_user == questions


def test_complete_correction_asks_no_more_questions() -> None:
    evaluation = evaluate_meal(
        MealEstimate(
            **COMPLETE_MEAL,
            replaces_meal_id=uuid.uuid4(),
            open_questions=["Was it mostly broth?"],
        )
    )
    assert evaluation.complete
    assert evaluation.questions_for_user == []


def test_open_questions_are_passed_through_when_values_are_missing() -> None:
    questions = ["How many grams of meat were in your portion?"]
    evaluation = evaluate_meal(MealEstimate(dish_name="Stew", open_questions=questions))
    assert not evaluation.complete
    assert evaluation.questions_for_user == questions


def test_energy_mismatch_is_reported_as_warning() -> None:
    meal = COMPLETE_MEAL | {"calories_kcal": 200}
    evaluation = evaluate_meal(MealEstimate(**meal))
    assert evaluation.complete
    assert len(evaluation.warnings) == 1


def test_energy_from_macros_uses_atwater_factors() -> None:
    assert energy_from_macros(protein_g=10, fat_g=10, carbohydrates_g=10) == 170


# MCP tool


@pytest.fixture
async def access_token(http: httpx.AsyncClient) -> str:
    client = await register_client(http, name="Claude")
    tokens = await sign_in(http, client, email="henry@example.com")
    token: str = tokens["access_token"]
    return token


@pytest.fixture
def mcp(app: FastAPI, access_token: str) -> Client[StreamableHttpTransport]:
    return mcp_client(app, access_token)


async def stored_meals(app: FastAPI) -> list[Meal]:
    db: Database = app.state.db
    async with db.session() as session:
        return list(await session.scalars(select(Meal).order_by(Meal.created_at)))


async def call_log_meal(
    mcp: Client[StreamableHttpTransport], arguments: dict[str, Any]
) -> dict[str, Any]:
    async with mcp:
        result = await mcp.call_tool("log_meal", arguments)
    assert result.structured_content is not None
    return result.structured_content


async def test_log_meal_saves_assistant_estimate(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await call_log_meal(mcp, COMPLETE_MEAL)

    assert content["status"] == "logged"
    assert content["saved_to_healthy_eating"] is True
    assert content["user_message"] == (
        "Saved to HealthyEating: Chicken Caesar salad, 480 kcal "
        "(protein 35 g, fat 30 g, carbohydrates 18 g)."
    )

    [meal] = await stored_meals(app)
    assert str(meal.id) == content["meal"]["meal_id"]
    assert meal.dish_name == "Chicken Caesar salad"
    assert meal.source == "estimated_by_assistant"
    assert (meal.calories_kcal, meal.protein_g, meal.fat_g, meal.carbohydrates_g) == (
        480,
        35,
        30,
        18,
    )
    assert meal.estimated_fields == ["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"]
    assert meal.utc_offset_minutes is None
    assert abs((utcnow() - meal.eaten_at).total_seconds()) < 60


async def test_log_meal_does_not_log_meal_contents(
    mcp: Client[StreamableHttpTransport], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await call_log_meal(mcp, COMPLETE_MEAL)
    # Ignore the test's own MCP client, which logs what it sends.
    server_records = [r for r in caplog.records if not r.name.startswith("mcp.client")]
    assert not [r for r in server_records if "Caesar" in r.getMessage()]


async def test_log_meal_asks_to_complete_existing_calculation(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await call_log_meal(
        mcp,
        {
            "dish_name": "Borscht with sour cream",
            "source": "from_conversation",
            "source_note": "ChatGPT project instructions",
            "calories_kcal": 412,
        },
    )

    assert content["status"] == "needs_more_info"
    assert content["missing_fields"] == ["protein_g", "fat_g", "carbohydrates_g"]
    assert content["questions_for_user"] == [COMPLETE_DETAILS_QUESTION]
    assert await stored_meals(app) == []


async def test_log_meal_saves_completed_existing_calculation(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await call_log_meal(
        mcp,
        {
            "dish_name": "Borscht with sour cream",
            "source": "from_conversation",
            "source_note": "Google Sheets log",
            "calories_kcal": 412,
            "protein_g": 14,
            "fat_g": 24,
            "carbohydrates_g": 36,
            "estimated_fields": ["protein_g", "fat_g", "carbohydrates_g"],
            "eaten_at": "2026-10-18T08:30:00+02:00",
        },
    )

    assert content["status"] == "logged"
    assert content["user_message"] == (
        "Saved to HealthyEating: Borscht with sour cream, 412 kcal "
        "(protein 14 g, fat 24 g, carbohydrates 36 g); "
        "protein, fat, carbohydrates estimated by the assistant."
    )
    [meal] = await stored_meals(app)
    assert meal.source == "from_conversation"
    assert meal.source_note == "Google Sheets log"
    assert meal.calories_kcal == 412
    assert meal.estimated_fields == ["protein_g", "fat_g", "carbohydrates_g"]
    assert meal.eaten_at == datetime(2026, 10, 18, 6, 30, tzinfo=UTC)
    assert meal.utc_offset_minutes == 120


async def test_log_meal_correction_updates_the_same_meal(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    first = await call_log_meal(mcp, COMPLETE_MEAL | {"eaten_at": "2026-10-18T13:00:00+02:00"})
    corrected = await call_log_meal(
        mcp,
        COMPLETE_MEAL | {"calories_kcal": 560, "replaces_meal_id": first["meal"]["meal_id"]},
    )

    assert corrected["user_message"].startswith("Updated in HealthyEating:")
    assert corrected["meal"]["meal_id"] == first["meal"]["meal_id"]
    [meal] = await stored_meals(app)
    assert meal.calories_kcal == 560
    # The correction did not give a time, so the original one is kept.
    assert meal.eaten_at == datetime(2026, 10, 18, 11, 0, tzinfo=UTC)


async def test_log_meal_cannot_correct_another_users_meal(
    app: FastAPI, http: httpx.AsyncClient, mcp: Client[StreamableHttpTransport]
) -> None:
    other_client = await register_client(http)
    other_tokens = await sign_in(http, other_client, email="mallory@example.com")
    other = await call_log_meal(mcp_client(app, other_tokens["access_token"]), COMPLETE_MEAL)

    async with mcp:
        result = await mcp.call_tool(
            "log_meal",
            COMPLETE_MEAL | {"calories_kcal": 1, "replaces_meal_id": other["meal"]["meal_id"]},
            raise_on_error=False,
        )

    assert result.is_error
    assert "Not saved to HealthyEating" in str(result.content)
    [meal] = await stored_meals(app)
    assert meal.calories_kcal == 480


async def test_meals_are_deleted_with_the_account(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    await call_log_meal(mcp, COMPLETE_MEAL)
    db: Database = app.state.db
    async with db.session() as session:
        await session.execute(delete(User))
    assert await stored_meals(app) == []


async def test_log_meal_asks_for_missing_values(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await call_log_meal(
        mcp,
        {
            "dish_name": "Homemade stew",
            "source": "estimated_by_assistant",
            "confidence": "low",
            "open_questions": ["How many grams of meat were in your portion?"],
        },
    )

    assert content["status"] == "needs_more_info"
    assert content["saved_to_healthy_eating"] is False
    assert content["user_message"].startswith("Not saved to HealthyEating yet")
    assert content["meal"] is None
    assert content["questions_for_user"] == ["How many grams of meat were in your portion?"]
    assert await stored_meals(app) == []


async def test_log_meal_rejects_impossible_values(mcp: Client[StreamableHttpTransport]) -> None:
    async with mcp:
        result = await mcp.call_tool(
            "log_meal", COMPLETE_MEAL | {"protein_g": -5}, raise_on_error=False
        )
    assert result.is_error


async def test_log_meal_accepts_eaten_at_without_offset(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    content = await call_log_meal(mcp, COMPLETE_MEAL | {"eaten_at": "2026-10-17T19:00:00"})

    assert content["status"] == "logged"
    [meal] = await stored_meals(app)
    # Stored as UTC until the user's time zone is known; the offset is recorded as unknown.
    assert meal.eaten_at == datetime(2026, 10, 17, 19, 0, tzinfo=UTC)
    assert meal.utc_offset_minutes is None


async def test_log_meal_saves_estimate_with_open_questions(
    app: FastAPI, mcp: Client[StreamableHttpTransport]
) -> None:
    questions = ["Did you drink the juice too?"]
    content = await call_log_meal(
        mcp, COMPLETE_MEAL | {"confidence": "low", "open_questions": questions}
    )

    assert content["status"] == "logged"
    assert content["saved_to_healthy_eating"] is True
    assert content["questions_for_user"] == questions
    assert content["user_message"].startswith("Saved to HealthyEating:")
    assert content["user_message"].endswith("I'll update it if your answer changes the estimate.")
    assert "replaces_meal_id" in content["message"]
    [meal] = await stored_meals(app)
    assert meal.confidence == "low"
