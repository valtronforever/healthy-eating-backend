from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.services.data_import import (
    MAX_FILE_BYTES,
    UnsupportedFileError,
    import_export,
    read_export,
)
from healthy_eating.services.export import export_csv_zip, export_json
from healthy_eating.services.users import get_user

# How many rejected entries the answer names; `rejected_count` has them all.
MAX_REJECTED_LISTED = 50

router = APIRouter(tags=["account"])


@router.get(
    "/export",
    operation_id="exportData",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "All of the user's data; format described in docs/export-format.md",
            "content": {"application/json": {}, "application/zip": {}},
        }
    },
)
async def export(
    request: Request,
    user_id: CurrentUserId,
    session: SessionDep,
    format: Annotated[
        Literal["json", "csv"], Query(description="'json', or 'csv' (a ZIP)")
    ] = "json",
) -> StreamingResponse:
    """Download all of the user's data: account, logins, settings, connections, days, meals."""
    if await get_user(session, user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    db: Database = request.app.state.db
    now = utcnow()
    stamp = now.date().isoformat()
    if format == "csv":
        body = export_csv_zip(db, user_id, now=now)
        media_type, filename = "application/zip", f"healthy-eating-export-{stamp}.zip"
    else:
        body = export_json(db, user_id, now=now)
        media_type, filename = "application/json", f"healthy-eating-export-{stamp}.json"
    return StreamingResponse(
        body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


class ImportCounts(BaseModel):
    imported: int = Field(description="New entries (with dry_run: entries that would be imported)")
    skipped_duplicates: int = Field(description="Entries this account already has")


class RejectedImportEntry(BaseModel):
    kind: Literal["meal", "weight"]
    index: int = Field(description="Position among the file's meals or weigh-ins, from 0")
    reason: str


class ImportResponse(BaseModel):
    dry_run: bool = Field(description="True when nothing was stored (a preview)")
    format: Literal["json", "csv"] = Field(description="The export file's format")
    exported_at: datetime | None = Field(description="When the export was made, if it says")
    meals: ImportCounts
    weights: ImportCounts
    days_marked: int = Field(
        description="Days with imported meals that got a complete or partial mark"
    )
    first_day: date | None = Field(description="Local day of the earliest imported meal")
    last_day: date | None = Field(description="Local day of the latest imported meal")
    rejected_count: int = Field(description="Entries that are not valid and are left out")
    rejected: list[RejectedImportEntry] = Field(
        description=f"The first {MAX_REJECTED_LISTED} entries left out, with the reason"
    )


async def _read_body(request: Request) -> bytes:
    if int(request.headers.get("content-length") or 0) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FILE_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="The file is too large")
    return bytes(body)


@router.post(
    "/import",
    operation_id="importData",
    responses={
        400: error_response("Not a HealthyEating export this server can read"),
        413: error_response(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB"),
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The JSON file or the ZIP file of GET /api/v1/export",
            "content": {
                "application/json": {"schema": {"type": "string", "format": "binary"}},
                "application/zip": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def import_data(
    request: Request,
    user_id: CurrentUserId,
    session: SessionDep,
    dry_run: Annotated[
        bool, Query(description="Check the file and count what would be imported; store nothing")
    ] = False,
) -> ImportResponse:
    """Import a HealthyEating export: its meals, weigh-ins and day marks.

    Entries this account already has are skipped, so importing the same file again changes
    nothing. The account, settings and connections stay as they are. Rules:
    services/data_import.py.
    """
    body = await _read_body(request)
    try:
        export = read_export(body)
    except UnsupportedFileError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    savepoint = await session.begin_nested()
    result = await import_export(session, user_id, export, now=utcnow())
    if dry_run:
        await savepoint.rollback()
    else:
        await savepoint.commit()
    return ImportResponse(
        dry_run=dry_run,
        format=result.format,
        exported_at=result.exported_at,
        meals=ImportCounts(
            imported=result.meals_imported, skipped_duplicates=result.meals_duplicate
        ),
        weights=ImportCounts(
            imported=result.weights_imported, skipped_duplicates=result.weights_duplicate
        ),
        days_marked=result.days_marked,
        first_day=result.first_day,
        last_day=result.last_day,
        rejected_count=len(result.rejected),
        rejected=[
            RejectedImportEntry(kind=r.kind, index=r.index, reason=r.reason)
            for r in result.rejected[:MAX_REJECTED_LISTED]
        ],
    )
