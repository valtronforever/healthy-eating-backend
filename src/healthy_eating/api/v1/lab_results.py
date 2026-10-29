import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.db.models import LabResult
from healthy_eating.nutrition.lab_analytes import ANALYTE_INFO, LabAnalyte, LabComparator
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.services.lab_results import (
    LabReport,
    analyte_of,
    comparator_of,
    delete_lab_report,
    delete_lab_result,
    list_lab_results,
)

router = APIRouter(tags=["lab results"])


class CanonicalValueResponse(BaseModel):
    value: float
    unit: str
    reference_low: float | None
    reference_high: float | None


class LabResultResponse(BaseModel):
    id: uuid.UUID
    name: str = Field(description="As printed on the report")
    analyte: LabAnalyte | None = Field(description="The known analyte; null for any other")
    value: float = Field(description="As printed")
    comparator: LabComparator | None = Field(
        description="'<' or '>' when the value was printed with one"
    )
    unit: str | None = Field(description="As printed")
    reference_low: float | None = Field(description="The lab's range as printed, in `unit`")
    reference_high: float | None
    canonical: CanonicalValueResponse | None = Field(
        description="In the analyte's canonical unit; null for other analytes or units"
    )
    related_nutrient: Micronutrient | None = Field(
        description="The vitamin or mineral this analyte relates to, where there is one"
    )
    created_at: datetime
    updated_at: datetime


class LabReportResponse(BaseModel):
    report_id: uuid.UUID
    taken_on: date = Field(description="The day the sample was taken")
    results: list[LabResultResponse] = Field(description="In the order of the report")


class LabResultsResponse(BaseModel):
    reports: list[LabReportResponse] = Field(description="Newest sample first")


def _result(row: LabResult) -> LabResultResponse:
    analyte = analyte_of(row)
    canonical = None
    if row.canonical_value is not None and row.canonical_unit is not None:
        canonical = CanonicalValueResponse(
            value=row.canonical_value,
            unit=row.canonical_unit,
            reference_low=row.canonical_low,
            reference_high=row.canonical_high,
        )
    return LabResultResponse(
        id=row.id,
        name=row.name,
        analyte=analyte,
        value=row.value,
        comparator=comparator_of(row),
        unit=row.unit,
        reference_low=row.reference_low,
        reference_high=row.reference_high,
        canonical=canonical,
        related_nutrient=None if analyte is None else ANALYTE_INFO[analyte].related_nutrient,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _report(report: LabReport) -> LabReportResponse:
    return LabReportResponse(
        report_id=report.report_id,
        taken_on=report.taken_on,
        results=[_result(row) for row in report.results],
    )


@router.get(
    "/lab-results", operation_id="listLabResults", responses={400: error_response("Invalid range")}
)
async def read_lab_results(
    user_id: CurrentUserId,
    session: SessionDep,
    from_: Annotated[
        date | None, Query(alias="from", description="Earliest sample day; default any")
    ] = None,
    to: Annotated[date | None, Query(description="Latest sample day; default any")] = None,
    analyte: Annotated[
        list[LabAnalyte] | None, Query(description="Only these known analytes; repeat for more")
    ] = None,
) -> LabResultsResponse:
    """The user's lab reports with their results. Lab results are saved from the chat only."""
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="`from` is after `to`")
    reports = await list_lab_results(
        session, user_id, first_day=from_, last_day=to, analytes=analyte
    )
    return LabResultsResponse(reports=[_report(report) for report in reports])


@router.delete(
    "/lab-results/reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteLabReport",
    responses={404: error_response("Not found")},
)
async def remove_lab_report(
    report_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep
) -> Response:
    """Delete one of the user's lab reports with all of its results."""
    if not await delete_lab_report(session, user_id, report_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Lab report not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/lab-results/{result_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteLabResult",
    responses={404: error_response("Not found")},
)
async def remove_lab_result(
    result_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep
) -> Response:
    """Delete one result of a lab report."""
    if not await delete_lab_result(session, user_id, result_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Lab result not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
