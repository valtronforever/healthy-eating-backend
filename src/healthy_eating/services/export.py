"""Export of all of a user's data, as JSON or as a ZIP of CSV files (docs/export-format.md).

Both formats are produced as a stream of chunks: meals are read from the database in batches,
so a large history never has to fit in memory. Contains no tokens or secrets.
"""

import csv
import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db import Database
from healthy_eating.db.models import DayStatusRecord, Grant, Identity, Meal, OAuthClient, User
from healthy_eating.services.connections import connection_kind
from healthy_eating.services.local_time import local_eaten_at
from healthy_eating.services.settings import settings_of

FORMAT_VERSION = 1
MEAL_BATCH = 500

MEAL_COLUMNS = [
    "meal_id",
    "dish_name",
    "description",
    "eaten_at_utc",
    "eaten_at_local",
    "local_date",
    "utc_offset_minutes",
    "portion_grams",
    "calories_kcal",
    "protein_g",
    "fat_g",
    "carbohydrates_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
    "source",
    "source_note",
    "estimated_fields",
    "confidence",
    "client_name",
    "created_at",
    "updated_at",
]
DAY_STATUS_COLUMNS = ["date", "status", "set_by", "updated_at"]
CONNECTION_COLUMNS = ["client_name", "kind", "created_at", "last_used_at", "revoked_at"]
LOGIN_COLUMNS = ["provider", "email", "email_verified", "created_at", "last_login_at"]
SETTING_COLUMNS = ["setting", "value"]


class UserNotFoundError(Exception):
    pass


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True)
class _Header:
    exported_at: datetime
    account: dict[str, Any]
    logins: list[dict[str, Any]]
    settings: dict[str, Any]
    connections: list[dict[str, Any]]
    day_statuses: list[dict[str, Any]]
    time_zone: str | None


