from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.mcp_helpers import mcp_client
from tests.test_meal_items import call, rest_meal
from tests.test_meals_api import Account, account, add_meal

# A yogurt whose values came from its label, for a 150 g cup.
LABEL_YOGURT: dict[str, Any] = {
    "dish_name": "Йогурт Danone 2,5%",
    "source": "from_conversation",
    "source_note": "nutrition label",
    "portion_grams": 150,
    "calories_kcal": 105,
    "protein_g": 4.5,
    "fat_g": 3.75,
    "carbohydrates_g": 13.5,
    "sugar_g": 12,
    "confidence": "high",
    "items": [
        {"name": "yogurt, plain", "grams": 150},
        {"name": "Vitamin D3 capsule", "label_values": {"vitamin_d_ug": 25}},
    ],
}


def recent(days: int = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def find(app: FastAPI, owner: Account, query: str) -> list[dict[str, Any]]:
    result = await call(app, owner, "find_meals", {"query": query})
    meals: list[dict[str, Any]] = result["meals"]
    return meals


async def test_known_meals_are_found_by_name_in_any_order_and_case(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ana.find@example.com")
    await call(app, owner, "log_meal", {**LABEL_YOGURT, "eaten_at": recent(3)})
    await call(app, owner, "log_meal", {**LABEL_YOGURT, "eaten_at": recent(1)})

    for query in ("danone йогурт", "ЙОГУРТ", "Danone"):
        [found] = await find(app, owner, query)
        assert found["dish_name"] == "Йогурт Danone 2,5%"
        assert found["times"] == 2
        assert found["portion_grams"] == 150
        assert found["source_note"] == "nutrition label"
        assert found["per_100g"] == {
            "calories_kcal": 70,
            "protein_g": 3,
            "fat_g": 2.5,
            "carbohydrates_g": 9,
            "fiber_g": None,
            "sugar_g": 8,
            "sodium_mg": None,
        }
    # Item names count too, and a word matches the words it starts.
    assert len(await find(app, owner, "plain yog")) == 1
    assert await find(app, owner, "kefir") == []


async def test_only_meals_with_known_values_and_weight_are_offered(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ben.find@example.com")
    yesterday = recent(1)
    # Estimated from a photo: never offered, however it is named.
    await add_meal(app, owner, yesterday, dish_name="Yogurt estimated", portion_grams=150)
    # Known numbers, but calories were estimated.
    await add_meal(
        app,
        owner,
        yesterday,
        dish_name="Yogurt partly estimated",
        source="from_conversation",
        estimated_fields=["calories_kcal"],
        portion_grams=150,
    )
    # Known numbers without a weight: nothing to give per 100 g.
    await add_meal(
        app,
        owner,
        yesterday,
        dish_name="Yogurt without weight",
        source="from_conversation",
        estimated_fields=[],
    )
    # Known numbers, but more than a year ago.
    await add_meal(
        app,
        owner,
        recent(400),
        dish_name="Yogurt last year",
        source="from_conversation",
        estimated_fields=[],
        portion_grams=150,
    )
    # Only fiber estimated: the four main values are still known.
    await add_meal(
        app,
        owner,
        yesterday,
        dish_name="Yogurt known",
        source="from_conversation",
        estimated_fields=["fiber_g"],
        portion_grams=200,
    )

    assert [meal["dish_name"] for meal in await find(app, owner, "yogurt")] == ["Yogurt known"]


async def test_each_user_finds_only_their_own_meals(app: FastAPI, http: httpx.AsyncClient) -> None:
    one = await account(http, "cara.find@example.com")
    two = await account(http, "dan.find@example.com")
    await call(app, one, "log_meal", {**LABEL_YOGURT, "eaten_at": recent()})

    assert await find(app, two, "danone") == []


async def test_relogging_at_another_weight_scales_every_value(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "eve.scale@example.com")
    first = await call(app, owner, "log_meal", {**LABEL_YOGURT, "eaten_at": recent()})

    saved = await call(
        app, owner, "relog_meal", {"meal_id": first["meal"]["meal_id"], "portion_grams": 200}
    )

    assert saved["user_message"].startswith(
        "Saved to HealthyEating again: Йогурт Danone 2,5% (200 g), 140 kcal"
    )
    before = await rest_meal(http, owner, first["meal"]["meal_id"])
    after = await rest_meal(http, owner, saved["meal_id"])
    assert after["portion_grams"] == 200
    assert (after["calories_kcal"], after["protein_g"], after["fat_g"]) == (140, 6, 5)
    assert (after["carbohydrates_g"], after["sugar_g"]) == (18, 16)
    assert after["source"] == "from_conversation"
    assert after["source_note"] == "nutrition label"
    assert after["estimated_fields"] == [] and after["confidence"] == "high"
    assert [item["grams"] for item in after["items"]] == [200]
    # The capsule is counted, not weighed: its label values stay.
    assert after["label_items"] == before["label_items"]
    from_food = {k: v for k, v in before["micronutrients"].items() if k != "vitamin_d_ug"}
    for code, amount in from_food.items():
        assert after["micronutrients"][code] == pytest.approx(amount * 4 / 3, abs=0.002)


async def test_relogging_at_another_weight_needs_a_known_weight(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "fay.scale@example.com")
    meal_id = await add_meal(app, owner, recent())

    async with mcp_client(app, owner.token) as mcp:
        result = await mcp.call_tool(
            "relog_meal", {"meal_id": str(meal_id), "portion_grams": 200}, raise_on_error=False
        )
        # The same weight, or none, is a plain copy.
        copy = await mcp.call_tool("relog_meal", {"meal_id": str(meal_id)})

    assert result.is_error
    assert "no portion weight" in str(result.content)
    assert copy.structured_content is not None
    assert copy.structured_content["saved_to_healthy_eating"] is True


ESTIMATED_YOGURT: dict[str, Any] = {
    "dish_name": "йогурт danone",
    "source": "estimated_by_assistant",
    "portion_grams": 200,
    "calories_kcal": 120,
    "protein_g": 8,
    "fat_g": 3,
    "carbohydrates_g": 10,
}


async def test_an_estimate_points_to_the_earlier_known_values(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "gil.hint@example.com")
    earlier = await call(app, owner, "log_meal", {**LABEL_YOGURT, "eaten_at": recent(2)})

    result = await call(app, owner, "log_meal", ESTIMATED_YOGURT)

    assert result["status"] == "logged"
    hint = result["earlier_meal"]
    assert hint["meal_id"] == earlier["meal"]["meal_id"]
    assert hint["per_100g"]["calories_kcal"] == 70
    [question] = result["questions_for_user"]
    assert "Йогурт Danone 2,5%" in question and "70 kcal per 100 g" in question
    assert "replaces_meal_id" in result["message"]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"calories_kcal": 145, "protein_g": 6, "fat_g": 5, "carbohydrates_g": 18}, "same values"),
        ({"source": "from_conversation", "estimated_fields": []}, "known values of its own"),
        ({"portion_grams": None}, "no weight to compare"),
        ({"dish_name": "Danone yogurt with granola"}, "another dish"),
    ],
)
async def test_no_hint_without_a_clear_reason(
    app: FastAPI, http: httpx.AsyncClient, changes: dict[str, Any], reason: str
) -> None:
    owner = await account(http, f"hal.{abs(hash(reason))}@example.com")
    await call(app, owner, "log_meal", {**LABEL_YOGURT, "eaten_at": recent(2)})
    meal = {
        key: value for key, value in {**ESTIMATED_YOGURT, **changes}.items() if value is not None
    }

    result = await call(app, owner, "log_meal", meal)

    assert result["earlier_meal"] is None, reason
    assert result["questions_for_user"] == []


async def test_a_correction_gets_no_hint(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "ivy.hint@example.com")
    await call(app, owner, "log_meal", {**LABEL_YOGURT, "eaten_at": recent(2)})
    first = await call(app, owner, "log_meal", ESTIMATED_YOGURT)

    corrected = await call(
        app,
        owner,
        "log_meal",
        {**ESTIMATED_YOGURT, "calories_kcal": 130, "replaces_meal_id": first["meal"]["meal_id"]},
    )

    assert corrected["earlier_meal"] is None
