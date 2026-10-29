import io
import json
import zipfile
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from healthy_eating.db import Database
from healthy_eating.services.days import counts_complete_days_only
from tests.test_export import LAB_REPORT
from tests.test_meal_import import HISTORY, import_history, meals_between
from tests.test_meal_items import call
from tests.test_meals_api import Account, account

LUNCH: dict[str, Any] = {
    "dish_name": "Rice with salmon",
    "source": "from_conversation",
    "source_note": "Google Sheets log",
    "calories_kcal": 610,
    "protein_g": 38,
    "fat_g": 18,
    "carbohydrates_g": 70,
    "fiber_g": 3,
    "estimated_fields": ["fat_g"],
    "confidence": "high",
    "eaten_at": "2026-10-20T13:00:00+02:00",
    "items": [
        {"name": "boiled rice", "grams": 150},
        {"name": "Vitamin D3 capsule", "label_values": {"vitamin_d_ug": 50}},
    ],
}
DINNER: dict[str, Any] = {
    **LUNCH,
    "dish_name": "Lentil soup",
    "eaten_at": "2026-10-21T19:30:00+02:00",
    "items": [],
}


async def history_account(app: FastAPI, http: httpx.AsyncClient, email: str) -> Account:
    """An account with two meals (one with items), two weigh-ins, a lab report of two results and
    a complete day."""
    owner = await account(http, email, "Europe/Berlin")
    await call(app, owner, "log_meal", LUNCH)
    await call(app, owner, "log_meal", DINNER)
    await call(app, owner, "log_weight", {"weight": 70.2, "measured_at": "2026-10-20T07:00:00"})
    await call(app, owner, "log_lab_results", LAB_REPORT)
    await http.post(
        "/api/v1/weights",
        json={
            "entries": [
                {
                    "weight_kg": 70.0,
                    "measured_at": "2026-10-21T07:00:00+02:00",
                    "source": "apple_health",
                    "external_id": "HK-1",
                }
            ]
        },
        headers=owner.headers,
    )
    await http.put(
        "/api/v1/days/2026-10-20/status", json={"status": "complete"}, headers=owner.headers
    )
    return owner


async def export(http: httpx.AsyncClient, owner: Account, format: str = "json") -> bytes:
    response = await http.get("/api/v1/export", params={"format": format}, headers=owner.headers)
    assert response.status_code == 200, response.text
    return response.content


async def upload(
    http: httpx.AsyncClient,
    owner: Account,
    body: bytes,
    content_type: str = "application/json",
    **params: Any,
) -> httpx.Response:
    return await http.post(
        "/api/v1/import",
        content=body,
        params=params,
        headers={**owner.headers, "Content-Type": content_type},
    )


def comparable(meal: dict[str, Any]) -> dict[str, Any]:
    # What a meal keeps when it moves: everything but the server's own bookkeeping.
    skipped = {"id", "client_name", "updated_at"}
    return {key: value for key, value in meal.items() if key not in skipped}


@pytest.mark.parametrize(
    ("format", "content_type"), [("json", "application/json"), ("csv", "application/zip")]
)
async def test_an_export_moves_to_another_account_unchanged(
    app: FastAPI, http: httpx.AsyncClient, format: str, content_type: str
) -> None:
    source = await history_account(app, http, f"ana.{format}@example.com")
    target = await account(http, f"ben.{format}@example.com", "Europe/Berlin")

    response = await upload(http, target, await export(http, source, format), content_type)

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["dry_run"] is False
    assert result["format"] == format
    assert result["meals"] == {"imported": 2, "skipped_duplicates": 0}
    assert result["weights"] == {"imported": 2, "skipped_duplicates": 0}
    assert result["lab_results"] == {"imported": 2, "skipped_duplicates": 0}
    assert (result["first_day"], result["last_day"]) == ("2026-10-20", "2026-10-21")
    assert result["rejected_count"] == 0
    before = await meals_between(http, source, "2026-10-20", "2026-10-21")
    after = await meals_between(http, target, "2026-10-20", "2026-10-21")
    assert [comparable(m) for m in after] == [comparable(m) for m in before]
    # The ids belong to the other account on this server, so the copies get new ones.
    assert {m["id"] for m in after}.isdisjoint({m["id"] for m in before})
    lunch = next(m for m in after if m["dish_name"] == "Rice with salmon")
    assert lunch["micronutrients"]["vitamin_d_ug"] >= 50
    assert lunch["label_items"] == [
        {"name": "Vitamin D3 capsule", "grams": None, "values": {"vitamin_d_ug": 50}}
    ]
    assert lunch["imported_from"] is None
    weights = (await http.get("/api/v1/weights", headers=target.headers)).json()
    assert sorted(w["weight_kg"] for w in weights["entries"]) == [70.0, 70.2]
    settings = (await http.get("/api/v1/me/settings", headers=target.headers)).json()
    assert settings["weight_tracking"] is True
    labs = (await http.get("/api/v1/lab-results", headers=target.headers)).json()["reports"]
    before_labs = (await http.get("/api/v1/lab-results", headers=source.headers)).json()["reports"]
    assert len(labs) == 1
    assert [
        (r["name"], r["value"], r["unit"], r["comparator"], r["canonical"])
        for r in labs[0]["results"]
    ] == [
        (r["name"], r["value"], r["unit"], r["comparator"], r["canonical"])
        for r in before_labs[0]["results"]
    ]
    assert labs[0]["taken_on"] == "2026-10-15"


