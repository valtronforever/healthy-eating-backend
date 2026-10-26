from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.nutrition.types import DayStatus, StatusSetter
from healthy_eating.services.days import (
    DayStatusInfo,
    FutureDayError,
    clear_day_status,
    set_day_status,
)
from healthy_eating.services.summaries import (
    CountingRule,
    DaySummary,
    NutrientTotal,
    RangeTooLongError,
    daily_summaries,
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


class NutrientTotalResponse(BaseModel):
    amount: float = Field(description="Sum over the meals of the day that have a value")
    meal_count: int = Field(description="How many of the day's meals have a value")


def nutrient_total(total: NutrientTotal | None) -> NutrientTotalResponse | None:
    if total is None:
        return None
    return NutrientTotalResponse(amount=total.amount, meal_count=total.meal_count)


class DaySummaryResponse(BaseModel):
    date: date
    meal_count: int
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    fiber_g: NutrientTotalResponse | None = Field(
        description="Optional nutrients: null when no meal of the day has a value"
    )
    sugar_g: NutrientTotalResponse | None
    sodium_mg: NutrientTotalResponse | None = Field(description="In mg")
    micronutrients: dict[Micronutrient, NutrientTotalResponse] = Field(
        description=(
            "Vitamins and minerals of the meals' food items (USDA reference data), per nutrient "
            "(the unit is the suffix, mg or ug); a nutrient no meal of the day has is left out"
        )
    )
    estimated_calories_share: float | None = Field(
        description=(
            "Share of the day's calories the assistant estimated (0 to 1); null without calories"
        )
    )
    status: DayStatus | None = Field(description="Completeness mark; null if the day is not marked")
    status_set_by: StatusSetter | None
    counted: bool = Field(description="Whether the day counts for averages (see counting_rule)")


class DaysResponse(BaseModel):
    days: list[DaySummaryResponse] = Field(description="Every local day of the range, oldest first")
    counting_rule: CountingRule = Field(
        description=(
            "'complete_days': only days marked complete count (the user has marked one); "
            "'logged_days': every day with meals counts except days marked partial. "
            "Days without meals never count."
        )
    )


def _day_response(day: DaySummary) -> DaySummaryResponse:
    return DaySummaryResponse(
        date=day.local_date,
        meal_count=day.meal_count,
        calories_kcal=day.calories_kcal,
        protein_g=day.protein_g,
        fat_g=day.fat_g,
        carbohydrates_g=day.carbohydrates_g,
        fiber_g=nutrient_total(day.optional.get("fiber_g")),
        sugar_g=nutrient_total(day.optional.get("sugar_g")),
        sodium_mg=nutrient_total(day.optional.get("sodium_mg")),
        micronutrients={
            code: NutrientTotalResponse(amount=total.amount, meal_count=total.meal_count)
            for code, total in day.micronutrients.items()
        },
        estimated_calories_share=day.estimated_calories_share,
        status=day.status,
        status_set_by=day.status_set_by,
        counted=day.counted,
    )


@router.get(
    "/days", operation_id="listDays", responses={400: error_response("Invalid range or cursor")}
)
async def read_days(
    user_id: CurrentUserId,
    session: SessionDep,
    from_: Annotated[date, Query(alias="from", description="First local day (YYYY-MM-DD)")],
    to: Annotated[date, Query(description="Last local day (YYYY-MM-DD), inclusive")],
) -> DaysResponse:
    """Totals per local day, how much was estimated, and completeness."""
    if from_ > to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="`from` is after `to`")
    try:
        summaries = await daily_summaries(session, user_id, from_, to)
    except RangeTooLongError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
    return DaysResponse(
        days=[_day_response(day) for day in summaries.days],
        counting_rule=summaries.counting_rule,
    )


@router.put("/days/{day}/status", operation_id="setDayStatus")
async def mark_day(
    day: date, update: DayStatusUpdate, user_id: CurrentUserId, session: SessionDep
) -> DayStatusResponse:
    """Mark one of the user's local days as complete or partial."""
    try:
        info = await set_day_status(session, user_id, day, update.status, "user", now=utcnow())
    except FutureDayError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from None
    return day_status_response(info)


@router.delete(
    "/days/{day}/status",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="clearDayStatus",
    responses={404: error_response("Not found")},
)
async def unmark_day(day: date, user_id: CurrentUserId, session: SessionDep) -> Response:
    """Remove the mark of a day; 404 if the day was not marked."""
    if not await clear_day_status(session, user_id, day):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Day is not marked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
