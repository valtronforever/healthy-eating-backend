"""Lab results (healthy-eating-backend#37): values from a lab report the user shared in the chat.

The AI client reads the report (a photo, a PDF or text); HealthyEating only checks and stores
the numbers. Each result keeps its value, unit and the lab's reference range as printed. A
known analyte in a unit that converts also gets its canonical value (nutrition/lab_analytes.py).
Nothing here interprets a value.

The results of one report share a `report_id`. Correcting a report replaces all of its results
under the same id, so a report is never stored twice. The app reads and deletes lab results but
never creates or edits them.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import LabResult
from healthy_eating.nutrition.lab_analytes import (
    ANALYTE_INFO,
    LabAnalyte,
    LabComparator,
    conversion,
    to_canonical,
)
from healthy_eating.nutrition.micronutrients import Micronutrient

MAX_RESULTS = 100
MAX_NAME_LENGTH = 100
MAX_UNIT_LENGTH = 30
MAX_VALUE = 10_000_000
EARLIEST_SAMPLE = date(1900, 1, 1)
# Reports are listed newest first; the MCP tool shows at most this many results.
MAX_LISTED = 300


class InvalidLabReportError(ValueError):
    """A report or one of its results cannot be stored."""


class ReportNotFoundError(LookupError):
    """The report to replace is not one of the user's."""


@dataclass(frozen=True)
class LabResultInput:
    name: str
    value: float
    unit: str | None = None
    analyte: LabAnalyte | None = None
    comparator: LabComparator | None = None
    reference_low: float | None = None
    reference_high: float | None = None


@dataclass(frozen=True)
class LabReport:
    report_id: uuid.UUID
    taken_on: date
    results: list[LabResult]


def _clean(text: str | None, limit: int, what: str, index: int) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split())
    if len(cleaned) > limit:
        raise InvalidLabReportError(f"result {index}: {what} is longer than {limit} characters")
    return cleaned or None


def _number(value: float | None, what: str, index: int) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or abs(value) > MAX_VALUE:
        raise InvalidLabReportError(f"result {index}: {what} is not a plausible number")
    return value


def check_report(taken_on: date, results: list[LabResultInput], *, today: date) -> None:
    """Refuse what cannot be a lab report. Values themselves are never judged."""
    if not results:
        raise InvalidLabReportError("a report needs at least one result")
    if len(results) > MAX_RESULTS:
        raise InvalidLabReportError(f"send at most {MAX_RESULTS} results per report")
    # One day of tolerance: the user's day may be ahead of the server's idea of it.
    if taken_on > today + timedelta(days=1):
        raise InvalidLabReportError("taken_on is in the future")
    if taken_on < EARLIEST_SAMPLE:
        raise InvalidLabReportError("taken_on is before 1900")
    for index, result in enumerate(results):
        if not _clean(result.name, MAX_NAME_LENGTH, "name", index):
            raise InvalidLabReportError(f"result {index}: the name is empty")
        _clean(result.unit, MAX_UNIT_LENGTH, "unit", index)
        _number(result.value, "value", index)
        low = _number(result.reference_low, "reference_low", index)
        high = _number(result.reference_high, "reference_high", index)
        if low is not None and high is not None and low > high:
            raise InvalidLabReportError(f"result {index}: reference_low is above reference_high")


def _row(
    user_id: uuid.UUID,
    report_id: uuid.UUID,
    taken_on: date,
    position: int,
    result: LabResultInput,
    client_id: str | None,
) -> LabResult:
    unit = _clean(result.unit, MAX_UNIT_LENGTH, "unit", position)
    row = LabResult(
        id=uuid.uuid4(),
        user_id=user_id,
        report_id=report_id,
        position=position,
        taken_on=taken_on,
        name=_clean(result.name, MAX_NAME_LENGTH, "name", position),
        analyte=result.analyte,
        value=result.value,
        comparator=result.comparator,
        unit=unit,
        reference_low=result.reference_low,
        reference_high=result.reference_high,
        client_id=client_id,
    )
    set_canonical(row)
    return row


