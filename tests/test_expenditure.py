from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.db.base import utcnow
from healthy_eating.services.expenditure import KCAL_PER_KG, Expenditure, estimate
from healthy_eating.services.weights import TrendPoint, daily_trend
from tests.test_meals_api import account, add_meal

FIRST = date(2026, 3, 1)
LAST = FIRST + timedelta(days=27)


def trend(kg_per_day: float, days: int = 60, every: int = 1) -> list[TrendPoint]:
    """A trend from weigh-ins every `every` days, ending on LAST."""
    start = LAST - timedelta(days=days - 1)
    weights = [(start + timedelta(days=i), 80 + kg_per_day * i) for i in range(0, days, every)]
    return daily_trend(weights)


def run(calories: list[float], points: list[TrendPoint], weigh_ins: int = 28) -> Expenditure:
    return estimate(
        first_day=FIRST,
        last_day=LAST,
        counting_rule="logged_days",
        counted_calories=calories,
        weigh_in_count=weigh_ins,
        trend=points,
    )


def test_a_steady_loss_means_using_more_than_the_log_says() -> None:
    # 0.5 kg less per week on 2000 kcal a day: about 2000 + 0.5 x 7700 / 7 = 2550 kcal.
    result = run([2000] * 28, trend(-0.5 / 7))
    assert result.maintenance_kcal == pytest.approx(2000 + 0.5 * KCAL_PER_KG / 7, abs=20)
    assert result.maintenance_kcal is not None and result.maintenance_kcal % 10 == 0
    assert result.confidence == "high"
    assert result.reason is None


def test_a_stable_weight_means_maintaining_at_the_logged_intake() -> None:
    result = run([2300] * 20, trend(0))
    assert result.maintenance_kcal == 2300
    assert result.trend_change_kg == 0


def test_a_gain_means_using_less() -> None:
    result = run([2500] * 28, trend(0.25 / 7))
    assert result.maintenance_kcal is not None
    assert result.maintenance_kcal < 2500


def test_no_estimate_without_enough_logged_days() -> None:
    result = run([2000] * 13, trend(0))
    assert result.maintenance_kcal is None
    assert result.confidence is None
    assert result.reason == "not_enough_logged_days"
    assert result.average_intake_kcal == 2000


def test_no_estimate_without_enough_weigh_ins() -> None:
    sparse = trend(0, days=28, every=6)  # five weigh-in days
    result = run([2000] * 28, sparse, weigh_ins=5)
    assert result.reason == "not_enough_weigh_ins"
    assert run([2000] * 28, [], weigh_ins=0).reason == "not_enough_weigh_ins"


def test_confidence_grows_with_data() -> None:
    assert run([2000] * 28, trend(0)).confidence == "high"
    assert run([2000] * 19, trend(0, every=2)).confidence == "medium"
    assert run([2000] * 15, trend(0, every=3)).confidence == "low"


async def test_expenditure_from_stored_meals_and_weigh_ins(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "eve@example.com", "Europe/Berlin")
    now = utcnow()
    for days_ago in range(1, 29):
        await add_meal(
            app, owner, (now - timedelta(days=days_ago)).isoformat(), calories_kcal=2000.0
        )
    entries = [
        {
            "weight_kg": round(82 - 0.5 / 7 * (60 - days_ago), 3),
            "measured_at": (now - timedelta(days=days_ago)).astimezone(UTC).isoformat(),
        }
        for days_ago in range(60, 0, -1)
    ]
    added = await http.post("/api/v1/weights", json={"entries": entries}, headers=owner.headers)
    assert added.status_code == 201, added.text

    result = (await http.get("/api/v1/expenditure", headers=owner.headers)).json()

    # Today is not complete, so the 27 earlier days of the 28-day window count.
    assert result["counted_days"] == 27
    assert result["average_intake_kcal"] == 2000
    assert result["maintenance_kcal"] == pytest.approx(2550, abs=30)
    assert result["confidence"] == "high"
    assert result["reason"] is None


async def test_expenditure_for_a_new_account_and_isolation(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "fay@example.com")
    await add_meal(app, owner, datetime.now(UTC).isoformat())
    other = await account(http, "gus@example.com")

    result = (await http.get("/api/v1/expenditure", headers=other.headers)).json()

    assert result["counted_days"] == 0
    assert result["maintenance_kcal"] is None
    assert result["reason"] == "not_enough_logged_days"
    assert (await http.get("/api/v1/expenditure")).status_code == 401
