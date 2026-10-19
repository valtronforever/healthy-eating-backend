from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.db.base import utcnow
from healthy_eating.services.days import local_today
from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account, add_meal

TIME_ZONE = "Europe/Lisbon"


@pytest.fixture
async def kim(http: httpx.AsyncClient) -> Account:
    return await account(http, "kim@example.com", TIME_ZONE)


async def meal_on(app: FastAPI, owner: Account, days_ago: int, name: str, kcal: float) -> str:
    day = local_today(TIME_ZONE, utcnow()) - timedelta(days=days_ago)
    eaten_at = datetime.combine(day, time(0, 30), ZoneInfo(TIME_ZONE))
    return str(await add_meal(app, owner, eaten_at.isoformat(), dish_name=name, calories_kcal=kcal))


async def call(
    app: FastAPI, owner: Account, tool: str, arguments: dict[str, Any] | None = None
) -> Any:
    async with mcp_client(app, owner.token) as mcp:
        return await mcp.call_tool(tool, arguments or {}, raise_on_error=False)


async def test_read_tools_are_annotated(app: FastAPI, kim: Account) -> None:
    async with mcp_client(app, kim.token) as mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("list_meals", "get_daily_summary"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
    delete = tools["delete_meal"].annotations
    assert delete is not None
    assert delete.destructive_hint is True


async def test_list_meals_defaults_to_today(app: FastAPI, kim: Account) -> None:
    today_id = await meal_on(app, kim, 0, "Soup", 300)
    await meal_on(app, kim, 1, "Pasta", 700)

    result = await call(app, kim, "list_meals")

    content = result.structured_content
    assert [meal["dish_name"] for meal in content["meals"]] == ["Soup"]
    assert content["meals"][0]["meal_id"] == today_id
    assert content["first_day"] == content["last_day"]
    assert content["more"] is False

    yesterday = (local_today(TIME_ZONE, utcnow()) - timedelta(days=1)).isoformat()
    result = await call(app, kim, "list_meals", {"first_day": yesterday})
    assert [meal["dish_name"] for meal in result.structured_content["meals"]] == ["Pasta"]


async def test_daily_summary_for_today_and_a_range(app: FastAPI, kim: Account) -> None:
    await meal_on(app, kim, 0, "Soup", 300)
    await meal_on(app, kim, 0, "Bread", 150)
    await meal_on(app, kim, 2, "Pasta", 700)

    today = (await call(app, kim, "get_daily_summary")).structured_content
    assert [(d["meal_count"], d["calories_kcal"]) for d in today["days"]] == [(2, 450)]

    first = (local_today(TIME_ZONE, utcnow()) - timedelta(days=2)).isoformat()
    week = (await call(app, kim, "get_daily_summary", {"first_day": first})).structured_content
    # Only first_day given: just that day.
    assert [d["calories_kcal"] for d in week["days"]] == [700]
    last = local_today(TIME_ZONE, utcnow()).isoformat()
    span = await call(app, kim, "get_daily_summary", {"first_day": first, "last_day": last})
    assert [d["calories_kcal"] for d in span.structured_content["days"]] == [700, 0, 450]


@pytest.mark.parametrize("tool", ["list_meals", "get_daily_summary"])
async def test_read_tools_reject_bad_ranges(app: FastAPI, kim: Account, tool: str) -> None:
    for arguments in (
        {"first_day": "2026-10-10", "last_day": "2026-10-01"},
        {"first_day": "2026-01-01", "last_day": "2026-03-01"},
    ):
        result = await call(app, kim, tool, arguments)
        assert result.is_error


async def test_delete_meal(app: FastAPI, http: httpx.AsyncClient, kim: Account) -> None:
    first = await meal_on(app, kim, 0, "Oatmeal", 300)
    await meal_on(app, kim, 0, "Oatmeal", 300)

    result = await call(app, kim, "delete_meal", {"meal_id": first})

    assert result.structured_content == {
        "deleted_from_healthy_eating": True,
        "user_message": "Deleted from HealthyEating: Oatmeal.",
    }
    remaining = (await call(app, kim, "list_meals")).structured_content["meals"]
    assert [meal["meal_id"] for meal in remaining] != [first]
    assert len(remaining) == 1
    again = await call(app, kim, "delete_meal", {"meal_id": first})
    assert again.is_error
    assert "Not deleted from HealthyEating" in str(again.content)


async def test_tools_only_see_the_users_meals(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    kims_meal = await meal_on(app, kim, 0, "Soup", 300)
    mallory = await account(http, "mallory@example.com", TIME_ZONE)

    assert (await call(app, mallory, "list_meals")).structured_content["meals"] == []
    summary = (await call(app, mallory, "get_daily_summary")).structured_content
    assert summary["days"][0]["meal_count"] == 0
    assert (await call(app, mallory, "delete_meal", {"meal_id": kims_meal})).is_error
    assert len((await call(app, kim, "list_meals")).structured_content["meals"]) == 1
