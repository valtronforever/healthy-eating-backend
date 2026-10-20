from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from healthy_eating.db.base import utcnow
from healthy_eating.services.integration import arrival_status
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, register_client, sign_in
from tests.test_meals_api import account, add_meal

MEAL: dict[str, Any] = {
    "dish_name": "Porridge",
    "source": "from_conversation",
    "calories_kcal": 350,
    "protein_g": 12,
    "fat_g": 8,
    "carbohydrates_g": 55,
}


async def tokens_for(http: httpx.AsyncClient, client_name: str, email: str) -> str:
    client = await register_client(http, name=client_name)
    token: str = (await sign_in(http, client, email=email))["access_token"]
    return token


async def log_meal(app: FastAPI, token: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async with mcp_client(app, token) as mcp:
        result = await mcp.call_tool("log_meal", arguments)
    content: dict[str, Any] | None = result.structured_content
    assert content is not None
    return content


async def test_meals_record_the_assistant_that_saved_them(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    claude = await tokens_for(http, "Claude", "nina@example.com")
    chatgpt = await tokens_for(http, "ChatGPT", "nina@example.com")
    saved = await log_meal(app, claude, MEAL)
    meal_id = saved["meal"]["meal_id"]

    meal = (await http.get(f"/api/v1/meals/{meal_id}", headers=bearer(claude))).json()
    assert meal["client_name"] == "Claude"
    listed = (await http.get("/api/v1/meals", headers=bearer(claude))).json()
    assert listed["meals"][0]["client_name"] == "Claude"

    # A correction from another assistant, or from the app, keeps the original assistant.
    await log_meal(app, chatgpt, MEAL | {"calories_kcal": 400, "replaces_meal_id": meal_id})
    edited = await http.patch(f"/api/v1/meals/{meal_id}", json={"fat_g": 9}, headers=bearer(claude))
    assert edited.json()["meal"]["client_name"] == "Claude"
    assert edited.json()["meal"]["calories_kcal"] == 400


async def test_integration_status_reports_the_last_meal_without_contents(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    token = await tokens_for(http, "Claude", "nina@example.com")
    await log_meal(app, token, MEAL)

    response = await http.get("/api/v1/integration-status", headers=bearer(token))

    assert response.status_code == 200
    status = response.json()
    assert set(status) == {
        "last_meal_saved_at",
        "last_client_name",
        "meals_today",
        "arrival_status",
    }
    assert status["last_client_name"] == "Claude"
    assert status["meals_today"] == 1
    saved_at = datetime.fromisoformat(status["last_meal_saved_at"])
    assert abs((utcnow() - saved_at).total_seconds()) < 60


async def test_meals_today_uses_the_users_local_day(app: FastAPI, http: httpx.AsyncClient) -> None:
    lee = await account(http, "lee@example.com", "Pacific/Kiritimati")
    await add_meal(app, lee, (utcnow() - timedelta(days=2)).isoformat())
    await add_meal(app, lee, utcnow().isoformat())

    status = (await http.get("/api/v1/integration-status", headers=lee.headers)).json()

    assert status["meals_today"] == 1
    # Meals added outside MCP have no assistant.
    assert status["last_client_name"] is None
    assert status["last_meal_saved_at"] is not None


async def test_integration_status_for_a_new_user_and_isolation(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    token = await tokens_for(http, "Claude", "nina@example.com")
    await log_meal(app, token, MEAL)
    other = await account(http, "omar@example.com")

    status = (await http.get("/api/v1/integration-status", headers=other.headers)).json()

    assert status == {
        "last_meal_saved_at": None,
        "last_client_name": None,
        "meals_today": 0,
        "arrival_status": "unknown",
    }
    assert (await http.get("/api/v1/integration-status")).status_code == 401


# Whether meals still arrive (frozen clock: `now` is passed in)

BERLIN = "Europe/Berlin"


def at(day: int, hour: int, minute: int = 0, zone: str = BERLIN) -> datetime:
    """A moment on 2026-03-<day> in the given zone (no DST change in this range)."""
    return datetime(2026, 3, day, hour, minute, tzinfo=ZoneInfo(zone))


def daily_saves(first_day: int, last_day: int, hour: int, zone: str = BERLIN) -> list[datetime]:
    return [at(day, hour, zone=zone) for day in range(first_day, last_day + 1)]


def test_no_status_without_a_usual_pattern() -> None:
    now = at(30, 15)
    assert arrival_status([], BERLIN, now=now) == "unknown"
    # Meals on 22 of the last 28 days are not regular enough.
    assert arrival_status(daily_saves(8, 29, 9), BERLIN, now=now) == "unknown"
    # An import of older meals arrives in one burst: one day, not a pattern.
    burst = [at(29, 20) + timedelta(seconds=n) for n in range(60)]
    assert arrival_status(burst, BERLIN, now=now) == "unknown"


def test_ok_while_meals_arrive_as_usual() -> None:
    saves = daily_saves(2, 29, 9)  # 28 days, first meal at 09:00
    assert arrival_status([*saves, at(30, 7)], BERLIN, now=at(30, 23)) == "ok"
    # Nothing yet today, but the usual time (09:00 plus two hours) has not passed.
    assert arrival_status(saves, BERLIN, now=at(30, 10, 59)) == "ok"


def test_quiet_when_meals_stop_arriving() -> None:
    saves = daily_saves(2, 29, 9)
    assert arrival_status(saves, BERLIN, now=at(30, 11)) == "quiet"
    # Nothing all of yesterday: quiet even early in the morning.
    assert arrival_status(saves[:-1], BERLIN, now=at(30, 6)) == "quiet"


def test_usual_time_allows_for_later_days() -> None:
    # Usually 08:00, sometimes 13:00: 80% of days have had a meal by 13:00.
    saves = [*daily_saves(2, 21, 8), *daily_saves(22, 29, 13)]
    assert arrival_status(saves, BERLIN, now=at(30, 14, 59)) == "ok"
    assert arrival_status(saves, BERLIN, now=at(30, 15)) == "quiet"


def test_days_and_times_are_the_users_local_ones() -> None:
    tokyo = "Asia/Tokyo"
    saves = daily_saves(2, 29, 8, zone=tokyo)
    # 10:30 in Tokyo is 01:30 UTC, still "yesterday" there: the local day decides.
    assert arrival_status(saves, tokyo, now=at(30, 10, 30, zone=tokyo)) == "quiet"
    assert arrival_status(saves, tokyo, now=at(30, 9, 59, zone=tokyo)) == "ok"
    # Without a time zone, days and times are in UTC.
    utc_saves = [moment.astimezone(UTC) for moment in daily_saves(2, 29, 9, zone="UTC")]
    assert arrival_status(utc_saves, None, now=at(30, 10, zone="UTC")) == "ok"


async def test_integration_status_reports_quiet_meals(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    regular = await account(http, "rita@example.com", BERLIN)
    stopped = await account(http, "sam@example.com", BERLIN)
    now = utcnow()
    for days_ago in range(28):
        await add_meal(app, regular, now.isoformat(), created_at=now - timedelta(days=days_ago))
        # The other account's meals stopped arriving two days ago.
        moment = now - timedelta(days=days_ago + 2)
        await add_meal(app, stopped, moment.isoformat(), created_at=moment)

    ok = (await http.get("/api/v1/integration-status", headers=regular.headers)).json()
    quiet = (await http.get("/api/v1/integration-status", headers=stopped.headers)).json()

    assert ok["arrival_status"] == "ok"
    assert quiet["arrival_status"] == "quiet"
