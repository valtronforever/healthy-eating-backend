from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.types import Confidence
from healthy_eating.services.expenditure import (
    KCAL_PER_KG,
    MIN_COUNTED_DAYS,
    MIN_SPAN_DAYS,
    MIN_WEIGH_IN_DAYS,
    WINDOW_DAYS,
    NoEstimateReason,
    expenditure,
)
from healthy_eating.services.summaries import CountingRule

router = APIRouter(tags=["weight"])


class ExpenditureResponse(BaseModel):
    first_day: date = Field(description=f"First local day of the {WINDOW_DAYS}-day window")
    last_day: date = Field(description="Today, in the user's time zone")
    counting_rule: CountingRule
    counted_days: int = Field(description="Days whose intake counts, as in Trends")
    weigh_ins: int = Field(description="Weigh-ins in the window")
    weigh_in_days: int = Field(description="Days with weigh-ins in the window")
    average_intake_kcal: float | None = Field(description="Average calories of the counted days")
    trend_change_kg: float | None = Field(description="Change of the weight trend over span_days")
    span_days: int | None = Field(description="Days between the two trend values compared")
    maintenance_kcal: float | None = Field(
        description=(
            "Estimated energy the user maintains at, per day, rounded to 10 kcal: average "
            f"intake minus the trend change x {KCAL_PER_KG:g} kcal per kg over span_days. Null "
            "when there is not enough data (see reason)"
        )
    )
    confidence: Confidence | None
    reason: NoEstimateReason | None = Field(
        description=(
            f"Why there is no estimate: fewer than {MIN_COUNTED_DAYS} counted days, or weigh-ins "
            f"on fewer than {MIN_WEIGH_IN_DAYS} days or spanning fewer than {MIN_SPAN_DAYS} days"
        )
    )


@router.get("/expenditure", operation_id="getExpenditure")
async def read_expenditure(user_id: CurrentUserId, session: SessionDep) -> ExpenditureResponse:
    """Energy the user maintains at, estimated from their own log and weight trend."""
    result = await expenditure(session, user_id, now=utcnow())
    return ExpenditureResponse(
        first_day=result.first_day,
        last_day=result.last_day,
        counting_rule=result.counting_rule,
        counted_days=result.counted_days,
        weigh_ins=result.weigh_ins,
        weigh_in_days=result.weigh_in_days,
        average_intake_kcal=result.average_intake_kcal,
        trend_change_kg=result.trend_change_kg,
        span_days=result.span_days,
        maintenance_kcal=result.maintenance_kcal,
        confidence=result.confidence,
        reason=result.reason,
    )
