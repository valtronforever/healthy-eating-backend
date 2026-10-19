from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.db.base import utcnow
from healthy_eating.services.days import local_today
from tests.test_meals_api import Account, account, add_meal

TIME_ZONE = "Asia/Tokyo"


@pytest.fixture
async def kim(http: httpx.AsyncClient) -> Account:
    return await account(http, "kim@example.com", TIME_ZONE)


def today() -> date:
    return local_today(TIME_ZONE, utcnow())


async def meal_on(app: FastAPI, owner: Account, days_ago: int, kcal: float) -> None:
    eaten_at = datetime.combine(
        today() - timedelta(days=days_ago), time(0, 30), ZoneInfo(TIME_ZONE)
    )
    await add_meal(app, owner, eaten_at.isoformat(), calories_kcal=kcal)


async def get_trends(http: httpx.AsyncClient, owner: Account, **params: Any) -> dict[str, Any]:
    response = await http.get("/api/v1/trends", params=params, headers=owner.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_averages_per_counted_day_compared_with_the_previous_window(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    await meal_on(app, kim, 1, 500)
    await meal_on(app, kim, 1, 300)
    await meal_on(app, kim, 3, 600)
    await meal_on(app, kim, 0, 400)  # today, not marked complete: does not count yet
    await meal_on(app, kim, 8, 1000)

    body = await get_trends(http, kim, window=7)

    assert body["window_days"] == 7
    assert body["counting_rule"] == "logged_days"
    current, previous = body["current"], body["previous"]
    assert current["first_day"] == (today() - timedelta(days=6)).isoformat()
    assert current["last_day"] == today().isoformat()
    assert previous["last_day"] == (today() - timedelta(days=7)).isoformat()
    assert (current["counted_days"], current["logged_days"]) == (2, 3)
    assert current["averages"]["calories_kcal"] == 700
    assert current["averages"]["protein_g"] == 9  # (6 + 6 + 6) / 2
    assert current["estimated_calories_share"] == 1
    assert (previous["counted_days"], previous["averages"]["calories_kcal"]) == (1, 1000)
    assert body["change"]["calories_kcal"] == -300


async def test_today_counts_once_marked_complete(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    await meal_on(app, kim, 0, 400)
    await meal_on(app, kim, 2, 900)
    response = await http.put(
        f"/api/v1/days/{today().isoformat()}/status",
        json={"status": "complete"},
        headers=kim.headers,
    )
    assert response.status_code == 200

    body = await get_trends(http, kim)

    # A complete mark switches to counting complete days only.
    assert body["counting_rule"] == "complete_days"
    assert body["current"]["counted_days"] == 1
    assert body["current"]["averages"]["calories_kcal"] == 400
    assert body["previous"]["averages"] is None
    assert body["change"] is None


async def test_no_meals_means_no_averages(http: httpx.AsyncClient, kim: Account) -> None:
    body = await get_trends(http, kim, window=90)
    assert body["current"]["counted_days"] == 0
    assert body["current"]["averages"] is None
    assert body["current"]["estimated_calories_share"] is None
    assert body["change"] is None
    first = date.fromisoformat(body["previous"]["first_day"])
    assert (today() - first).days == 179


async def test_trends_only_use_the_users_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    await meal_on(app, kim, 1, 500)
    mallory = await account(http, "mallory@example.com", TIME_ZONE)
    assert (await get_trends(http, mallory))["current"]["counted_days"] == 0


async def test_invalid_windows_are_rejected(http: httpx.AsyncClient, kim: Account) -> None:
    for window in ("14", "week", "0"):
        response = await http.get("/api/v1/trends", params={"window": window}, headers=kim.headers)
        assert response.status_code == 422
    assert (await http.get("/api/v1/trends")).status_code == 401
