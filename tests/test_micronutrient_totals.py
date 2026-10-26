"""Vitamins and minerals per day and in Trends (healthy-eating-backend#25)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from tests.test_meals_api import Account, account, add_meal


def days_ago(days: int, hour: int = 12) -> str:
    moment = datetime.now(UTC).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (moment - timedelta(days=days)).isoformat()


async def day(http: httpx.AsyncClient, owner: Account, date: str) -> dict[str, Any]:
    response = await http.get(
        "/api/v1/days", params={"from": date, "to": date}, headers=owner.headers
    )
    assert response.status_code == 200, response.text
    [summary] = response.json()["days"]
    result: dict[str, Any] = summary
    return result


async def test_day_totals_say_how_many_meals_they_cover(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "abe.micro@example.com")
    when = days_ago(1)
    await add_meal(app, owner, when, micronutrients={"iron_mg": 1.25, "vitamin_c_mg": 30})
    await add_meal(app, owner, when, micronutrients={"iron_mg": 2.5})
    await add_meal(app, owner, when)  # no items

    summary = await day(http, owner, when[:10])

    assert summary["meal_count"] == 3
    assert summary["micronutrients"] == {
        "iron_mg": {"amount": 3.75, "meal_count": 2},
        "vitamin_c_mg": {"amount": 30, "meal_count": 1},
    }


async def test_a_day_without_items_has_no_micronutrients(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "bo.micro@example.com")
    when = days_ago(1)
    await add_meal(app, owner, when)

    assert (await day(http, owner, when[:10]))["micronutrients"] == {}


async def test_trends_average_only_days_where_every_meal_has_a_value(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "cia.micro@example.com")
    # Fully covered: both meals have iron.
    await add_meal(app, owner, days_ago(1, 9), micronutrients={"iron_mg": 4})
    await add_meal(app, owner, days_ago(1, 13), micronutrients={"iron_mg": 6})
    # Fully covered too.
    await add_meal(app, owner, days_ago(2), micronutrients={"iron_mg": 8, "zinc_mg": 3})
    # Only one of two meals has items: left out of the iron average.
    await add_meal(app, owner, days_ago(3, 9), micronutrients={"iron_mg": 1})
    await add_meal(app, owner, days_ago(3, 13))

    response = await http.get("/api/v1/trends", params={"window": 7}, headers=owner.headers)

    assert response.status_code == 200, response.text
    current = response.json()["current"]
    assert current["counted_days"] == 3
    assert current["micronutrient_averages"] == {
        "iron_mg": {"average": 9.0, "days": 2},
        "zinc_mg": {"average": 3.0, "days": 1},
    }
    assert response.json()["previous"]["micronutrient_averages"] == {}


async def test_other_users_meals_never_count(app: FastAPI, http: httpx.AsyncClient) -> None:
    one = await account(http, "dee.micro@example.com")
    two = await account(http, "eva.micro@example.com")
    when = days_ago(1)
    await add_meal(app, one, when, micronutrients={"iron_mg": 5})

    assert (await day(http, two, when[:10]))["micronutrients"] == {}
