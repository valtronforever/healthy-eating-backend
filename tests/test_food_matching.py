import pytest
from fastapi import FastAPI
from sqlalchemy import select

from healthy_eating.db import Database
from healthy_eating.db.models import ReferenceData, ReferenceFood
from healthy_eating.services.food_matching import (
    FoodItem,
    match_items,
    meal_totals,
    search_foods,
)
from healthy_eating.services.reference_data import (
    USDA,
    bundled_usda,
    ensure_reference_data,
    reference_food_count,
)


def db_of(app: FastAPI) -> Database:
    database: Database = app.state.db
    return database


async def test_the_bundled_usda_data_is_loaded_once(app: FastAPI) -> None:
    bundled = bundled_usda()
    async with db_of(app).session() as session:
        loaded = await session.get(ReferenceData, USDA)
        count = await reference_food_count(session)
        again = await ensure_reference_data(session, bundled)

    assert loaded is not None and loaded.version == bundled.version
    assert set(loaded.datasets) == {"fndds", "foundation", "sr_legacy"}
    assert count == len(bundled.rows) > 13000
    assert again is False


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        # Cooking words never block a match; boiled counts as cooked.
        ("boiled rice", "Rice, cooked, NFS"),
        ("white rice, cooked", "Rice, white, cooked, NS as to fat"),
        # Unprocessed and general entries win when the item names nothing else.
        ("apple", "Apple, raw"),
        ("orange juice", "Orange juice, 100%, NFS"),
        # A leading category word does not hide the food.
        ("salmon", "Fish, salmon, raw"),
        ("salmon, baked", "Fish, salmon, baked or broiled"),
        ("beef steak", "Beef, steak, NFS"),
        ("cottage cheese", "Cheese, cottage, NFS"),
    ],
)
async def test_items_match_the_food_people_mean(app: FastAPI, item: str, expected: str) -> None:
    async with db_of(app).session() as session:
        [(food, status)] = await search_foods(session, item, limit=1)

    assert (food.description, status) == (expected, "matched")


async def test_doubtful_items_get_no_values(app: FastAPI) -> None:
    items = [FoodItem("50% cream", 30), FoodItem("it's", 10), FoodItem("zzqx", 10)]
    async with db_of(app).session() as session:
        matches = await match_items(session, items)

    assert [match.status for match in matches] == ["weak", "none", "none"]
    assert all(match.nutrients == {} for match in matches)
    assert meal_totals(matches) == {}


async def test_values_scale_with_grams_and_unknown_values_stay_unknown(app: FastAPI) -> None:
    async with db_of(app).session() as session:
        rice = await session.scalar(
            select(ReferenceFood).where(ReferenceFood.description == "Rice, cooked, NFS")
        )
        # A food without a vitamin D value.
        without_d = await session.scalar(
            select(ReferenceFood)
            .where(~ReferenceFood.nutrients.has_key("vitamin_d_ug"))
            .where(ReferenceFood.nutrients.has_key("iron_mg"))
            .limit(1)
        )
        assert rice is not None and without_d is not None
        matches = await match_items(
            session, [FoodItem("boiled rice", 150), FoodItem("anything", 50, food_id=without_d.id)]
        )

    rice_match, chosen = matches
    assert rice_match.nutrients["iron_mg"] == pytest.approx(rice.nutrients["iron_mg"] * 1.5)
    assert chosen.status == "matched" and chosen.food is not None
    assert chosen.food.description == without_d.description
    assert "vitamin_d_ug" not in chosen.nutrients
    totals = meal_totals(matches)
    assert totals["iron_mg"].items == 2
    assert totals["iron_mg"].amount == pytest.approx(
        rice.nutrients["iron_mg"] * 1.5 + without_d.nutrients["iron_mg"] * 0.5
    )
    if "vitamin_d_ug" in rice.nutrients:
        assert totals["vitamin_d_ug"].items == 1


async def test_an_unknown_food_id_matches_nothing(app: FastAPI) -> None:
    async with db_of(app).session() as session:
        [match] = await match_items(session, [FoodItem("rice", 100, food_id=1)])

    assert (match.status, match.food, match.nutrients) == ("none", None, {})


async def test_quotes_and_backslashes_in_names_are_safe(app: FastAPI) -> None:
    async with db_of(app).session() as session:
        [(food, status)] = await search_foods(session, "'apple' pie \\", limit=1)

    assert status == "matched"
    assert "pie" in food.description.lower()
