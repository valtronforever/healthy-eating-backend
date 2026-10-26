"""Importing a HealthyEating export (docs/export-format.md) into the signed-in account.

The counterpart of services/export.py (healthy-eating-backend#27, owner decision 2026-10-26):
it moves a history between servers or accounts, from the JSON file or the ZIP of CSV files.
Only the history is imported: meals with their items, weigh-ins and the marks of their days.
The account, logins, settings and connections stay as they are on this server.

Meals keep everything the export has: their values, provenance, times with their offsets,
`imported_from` (empty for meals that were logged as they were eaten, so they still count as
usual meals) and their items. Vitamins and minerals are computed again from the items. Each
meal and weigh-in keeps its id unless another account here already uses it.

Nothing is counted twice:
- a meal whose id this account already has, or with the same name (case and spacing ignored),
  minute and calories as a stored meal, is skipped;
- a weigh-in whose id or Apple Health sample this account already has, or with the same time
  and weight, is skipped.

Days that get meals are marked like services/days.py:mark_imported_days describes: the
export's own mark when it has one; otherwise complete, unless the exported account counted
complete days only, where an unmarked day did not count and is marked partial. A day this
account already marked keeps its mark.

Entries that are not valid are reported with their position and reason; the rest is imported.
"""

import csv
import io
import json
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal, User, WeightEntry
from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS, ItemMatchStatus
from healthy_eating.nutrition.types import (
    Confidence,
    DayStatus,
    MealSource,
    StatusSetter,
    TrackedNutrientField,
    WeightSource,
)
from healthy_eating.services.days import mark_imported_days
from healthy_eating.services.export import FORMAT_VERSION
from healthy_eating.services.food_matching import FoodItem, SearchCache
from healthy_eating.services.local_time import local_eaten_at
from healthy_eating.services.meal_import import FUTURE_TOLERANCE, MAX_IMPORTED_FROM_LENGTH
from healthy_eating.services.meals import (
    MAX_CALORIES_KCAL,
    MAX_DESCRIPTION_LENGTH,
    MAX_DISH_NAME_LENGTH,
    MAX_ITEMS,
    MAX_MACRONUTRIENT_G,
    MAX_PORTION_GRAMS,
    MAX_SODIUM_MG,
    set_items,
)
from healthy_eating.services.usual_meals import dish_key
from healthy_eating.services.weights import MAX_WEIGHT_KG

# The largest file accepted, and the most a ZIP may unpack to.
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_SOURCE_NOTE_LENGTH = 200
MAX_EXTERNAL_ID_LENGTH = 255
MAX_LABEL_VALUE = 100_000
# Ids are looked up in parts, to stay far below Postgres' limit of parameters per statement.
ID_BATCH = 1000

ImportFormat = Literal["json", "csv"]
EntryKind = Literal["meal", "weight"]


class UnsupportedFileError(ValueError):
    """The file is not a HealthyEating export this server can read."""


@dataclass(frozen=True)
class ExportFile:
    format: ImportFormat
    exported_at: datetime | None
    meals: list[dict[str, Any]]
    weights: list[dict[str, Any]]
    day_statuses: list[dict[str, Any]]


class ExportedItem(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_DISH_NAME_LENGTH)
    grams: float | None = Field(default=None, gt=0, le=MAX_PORTION_GRAMS)
    status: ItemMatchStatus
    food_id: int | None = None
    label_values: dict[str, Annotated[float, Field(ge=0, le=MAX_LABEL_VALUE)]] | None = None


