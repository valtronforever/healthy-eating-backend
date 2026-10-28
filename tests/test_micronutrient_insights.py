"""Vitamin and mineral insights over weeks (#36): averages, range days, sources, coverage."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from tests.test_meal_items import LUNCH, SUPPLEMENTS, call, food
from tests.test_meals_api import Account, account, add_meal
from tests.test_reference_group import settings

BERLIN = ZoneInfo("Europe/Berlin")
# Two past weeks, Monday 2026-03-02 to Sunday 2026-03-15 (UTC+1 in Berlin, no DST change).
FIRST, LAST = "2026-03-02", "2026-03-15"


def at(month: int, day: int, hour: int = 12) -> str:
    return datetime(2026, month, day, hour, tzinfo=BERLIN).isoformat()


async def iron(app: FastAPI, owner: Account, month: int, day: int, amount: float | None) -> None:
    values = None if amount is None else {"iron_mg": amount}
    await add_meal(app, owner, at(month, day), utc_offset_minutes=60, micronutrients=values)


async def insights(http: httpx.AsyncClient, owner: Account, **params: Any) -> dict[str, Any]:
    response = await http.get(
        "/api/v1/micronutrients/insights",
        params={"from": FIRST, "to": LAST} | params,
        headers=owner.headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_weekly_averages_and_days_in_the_range(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "ines.insights@example.com", "Europe/Berlin")
    await settings(http, owner, micronutrient_goals={"iron_mg": {"min": 10, "max": 20}})
    await iron(app, owner, 3, 2, 8)
    await iron(app, owner, 3, 3, 15)
    await iron(app, owner, 3, 4, 12)
    await iron(app, owner, 3, 4, 13)
    # Only one of two meals has values: left out of averages and range days.
    await iron(app, owner, 3, 5, 30)
    await iron(app, owner, 3, 5, None)
    await iron(app, owner, 3, 10, 14)
    await iron(app, owner, 2, 20, 9)

    body = await insights(http, owner)

    assert body["counting_rule"] == "logged_days"
    assert body["ranges"] == {"iron_mg": {"min": 10, "max": 20, "source": "personal"}}
    current = body["current"]
    assert (current["first_day"], current["last_day"]) == (FIRST, LAST)
    assert (current["counted_days"], current["meal_count"], current["meals_with_values"]) == (
        5,
        7,
        6,
    )
    assert list(current["nutrients"]) == ["iron_mg"]
    found = current["nutrients"]["iron_mg"]
    assert found["average"] == {"average": 15.5, "days": 4}  # 8, 15, 25 and 14
    assert found["range_days"] == {"below": 1, "within": 2, "above": 1}
    assert found["weeks"] == [
        {"first_day": "2026-03-02", "last_day": "2026-03-08", "average": 16.0, "days": 3},
        {"first_day": "2026-03-09", "last_day": "2026-03-15", "average": 14.0, "days": 1},
    ]
    # Meals without items have no sources.
    assert (found["food_sources"], found["label_sources"]) == ([], [])

    previous = body["previous"]
    assert (previous["first_day"], previous["last_day"]) == ("2026-02-16", "2026-03-01")
    assert previous["nutrients"]["iron_mg"]["average"] == {"average": 9.0, "days": 1}
    assert [week["average"] for week in previous["nutrients"]["iron_mg"]["weeks"]] == [9.0, None]


async def test_a_week_is_cut_to_the_period(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "jon.insights@example.com", "Europe/Berlin")
    await iron(app, owner, 3, 5, 6)

    body = await insights(http, owner, **{"from": "2026-03-05", "to": "2026-03-10"})

    found = body["current"]["nutrients"]["iron_mg"]
    assert [(week["first_day"], week["last_day"]) for week in found["weeks"]] == [
        ("2026-03-05", "2026-03-08"),
        ("2026-03-09", "2026-03-10"),
    ]
    # No life stage and no goal: no range, so no range days.
    assert body["ranges"] == {}
    assert found["range_days"] is None
    assert body["previous"]["first_day"] == "2026-02-27"


async def test_sources_list_foods_and_label_items_apart(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "kai.insights@example.com", "Europe/Berlin")
    await call(
        app,
        owner,
        "log_meal",
        {
            **LUNCH,
            "eaten_at": at(3, 3, 13),
            "items": [
                {"name": "boiled rice", "grams": 150},
                {"name": "salmon, baked", "grams": 120},
            ],
        },
    )
    await call(
        app,
        owner,
        "log_meal",
        {
            **SUPPLEMENTS,
            "eaten_at": at(3, 4, 8),
            "items": [
                {"name": "Vitamin D3 capsule", "label_values": {"vitamin_d_ug": 50}},
                {"name": "Boiled rice", "grams": 100},
            ],
        },
    )

    nutrients = (await insights(http, owner))["current"]["nutrients"]

    rice = await food(app, "Rice, cooked, NFS")
    salmon = await food(app, "Fish, salmon, baked or broiled")
    rice_iron = rice.nutrients["iron_mg"] * 2.5
    salmon_iron = salmon.nutrients["iron_mg"] * 1.2
    sources = nutrients["iron_mg"]["food_sources"]
    # Grouped ignoring case, named as written last, largest amount first.
    assert {source["name"] for source in sources} == {"Boiled rice", "salmon, baked"}
    by_name = {source["name"]: source for source in sources}
    assert by_name["Boiled rice"]["meals"] == 2
    assert by_name["Boiled rice"]["amount"] == pytest.approx(rice_iron, abs=0.01)
    assert by_name["Boiled rice"]["share"] == pytest.approx(
        rice_iron / (rice_iron + salmon_iron), abs=0.001
    )
    assert sources[0]["amount"] >= sources[1]["amount"]
    assert nutrients["iron_mg"]["label_sources"] == []

    vitamin_d = nutrients["vitamin_d_ug"]
    assert [(s["name"], s["amount"], s["meals"]) for s in vitamin_d["label_sources"]] == [
        ("Vitamin D3 capsule", 50, 1)
    ]
    label_share = vitamin_d["label_sources"][0]["share"]
    food_shares = sum(source["share"] for source in vitamin_d["food_sources"])
    assert label_share + food_shares == pytest.approx(1, abs=0.002)


async def test_only_the_nutrients_asked_for(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "lea.insights@example.com", "Europe/Berlin")
    await settings(http, owner, reference_group="women_31_50")
    await add_meal(
        app,
        owner,
        at(3, 3),
        utc_offset_minutes=60,
        micronutrients={"iron_mg": 9, "zinc_mg": 4},
    )

    body = await insights(http, owner, nutrient=["zinc_mg", "vitamin_c_mg"])

    assert set(body["ranges"]) == {"zinc_mg", "vitamin_c_mg"}
    # Vitamin C has a range but no values: left out of the period.
    assert list(body["current"]["nutrients"]) == ["zinc_mg"]


@pytest.mark.parametrize(
    ("first", "last"), [("2026-03-10", "2026-03-09"), ("2025-03-01", "2026-03-02")]
)
async def test_reversed_or_too_long_periods_are_refused(
    http: httpx.AsyncClient, first: str, last: str
) -> None:
    owner = await account(http, "mo.insights@example.com")

    response = await http.get(
        "/api/v1/micronutrients/insights",
        params={"from": first, "to": last},
        headers=owner.headers,
    )

    assert response.status_code == 400, response.text


async def test_other_users_meals_never_count(app: FastAPI, http: httpx.AsyncClient) -> None:
    one = await account(http, "ned.insights@example.com", "Europe/Berlin")
    two = await account(http, "ola.insights@example.com", "Europe/Berlin")
    await iron(app, one, 3, 3, 5)

    current = (await insights(http, two))["current"]

    assert (current["meal_count"], current["nutrients"]) == (0, {})


async def test_the_assistant_reads_the_same_insights(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "pia.insights@example.com", "Europe/Berlin")
    await settings(http, owner, micronutrient_goals={"iron_mg": {"min": 10}})
    await iron(app, owner, 3, 2, 8)
    await iron(app, owner, 3, 9, 12)

    result = await call(
        app,
        owner,
        "get_micronutrient_insights",
        {"first_day": FIRST, "last_day": LAST, "nutrients": ["iron_mg"]},
    )

    assert result["ranges"] == {"iron_mg": {"min": 10, "max": None, "source": "personal"}}
    found = result["current"]["nutrients"]["iron_mg"]
    assert (found["average"], found["days"]) == (10.0, 2)
    assert found["range_days"] == {"below": 1, "within": 1, "above": 0}
    assert [week["average"] for week in found["weeks"]] == [8.0, 12.0]
    assert (result["current"]["meal_count"], result["current"]["meals_with_values"]) == (2, 2)


async def test_the_assistant_gets_the_last_four_weeks_by_default(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "rio.insights@example.com", "Europe/Berlin")

    result = await call(app, owner, "get_micronutrient_insights", {})

    first = datetime.fromisoformat(result["current"]["first_day"])
    last = datetime.fromisoformat(result["current"]["last_day"])
    assert (last - first).days == 27
    assert result["current"]["nutrients"] == {}
