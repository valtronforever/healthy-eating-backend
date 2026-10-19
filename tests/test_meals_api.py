import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.db import Database
from healthy_eating.db.models import Meal
from tests.oauth_helpers import bearer, register_client, sign_in


class Account:
    def __init__(self, user_id: uuid.UUID, token: str) -> None:
        self.user_id = user_id
        self.token = token
        self.headers = bearer(token)


async def account(http: httpx.AsyncClient, email: str, time_zone: str | None = None) -> Account:
    client = await register_client(http, redirect_uri="healthyeating://oauth/callback")
    token = (await sign_in(http, client, email=email))["access_token"]
    me = (await http.get("/api/v1/me", headers=bearer(token))).json()
    if time_zone is not None:
        await http.patch("/api/v1/me/settings", json={"timezone": time_zone}, headers=bearer(token))
    return Account(uuid.UUID(me["id"]), token)


@pytest.fixture
async def kim(http: httpx.AsyncClient) -> Account:
    return await account(http, "kim@example.com", "America/Los_Angeles")


async def add_meal(
    app: FastAPI,
    owner: Account,
    eaten_at: str,
    utc_offset_minutes: int | None = None,
    dish_name: str = "Toast",
    meal_id: uuid.UUID | None = None,
) -> uuid.UUID:
    db: Database = app.state.db
    async with db.session() as session:
        meal = Meal(
            id=meal_id or uuid.uuid4(),
            user_id=owner.user_id,
            dish_name=dish_name,
            source="estimated_by_assistant",
            eaten_at=datetime.fromisoformat(eaten_at),
            utc_offset_minutes=utc_offset_minutes,
            calories_kcal=200,
            protein_g=6,
            fat_g=4,
            carbohydrates_g=34,
            estimated_fields=["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"],
            confidence="medium",
        )
        session.add(meal)
    return meal.id


async def day(http: httpx.AsyncClient, owner: Account, date: str) -> list[str]:
    response = await http.get(
        "/api/v1/meals", params={"from": date, "to": date}, headers=owner.headers
    )
    assert response.status_code == 200, response.text
    return [meal["dish_name"] for meal in response.json()["meals"]]


