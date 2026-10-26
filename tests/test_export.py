import csv
import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.db import Database
from healthy_eating.db.models import Meal
from healthy_eating.services.export import MEAL_BATCH, MEAL_COLUMNS, export_csv_zip, export_json
from tests.mcp_helpers import mcp_client
from tests.oauth_helpers import bearer, register_client, sign_in

MEAL: dict[str, Any] = {
    "dish_name": "Borscht, with sour cream",
    "source": "from_conversation",
    "source_note": "Google Sheets log",
    "calories_kcal": 412,
    "protein_g": 14,
    "fat_g": 24,
    "carbohydrates_g": 36,
    "estimated_fields": ["protein_g", "fat_g"],
    "eaten_at": "2026-10-18T13:00:00+02:00",
}


@pytest.fixture
async def account(app: FastAPI, http: httpx.AsyncClient) -> dict[str, Any]:
    claude = await register_client(http, name="Claude")
    claude_tokens = await sign_in(http, claude, email="nina@example.com")
    ios = await register_client(http, redirect_uri="healthyeating://oauth/callback")
    tokens = await sign_in(http, ios, email="nina@example.com")
    headers = bearer(tokens["access_token"])
    await http.patch(
        "/api/v1/me/settings",
        json={"timezone": "Europe/Berlin", "targets": {"protein_g": {"min": 90}}},
        headers=headers,
    )
    await http.put("/api/v1/days/2026-10-18/status", json={"status": "complete"}, headers=headers)
    async with mcp_client(app, claude_tokens["access_token"]) as mcp:
        await mcp.call_tool("log_meal", MEAL)
    me = (await http.get("/api/v1/me", headers=headers)).json()
    return {"headers": headers, "user_id": uuid.UUID(me["id"])}


async def test_json_export_contains_all_data(
    http: httpx.AsyncClient, account: dict[str, Any]
) -> None:
    response = await http.get("/api/v1/export", headers=account["headers"])

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="healthy-eating-export-'
    )
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["format_version"] == 1
    assert data["account"]["id"] == str(account["user_id"])
    assert data["account"]["email"] == "nina@example.com"
    assert [login["provider"] for login in data["logins"]] == ["dev"]
    assert data["settings"]["timezone"] == "Europe/Berlin"
    assert data["settings"]["targets"]["protein_g"] == {"min": 90, "max": None}
    assert sorted(c["kind"] for c in data["connections"]) == ["ai_assistant", "ios_app"]
    assert data["day_statuses"] == [
        {
            "date": "2026-10-18",
            "status": "complete",
            "set_by": "user",
            "from_import": False,
            "updated_at": data["day_statuses"][0]["updated_at"],
        }
    ]
    [meal] = data["meals"]
    assert set(meal) == set(MEAL_COLUMNS)
    assert meal["dish_name"] == "Borscht, with sour cream"
    assert meal["eaten_at_utc"] == "2026-10-18T11:00:00+00:00"
    assert meal["eaten_at_local"] == "2026-10-18T13:00:00+02:00"
    assert meal["local_date"] == "2026-10-18"
    assert meal["estimated_fields"] == ["protein_g", "fat_g"]
    assert meal["source_note"] == "Google Sheets log"
    assert meal["client_name"] == "Claude"
    assert "token" not in response.text.lower()


async def test_csv_export_is_a_zip_of_tables(
    http: httpx.AsyncClient, account: dict[str, Any]
) -> None:
    response = await http.get(
        "/api/v1/export", params={"format": "csv"}, headers=account["headers"]
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == [
        "account.json",
        "connections.csv",
        "day_statuses.csv",
        "logins.csv",
        "meals.csv",
        "settings.csv",
        "weights.csv",
    ]

    def table(name: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(archive.read(name).decode())))

    [meal] = table("meals.csv")
    assert meal["dish_name"] == "Borscht, with sour cream"
    assert meal["estimated_fields"] == "protein_g;fat_g"
    assert meal["portion_grams"] == ""
    assert meal["calories_kcal"] == "412.0"
    settings = {row["setting"]: row["value"] for row in table("settings.csv")}
    assert settings["timezone"] == "Europe/Berlin"
    assert settings["calm_mode"] == "false"
    assert settings["target_protein_g_min"] == "90.0"
    assert settings["target_protein_g_max"] == ""
    assert table("day_statuses.csv")[0]["status"] == "complete"
    assert len(table("connections.csv")) == 2
    assert json.loads(archive.read("account.json"))["account"]["email"] == "nina@example.com"


async def test_large_histories_are_exported_in_batches(
    app: FastAPI, http: httpx.AsyncClient, account: dict[str, Any]
) -> None:
    db: Database = app.state.db
    start = datetime(2025, 1, 1, 12, tzinfo=UTC)
    count = MEAL_BATCH * 2 + 7
    async with db.session() as session:
        session.add_all(
            Meal(
                user_id=account["user_id"],
                dish_name=f"Meal {i}",
                source="estimated_by_assistant",
                eaten_at=start + timedelta(hours=i),
                calories_kcal=100,
                protein_g=1,
                fat_g=1,
                carbohydrates_g=1,
                estimated_fields=[],
                confidence="low",
            )
            for i in range(count)
        )

    # Header, one chunk per batch of meals, closing bracket.
    chunks = [chunk async for chunk in export_json(db, account["user_id"], now=start)]
    assert len(chunks) == 1 + 3 + 1
    zip_chunks = [chunk async for chunk in export_csv_zip(db, account["user_id"], now=start)]
    assert len(zip_chunks) >= 1 + 3

    data = (await http.get("/api/v1/export", headers=account["headers"])).json()
    assert len(data["meals"]) == count + 1
    assert data["meals"][0]["dish_name"] == "Meal 0"

    response = await http.get(
        "/api/v1/export", params={"format": "csv"}, headers=account["headers"]
    )
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    rows = list(csv.DictReader(io.StringIO(archive.read("meals.csv").decode())))
    assert len(rows) == count + 1


async def test_export_only_contains_the_users_data(
    http: httpx.AsyncClient, account: dict[str, Any]
) -> None:
    client = await register_client(http)
    other = await sign_in(http, client, email="omar@example.com")
    data = (await http.get("/api/v1/export", headers=bearer(other["access_token"]))).json()
    assert data["meals"] == []
    assert data["day_statuses"] == []
    assert data["account"]["email"] == "omar@example.com"
    assert len(data["connections"]) == 1


async def test_export_validation(http: httpx.AsyncClient, account: dict[str, Any]) -> None:
    response = await http.get(
        "/api/v1/export", params={"format": "xml"}, headers=account["headers"]
    )
    assert response.status_code == 422
    assert (await http.get("/api/v1/export")).status_code == 401
