from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.types import DayStatus, StatusSetter
from healthy_eating.services.days import (
    DayStatusInfo,
    FutureDayError,
    clear_day_status,
    set_day_status,
)

router = APIRouter(tags=["days"])


class DayStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DayStatus = Field(
        description="'complete': every meal of the day is logged; 'partial': some are missing"
    )


class DayStatusResponse(BaseModel):
    date: date
    status: DayStatus
    set_by: StatusSetter = Field(description="'user' (the app) or 'assistant' (MCP)")
    updated_at: datetime


def day_status_response(info: DayStatusInfo) -> DayStatusResponse:
    return DayStatusResponse(
        date=info.local_date, status=info.status, set_by=info.set_by, updated_at=info.updated_at
    )


@router.put("/days/{day}/status")
async def mark_day(
    day: date, update: DayStatusUpdate, user_id: CurrentUserId, session: SessionDep
) -> DayStatusResponse:
    """Mark one of the user's local days as complete or partial."""
    try:
        info = await set_day_status(session, user_id, day, update.status, "user", now=utcnow())
    except FutureDayError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from None
    return day_status_response(info)


@router.delete("/days/{day}/status", status_code=status.HTTP_204_NO_CONTENT)
async def unmark_day(day: date, user_id: CurrentUserId, session: SessionDep) -> Response:
    """Remove the mark of a day; 404 if the day was not marked."""
    if not await clear_day_status(session, user_id, day):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Day is not marked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
