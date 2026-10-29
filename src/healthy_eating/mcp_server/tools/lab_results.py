"""MCP tools to save, read back and delete lab results (healthy-eating-backend#37)."""

import uuid
from datetime import date
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import LabResult
from healthy_eating.mcp_server.context import current_client_id, current_user_id
from healthy_eating.nutrition.lab_analytes import LabAnalyte, LabComparator
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.services.days import user_today
from healthy_eating.services.lab_results import (
    MAX_LISTED,
    MAX_NAME_LENGTH,
    MAX_RESULTS,
    MAX_UNIT_LENGTH,
    InvalidLabReportError,
    LabReport,
    LabResultInput,
    ReportNotFoundError,
    analyte_of,
    comparator_of,
    delete_lab_report,
    delete_lab_result,
    list_lab_results,
    related_nutrients,
    save_lab_report,
)

# Owner decision 2026-10-27: goals change only when the user asks; the assistant only says that
# they can.
GOALS_NOTE = "You can update your goals in Healthy Eating based on these results."

NEUTRAL_RULES = """
Lab results are sensitive health data. HealthyEating stores them and never interprets them: do
not call a value low, high, normal, good or bad when you report saving or listing them, never
diagnose, and never suggest a goal, a value, a food or a supplement based on them.
"""

LOG_LAB_RESULTS_DESCRIPTION = (
    """\
Save values from a lab report (blood test) to HealthyEating. Call it when the user shares lab
results with you: a photo or PDF of the report, or the values in text. One call per report
(one sample date).

Pass only the results: never the person's name, date of birth, patient or sample number,
doctor, lab name or address. For each result keep the name, value and unit exactly as printed
(a decimal comma becomes a point; "< 5" is value 5 with comparator "<"), and the lab's own
reference range as printed, in the same unit (either bound may be missing). Set `analyte` when
the result is one of the known ones (for example "25-OH-Vitamin D" is vitamin_d_25oh,
"Hämoglobin" is hemoglobin); leave it empty for anything else. Do not convert units yourself:
HealthyEating converts known ones.

To correct a report saved earlier in the chat, call it again with the whole corrected report and
its `report_id` as `replaces_report_id`; it replaces that report instead of adding a second one.

Finish with the one-line `user_message` as given. When `goals_note` is set, also tell the user
that one line, as given, and nothing more about goals: they decide whether to change them. Your
normal answer to what the user asked about the report stays as it is.
"""
    + NEUTRAL_RULES
)

LIST_LAB_RESULTS_DESCRIPTION = (
    """\
List the lab results saved in HealthyEating, newest report first, each with its results as
printed (and in the canonical unit for known analytes). Use it when the user asks about lab
values they saved, to compare reports, or to find a `report_id` or `result_id` to correct or
delete. Optionally filter by sample dates or known analytes.
"""
    + NEUTRAL_RULES
)

DELETE_LAB_RESULTS_DESCRIPTION = """\
Delete lab results from HealthyEating, only when the user asks: a whole report (`report_id`) or
one result (`result_id`), never both. Get the ids from log_lab_results or list_lab_results.
Finish with the one-line `user_message`.
"""


class LabResultEntry(BaseModel):
    name: str = Field(
        min_length=1, max_length=MAX_NAME_LENGTH, description="The result's name as printed"
    )
    value: float = Field(allow_inf_nan=False, description="The number as printed")
    unit: str | None = Field(
        default=None, max_length=MAX_UNIT_LENGTH, description="The unit as printed, e.g. ng/mL"
    )
    analyte: LabAnalyte | None = Field(
        default=None, description="The known analyte this result is; empty for any other"
    )
    comparator: LabComparator | None = Field(
        default=None, description="'<' or '>' when the value is printed with one"
    )
    reference_low: float | None = Field(
        default=None, allow_inf_nan=False, description="Lower end of the lab's range as printed"
    )
    reference_high: float | None = Field(
        default=None, allow_inf_nan=False, description="Upper end of the lab's range as printed"
    )


class SavedLabResult(BaseModel):
    result_id: uuid.UUID
    name: str
    analyte: LabAnalyte | None
    value: float
    comparator: LabComparator | None
    unit: str | None
    reference_low: float | None
    reference_high: float | None
    canonical_value: float | None = Field(description="In canonical_unit; null when not converted")
    canonical_unit: str | None


class LogLabResultsResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    report_id: uuid.UUID = Field(description="Pass as replaces_report_id to correct the report")
    taken_on: date
    results: list[SavedLabResult]
    related_nutrients: list[Micronutrient] = Field(
        description="Vitamins and minerals these results relate to"
    )
    goals_note: str | None = Field(
        description="When set, tell the user this one line as given; never suggest a value"
    )


class LabReportResult(BaseModel):
    report_id: uuid.UUID
    taken_on: date = Field(description="The day the sample was taken")
    results: list[SavedLabResult]