class ExportedMeal(BaseModel):
    meal_id: uuid.UUID | None = None
    dish_name: str = Field(min_length=1, max_length=MAX_DISH_NAME_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    eaten_at_utc: AwareDatetime
    utc_offset_minutes: int | None = Field(default=None, ge=-24 * 60, le=24 * 60)
    portion_grams: float | None = Field(default=None, gt=0, le=MAX_PORTION_GRAMS)
    calories_kcal: float = Field(ge=0, le=MAX_CALORIES_KCAL)
    protein_g: float = Field(ge=0, le=MAX_MACRONUTRIENT_G)
    fat_g: float = Field(ge=0, le=MAX_MACRONUTRIENT_G)
    carbohydrates_g: float = Field(ge=0, le=MAX_MACRONUTRIENT_G)
    fiber_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    sugar_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    sodium_mg: float | None = Field(default=None, ge=0, le=MAX_SODIUM_MG)
    source: MealSource
    source_note: str | None = Field(default=None, max_length=MAX_SOURCE_NOTE_LENGTH)
    imported_from: str | None = Field(default=None, max_length=MAX_IMPORTED_FROM_LENGTH)
    # Only whether it is set: the values are computed again from the items. Set (`{}`) also when
    # the meal was logged with an empty list of items.
    micronutrients: dict[str, Any] | None = None
    items: list[ExportedItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    estimated_fields: list[TrackedNutrientField] = Field(default_factory=list)
    confidence: Confidence
    created_at: AwareDatetime | None = None


class ExportedWeight(BaseModel):
    weight_id: uuid.UUID | None = None
    weight_kg: float = Field(gt=0, le=MAX_WEIGHT_KG)
    measured_at_utc: AwareDatetime
    utc_offset_minutes: int | None = Field(default=None, ge=-24 * 60, le=24 * 60)
    source: WeightSource
    external_id: str | None = Field(default=None, max_length=MAX_EXTERNAL_ID_LENGTH)
    created_at: AwareDatetime | None = None


class ExportedDayStatus(BaseModel):
    date: date
    status: DayStatus
    set_by: StatusSetter
    from_import: bool = False


@dataclass(frozen=True)
class RejectedEntry:
    kind: EntryKind
    # Position in the file's meals or weigh-ins, from 0.
    index: int
    reason: str


@dataclass(frozen=True)
class DataImportResult:
    format: ImportFormat
    exported_at: datetime | None
    meals_imported: int = 0
    meals_duplicate: int = 0
    weights_imported: int = 0
    weights_duplicate: int = 0
    days_marked: int = 0
    first_day: date | None = None
    last_day: date | None = None
    rejected: list[RejectedEntry] = field(default_factory=list)


# Reading the file


def _check_version(value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise UnsupportedFileError("This is not a HealthyEating export")
    if value > FORMAT_VERSION:
        raise UnsupportedFileError(
            "This export was made by a newer HealthyEating server; update this server first"
        )


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry if isinstance(entry, dict) else {} for entry in value]


def _read_json(data: bytes) -> ExportFile:
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnsupportedFileError("The file is not valid JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("meals"), list):
        raise UnsupportedFileError("This is not a HealthyEating export")
    _check_version(document.get("format_version"))
    return ExportFile(
        format="json",
        exported_at=_timestamp(document.get("exported_at")),
        meals=_list(document.get("meals")),
        weights=_list(document.get("weights")),
        day_statuses=_list(document.get("day_statuses")),
    )


def _unpack(archive: zipfile.ZipFile, name: str, budget: list[int]) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    # Read in chunks and stop at the budget: the sizes in the ZIP's directory can lie.
    chunks = []
    with archive.open(info) as entry:
        while chunk := entry.read(1024 * 1024):
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise UnsupportedFileError("The ZIP file unpacks to more than this server accepts")
            chunks.append(chunk)
    return b"".join(chunks)


def _csv_cell(column: str, value: str) -> Any:
    if value == "":
        # Lists are empty, everything else unknown.
        return [] if column in ("items", "estimated_fields") else None
    if column in ("items", "micronutrients"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if column == "estimated_fields":
        return value.split(";")
    return value


def _csv_rows(data: bytes | None) -> list[dict[str, Any]]:
    if data is None:
        return []
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UnsupportedFileError("The CSV files are not UTF-8") from error
    return [
        {column: _csv_cell(column, value) for column, value in row.items() if column is not None}
        for row in csv.DictReader(io.StringIO(text, newline=""))
    ]


def _read_zip(data: bytes) -> ExportFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise UnsupportedFileError("The file is not a valid ZIP file") from error
    budget = [MAX_UNPACKED_BYTES]
    with archive:
        try:
            account = _unpack(archive, "account.json", budget)
            meals = _unpack(archive, "meals.csv", budget)
            if account is None or meals is None:
                raise UnsupportedFileError("This is not a HealthyEating export")
            weights = _unpack(archive, "weights.csv", budget)
            statuses = _unpack(archive, "day_statuses.csv", budget)
        except (zipfile.BadZipFile, NotImplementedError, EOFError) as error:
            raise UnsupportedFileError("The ZIP file cannot be read") from error
    try:
        readme = json.loads(account)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnsupportedFileError("This is not a HealthyEating export") from error
    if not isinstance(readme, dict):
        raise UnsupportedFileError("This is not a HealthyEating export")
    _check_version(readme.get("format_version"))
    return ExportFile(
        format="csv",
        exported_at=_timestamp(readme.get("exported_at")),
        meals=_csv_rows(meals),
        weights=_csv_rows(weights),
        day_statuses=_csv_rows(statuses),
    )


def read_export(data: bytes) -> ExportFile:
    """Read an export in either format, recognized by its content."""
    if data.startswith(b"PK"):
        return _read_zip(data)
    if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{"):
        return _read_json(data)
    raise UnsupportedFileError("Choose the JSON or ZIP file of a HealthyEating export")


# Importing


def _reason(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg"))


def _meal_key(name: str, eaten_at: datetime, calories_kcal: float) -> tuple[str, datetime, int]:
    return dish_key(name), eaten_at.replace(second=0, microsecond=0), round(calories_kcal)


def _weight_key(measured_at: datetime, weight_kg: float) -> tuple[datetime, float]:
    return measured_at.replace(microsecond=0), round(weight_kg, 2)


def _food_item(item: ExportedItem) -> FoodItem:
    if item.status == "label":
        given = item.label_values or {}
        values = {code: given[code] for code in MICRONUTRIENTS if code in given}
        return FoodItem(item.name, item.grams, None, values)
    # A matched item names its food; the others are matched again like a new item.
    food_id = item.food_id if item.status == "matched" else None
    return FoodItem(item.name, item.grams, food_id)


def _batches(values: list[uuid.UUID]) -> Iterable[list[uuid.UUID]]:
    for start in range(0, len(values), ID_BATCH):
        yield values[start : start + ID_BATCH]


async def _owners(
    session: AsyncSession, model: type[Meal] | type[WeightEntry], ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """The account each of these ids already belongs to, for those in use."""
    owners: dict[uuid.UUID, uuid.UUID] = {}
    for batch in _batches(ids):
        rows = await session.execute(select(model.id, model.user_id).where(model.id.in_(batch)))
        owners.update({row_id: owner for row_id, owner in rows})
    return owners


def _validated[M: BaseModel](
    model: type[M], rows: list[dict[str, Any]], kind: EntryKind, rejected: list[RejectedEntry]
) -> list[tuple[int, M]]:
    valid = []
    for index, row in enumerate(rows):
        try:
            valid.append((index, model.model_validate(row)))
        except ValidationError as error:
            rejected.append(RejectedEntry(kind, index, _reason(error)))
    return valid


async def _import_meals(
    session: AsyncSession,
    user: User,
    rows: list[dict[str, Any]],
    rejected: list[RejectedEntry],
    now: datetime,
) -> tuple[int, int, set[date]]:
    """Store the new meals; returns how many were imported and skipped, and their local days."""
    meals = []
    for index, meal in _validated(ExportedMeal, rows, "meal", rejected):
        if meal.eaten_at_utc > now + FUTURE_TOLERANCE:
            rejected.append(RejectedEntry("meal", index, "eaten_at_utc is in the future"))
        else:
            meals.append(meal)
    if not meals:
        return 0, 0, set()

    owners = await _owners(session, Meal, [m.meal_id for m in meals if m.meal_id is not None])
    start = min(m.eaten_at_utc for m in meals) - timedelta(minutes=1)
    end = max(m.eaten_at_utc for m in meals) + timedelta(minutes=1)
    stored = await session.execute(
        select(Meal.dish_name, Meal.eaten_at, Meal.calories_kcal).where(
            Meal.user_id == user.id, Meal.eaten_at >= start, Meal.eaten_at <= end
        )
    )
    seen = {_meal_key(name, eaten_at, calories) for name, eaten_at, calories in stored}
    seen_ids: set[uuid.UUID] = set()
    search_cache: SearchCache = {}

    imported = duplicates = 0
    days: set[date] = set()
    for meal in meals:
        key = _meal_key(meal.dish_name, meal.eaten_at_utc, meal.calories_kcal)
        ours = meal.meal_id is not None and (
            owners.get(meal.meal_id) == user.id or meal.meal_id in seen_ids
        )
        if ours or key in seen:
            duplicates += 1
            continue
        seen.add(key)
        # An id another account uses here gets a new one.
        meal_id = meal.meal_id if meal.meal_id is not None and meal.meal_id not in owners else None
        if meal_id is not None:
            seen_ids.add(meal_id)
        row = Meal(
            id=meal_id or uuid.uuid4(),
            user_id=user.id,
            client_id=None,
            dish_name=meal.dish_name,
            description=meal.description,
            source=meal.source,
            source_note=meal.source_note,
            eaten_at=meal.eaten_at_utc,
            utc_offset_minutes=meal.utc_offset_minutes,
            portion_grams=meal.portion_grams,
            calories_kcal=meal.calories_kcal,
            protein_g=meal.protein_g,
            fat_g=meal.fat_g,
            carbohydrates_g=meal.carbohydrates_g,
            fiber_g=meal.fiber_g,
            sugar_g=meal.sugar_g,
            sodium_mg=meal.sodium_mg,
            estimated_fields=list(dict.fromkeys(meal.estimated_fields)),
            confidence=meal.confidence,
            imported_from=meal.imported_from,
            # Loaded and empty, so set_items needs no lazy load (see save_meal).
            items=[],
        )
        if meal.created_at is not None:
            row.created_at = meal.created_at
        session.add(row)
        if meal.items or meal.micronutrients is not None:
            items = [_food_item(item) for item in meal.items]
            await set_items(session, row, items, search_cache)
        imported += 1
        days.add(local_eaten_at(row.eaten_at, row.utc_offset_minutes, user.timezone).date())
    await session.flush()
    return imported, duplicates, days


async def _import_weights(
    session: AsyncSession, user: User, rows: list[dict[str, Any]], rejected: list[RejectedEntry]
) -> tuple[int, int]:
    weights = [w for _, w in _validated(ExportedWeight, rows, "weight", rejected)]
    if not weights:
        return 0, 0

    owners = await _owners(
        session, WeightEntry, [w.weight_id for w in weights if w.weight_id is not None]
    )
    stored = await session.execute(
        select(WeightEntry.measured_at, WeightEntry.weight_kg, WeightEntry.external_id).where(
            WeightEntry.user_id == user.id
        )
    )
    seen: set[tuple[datetime, float]] = set()
    external_ids: set[str] = set()
    for measured_at, weight_kg, external_id in stored:
        seen.add(_weight_key(measured_at, weight_kg))
        if external_id is not None:
            external_ids.add(external_id)
    seen_ids: set[uuid.UUID] = set()

    imported = duplicates = 0
    for weight in weights:
        key = _weight_key(weight.measured_at_utc, weight.weight_kg)
        ours = weight.weight_id is not None and (
            owners.get(weight.weight_id) == user.id or weight.weight_id in seen_ids
        )
        known_sample = weight.external_id is not None and weight.external_id in external_ids
        if ours or key in seen or known_sample:
            duplicates += 1
            continue
        seen.add(key)
        # Only Apple Health samples have an external id (services/weights.py).
        external_id = weight.external_id if weight.source == "apple_health" else None
        if external_id is not None:
            external_ids.add(external_id)
        weight_id = (
            weight.weight_id
            if weight.weight_id is not None and weight.weight_id not in owners
            else None
        )
        if weight_id is not None:
            seen_ids.add(weight_id)
        entry = WeightEntry(
            id=weight_id or uuid.uuid4(),
            user_id=user.id,
            weight_kg=weight.weight_kg,
            measured_at=weight.measured_at_utc,
            utc_offset_minutes=weight.utc_offset_minutes,
            source=weight.source,
            external_id=external_id,
        )
        if weight.created_at is not None:
            entry.created_at = weight.created_at
        session.add(entry)
        imported += 1
    if imported:
        # Weigh-ins of one's own turn weight tracking on, as in services/weights.py.
        user.weight_tracking = True
    await session.flush()
    return imported, duplicates


def _day_marks(
    rows: list[dict[str, Any]], days: set[date]
) -> dict[date, tuple[DayStatus, StatusSetter]]:
    statuses: dict[date, ExportedDayStatus] = {}
    for row in rows:
        try:
            status = ExportedDayStatus.model_validate(row)
        except ValidationError:
            continue
        statuses[status.date] = status
    complete_days_only = any(
        s.status == "complete" and not s.from_import for s in statuses.values()
    )
    marks: dict[date, tuple[DayStatus, StatusSetter]] = {}
    for day in days:
        if (exported := statuses.get(day)) is not None:
            marks[day] = (exported.status, exported.set_by)
        else:
            marks[day] = ("partial" if complete_days_only else "complete", "user")
    return marks


async def import_export(
    session: AsyncSession, user_id: uuid.UUID, export: ExportFile, *, now: datetime
) -> DataImportResult:
    """Store what is new in the export; see the module docstring for what is skipped."""
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        raise LookupError("user not found")
    rejected: list[RejectedEntry] = []
    meals_imported, meals_duplicate, days = await _import_meals(
        session, user, export.meals, rejected, now
    )
    weights_imported, weights_duplicate = await _import_weights(
        session, user, export.weights, rejected
    )
    days_marked = await mark_imported_days(session, user_id, _day_marks(export.day_statuses, days))
    return DataImportResult(
        format=export.format,
        exported_at=export.exported_at,
        meals_imported=meals_imported,
        meals_duplicate=meals_duplicate,
        weights_imported=weights_imported,
        weights_duplicate=weights_duplicate,
        days_marked=days_marked,
        first_day=min(days, default=None),
        last_day=max(days, default=None),
        rejected=rejected,
    )
