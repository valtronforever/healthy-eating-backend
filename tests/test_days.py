from datetime import date, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import DayStatusRecord
from healthy_eating.services.days import counts_complete_days_only, day_counts, local_today
from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account


@pytest.fixture
async def kim(http: httpx.AsyncClient) -> Account:
    return await account(http, "kim@example.com", "Pacific/Kiritimati")


async def stored(app: FastAPI) -> list[tuple[date, str, str]]:
    db: Database = app.state.db
    async with db.session() as session:
        records = await session.scalars(
            select(DayStatusRecord).order_by(DayStatusRecord.local_date)
        )
        return [(r.local_date, r.status, r.set_by) for r in records]


def test_averages_count_complete_days_once_the_user_marks_any() -> None:
    assert day_counts("complete", 3, complete_days_only=True)
    assert not day_counts(None, 3, complete_days_only=True)
    assert not day_counts("partial", 3, complete_days_only=True)


def test_averages_count_logged_days_when_the_user_never_marks_complete() -> None:
    assert day_counts(None, 1, complete_days_only=False)
    assert day_counts("complete", 1, complete_days_only=False)
    assert not day_counts("partial", 3, complete_days_only=False)


def test_days_without_meals_never_count() -> None:
    assert not day_counts("complete", 0, complete_days_only=True)
    assert not day_counts(None, 0, complete_days_only=False)


async def test_mark_a_day_and_change_the_mark(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    response = await http.put(
        "/api/v1/days/2026-10-18/status", json={"status": "complete"}, headers=kim.headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["date"] == "2026-10-18"
    assert response.json()["status"] == "complete"
    assert response.json()["set_by"] == "user"

    response = await http.put(
        "/api/v1/days/2026-10-18/status", json={"status": "partial"}, headers=kim.headers
    )
    assert response.json()["status"] == "partial"
    assert await stored(app) == [(date(2026, 10, 18), "partial", "user")]


async def test_remove_a_mark(app: FastAPI, http: httpx.AsyncClient, kim: Account) -> None:
    url = "/api/v1/days/2026-10-18/status"
    await http.put(url, json={"status": "complete"}, headers=kim.headers)
    assert (await http.delete(url, headers=kim.headers)).status_code == 204
    assert await stored(app) == []
    assert (await http.delete(url, headers=kim.headers)).status_code == 404


async def test_days_that_have_not_started_cannot_be_marked(
    http: httpx.AsyncClient, kim: Account
) -> None:
    today = local_today("Pacific/Kiritimati", utcnow())
    for offset, expected in ((1, 200), (2, 422)):
        day = (today + timedelta(days=offset)).isoformat()
        response = await http.put(
            f"/api/v1/days/{day}/status", json={"status": "complete"}, headers=kim.headers
        )
        assert response.status_code == expected


@pytest.mark.parametrize(
    ("day", "body"),
    [
        ("2026-10-18", {"status": "done"}),
        ("2026-10-18", {}),
        ("2026-10-18", {"status": "complete", "set_by": "assistant"}),
        ("18.10.2026", {"status": "complete"}),
    ],
)
async def test_invalid_marks_are_rejected(
    http: httpx.AsyncClient, kim: Account, day: str, body: dict[str, str]
) -> None:
    response = await http.put(f"/api/v1/days/{day}/status", json=body, headers=kim.headers)
    assert response.status_code == 422


async def test_marks_belong_to_one_user(
    app: FastAPI, http: httpx.AsyncClient, kim: Account
) -> None:
    mallory = await account(http, "mallory@example.com")
    url = "/api/v1/days/2026-10-18/status"
    await http.put(url, json={"status": "complete"}, headers=kim.headers)

    assert (await http.delete(url, headers=mallory.headers)).status_code == 404
    assert len(await stored(app)) == 1
    db: Database = app.state.db
    async with db.session() as session:
        assert await counts_complete_days_only(session, kim.user_id)
        assert not await counts_complete_days_only(session, mallory.user_id)
    assert (await http.put(url, json={"status": "partial"})).status_code == 401


async def test_assistant_marks_today_in_the_users_time_zone(app: FastAPI, kim: Account) -> None:
    async with mcp_client(app, kim.token) as mcp:
        result = await mcp.call_tool("set_day_status", {"status": "complete"})
        earlier = await mcp.call_tool("set_day_status", {"status": "partial", "day": "2026-10-17"})
        future = await mcp.call_tool(
            "set_day_status", {"status": "complete", "day": "2999-01-01"}, raise_on_error=False
        )

    today = local_today("Pacific/Kiritimati", utcnow())
    assert result.structured_content is not None
    assert result.structured_content["date"] == today.isoformat()
    assert result.structured_content["user_message"] == (
        f"Marked {today.isoformat()} as complete in HealthyEating."
    )
    assert earlier.structured_content is not None
    assert earlier.structured_content["status"] == "partial"
    assert future.is_error
    assert "Not saved to HealthyEating" in str(future.content)
    assert await stored(app) == [
        (date(2026, 10, 17), "partial", "assistant"),
        (today, "complete", "assistant"),
    ]