async def test_meals_are_grouped_by_the_users_local_day(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    # 05:00 UTC on the 18th is 22:00 on the 17th in Los Angeles.
    await add_meal(app, kim, "2026-10-18T05:00:00+00:00", dish_name="Late dinner")
    await add_meal(app, kim, "2026-10-18T16:00:00+00:00", dish_name="Lunch")

    assert await day(http, kim, "2026-10-17") == ["Late dinner"]
    assert await day(http, kim, "2026-10-18") == ["Lunch"]


async def test_a_meals_own_offset_decides_its_local_day(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    # Logged while travelling in Tokyo: 16:00 UTC on the 17th is 01:00 on the 18th there,
    # but still the 17th in Los Angeles.
    await add_meal(app, kim, "2026-10-17T16:00:00+00:00", 540, dish_name="Ramen")

    assert await day(http, kim, "2026-10-17") == []
    assert await day(http, kim, "2026-10-18") == ["Ramen"]


async def test_without_a_time_zone_local_days_are_utc_days(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    lee = await account(http, "lee@example.com")
    await add_meal(app, lee, "2026-10-18T05:00:00+00:00", dish_name="Breakfast")

    assert await day(http, lee, "2026-10-18") == ["Breakfast"]


async def test_meal_fields_and_local_time(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    meal_id = await add_meal(app, kim, "2026-10-17T16:00:00+00:00", 540, dish_name="Ramen")

    response = await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)

    assert response.status_code == 200
    meal = response.json()
    assert meal["id"] == str(meal_id)
    assert meal["eaten_at"] == "2026-10-18T01:00:00+09:00"
    assert meal["local_date"] == "2026-10-18"
    assert meal["source"] == "estimated_by_assistant"
    assert meal["estimated_fields"] == ["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"]
    assert meal["confidence"] == "medium"
    assert {"calories_kcal", "protein_g", "fat_g", "carbohydrates_g", "source_note"} <= set(meal)
    # Without a stored offset, the meal is shown in the user's time zone.
    other_id = await add_meal(app, kim, "2026-10-17T16:00:00+00:00")
    other = (await http.get(f"/api/v1/meals/{other_id}", headers=kim.headers)).json()
    assert other["eaten_at"] == "2026-10-17T09:00:00-07:00"


async def test_meals_are_paginated_newest_first(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    start = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)
    for hour in range(4):
        await add_meal(app, kim, (start + timedelta(hours=hour)).isoformat(), dish_name=f"#{hour}")
    # Two meals at the same time are ordered by id, so no page repeats or skips one.
    same_time = (start + timedelta(hours=1)).isoformat()
    await add_meal(app, kim, same_time, dish_name="#1b", meal_id=uuid.UUID(int=2**128 - 1))

    names: list[str] = []
    params: dict[str, Any] = {"from": "2026-10-18", "to": "2026-10-18", "limit": 2}
    pages = 0
    while True:
        response = await http.get("/api/v1/meals", params=params, headers=kim.headers)
        body = response.json()
        names += [meal["dish_name"] for meal in body["meals"]]
        pages += 1
        if body["next_cursor"] is None:
            break
        params["cursor"] = body["next_cursor"]

    assert pages == 3
    assert names == ["#3", "#2", "#1b", "#1", "#0"]


async def test_meals_without_a_range_returns_everything(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    await add_meal(app, kim, "2025-01-01T12:00:00+00:00", dish_name="Old")
    await add_meal(app, kim, "2026-10-18T12:00:00+00:00", dish_name="New")
    response = await http.get("/api/v1/meals", headers=kim.headers)
    assert [meal["dish_name"] for meal in response.json()["meals"]] == ["New", "Old"]


async def test_invalid_list_requests_are_rejected(http: httpx.AsyncClient, kim: Account) -> None:
    bad = [
        {"from": "2026-10-18", "to": "2026-10-17"},
        {"cursor": "not-a-cursor"},
        {"cursor": "MjAyNi0xMC0xOFQxMjowMDowMHxub3QtYS11dWlk"},
    ]
    for params in bad:
        response = await http.get("/api/v1/meals", params=params, headers=kim.headers)
        assert response.status_code == 400, params
    response = await http.get("/api/v1/meals", params={"from": "18.10.2026"}, headers=kim.headers)
    assert response.status_code == 422
    response = await http.get("/api/v1/meals", params={"limit": 0}, headers=kim.headers)
    assert response.status_code == 422


async def test_meals_require_a_token(http: httpx.AsyncClient) -> None:
    assert (await http.get("/api/v1/meals")).status_code == 401
    assert (await http.get(f"/api/v1/meals/{uuid.uuid4()}")).status_code == 401


async def test_users_only_see_their_own_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    mallory = await account(http, "mallory@example.com", "America/Los_Angeles")
    kims_meal = await add_meal(app, kim, "2026-10-18T16:00:00+00:00", dish_name="Kim's lunch")

    listed = await http.get("/api/v1/meals", headers=mallory.headers)
    assert listed.json()["meals"] == []
    assert await day(http, mallory, "2026-10-18") == []
    response = await http.get(f"/api/v1/meals/{kims_meal}", headers=mallory.headers)
    assert response.status_code == 404
    response = await http.get(f"/api/v1/meals/{uuid.uuid4()}", headers=kim.headers)
    assert response.status_code == 404


# Delete


async def test_delete_removes_the_meal(app: FastAPI, http: httpx.AsyncClient, kim: Account) -> None:
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")
    kept = await add_meal(app, kim, "2026-10-18T17:00:00+00:00", dish_name="Kept")

    response = await http.delete(f"/api/v1/meals/{meal_id}", headers=kim.headers)

    assert response.status_code == 204
    assert response.content == b""
    assert (await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)).status_code == 404
    assert await day(http, kim, "2026-10-18") == ["Kept"]
    again = await http.delete(f"/api/v1/meals/{meal_id}", headers=kim.headers)
    assert again.status_code == 404
    assert (await http.get(f"/api/v1/meals/{kept}", headers=kim.headers)).status_code == 200


async def test_users_cannot_delete_other_users_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    mallory = await account(http, "mallory@example.com")
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")

    response = await http.delete(f"/api/v1/meals/{meal_id}", headers=mallory.headers)

    assert response.status_code == 404
    assert (await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)).status_code == 200
    assert (await http.delete(f"/api/v1/meals/{meal_id}")).status_code == 401


# Correct


async def edit(
    http: httpx.AsyncClient, owner: Account, meal_id: uuid.UUID, body: dict[str, Any]
) -> httpx.Response:
    return await http.patch(f"/api/v1/meals/{meal_id}", json=body, headers=owner.headers)


async def test_edited_values_are_no_longer_estimated(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")

    response = await edit(http, kim, meal_id, {"protein_g": 9, "carbohydrates_g": 31})

    assert response.status_code == 200, response.text
    body = response.json()
    meal = body["meal"]
    assert meal["id"] == str(meal_id)
    assert (meal["calories_kcal"], meal["protein_g"], meal["fat_g"]) == (200, 9, 4)
    assert meal["carbohydrates_g"] == 31
    assert meal["estimated_fields"] == ["calories_kcal", "fat_g"]
    assert meal["source"] == "estimated_by_assistant"
    assert body["warnings"] == []
    stored = (await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)).json()
    assert stored == meal


async def test_unchanged_values_keep_their_estimated_mark(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")
    # An edit form sends every field, also the ones the user did not touch.
    response = await edit(
        http,
        kim,
        meal_id,
        {"dish_name": "Toast", "calories_kcal": 200, "protein_g": 6, "fat_g": 4},
    )
    assert response.json()["meal"]["estimated_fields"] == [
        "calories_kcal",
        "protein_g",
        "fat_g",
        "carbohydrates_g",
    ]


async def test_edit_name_portion_and_time(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")

    response = await edit(
        http,
        kim,
        meal_id,
        {"dish_name": "Rye toast", "portion_grams": 80, "eaten_at": "2026-10-17T23:30:00+09:00"},
    )
    meal = response.json()["meal"]
    assert meal["dish_name"] == "Rye toast"
    assert meal["portion_grams"] == 80
    assert meal["eaten_at"] == "2026-10-17T23:30:00+09:00"
    assert meal["local_date"] == "2026-10-17"
    assert len(meal["estimated_fields"]) == 4

    # A time without an offset is read in the user's time zone (Los Angeles); null removes the
    # portion weight.
    response = await edit(
        http, kim, meal_id, {"portion_grams": None, "eaten_at": "2026-10-18T07:15:00"}
    )
    meal = response.json()["meal"]
    assert meal["portion_grams"] is None
    assert meal["eaten_at"] == "2026-10-18T07:15:00-07:00"


async def test_edit_reports_calories_that_do_not_match_the_macros(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")
    response = await edit(http, kim, meal_id, {"calories_kcal": 600})
    assert response.status_code == 200
    assert len(response.json()["warnings"]) == 1
    assert response.json()["meal"]["calories_kcal"] == 600


@pytest.mark.parametrize(
    "body",
    [
        {"calories_kcal": None},
        {"protein_g": -1},
        {"fat_g": 1001},
        {"portion_grams": 0},
        {"dish_name": ""},
        {"dish_name": None},
        {"eaten_at": None},
        {"eaten_at": "yesterday"},
        {"source": "from_conversation"},
        {"estimated_fields": []},
    ],
)
async def test_invalid_edits_are_rejected(
    app: FastAPI, http: httpx.AsyncClient, kim: Account, body: dict[str, Any]
) -> None:
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")
    before = (await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)).json()

    response = await edit(http, kim, meal_id, body)

    assert response.status_code == 422
    after = (await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)).json()
    assert after == before


async def test_users_cannot_edit_other_users_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    mallory = await account(http, "mallory@example.com")
    meal_id = await add_meal(app, kim, "2026-10-18T16:00:00+00:00")

    response = await edit(http, mallory, meal_id, {"calories_kcal": 1})
    assert response.status_code == 404
    assert (await edit(http, kim, uuid.uuid4(), {"calories_kcal": 1})).status_code == 404
    unauthenticated = await http.patch(f"/api/v1/meals/{meal_id}", json={"calories_kcal": 1})
    assert unauthenticated.status_code == 401
    meal = (await http.get(f"/api/v1/meals/{meal_id}", headers=kim.headers)).json()
    assert meal["calories_kcal"] == 200
