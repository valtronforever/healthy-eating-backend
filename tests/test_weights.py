from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

from healthy_eating.services.weights import (
    TREND_SMOOTHING,
    TrendPoint,
    daily_trend,
    weekly_change,
)
from tests.test_meals_api import Account, account

START = date(2026, 3, 1)


def days(*weights: float, every: int = 1) -> list[tuple[date, float]]:
    return [(START + timedelta(days=i * every), weight) for i, weight in enumerate(weights)]


# Trend method


def test_a_steady_weight_is_its_own_trend() -> None:
    assert [point.trend_kg for point in daily_trend(days(80, 80, 80))] == [80, 80, 80]


def test_one_weigh_in_moves_the_trend_by_a_tenth() -> None:
    trend = daily_trend(days(80, 81))
    assert trend[1].trend_kg == pytest.approx(80 + TREND_SMOOTHING * 1)


def test_a_gap_counts_like_several_days() -> None:
    trend = daily_trend(days(80, 81, every=3))
    assert trend[1].trend_kg == pytest.approx(80 + (1 - (1 - TREND_SMOOTHING) ** 3))


def test_weekly_change_follows_a_steady_loss() -> None:
    # 0.05 kg less every day for 70 days: the trend settles on the same slope.
    trend = daily_trend(days(*(80 - 0.05 * i for i in range(70))))
    change, span = weekly_change(trend)
    assert span == 28
    assert change == pytest.approx(-0.35, abs=0.01)


def test_no_weekly_change_for_a_short_history() -> None:
    assert weekly_change(daily_trend(days(80, 79.8, 79.9))) == (None, None)
    assert weekly_change([]) == (None, None)
    point = TrendPoint(local_date=START, weight_kg=80, trend_kg=80)
    assert weekly_change([point]) == (None, None)


# REST API


async def add(
    http: httpx.AsyncClient, owner: Account, entries: list[dict[str, Any]]
) -> httpx.Response:
    return await http.post("/api/v1/weights", json={"entries": entries}, headers=owner.headers)


async def settings(http: httpx.AsyncClient, owner: Account) -> dict[str, Any]:
    result: dict[str, Any] = (await http.get("/api/v1/me/settings", headers=owner.headers)).json()
    return result