def analyte_of(row: LabResult) -> LabAnalyte | None:
    # The column's CHECK allows only LAB_ANALYTES.
    return cast(LabAnalyte | None, row.analyte)


def comparator_of(row: LabResult) -> LabComparator | None:
    return cast(LabComparator | None, row.comparator)


def set_canonical(row: LabResult) -> None:
    """Fill the canonical value and range from the analyte and the printed unit."""
    analyte = analyte_of(row)
    how = None if analyte is None else conversion(analyte, row.unit)
    if analyte is None or how is None:
        row.canonical_value = row.canonical_unit = None
        row.canonical_low = row.canonical_high = None
        return
    row.canonical_value = to_canonical(row.value, how)
    row.canonical_unit = ANALYTE_INFO[analyte].canonical_unit
    row.canonical_low = None if row.reference_low is None else to_canonical(row.reference_low, how)
    row.canonical_high = (
        None if row.reference_high is None else to_canonical(row.reference_high, how)
    )


def related_nutrients(rows: list[LabResult]) -> list[Micronutrient]:
    """Vitamins and minerals the user can set a goal for, related to these results."""
    found: list[Micronutrient] = []
    for row in rows:
        analyte = analyte_of(row)
        if analyte is None:
            continue
        nutrient = ANALYTE_INFO[analyte].related_nutrient
        if nutrient is not None and nutrient not in found:
            found.append(nutrient)
    return found


async def save_lab_report(
    session: AsyncSession,
    user_id: uuid.UUID,
    taken_on: date,
    results: list[LabResultInput],
    *,
    client_id: str | None,
    today: date,
    replaces_report_id: uuid.UUID | None = None,
) -> LabReport:
    """Store a report's results; with `replaces_report_id`, they replace that report's."""
    check_report(taken_on, results, today=today)
    report_id = replaces_report_id or uuid.uuid4()
    if replaces_report_id is not None:
        replaced = await session.scalars(
            delete(LabResult)
            .where(LabResult.user_id == user_id, LabResult.report_id == replaces_report_id)
            .returning(LabResult.id)
        )
        if not replaced.all():
            raise ReportNotFoundError
    rows = [
        _row(user_id, report_id, taken_on, position, result, client_id)
        for position, result in enumerate(results)
    ]
    session.add_all(rows)
    await session.flush()
    return LabReport(report_id=report_id, taken_on=taken_on, results=rows)


async def list_lab_results(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    first_day: date | None = None,
    last_day: date | None = None,
    analytes: list[LabAnalyte] | None = None,
    limit: int | None = None,
) -> list[LabReport]:
    """The user's reports, newest sample first, each with its results in report order."""
    query = select(LabResult).where(LabResult.user_id == user_id)
    if first_day is not None:
        query = query.where(LabResult.taken_on >= first_day)
    if last_day is not None:
        query = query.where(LabResult.taken_on <= last_day)
    if analytes:
        query = query.where(LabResult.analyte.in_(analytes))
    query = query.order_by(
        LabResult.taken_on.desc(),
        LabResult.created_at.desc(),
        LabResult.report_id,
        LabResult.position,
    )
    if limit is not None:
        query = query.limit(limit)
    reports: dict[uuid.UUID, LabReport] = {}
    for row in await session.scalars(query):
        report = reports.get(row.report_id)
        if report is None:
            report = reports[row.report_id] = LabReport(row.report_id, row.taken_on, [])
        report.results.append(row)
    return list(reports.values())


async def delete_lab_report(session: AsyncSession, user_id: uuid.UUID, report_id: uuid.UUID) -> int:
    """Delete every result of one report; returns how many were deleted."""
    deleted = await session.scalars(
        delete(LabResult)
        .where(LabResult.user_id == user_id, LabResult.report_id == report_id)
        .returning(LabResult.id)
    )
    return len(deleted.all())


async def delete_lab_result(
    session: AsyncSession, user_id: uuid.UUID, result_id: uuid.UUID
) -> bool:
    deleted = await session.scalar(
        delete(LabResult)
        .where(LabResult.id == result_id, LabResult.user_id == user_id)
        .returning(LabResult.id)
    )
    return deleted is not None
