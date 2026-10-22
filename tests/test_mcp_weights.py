from typing import Any

import httpx
from fastapi import FastAPI

from tests.mcp_helpers import mcp_client
from tests.test_meals_api import Account, account


async def call(
    app: FastAPI, owner: Account, tool: str, arguments: dict[str, Any] | None = None
) -> Any:
    async with mcp_client(app, owner.token) as mcp:
        return await mcp.call_tool(tool, arguments or {}, raise_on_error=False)


async def settings(http: httpx.AsyncClient, owner: Account) -> dict[str, Any]:
    result: dict[str, Any] = (await http.get("/api/v1/me/settings", headers=owner.headers)).json()
    return result


async def test_first_weigh_in_turns_tracking_on_and_says_how_to_turn_it_off(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "hal@example.com", "Europe/Berlin")

    result = await call(app, owner, "log_weight", {"weight": 80.4})

    content = result.structured_content
    assert content["saved_to_healthy_eating"] is True
    assert content["weight_kg"] == 80.4
    assert content["weight_tracking_turned_on"] is True
    assert content["user_message"].startswith("Saved to HealthyEating: 80.4 kg.")
    assert "turn it off in the Healthy Eating app" in content["user_message"]
    assert (await settings(http, owner))["weight_tracking"] is True

    again = (await call(app, owner, "log_weight", {"weight": 80.2})).structured_content
    assert again["weight_tracking_turned_on"] is False
    assert again["user_message"] == "Saved to HealthyEating: 80.2 kg."


async def test_pounds_are_stored_in_kg_and_echoed_in_pounds(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ida@example.com")

    content = (
        await call(app, owner, "log_weight", {"weight": 177, "unit": "lb"})
    ).structured_content

    assert content["weight_kg"] == 80.29
    assert content["user_message"].startswith("Saved to HealthyEating: 177 lb.")


async def test_time_correction_and_listing(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "jon@example.com", "Europe/Berlin")
    morning = "2026-03-02T08:30:00+01:00"
    first = (
        await call(app, owner, "log_weight", {"weight": 81, "measured_at": morning})
    ).structured_content

    fixed = await call(
        app, owner, "log_weight", {"weight": 80.6, "replaces_weight_id": first["weight_id"]}
    )
    assert fixed.structured_content["user_message"] == "Updated in HealthyEating: 80.6 kg."
    assert fixed.structured_content["weight_id"] == first["weight_id"]

    listed = (
        await call(
            app, owner, "list_weights", {"first_day": "2026-03-01", "last_day": "2026-03-05"}
        )
    ).structured_content
    assert listed["weight_tracking"] is True
    assert [w["weight_kg"] for w in listed["weigh_ins"]] == [80.6]
    assert listed["weigh_ins"][0]["measured_at"] == "2026-03-02T08:30:00+01:00"
    assert listed["latest_trend_kg"] == 80.6


async def test_nothing_is_listed_while_tracking_is_off(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "kai@example.com")
    await call(app, owner, "log_weight", {"weight": 70})
    await http.patch("/api/v1/me/settings", json={"weight_tracking": False}, headers=owner.headers)

    listed = (await call(app, owner, "list_weights")).structured_content

    assert listed["weight_tracking"] is False
    assert listed["weigh_ins"] == []


async def test_delete_and_errors_say_nothing_was_saved(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "lou@example.com")
    other = await account(http, "max@example.com")
    saved = (await call(app, owner, "log_weight", {"weight": 75})).structured_content

    foreign = await call(app, other, "delete_weight", {"weight_id": saved["weight_id"]})
    assert foreign.is_error
    assert "Not deleted from HealthyEating" in foreign.content[0].text

    deleted = await call(app, owner, "delete_weight", {"weight_id": saved["weight_id"]})
    assert deleted.structured_content["deleted_from_healthy_eating"] is True

    implausible = await call(app, owner, "log_weight", {"weight": 900})
    assert implausible.is_error
    assert implausible.content[0].text.startswith("Not saved to HealthyEating")


async def test_weight_tools_are_annotated(app: FastAPI, http: httpx.AsyncClient) -> None:
    owner = await account(http, "ned@example.com")
    async with mcp_client(app, owner.token) as mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
    log, listing, delete = (
        tools[name].annotations for name in ("log_weight", "list_weights", "delete_weight")
    )
    assert log is not None and listing is not None and delete is not None
    assert log.destructive_hint is False
    assert listing.read_only_hint is True
    assert delete.destructive_hint is True
