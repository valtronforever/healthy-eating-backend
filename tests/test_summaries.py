from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.test_meals_api import Account, account, add_meal


@pytest.fixture
async def kim(http: httpx.AsyncClient) -> Account:
    return await account(http, "kim@example.com", "America/Los_Angeles")


async def days(http: httpx.AsyncClient, owner: Account, first: str, last: str) -> dict[str, Any]:
    response = await http.get(
        "/api/v1/days", params={"from": first, "to": last}, headers=owner.headers
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_totals_per_local_day_including_days_without_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    # 05:00 UTC on the 18th is 22:00 on the 17th in Los Angeles.
    await add_meal(app, kim, "2026-10-18T05:00:00+00:00")
    await add_meal(app, kim, "2026-10-17T18:00:00+00:00", calories_kcal=350.25, protein_g=20)
    await add_meal(app, kim, "2026-10-19T18:00:00+00:00")

    body = await days(http, kim, "2026-10-17", "2026-10-18")

    assert body["counting_rule"] == "logged_days"
    first, second = body["days"]
    assert first["date"] == "2026-10-17"
    assert first["meal_count"] == 2
    assert (first["calories_kcal"], first["protein_g"], first["fat_g"]) == (550.2, 26, 8)
    assert first["carbohydrates_g"] == 68
    assert first["counted"] is True
    assert second == {
        "date": "2026-10-18",
        "meal_count": 0,
        "calories_kcal": 0,
        "protein_g": 0,
        "fat_g": 0,
        "carbohydrates_g": 0,
        "fiber_g": None,
        "sugar_g": None,
        "sodium_mg": None,
        "estimated_calories_share": None,
        "status": None,
        "status_set_by": None,
        "counted": False,
    }


async def test_share_of_estimated_calories(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    await add_meal(app, kim, "2026-10-18T16:00:00+00:00", calories_kcal=300)
    await add_meal(
        app,
        kim,
        "2026-10-18T19:00:00+00:00",
        calories_kcal=200,
        source="from_conversation",
        estimated_fields=["protein_g"],
    )
    [day] = (await days(http, kim, "2026-10-18", "2026-10-18"))["days"]
    assert day["estimated_calories_share"] == 0.6


async def test_counted_days_follow_the_completeness_rule(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    for date in ("07", "08", "09"):
        await add_meal(app, kim, f"2026-10-{date}T18:00:00+00:00")

    async def mark(day: str, status: str) -> None:
        response = await http.put(
            f"/api/v1/days/2026-10-{day}/status", json={"status": status}, headers=kim.headers
        )
        assert response.status_code == 200

    await mark("08", "partial")
    body = await days(http, kim, "2026-10-07", "2026-10-10")
    assert body["counting_rule"] == "logged_days"
    assert [d["counted"] for d in body["days"]] == [True, False, True, False]
    assert body["days"][1]["status"] == "partial"
    assert body["days"][1]["status_set_by"] == "user"

    # Once a day is marked complete, only complete days with meals count.
    await mark("09", "complete")
    await mark("10", "complete")
    body = await days(http, kim, "2026-10-07", "2026-10-10")
    assert body["counting_rule"] == "complete_days"
    assert [d["counted"] for d in body["days"]] == [False, False, True, False]


async def test_invalid_ranges_are_rejected(http: httpx.AsyncClient, kim: Account) -> None:
    for params, expected in (
        ({"from": "2026-10-18", "to": "2026-10-17"}, 400),
        ({"from": "2025-01-01", "to": "2026-01-02"}, 400),
        ({"from": "2026-10-18"}, 422),
        ({}, 422),
    ):
        response = await http.get("/api/v1/days", params=params, headers=kim.headers)
        assert response.status_code == expected, params
    longest = {"from": "2025-01-01", "to": "2026-01-01"}
    response = await http.get("/api/v1/days", params=longest, headers=kim.headers)
    assert response.status_code == 200
    assert len(response.json()["days"]) == 366


async def test_summaries_only_include_the_users_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    mallory = await account(http, "mallory@example.com", "America/Los_Angeles")
    await add_meal(app, kim, "2026-10-18T18:00:00+00:00")
    await http.put(
        "/api/v1/days/2026-10-18/status", json={"status": "complete"}, headers=kim.headers
    )

    body = await days(http, mallory, "2026-10-18", "2026-10-18")
    assert body["counting_rule"] == "logged_days"
    assert body["days"][0]["meal_count"] == 0
    assert body["days"][0]["status"] is None
    params = {"from": "2026-10-18", "to": "2026-10-18"}
    assert (await http.get("/api/v1/days", params=params)).status_code == 401
