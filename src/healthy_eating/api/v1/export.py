from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.services.export import export_csv_zip, export_json
from healthy_eating.services.users import get_user

router = APIRouter(tags=["account"])


@router.get(
    "/export",
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
