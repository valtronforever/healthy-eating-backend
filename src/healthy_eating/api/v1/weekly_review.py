from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.v1.trends import Nutrients
from healthy_eating.db.base import utcnow
from healthy_eating.services.summaries import CountingRule
from healthy_eating.services.weekly_review import (
    LATE_FROM,
    LATE_UNTIL,
    DayGroup,
    LeftOutReason,
    weekly_review,
)

router = APIRouter(tags=["days"])


class LeftOutDayResponse(BaseModel):
    local_date: date
    reason: LeftOutReason = Field(
        description=(
            "'future': not yet; 'in_progress': today, not marked complete; 'not_logged': no "
            "meals; 'partial': marked partial; 'not_marked_complete': only complete days count"
        )
    )


class DayGroupResponse(BaseModel):
    counted_days: int
    averages: Nutrients | None = Field(description="Average per counted day; null if none counts")


class ProteinBucketResponse(BaseModel):
    min_g: float
    max_g: float | None = Field(description="Exclusive; null for the last bucket")
    meals: int


class WeeklyReviewResponse(BaseModel):
    first_day: date = Field(description="Monday of the local week")
    last_day: date = Field(description="Sunday of the local week")
    counting_rule: CountingRule
    logged_days: int = Field(description="Days with at least one meal")
    complete_days: int = Field(description="Days marked complete")
    counted_days: int = Field(description="Days the averages are taken over")
    averages: Nutrients | None = Field(
        description="Average per counted day (as in Trends); null if no day counts"
    )
    left_out: list[LeftOutDayResponse] = Field(
        description="Every day of the week the averages leave out, with the reason"
    )
    weekdays: DayGroupResponse = Field(description="Counted days Monday to Friday")
    weekend: DayGroupResponse = Field(description="Counted Saturday and Sunday")
    meal_count: int = Field(description="All meals of the week, counted days or not")
    protein_per_meal_g: float | None = Field(description="Average protein per meal")
    protein_buckets: list[ProteinBucketResponse] = Field(
        description="How many meals had protein in each range (0-10, 10-20, 20-30, 30-40, 40+ g)"
    )
    late_meals: int = Field(
        description=(
            f"Meals eaten from {LATE_FROM:%H:%M} until {LATE_UNTIL:%H:%M} in their local time"
        )
    )
    estimated_values_share: float | None = Field(
        description=(
            "Share of the meals' values (calories and three macros each) the assistant "
            "estimated, 0 to 1; the rest are the person's own numbers. Null without meals"
        )
    )
    low_confidence_meals: int


def _group(group: DayGroup) -> DayGroupResponse:
    return DayGroupResponse(
        counted_days=group.counted_days,
        averages=None if group.averages is None else Nutrients(**group.averages),
    )


@router.get("/weekly-review", operation_id="getWeeklyReview")
async def read_weekly_review(
    user_id: CurrentUserId,
    session: SessionDep,
    week: Annotated[
        date | None,
        Query(description="Any local day (YYYY-MM-DD) of the week; default: this week"),
    ] = None,
) -> WeeklyReviewResponse:
    """One local week, Monday to Sunday, as neutral facts: averages, patterns, data quality."""
    review = await weekly_review(session, user_id, week, now=utcnow())
    return WeeklyReviewResponse(
        first_day=review.first_day,
        last_day=review.last_day,
        counting_rule=review.counting_rule,
        logged_days=review.logged_days,
        complete_days=review.complete_days,
        counted_days=review.counted_days,
        averages=None if review.averages is None else Nutrients(**review.averages),
        left_out=[
            LeftOutDayResponse(local_date=day.local_date, reason=day.reason)
            for day in review.left_out
        ],
        weekdays=_group(review.weekdays),
        weekend=_group(review.weekend),
        meal_count=review.meal_count,
        protein_per_meal_g=review.protein_per_meal_g,
        protein_buckets=[
            ProteinBucketResponse(min_g=bucket.min_g, max_g=bucket.max_g, meals=bucket.meals)
            for bucket in review.protein_buckets
        ],
        late_meals=review.late_meals,
        estimated_values_share=review.estimated_values_share,
        low_confidence_meals=review.low_confidence_meals,
    )
