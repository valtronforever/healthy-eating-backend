import csv
import io
import json
import uuid
import zipfile
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from healthy_eating.db import Database
from healthy_eating.db.models import MealItem, ReferenceFood
from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account

LUNCH: dict[str, Any] = {
    "dish_name": "Rice with salmon",
    "source": "from_conversation",
    "calories_kcal": 610,
    "protein_g": 38,
    "fat_g": 18,
    "carbohydrates_g": 70,
    "confidence": "high",
}


async def call(app: FastAPI, owner: Account, tool: str, arguments: dict[str, Any]) -> Any:
    async with mcp_client(app, owner.token) as mcp:
        result = await mcp.call_tool(tool, arguments)
    return result.structured_content


async def rest_meal(http: httpx.AsyncClient, owner: Account, meal_id: str) -> dict[str, Any]:
    response = await http.get(f"/api/v1/meals/{meal_id}", headers=owner.headers)
    assert response.status_code == 200, response.text
    meal: dict[str, Any] = response.json()
    return meal


async def food(app: FastAPI, description: str) -> ReferenceFood:
    db: Database = app.state.db
    async with db.session() as session:
        found = await session.scalar(
            select(ReferenceFood).where(ReferenceFood.description == description)
        )
    assert found is not None
    return found


async def test_items_add_vitamins_and_minerals_without_changing_the_values(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ada.items@example.com")
    items = [{"name": "boiled rice", "grams": 150}, {"name": "salmon, baked", "grams": 120}]

    result = await call(app, owner, "log_meal", {**LUNCH, "items": items})

    assert [(i["status"], i["matched_food"]) for i in result["items"]] == [
        ("matched", "Rice, cooked, NFS"),
        ("matched", "Fish, salmon, baked or broiled"),
    ]
    meal = await rest_meal(http, owner, result["meal"]["meal_id"])
    # The values from the conversation stay exactly as they were.
    assert (meal["calories_kcal"], meal["protein_g"], meal["fat_g"]) == (610, 38, 18)
    assert meal["estimated_fields"] == []
    rice = await food(app, "Rice, cooked, NFS")
    salmon = await food(app, "Fish, salmon, baked or broiled")
    expected = rice.nutrients["iron_mg"] * 1.5 + salmon.nutrients["iron_mg"] * 1.2
    assert meal["micronutrients"]["iron_mg"] == pytest.approx(expected, abs=0.001)
    assert [(i["name"], i["grams"], i["food_description"]) for i in meal["items"]] == [
        ("boiled rice", 150, "Rice, cooked, NFS"),
        ("salmon, baked", 120, "Fish, salmon, baked or broiled"),
    ]


async def test_a_meal_without_items_has_no_micronutrients(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "bea.items@example.com")

    result = await call(app, owner, "log_meal", LUNCH)

    assert result["items"] == []
    meal = await rest_meal(http, owner, result["meal"]["meal_id"])
    assert (meal["micronutrients"], meal["items"]) == (None, [])


async def test_doubtful_items_add_nothing_and_show_the_closest_food(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "cy.items@example.com")
    items = [{"name": "50% cream", "grams": 30}, {"name": "zzqx", "grams": 10}]

    result = await call(app, owner, "log_meal", {**LUNCH, "items": items})

    weak, none = result["items"]
    assert (weak["status"], weak["matched_food"], weak["food_id"]) == ("weak", None, None)
    assert weak["closest_food"] is not None
    assert (none["status"], none["closest_food"]) == ("none", None)
    meal = await rest_meal(http, owner, result["meal"]["meal_id"])
    assert meal["micronutrients"] == {}
    assert [i["status"] for i in meal["items"]] == ["weak", "none"]


async def test_find_foods_gives_a_food_id_to_correct_an_item(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "dan.items@example.com")
    first = await call(app, owner, "log_meal", {**LUNCH, "items": [{"name": "zzqx", "grams": 100}]})

    found = await call(app, owner, "find_foods", {"query": "salmon, baked", "limit": 3})
    best = found["foods"][0]
    corrected = await call(
        app,
        owner,
        "log_meal",
        {
            **LUNCH,
            "replaces_meal_id": first["meal"]["meal_id"],
            "items": [
                {"name": "salmon", "grams": 100, "food_id": best["food_id"]},
                {"name": "boiled rice", "grams": 150},
            ],
        },
    )

    assert best["description"] == "Fish, salmon, baked or broiled"
    assert best["match"] == "matched"
    assert [i["status"] for i in corrected["items"]] == ["matched", "matched"]
    meal = await rest_meal(http, owner, first["meal"]["meal_id"])
    assert [i["food_description"] for i in meal["items"]] == [
        "Fish, salmon, baked or broiled",
        "Rice, cooked, NFS",
    ]
    assert "iron_mg" in meal["micronutrients"]


async def test_a_correction_without_items_keeps_them(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "eli.items@example.com")
    items = [{"name": "boiled rice", "grams": 150}]
    first = await call(app, owner, "log_meal", {**LUNCH, "items": items})

    await call(
        app,
        owner,
        "log_meal",
        {**LUNCH, "calories_kcal": 650, "replaces_meal_id": first["meal"]["meal_id"]},
    )

    meal = await rest_meal(http, owner, first["meal"]["meal_id"])
    assert meal["calories_kcal"] == 650
    assert [i["name"] for i in meal["items"]] == ["boiled rice"]
    assert meal["micronutrients"]


async def test_relogging_copies_the_items(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "fay.items@example.com")
    first = await call(
        app, owner, "log_meal", {**LUNCH, "items": [{"name": "boiled rice", "grams": 150}]}
    )
    new_id = str(uuid.uuid4())

    response = await http.post(
        f"/api/v1/meals/{first['meal']['meal_id']}/relog",
        json={"id": new_id},
        headers=owner.headers,
    )

    assert response.status_code == 201, response.text
    original = await rest_meal(http, owner, first["meal"]["meal_id"])
    copy = response.json()
    assert copy["items"] == original["items"]
    assert copy["micronutrients"] == original["micronutrients"]


async def test_items_are_exported_and_deleted_with_their_meal(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "gil.items@example.com")
    result = await call(
        app, owner, "log_meal", {**LUNCH, "items": [{"name": "boiled rice", "grams": 150}]}
    )
    meal_id = result["meal"]["meal_id"]

    exported = await http.get("/api/v1/export", params={"format": "json"}, headers=owner.headers)
    zipped = await http.get("/api/v1/export", params={"format": "csv"}, headers=owner.headers)
    [meal] = exported.json()["meals"]
    assert meal["items"][0]["food_description"] == "Rice, cooked, NFS"
    assert "iron_mg" in meal["micronutrients"]
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
        [row] = list(csv.DictReader(io.StringIO(archive.read("meals.csv").decode())))
    assert json.loads(row["items"])[0]["name"] == "boiled rice"
    assert "iron_mg" in json.loads(row["micronutrients"])

    deleted = await http.delete(f"/api/v1/meals/{meal_id}", headers=owner.headers)
    assert deleted.status_code == 204
    db: Database = app.state.db
    async with db.session() as session:
        left = await session.scalar(
            select(func.count()).select_from(MealItem).where(MealItem.meal_id == uuid.UUID(meal_id))
        )
    assert left == 0


SUPPLEMENTS: dict[str, Any] = {
    "dish_name": "Daily supplements",
    "source": "from_conversation",
    "calories_kcal": 10,
    "protein_g": 0,
    "fat_g": 1,
    "carbohydrates_g": 0,
    "confidence": "high",
}


async def test_supplements_get_their_label_values(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "hal.label@example.com")
    items = [
        {"name": "Vitamin D3 capsule", "label_values": {"vitamin_d_ug": 50}},
        {"name": "Magnesium citrate, 2 tablets", "label_values": {"magnesium_mg": 200}},
        {"name": "boiled rice", "grams": 100},
    ]

    result = await call(app, owner, "log_meal", {**SUPPLEMENTS, "items": items})

    assert [i["status"] for i in result["items"]] == ["label", "label", "matched"]
    meal = await rest_meal(http, owner, result["meal"]["meal_id"])
    assert meal["label_items"] == [
        {"name": "Vitamin D3 capsule", "grams": None, "values": {"vitamin_d_ug": 50}},
        {"name": "Magnesium citrate, 2 tablets", "grams": None, "values": {"magnesium_mg": 200}},
    ]
    # Reference-food items stay in `items`; label items are not repeated there.
    assert [i["name"] for i in meal["items"]] == ["boiled rice"]
    rice = await food(app, "Rice, cooked, NFS")
    assert meal["micronutrients"]["vitamin_d_ug"] == 50 + rice.nutrients.get("vitamin_d_ug", 0)
    assert meal["micronutrients"]["magnesium_mg"] == pytest.approx(
        200 + rice.nutrients["magnesium_mg"], abs=0.001
    )
    day = meal["local_date"]
    days = await http.get("/api/v1/days", params={"from": day, "to": day}, headers=owner.headers)
    assert days.json()["days"][0]["micronutrients"]["vitamin_d_ug"]["meal_count"] == 1


async def test_an_item_without_grams_or_label_values_is_skipped(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ivy.label@example.com")
    items = [{"name": "fish oil"}, {"name": "boiled rice", "grams": 100}]

    # The meal is still saved; only the item without an amount carries nothing.
    result = await call(app, owner, "log_meal", {**SUPPLEMENTS, "items": items})

    assert [i["status"] for i in result["items"]] == ["none", "matched"]
    meal = await rest_meal(http, owner, result["meal"]["meal_id"])
    assert [i["name"] for i in meal["items"]] == ["boiled rice"]
    assert meal["label_items"] == []


async def test_relogging_supplements_copies_their_label_values(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "jo.label@example.com")
    items = [{"name": "Zinc 15 mg tablet", "label_values": {"zinc_mg": 15}}]
    first = await call(app, owner, "log_meal", {**SUPPLEMENTS, "items": items})

    response = await http.post(
        f"/api/v1/meals/{first['meal']['meal_id']}/relog",
        json={"id": str(uuid.uuid4())},
        headers=owner.headers,
    )

    assert response.status_code == 201, response.text
    copy = response.json()
    assert copy["label_items"] == [
        {"name": "Zinc 15 mg tablet", "grams": None, "values": {"zinc_mg": 15}}
    ]
    assert copy["micronutrients"] == {"zinc_mg": 15}


async def test_untracked_label_nutrients_are_left_out_not_refused(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "kim.label@example.com")
    values = {"vitamin_b12_ug": 1000, "biotin_ug": 600, "omega_3_mg": 600}
    items = [{"name": "B complex capsule", "label_values": values}]

    result = await call(app, owner, "log_meal", {**SUPPLEMENTS, "items": items})

    [item] = result["items"]
    assert (item["status"], item["untracked_values"]) == ("label", ["biotin_ug", "omega_3_mg"])
    meal = await rest_meal(http, owner, result["meal"]["meal_id"])
    assert meal["micronutrients"] == {"vitamin_b12_ug": 1000}