async def test_importing_the_same_export_again_changes_nothing(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await history_account(app, http, "cara.again@example.com")
    body = await export(http, owner)

    result = (await upload(http, owner, body)).json()

    assert result["meals"] == {"imported": 0, "skipped_duplicates": 2}
    assert result["weights"] == {"imported": 0, "skipped_duplicates": 2}
    assert result["lab_results"] == {"imported": 0, "skipped_duplicates": 2}
    assert result["days_marked"] == 0
    assert len(await meals_between(http, owner, "2026-10-20", "2026-10-21")) == 2


async def test_a_moved_history_keeps_its_ids(app: FastAPI, http: httpx.AsyncClient) -> None:
    source = await history_account(app, http, "dan.move@example.com")
    body = await export(http, source)
    ids = {m["id"] for m in await meals_between(http, source, "2026-10-20", "2026-10-21")}
    assert (
        await http.request(
            "DELETE", "/api/v1/me", json={"confirm": "delete-account"}, headers=source.headers
        )
    ).status_code == 204

    target = await account(http, "dan.new@example.com", "Europe/Berlin")
    await upload(http, target, body)

    moved = await meals_between(http, target, "2026-10-20", "2026-10-21")
    assert {m["id"] for m in moved} == ids


async def test_a_preview_stores_nothing(app: FastAPI, http: httpx.AsyncClient) -> None:
    source = await history_account(app, http, "eve.preview@example.com")
    target = await account(http, "fay.preview@example.com", "Europe/Berlin")
    body = await export(http, source)

    preview = (await upload(http, target, body, dry_run="true")).json()

    assert preview["dry_run"] is True
    assert preview["meals"] == {"imported": 2, "skipped_duplicates": 0}
    assert preview["days_marked"] == 2
    assert await meals_between(http, target, "2026-10-20", "2026-10-21") == []
    assert (await http.get("/api/v1/weights", headers=target.headers)).json()["entries"] == []
    # The real import afterwards gives the same numbers.
    result = (await upload(http, target, body)).json()
    assert {**result, "dry_run": True} == preview


async def test_imported_days_count_without_changing_how_days_count(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    source = await history_account(app, http, "gil.days@example.com")
    target = await account(http, "hal.days@example.com", "Europe/Berlin")
    # The target's own mark of the first day stays.
    await http.put(
        "/api/v1/days/2026-10-20/status", json={"status": "partial"}, headers=target.headers
    )

    result = (await upload(http, target, await export(http, source))).json()

    assert result["days_marked"] == 1
    days = await http.get(
        "/api/v1/days", params={"from": "2026-10-20", "to": "2026-10-21"}, headers=target.headers
    )
    statuses = {d["date"]: d["status"] for d in days.json()["days"]}
    # The source counted complete days only, so its unmarked day did not count there either.
    assert statuses == {"2026-10-20": "partial", "2026-10-21": "partial"}
    db: Database = app.state.db
    async with db.session() as session:
        assert not await counts_complete_days_only(session, target.user_id)


async def test_mcp_history_imports_mark_their_days_complete(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    owner = await account(http, "ivy.mark@example.com")

    await import_history(app, owner, HISTORY)

    days = await http.get(
        "/api/v1/days", params={"from": "2026-09-01", "to": "2026-09-02"}, headers=owner.headers
    )
    assert [(d["status"], d["counted"]) for d in days.json()["days"]] == [
        ("complete", True),
        ("complete", True),
    ]
    db: Database = app.state.db
    async with db.session() as session:
        # The import's marks do not switch the user to counting complete days only...
        assert not await counts_complete_days_only(session, owner.user_id)
    # ...but the user's own mark of such a day does.
    await http.put(
        "/api/v1/days/2026-09-01/status", json={"status": "complete"}, headers=owner.headers
    )
    async with db.session() as session:
        assert await counts_complete_days_only(session, owner.user_id)


async def test_invalid_entries_are_left_out_with_their_reason(
    app: FastAPI, http: httpx.AsyncClient
) -> None:
    source = await history_account(app, http, "jon.invalid@example.com")
    target = await account(http, "kai.invalid@example.com")
    document = json.loads(await export(http, source))
    document["meals"][1]["calories_kcal"] = -5
    document["weights"][0]["source"] = "scale"

    result = (await upload(http, target, json.dumps(document).encode())).json()

    assert result["meals"]["imported"] == 1
    assert result["weights"]["imported"] == 1
    assert result["rejected_count"] == 2
    assert [(r["kind"], r["index"]) for r in result["rejected"]] == [("meal", 1), ("weight", 0)]
    assert result["rejected"][0]["reason"].startswith("calories_kcal:")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"date,calories\n2026-10-20,2000\n", "Choose the JSON or ZIP file"),
        (b'{"format_version": 1}', "not a HealthyEating export"),
        (b'{"format_version": 2, "meals": []}', "newer HealthyEating server"),
        (b"PK\x03\x04broken", "not a valid ZIP file"),
    ],
)
async def test_other_files_are_refused(http: httpx.AsyncClient, body: bytes, message: str) -> None:
    owner = await account(http, "lea.refused@example.com")

    response = await upload(http, owner, body)

    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"
    assert message in response.json()["message"]


async def test_a_zip_without_meals_is_refused(http: httpx.AsyncClient) -> None:
    owner = await account(http, "max.zip@example.com")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("account.json", '{"format_version": 1}')

    response = await upload(http, owner, buffer.getvalue(), "application/zip")

    assert response.status_code == 400


async def test_large_files_are_refused(
    http: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await account(http, "ned.large@example.com")
    monkeypatch.setattr("healthy_eating.api.v1.export.MAX_FILE_BYTES", 10)

    response = await upload(http, owner, b'{"format_version": 1, "meals": []}')

    assert response.status_code == 413
    assert response.json()["code"] == "too_large"


async def test_import_needs_a_token(http: httpx.AsyncClient) -> None:
    response = await http.post("/api/v1/import", content=b"{}")

    assert response.status_code == 401
