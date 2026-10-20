from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from healthy_eating.db import Database
from healthy_eating.services.weekly_review import weekly_review
from tests.test_meals_api import Account, account, add_meal

BERLIN = ZoneInfo("Europe/Berlin")
# A past week, Monday 2026-03-02 to Sunday 2026-03-08 (UTC+1 in Berlin, no DST change).
MONDAY = "2026-03-02"


async def meal(
    app: FastAPI, owner: Account, day: int, hour: int, minute: int = 0, **overrides: Any
) -> None:
    eaten_at = datetime(2026, 3, day, hour, minute, tzinfo=BERLIN).isoformat()
    values = {"calories_kcal": 500.0} | overrides
    await add_meal(app, owner, eaten_at, utc_offset_minutes=60, **values)


async def week_with_meals(app: FastAPI, http: httpx.AsyncClient) -> Account:
    """Mon two meals (the person's own numbers), Tue a late one, Wed nothing, Thu partial,
    Fri nothing, Sat one (low confidence), Sun one at 02:00."""
    owner = await account(http, "wren@example.com", "Europe/Berlin")
    own: dict[str, Any] = {"source": "from_conversation", "estimated_fields": []}
    await meal(app, owner, 2, 8, protein_g=30.0, **own)
    await meal(app, owner, 2, 13, protein_g=12.0, **own)
    await meal(app, owner, 3, 22, 30)
    await meal(app, owner, 5, 12)
    await meal(app, owner, 7, 12, protein_g=45.0, confidence="low")
    await meal(app, owner, 8, 2)
    response = await http.put(
        "/api/v1/days/2026-03-05/status", json={"status": "partial"}, headers=owner.headers
    )
    assert response.status_code == 200, response.text
    return owner


async def review(http: httpx.AsyncClient, owner: Account, week: str) -> dict[str, Any]:
    response = await http.get("/api/v1/weekly-review", params={"week": week}, headers=owner.headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_a_week_in_facts(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await week_with_meals(app, http)

    # Any day of the week gives the same week.
    result = await review(http, owner, "2026-03-06")

    assert (result["first_day"], result["last_day"]) == (MONDAY, "2026-03-08")
    assert result["counting_rule"] == "logged_days"
    assert (result["logged_days"], result["complete_days"], result["counted_days"]) == (5, 0, 4)
    # Mon 1000, Tue 500, Sat 500, Sun 500 kcal: the partial Thursday is left out.
    assert result["averages"]["calories_kcal"] == 625
    assert result["left_out"] == [
        {"local_date": "2026-03-04", "reason": "not_logged"},
        {"local_date": "2026-03-05", "reason": "partial"},
        {"local_date": "2026-03-06", "reason": "not_logged"},
    ]
    assert result["weekdays"]["counted_days"] == 2
    assert result["weekdays"]["averages"]["calories_kcal"] == 750
    assert result["weekend"]["counted_days"] == 2
    assert result["weekend"]["averages"]["calories_kcal"] == 500

    # Per-meal facts use all six meals, the partial day's too.
    assert result["meal_count"] == 6
    assert result["protein_per_meal_g"] == 17.5  # (30 + 12 + 6 + 6 + 45 + 6) / 6
    assert [bucket["meals"] for bucket in result["protein_buckets"]] == [3, 1, 0, 1, 1]
    assert result["protein_buckets"][-1] == {"min_g": 40, "max_g": None, "meals": 1}
    assert result["late_meals"] == 2  # Tuesday 22:30 and Sunday 02:00
    assert result["estimated_values_share"] == 0.667  # 16 of 24 values
    assert result["low_confidence_meals"] == 1


async def test_complete_days_only_once_one_is_marked(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await week_with_meals(app, http)
    await http.put(
        "/api/v1/days/2026-03-02/status", json={"status": "complete"}, headers=owner.headers
    )

    result = await review(http, owner, MONDAY)

    assert result["counting_rule"] == "complete_days"
    assert (result["complete_days"], result["counted_days"]) == (1, 1)
    assert result["averages"]["calories_kcal"] == 1000
    reasons = {day["local_date"]: day["reason"] for day in result["left_out"]}
    assert reasons["2026-03-03"] == "not_marked_complete"
    assert result["weekend"] == {"counted_days": 0, "averages": None}


async def test_this_week_leaves_out_today_and_later_days(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await week_with_meals(app, http)
    db: Database = app.state.db
    wednesday_noon = datetime(2026, 3, 4, 12, tzinfo=BERLIN)

    async with db.session() as session:
        result = await weekly_review(session, owner.user_id, None, now=wednesday_noon)

    assert (result.first_day, result.last_day) == (date(2026, 3, 2), date(2026, 3, 8))
    reasons = {day.local_date.day: day.reason for day in result.left_out}
    assert reasons == {4: "in_progress", 5: "future", 6: "future", 7: "future", 8: "future"}
    assert result.counted_days == 2


async def test_an_empty_week_and_other_accounts(app: FastAPI, http: httpx.AsyncClient) -> None:
    await week_with_meals(app, http)
    other = await account(http, "xia@example.com", "Europe/Berlin")

    result = await review(http, other, MONDAY)

    assert result["meal_count"] == 0
    assert result["averages"] is None
    assert result["protein_per_meal_g"] is None
    assert result["estimated_values_share"] is None
    assert len(result["left_out"]) == 7
    assert (await http.get("/api/v1/weekly-review")).status_code == 401


async def test_default_is_the_current_week(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "yan@example.com", "Europe/Berlin")

    response = await http.get("/api/v1/weekly-review", headers=owner.headers)

    result = response.json()
    first = date.fromisoformat(result["first_day"])
    assert first.weekday() == 0
    assert any(day["reason"] == "in_progress" for day in result["left_out"])