async def _header(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> _Header:
    user = await session.get(User, user_id)
    if user is None:
        raise UserNotFoundError
    identities = await session.scalars(
        select(Identity).where(Identity.user_id == user_id).order_by(Identity.created_at)
    )
    settings = settings_of(user)
    grants = await session.execute(
        select(Grant, OAuthClient.client_info)
        .join(OAuthClient, OAuthClient.client_id == Grant.client_id)
        .where(Grant.user_id == user_id)
        .order_by(Grant.created_at)
    )
    statuses = await session.scalars(
        select(DayStatusRecord)
        .where(DayStatusRecord.user_id == user_id)
        .order_by(DayStatusRecord.local_date)
    )
    return _Header(
        exported_at=now,
        account={
            "id": str(user.id),
            "display_name": user.display_name,
            "email": user.primary_email,
            "created_at": _iso(user.created_at),
        },
        logins=[
            {
                "provider": identity.provider,
                "email": identity.email,
                "email_verified": identity.email_verified,
                "created_at": _iso(identity.created_at),
                "last_login_at": _iso(identity.last_login_at),
            }
            for identity in identities
        ],
        settings={
            "timezone": settings.timezone,
            "energy_unit": settings.energy_unit,
            "calm_mode": settings.calm_mode,
            "targets": {
                name: None if target is None else {"min": target.min, "max": target.max}
                for name, target in settings.targets.items()
            },
        },
        connections=[
            {
                "client_name": client_info.get("client_name"),
                "kind": connection_kind(client_info),
                "created_at": _iso(grant.created_at),
                "last_used_at": _iso(grant.last_used_at),
                "revoked_at": _iso(grant.revoked_at),
            }
            for grant, client_info in grants
        ],
        day_statuses=[
            {
                "date": status.local_date.isoformat(),
                "status": status.status,
                "set_by": status.set_by,
                "updated_at": _iso(status.updated_at),
            }
            for status in statuses
        ],
        time_zone=user.timezone,
    )


def _meal(meal: Meal, client_name: str | None, time_zone: str | None) -> dict[str, Any]:
    local = local_eaten_at(meal.eaten_at, meal.utc_offset_minutes, time_zone)
    return {
        "meal_id": str(meal.id),
        "dish_name": meal.dish_name,
        "description": meal.description,
        "eaten_at_utc": _iso(meal.eaten_at),
        "eaten_at_local": _iso(local),
        "local_date": local.date().isoformat(),
        "utc_offset_minutes": meal.utc_offset_minutes,
        "portion_grams": meal.portion_grams,
        "calories_kcal": meal.calories_kcal,
        "protein_g": meal.protein_g,
        "fat_g": meal.fat_g,
        "carbohydrates_g": meal.carbohydrates_g,
        "fiber_g": meal.fiber_g,
        "sugar_g": meal.sugar_g,
        "sodium_mg": meal.sodium_mg,
        "source": meal.source,
        "source_note": meal.source_note,
        "estimated_fields": list(meal.estimated_fields),
        "confidence": meal.confidence,
        "client_name": client_name,
        "created_at": _iso(meal.created_at),
        "updated_at": _iso(meal.updated_at),
    }


async def _meal_batches(
    session: AsyncSession, user_id: uuid.UUID, time_zone: str | None
) -> AsyncIterator[list[dict[str, Any]]]:
    """All meals, oldest first, in batches read from a server-side cursor."""
    result = await session.stream(
        select(Meal, OAuthClient.client_info["client_name"].astext)
        .outerjoin(OAuthClient, OAuthClient.client_id == Meal.client_id)
        .where(Meal.user_id == user_id)
        .order_by(Meal.eaten_at, Meal.id)
        .execution_options(yield_per=MEAL_BATCH)
    )
    async for partition in result.partitions():
        yield [_meal(meal, client_name, time_zone) for meal, client_name in partition]


async def export_json(db: Database, user_id: uuid.UUID, *, now: datetime) -> AsyncIterator[bytes]:
    async with db.session() as session:
        header = await _header(session, user_id, now)
        head = {
            "format_version": FORMAT_VERSION,
            "exported_at": _iso(header.exported_at),
            "units": {"energy": "kcal", "macronutrients": "g", "portion": "g"},
            "account": header.account,
            "logins": header.logins,
            "settings": header.settings,
            "connections": header.connections,
            "day_statuses": header.day_statuses,
        }
        # Everything but the closing brace, then the meals array piece by piece.
        yield (json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "meals": [').encode()
        first = True
        async for batch in _meal_batches(session, user_id, header.time_zone):
            parts = [json.dumps(meal, ensure_ascii=False) for meal in batch]
            prefix = "\n  " if first else ",\n  "
            first = False
            yield (prefix + ",\n  ".join(parts)).encode()
        yield b"\n ]\n}\n"


class _Chunks(io.RawIOBase):
    """A write-only, unseekable stream that collects what the ZIP writer produces."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def take(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return ";".join(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else value


def _write_csv(
    archive: zipfile.ZipFile, name: str, columns: list[str], rows: Iterable[dict[str, Any]]
) -> None:
    with (
        archive.open(name, "w") as entry,
        io.TextIOWrapper(entry, encoding="utf-8", newline="") as text,
    ):
        writer = csv.writer(text)
        writer.writerow(columns)
        writer.writerows([_cell(row[column]) for column in columns] for row in rows)


def _settings_rows(settings: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"setting": name, "value": settings[name]}
        for name in ("timezone", "energy_unit", "calm_mode")
    ]
    for nutrient, target in settings["targets"].items():
        for bound in ("min", "max"):
            value = None if target is None else target[bound]
            rows.append({"setting": f"target_{nutrient}_{bound}", "value": value})
    return rows


async def export_csv_zip(
    db: Database, user_id: uuid.UUID, *, now: datetime
) -> AsyncIterator[bytes]:
    sink = _Chunks()
    async with db.session() as session:
        header = await _header(session, user_id, now)
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            readme = {
                "format_version": FORMAT_VERSION,
                "exported_at": _iso(header.exported_at),
                "account": header.account,
            }
            archive.writestr("account.json", json.dumps(readme, ensure_ascii=False, indent=1))
            _write_csv(archive, "logins.csv", LOGIN_COLUMNS, header.logins)
            _write_csv(archive, "settings.csv", SETTING_COLUMNS, _settings_rows(header.settings))
            _write_csv(archive, "connections.csv", CONNECTION_COLUMNS, header.connections)
            _write_csv(archive, "day_statuses.csv", DAY_STATUS_COLUMNS, header.day_statuses)
            yield sink.take()

            with (
                archive.open("meals.csv", "w") as entry,
                io.TextIOWrapper(entry, encoding="utf-8", newline="") as text,
            ):
                writer = csv.writer(text)
                writer.writerow(MEAL_COLUMNS)
                async for batch in _meal_batches(session, user_id, header.time_zone):
                    writer.writerows(
                        [_cell(meal[column]) for column in MEAL_COLUMNS] for meal in batch
                    )
                    text.flush()
                    yield sink.take()
        yield sink.take()
