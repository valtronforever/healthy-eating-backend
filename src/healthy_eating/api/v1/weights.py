import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.types import WeightSource
from healthy_eating.services.days import user_today
from healthy_eating.services.summaries import MAX_SUMMARY_DAYS
from healthy_eating.services.unset import UNSET
from healthy_eating.services.weights import (
    MAX_BATCH,
    MAX_WEIGHT_KG,
    MIN_WEIGHT_KG,
    InvalidWeightError,
    LocalWeight,
    WeightInput,
    WeightTrackingOffError,
    add_weights,
    delete_all_weights,
    delete_weight,
    edit_weight,
    weight_history,
)

router = APIRouter(tags=["weight"])

# Without a range, the last 90 local days.
DEFAULT_DAYS = 90

WeightKg = Annotated[float, Field(ge=MIN_WEIGHT_KG, le=MAX_WEIGHT_KG, description="Weight in kg")]


class WeightEntryResponse(BaseModel):
    id: uuid.UUID
    weight_kg: float
    measured_at: datetime = Field(description="Local time of the weigh-in with its UTC offset")
    local_date: date
    source: WeightSource
    created_at: datetime
    updated_at: datetime


class TrendPointResponse(BaseModel):
    date: date
    weight_kg: float = Field(description="Mean of the day's weigh-ins")
    trend_kg: float = Field(description="Smoothed trend after that day")


class WeightsResponse(BaseModel):
    first_day: date
    last_day: date
    entries: list[WeightEntryResponse] = Field(description="Weigh-ins of the range, oldest first")
    trend: list[TrendPointResponse] = Field(
        description="One point per local day with weigh-ins in the range, oldest first"
    )
    latest_trend_kg: float | None = Field(description="The trend on the range's last weigh-in day")
    weekly_change_kg: float | None = Field(
        description=(
            "Change of the trend per 7 days over the last 28 days of weigh-ins; null with less "
            "than 14 days between the first and last of them"
        )
    )
    change_days: int | None = Field(description="Days the weekly change is measured over")


class WeightCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weight_kg: WeightKg
    measured_at: datetime | None = Field(
        default=None,
        description="ISO 8601; default now. Without an offset it is read in the user's time zone",
    )
    source: Literal["app", "apple_health"] = Field(
        default="app", description="'app' for manual entry, 'apple_health' for imported samples"
    )
    external_id: str | None = Field(
        default=None,
        max_length=255,
        description="Apple Health sample id; a sample sent again updates the same weigh-in",
    )


class WeightsCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[WeightCreate] = Field(min_length=1, max_length=MAX_BATCH)


class WeightsCreated(BaseModel):
    entries: list[WeightEntryResponse]


class WeightUpdate(BaseModel):
    """Only the fields present are changed."""

    model_config = ConfigDict(extra="forbid")

    weight_kg: WeightKg | None = None
    measured_at: datetime | None = Field(
        default=None, description="ISO 8601; without an offset it is read in the user's time zone"
    )


class DeleteAllWeightsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["delete-all-weights"] = Field(
        description="Must be 'delete-all-weights', so weight data is never deleted by accident"
    )


class WeightsDeleted(BaseModel):
    deleted: int = Field(description="How many weigh-ins were deleted")


def weight_response(item: LocalWeight) -> WeightEntryResponse:
    entry = item.entry
    return WeightEntryResponse(
        id=entry.id,
        weight_kg=entry.weight_kg,
        measured_at=item.measured_at,
        local_date=item.measured_at.date(),
        source=entry.source,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


@router.get(
    "/weights", operation_id="listWeights", responses={400: error_response("Invalid range")}
)
async def read_weights(
    user_id: CurrentUserId,
    session: SessionDep,
    from_: Annotated[
        date | None, Query(alias="from", description="First local day; default 89 days ago")
    ] = None,
    to: Annotated[date | None, Query(description="Last local day; default today")] = None,
) -> WeightsResponse:
    """Weigh-ins and the smoothed trend for a range of local days."""
    last = to or await user_today(session, user_id, utcnow())
    first = from_ or last - timedelta(days=DEFAULT_DAYS - 1)
    if first > last:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="`from` is after `to`")
    if (last - first).days >= MAX_SUMMARY_DAYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"Ask for at most {MAX_SUMMARY_DAYS} days"
        )
    history = await weight_history(session, user_id, first, last)
    return WeightsResponse(
        first_day=first,
        last_day=last,
        entries=[weight_response(item) for item in history.entries],
        trend=[
            TrendPointResponse(date=p.local_date, weight_kg=p.weight_kg, trend_kg=p.trend_kg)
            for p in history.trend
        ],
        latest_trend_kg=history.latest_trend_kg,
        weekly_change_kg=history.weekly_change_kg,
        change_days=history.change_days,
    )


@router.post(
    "/weights",
    status_code=status.HTTP_201_CREATED,
    operation_id="addWeights",
    responses={409: error_response("Weight tracking is off (Apple Health import)")},
)
async def create_weights(
    body: WeightsCreate, user_id: CurrentUserId, session: SessionDep
) -> WeightsCreated:
    """Add weigh-ins: one typed in the app, or a batch from Apple Health.

    A weigh-in typed in the app turns weight tracking on. Apple Health entries are refused
    while it is off, so imports only start after the user turns it on. All entries of one
    request must have the same source.
    """
    sources = {entry.source for entry in body.entries}
    if len(sources) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Send one source per request"
        )
    inputs = [
        WeightInput(weight_kg=e.weight_kg, measured_at=e.measured_at, external_id=e.external_id)
        for e in body.entries
    ]
    try:
        saved = await add_weights(session, user_id, inputs, sources.pop(), now=utcnow())
    except WeightTrackingOffError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Weight tracking is off; turn it on before importing from Apple Health",
        ) from None
    return WeightsCreated(entries=[weight_response(item) for item in saved])


@router.patch(
    "/weights/{weight_id}",
    operation_id="updateWeight",
    responses={404: error_response("Not found")},
)
async def update_weight(
    weight_id: uuid.UUID, update: WeightUpdate, user_id: CurrentUserId, session: SessionDep
) -> WeightEntryResponse:
    """Correct one of the user's weigh-ins in place."""
    fields = update.model_fields_set
    try:
        edited = await edit_weight(
            session,
            user_id,
            weight_id,
            weight_kg=update.weight_kg
            if "weight_kg" in fields and update.weight_kg is not None
            else UNSET,
            measured_at=update.measured_at
            if "measured_at" in fields and update.measured_at is not None
            else UNSET,
            now=utcnow(),
        )
    except InvalidWeightError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from None
    if edited is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Weigh-in not found")
    return weight_response(edited)


@router.delete(
    "/weights/{weight_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteWeight",
    responses={404: error_response("Not found")},
)
async def remove_weight(
    weight_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep
) -> Response:
    """Delete one of the user's weigh-ins."""
    if not await delete_weight(session, user_id, weight_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Weigh-in not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/weights", operation_id="deleteAllWeights")
async def remove_all_weights(
    body: DeleteAllWeightsRequest, user_id: CurrentUserId, session: SessionDep
) -> WeightsDeleted:
    """Delete every weigh-in of the user. The weight tracking setting stays as it is."""
    return WeightsDeleted(deleted=await delete_all_weights(session, user_id))
