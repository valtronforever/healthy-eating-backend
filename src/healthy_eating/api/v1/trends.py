from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.db.base import utcnow
from healthy_eating.services.summaries import CountingRule
from healthy_eating.services.trends import WINDOWS, OptionalAverage, Period, trends

router = APIRouter(tags=["days"])


class Nutrients(BaseModel):
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float


class OptionalAverageResponse(BaseModel):
    average: float = Field(description="Average over the counted days that have a value")
    days: int = Field(description="How many counted days have a value")


def _optional(average: OptionalAverage | None) -> OptionalAverageResponse | None:
    if average is None:
        return None
    return OptionalAverageResponse(average=average.average, days=average.days)


class PeriodResponse(BaseModel):
    first_day: date
    last_day: date
    counted_days: int = Field(description="Days the averages are taken over")
    logged_days: int = Field(description="Days with at least one meal, counted or not")
    averages: Nutrients | None = Field(description="Average per counted day; null if none counts")
    estimated_calories_share: float | None = Field(
        description="Share of the counted days' calories the assistant estimated (0 to 1)"
    )
    fiber_g: OptionalAverageResponse | None = Field(
        description="Optional nutrients: null when no counted day has a value"
    )
    sugar_g: OptionalAverageResponse | None
    sodium_mg: OptionalAverageResponse | None = Field(description="In mg")


class TrendsResponse(BaseModel):
    window_days: int
    counting_rule: CountingRule
    current: PeriodResponse = Field(
        description="The window ending today; today counts only once marked complete"
    )
    previous: PeriodResponse = Field(description="The window of the same length before it")
    change: Nutrients | None = Field(
        description="current minus previous averages; null unless both have counted days"
    )


def _period(period: Period) -> PeriodResponse:
    return PeriodResponse(
        first_day=period.first_day,
        last_day=period.last_day,
        counted_days=period.counted_days,
        logged_days=period.logged_days,
        averages=None if period.averages is None else Nutrients(**period.averages),
        estimated_calories_share=period.estimated_calories_share,
        fiber_g=_optional(period.optional_averages.get("fiber_g")),
        sugar_g=_optional(period.optional_averages.get("sugar_g")),
        sodium_mg=_optional(period.optional_averages.get("sodium_mg")),
    )


@router.get("/trends", operation_id="getTrends")
async def read_trends(
    user_id: CurrentUserId,
    session: SessionDep,
    window: Annotated[
        int,
        Query(
            description="Window length in days: 7, 30 or 90", json_schema_extra={"enum": WINDOWS}
        ),
    ] = 7,
) -> TrendsResponse:
    """Averages per counted day over the window, compared with the window before."""
    if window not in WINDOWS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="window must be 7, 30 or 90"
        )
    result = await trends(session, user_id, window, now=utcnow())
    return TrendsResponse(
        window_days=result.window_days,
        counting_rule=result.counting_rule,
        current=_period(result.current),
        previous=_period(result.previous),
        change=None if result.change is None else Nutrients(**result.change),
    )