class LabResultList(BaseModel):
    reports: list[LabReportResult] = Field(description="Newest sample first")


class DeleteLabResultsResult(BaseModel):
    deleted_from_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")


def _saved(row: LabResult) -> SavedLabResult:
    return SavedLabResult(
        result_id=row.id,
        name=row.name,
        analyte=analyte_of(row),
        value=row.value,
        comparator=comparator_of(row),
        unit=row.unit,
        reference_low=row.reference_low,
        reference_high=row.reference_high,
        canonical_value=row.canonical_value,
        canonical_unit=row.canonical_unit,
    )


def _report(report: LabReport) -> LabReportResult:
    return LabReportResult(
        report_id=report.report_id,
        taken_on=report.taken_on,
        results=[_saved(row) for row in report.results],
    )


def _count(results: int) -> str:
    return f"{results} lab result{'' if results == 1 else 's'}"


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        description=LOG_LAB_RESULTS_DESCRIPTION,
        annotations={
            "title": "Save lab results",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def log_lab_results(
        taken_on: Annotated[
            date, Field(description="The day the sample was taken (YYYY-MM-DD), from the report")
        ],
        results: Annotated[
            list[LabResultEntry],
            Field(min_length=1, max_length=MAX_RESULTS, description="The report's results"),
        ],
        replaces_report_id: Annotated[
            uuid.UUID | None,
            Field(description="report_id of a report saved earlier that this call corrects"),
        ] = None,
    ) -> LogLabResultsResult:
        user_id = current_user_id()
        inputs = [LabResultInput(**entry.model_dump()) for entry in results]
        try:
            async with db.session() as session:
                today = await user_today(session, user_id, utcnow())
                report = await save_lab_report(
                    session,
                    user_id,
                    taken_on,
                    inputs,
                    client_id=current_client_id(),
                    today=today,
                    replaces_report_id=replaces_report_id,
                )
        except InvalidLabReportError as error:
            raise ToolError(f"Not saved to HealthyEating: {error}.") from None
        except ReportNotFoundError:
            raise ToolError(
                "Not saved to HealthyEating: the report to correct (replaces_report_id) was not "
                "found. Save it as a new report."
            ) from None

        count, day = _count(len(report.results)), taken_on.isoformat()
        if replaces_report_id is None:
            message = f"Saved {count} from {day} to HealthyEating."
        else:
            message = f"Updated the lab report from {day} in HealthyEating: {count}."
        related = related_nutrients(report.results)
        return LogLabResultsResult(
            saved_to_healthy_eating=True,
            user_message=message,
            report_id=report.report_id,
            taken_on=report.taken_on,
            results=[_saved(row) for row in report.results],
            related_nutrients=related,
            goals_note=GOALS_NOTE if related else None,
        )

    @mcp.tool(
        name="list_lab_results",
        description=LIST_LAB_RESULTS_DESCRIPTION,
        annotations={"title": "List lab results", "readOnlyHint": True, "openWorldHint": False},
    )
    async def list_lab_results_tool(
        first_day: Annotated[
            date | None, Field(description="Earliest sample day (YYYY-MM-DD); default any")
        ] = None,
        last_day: Annotated[
            date | None, Field(description="Latest sample day (YYYY-MM-DD); default any")
        ] = None,
        analytes: Annotated[
            list[LabAnalyte] | None, Field(description="Only these known analytes")
        ] = None,
    ) -> LabResultList:
        if first_day is not None and last_day is not None and first_day > last_day:
            raise ToolError("first_day is after last_day")
        async with db.session() as session:
            reports = await list_lab_results(
                session,
                current_user_id(),
                first_day=first_day,
                last_day=last_day,
                analytes=analytes,
                limit=MAX_LISTED,
            )
        return LabResultList(reports=[_report(report) for report in reports])

    @mcp.tool(
        name="delete_lab_results",
        description=DELETE_LAB_RESULTS_DESCRIPTION,
        annotations={
            "title": "Delete lab results",
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        },
    )
    async def delete_lab_results_tool(
        report_id: Annotated[
            uuid.UUID | None, Field(description="Delete this whole report")
        ] = None,
        result_id: Annotated[uuid.UUID | None, Field(description="Delete only this result")] = None,
    ) -> DeleteLabResultsResult:
        if (report_id is None) == (result_id is None):
            raise ToolError("Pass either report_id or result_id")
        user_id = current_user_id()
        async with db.session() as session:
            if report_id is not None:
                deleted = await delete_lab_report(session, user_id, report_id)
                message = f"Deleted the lab report ({_count(deleted)}) from HealthyEating."
            else:
                assert result_id is not None
                deleted = int(await delete_lab_result(session, user_id, result_id))
                message = "Deleted the lab result from HealthyEating."
        if not deleted:
            raise ToolError(
                "Not deleted from HealthyEating: nothing with this id. Use list_lab_results to "
                "find it."
            )
        return DeleteLabResultsResult(deleted_from_healthy_eating=True, user_message=message)
