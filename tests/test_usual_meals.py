import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI

from healthy_eating.services.usual_meals import dish_key
from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account, add_meal


def days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


async def usual(http: httpx.AsyncClient, owner: Account) -> list[dict[str, Any]]:
    response = await http.get("/api/v1/usual-meals", headers=owner.headers)
    assert response.status_code == 200, response.text
    meals: list[dict[str, Any]] = response.json()["meals"]
    return meals


def test_names_differing_in_case_and_spacing_are_one_dish() -> None:
    assert dish_key("  Oatmeal   with Berries ") == dish_key("oatmeal with berries")
    assert dish_key("Oatmeal") != dish_key("Oatmeal with berries")


async def test_usual_meals_are_dishes_eaten_twice_most_often_first(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "uma.usual@example.com")
    for day in (1, 2, 3):
        await add_meal(app, owner, days_ago(day), dish_name="Oatmeal")
    latest_soup = await add_meal(app, owner, days_ago(1.5), dish_name="soup", calories_kcal=310)
    await add_meal(app, owner, days_ago(4), dish_name="Soup ")
    await add_meal(app, owner, days_ago(1), dish_name="Pizza")  # once: not usual
    await add_meal(app, owner, days_ago(100), dish_name="Pizza")  # too long ago

    meals = await usual(http, owner)

    assert [(m["meal"]["dish_name"], m["times"]) for m in meals] == [("Oatmeal", 3), ("soup", 2)]
    # The latest meal of a dish is the template, so a correction counts.
    assert meals[1]["meal"]["id"] == str(latest_soup)
    assert meals[1]["meal"]["calories_kcal"] == 310


async def test_relog_copies_values_and_provenance_with_a_new_time(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "vic.usual@example.com", "Europe/Berlin")
    original = await add_meal(
        app,
        owner,
        days_ago(1),
        dish_name="Oatmeal with berries",
        source="from_conversation",
        estimated_fields=["fat_g"],
        confidence="high",
        portion_grams=250,
        fiber_g=6,
    )
    new_id = uuid.uuid4()

    response = await http.post(
        f"/api/v1/meals/{original}/relog",
        json={"id": str(new_id), "eaten_at": "2026-10-23T08:10:00"},
        headers=owner.headers,
    )

    assert response.status_code == 201, response.text
    meal = response.json()
    assert meal["id"] == str(new_id)
    assert meal["dish_name"] == "Oatmeal with berries"
    assert meal["source"] == "from_conversation"
    assert meal["estimated_fields"] == ["fat_g"]
    assert meal["confidence"] == "high"
    assert meal["portion_grams"] == 250
    assert meal["fiber_g"] == 6
    assert meal["calories_kcal"] == 200
    # A time without an offset is read in the user's time zone, like every other meal.
    assert meal["eaten_at"] == "2026-10-23T08:10:00+02:00"
    assert meal["client_name"] is not None


async def test_relog_with_the_same_id_saves_once(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "wes.usual@example.com")
    original = await add_meal(app, owner, days_ago(1))
    body = {"id": str(uuid.uuid4())}

    first = await http.post(f"/api/v1/meals/{original}/relog", json=body, headers=owner.headers)
    again = await http.post(f"/api/v1/meals/{original}/relog", json=body, headers=owner.headers)

    assert first.json()["id"] == again.json()["id"] == body["id"]
    listed = (await http.get("/api/v1/meals", headers=owner.headers)).json()["meals"]
    assert len(listed) == 2


async def test_users_cannot_relog_or_see_other_users_meals(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    one = await account(http, "amy.usual@example.com")
    two = await account(http, "bob.usual@example.com")
    for day in (1, 2):
        await add_meal(app, one, days_ago(day), dish_name="Oatmeal")
    theirs = await add_meal(app, one, days_ago(3))

    relog = await http.post(f"/api/v1/meals/{theirs}/relog", json={}, headers=two.headers)
    taken = await add_meal(app, two, days_ago(1))
    reuse = await http.post(
        f"/api/v1/meals/{taken}/relog", json={"id": str(theirs)}, headers=two.headers
    )

    assert relog.status_code == 404
    assert reuse.status_code == 409
    assert await usual(http, two) == []


async def test_mcp_lists_usual_meals_and_relogs_one(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "cal.usual@example.com")
    for day in (1, 2):
        await add_meal(app, owner, days_ago(day), dish_name="Oatmeal")

    async with mcp_client(app, owner.token) as mcp:
        listed = (await mcp.call_tool("list_usual_meals", {})).structured_content
        [item] = listed["meals"]
        assert item["dish_name"] == "Oatmeal"
        assert item["times"] == 2
        request_id = str(uuid.uuid4())
        arguments = {"meal_id": item["meal_id"], "request_id": request_id}
        saved = (await mcp.call_tool("relog_meal", arguments)).structured_content
        retried = (await mcp.call_tool("relog_meal", arguments)).structured_content
        missing = await mcp.call_tool(
            "relog_meal", {"meal_id": str(uuid.uuid4())}, raise_on_error=False
        )

    assert saved["saved_to_healthy_eating"] is True
    assert saved["user_message"].startswith("Saved to HealthyEating again: Oatmeal, 200 kcal")
    assert saved["meal_id"] == retried["meal_id"] == request_id
    assert missing.is_error
    assert len(await usual(http, owner)) == 1
    assert (await usual(http, owner))[0]["times"] == 3