async def history(http: httpx.AsyncClient, owner: Account, **params: str) -> dict[str, Any]:
    response = await http.get("/api/v1/weights", params=params, headers=owner.headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def test_first_weigh_in_from_the_app_turns_weight_tracking_on(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "uma@example.com", "Europe/Berlin")
    assert (await settings(http, owner))["weight_tracking"] is False

    response = await add(http, owner, [{"weight_kg": 78.4, "measured_at": "2026-03-02T07:30:00"}])

    assert response.status_code == 201, response.text
    [entry] = response.json()["entries"]
    assert entry["source"] == "app"
    # A time without an offset is read in the user's time zone.
    assert entry["measured_at"] == "2026-03-02T07:30:00+01:00"
    assert entry["local_date"] == "2026-03-02"
    assert (await settings(http, owner))["weight_tracking"] is True


async def test_apple_health_imports_only_after_the_user_turns_it_on(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "vic@example.com", "Europe/Berlin")
    sample = {"weight_kg": 80.1, "source": "apple_health", "external_id": "hk-1"}

    refused = await add(http, owner, [sample])
    assert refused.status_code == 409
    assert (await settings(http, owner))["weight_tracking"] is False

    await http.patch("/api/v1/me/settings", json={"weight_tracking": True}, headers=owner.headers)
    assert (await add(http, owner, [sample])).status_code == 201
    # The same sample again updates the same weigh-in.
    again = await add(http, owner, [sample | {"weight_kg": 80.3}])
    assert again.status_code == 201
    entries = (await history(http, owner))["entries"]
    assert [(e["weight_kg"], e["source"]) for e in entries] == [(80.3, "apple_health")]


async def test_one_source_per_request_and_plausible_weights(http: httpx.AsyncClient) -> None:
    owner = await account(http, "wes@example.com")
    mixed: list[dict[str, Any]] = [{"weight_kg": 80}, {"weight_kg": 80, "source": "apple_health"}]
    assert (await add(http, owner, mixed)).status_code == 422
    assert (await add(http, owner, [{"weight_kg": 5}])).status_code == 422
    assert (await add(http, owner, [])).status_code == 422


async def test_trend_and_weekly_change_over_a_range(http: httpx.AsyncClient) -> None:
    owner = await account(http, "xan@example.com", "Europe/Berlin")
    start = datetime(2026, 2, 1, 7, tzinfo=UTC)
    entries = [
        {
            "weight_kg": round(80 - 0.05 * i, 2),
            "measured_at": (start + timedelta(days=i)).isoformat(),
        }
        for i in range(60)
    ]
    assert (await add(http, owner, entries)).status_code == 201

    result = await history(http, owner, **{"from": "2026-03-20", "to": "2026-04-01"})

    assert result["first_day"] == "2026-03-20"
    assert result["trend"][0]["date"] == "2026-03-20"
    assert len(result["entries"]) == len(result["trend"]) == 13
    # The trend is computed from the first weigh-in, not from the start of the range.
    assert result["trend"][0]["trend_kg"] > result["trend"][0]["weight_kg"]
    assert result["weekly_change_kg"] == pytest.approx(-0.35, abs=0.02)
    assert result["change_days"] == 28
    assert result["latest_trend_kg"] == result["trend"][-1]["trend_kg"]


async def test_correct_and_delete_weigh_ins(http: httpx.AsyncClient) -> None:
    owner = await account(http, "yara@example.com")
    other = await account(http, "zed@example.com")
    [entry] = (await add(http, owner, [{"weight_kg": 81.0}])).json()["entries"]
    url = f"/api/v1/weights/{entry['id']}"

    edited = await http.patch(url, json={"weight_kg": 80.6}, headers=owner.headers)
    assert edited.status_code == 200
    assert edited.json()["weight_kg"] == 80.6
    assert (
        await http.patch(url, json={"weight_kg": 1000}, headers=owner.headers)
    ).status_code == 422
    # Another account can neither change nor delete it.
    assert (await http.patch(url, json={"weight_kg": 70}, headers=other.headers)).status_code == 404
    assert (await http.delete(url, headers=other.headers)).status_code == 404
    assert (await history(http, other))["entries"] == []

    assert (await http.delete(url, headers=owner.headers)).status_code == 204
    assert (await history(http, owner))["entries"] == []


async def test_delete_all_weight_data_keeps_the_setting(http: httpx.AsyncClient) -> None:
    owner = await account(http, "abe@example.com")
    await add(http, owner, [{"weight_kg": 81.0}, {"weight_kg": 80.8}])

    unconfirmed = await http.request("DELETE", "/api/v1/weights", json={}, headers=owner.headers)
    assert unconfirmed.status_code == 422
    deleted = await http.request(
        "DELETE", "/api/v1/weights", json={"confirm": "delete-all-weights"}, headers=owner.headers
    )

    assert deleted.json() == {"deleted": 2}
    assert (await history(http, owner))["entries"] == []
    assert (await settings(http, owner))["weight_tracking"] is True


async def test_samples_deleted_in_apple_health_are_deleted_here(
    http: httpx.AsyncClient,
) -> None:
    owner = await account(http, "cal@example.com")
    other = await account(http, "dee@example.com")
    for user in (owner, other):
        await http.patch(
            "/api/v1/me/settings", json={"weight_tracking": True}, headers=user.headers
        )
        samples = [
            {"weight_kg": 85.0, "source": "apple_health", "external_id": "hk-a"},
            {"weight_kg": 84.0, "source": "apple_health", "external_id": "hk-b"},
        ]
        assert (await add(http, user, samples)).status_code == 201
    await add(http, owner, [{"weight_kg": 84.5}])
    url = "/api/v1/weights/apple-health-deletions"

    response = await http.post(
        url, json={"external_ids": ["hk-a", "hk-unknown"]}, headers=owner.headers
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": 1}
    owned = (await history(http, owner))["entries"]
    assert sorted((e["weight_kg"], e["source"]) for e in owned) == [
        (84.0, "apple_health"),
        (84.5, "app"),
    ]
    # Another account's sample with the same id stays.
    assert len((await history(http, other))["entries"]) == 2
    # Sent again after a lost response: nothing left to delete.
    again = await http.post(url, json={"external_ids": ["hk-a"]}, headers=owner.headers)
    assert again.json() == {"deleted": 0}
    empty = await http.post(url, json={"external_ids": []}, headers=owner.headers)
    assert empty.status_code == 422


async def test_turning_weight_tracking_off_keeps_the_entries(http: httpx.AsyncClient) -> None:
    owner = await account(http, "bea@example.com")
    await add(http, owner, [{"weight_kg": 81.0}])

    await http.patch("/api/v1/me/settings", json={"weight_tracking": False}, headers=owner.headers)

    assert (await settings(http, owner))["weight_tracking"] is False
    assert len((await history(http, owner))["entries"]) == 1


async def test_weight_unit_is_a_setting(http: httpx.AsyncClient) -> None:
    owner = await account(http, "cal@example.com")
    response = await http.patch(
        "/api/v1/me/settings", json={"weight_unit": "lb"}, headers=owner.headers
    )
    assert response.json()["weight_unit"] == "lb"
    bad = await http.patch("/api/v1/me/settings", json={"weight_unit": "st"}, headers=owner.headers)
    assert bad.status_code == 422


async def test_export_contains_weigh_ins(http: httpx.AsyncClient) -> None:
    owner = await account(http, "dee@example.com", "Europe/Berlin")
    await add(http, owner, [{"weight_kg": 79.5, "measured_at": "2026-03-02T07:30:00+01:00"}])

    data = (await http.get("/api/v1/export", headers=owner.headers)).json()

    assert data["units"]["weight"] == "kg"
    assert data["settings"]["weight_tracking"] is True
    [weight] = data["weights"]
    assert (weight["weight_kg"], weight["local_date"], weight["source"]) == (
        79.5,
        "2026-03-02",
        "app",
    )


async def test_weights_need_a_token(http: httpx.AsyncClient) -> None:
    assert (await http.get("/api/v1/weights")).status_code == 401
