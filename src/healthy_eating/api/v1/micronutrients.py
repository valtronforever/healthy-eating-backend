from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.api.v1.settings import MicronutrientRangeModel
from healthy_eating.api.v1.trends import OptionalAverageResponse
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.services.micronutrient_insights import (
    InsightsPeriod,
    NutrientInsight,
    Source,
    micronutrient_insights,
)
from healthy_eating.services.summaries import CountingRule, RangeTooLongError

router = APIRouter(tags=["days"])


class WeekAverageResponse(BaseModel):
    first_day: date = Field(description="Monday, or the period's first day")
    last_day: date = Field(description="Sunday, or the period's last day")
    average: float | None = Field(
        description="Over the week's counted days on which every meal has a value; else null"
    )
    days: int = Field(description="How many days the average uses")


class RangeDaysResponse(BaseModel):
    below: int
    within: int
    above: int


class SourceResponse(BaseModel):
    name: str = Field(description="The item's name; items are grouped ignoring case")
    amount: float = Field(description="Sum over the period (the unit is the code's suffix)")
    share: float = Field(description="Share of all items' amounts of this nutrient (0 to 1)")
    meals: int


class NutrientInsightResponse(BaseModel):
    average: OptionalAverageResponse | None = Field(
        description="Over the counted days on which every meal has a value; null if none"
    )
    range_days: RangeDaysResponse | None = Field(
        description=(
            "On the days the average uses: how many day totals were below, within or above the "
            "range that applies now. Null without a range or without such days"
        )
    )
    weeks: list[WeekAverageResponse] = Field(description="Every week of the period, oldest first")
    food_sources: list[SourceResponse] = Field(
        description="Up to 5 foods (USDA values) with the largest amounts, from every meal"
    )
    label_sources: list[SourceResponse] = Field(
        description="Up to 5 items with label values (supplements, packaged foods)"
    )


class InsightsPeriodResponse(BaseModel):
    first_day: date
    last_day: date
    counted_days: int
    meal_count: int = Field(description="Meals in the period")
    meals_with_values: int = Field(description="Meals with vitamin and mineral values")
    nutrients: dict[Micronutrient, NutrientInsightResponse] = Field(
        description="Nutrients without any value in the period are left out"
    )


class MicronutrientInsightsResponse(BaseModel):
    counting_rule: CountingRule
    ranges: dict[Micronutrient, MicronutrientRangeModel] = Field(
        description="The range that applies now per nutrient (the user's goal, else the reference)"
    )
    current: InsightsPeriodResponse
    previous: InsightsPeriodResponse = Field(
        description="The same number of days, ending the day before `from`"
    )


def _source(source: Source) -> SourceResponse:
    return SourceResponse(
        name=source.name, amount=source.amount, share=source.share, meals=source.meals
    )


def _nutrient(insight: NutrientInsight) -> NutrientInsightResponse:
    average = insight.average
    range_days = insight.range_days
    return NutrientInsightResponse(
        average=(
            None
            if average is None
            else OptionalAverageResponse(average=average.average, days=average.days)
        ),
        range_days=(
            None
            if range_days is None
            else RangeDaysResponse(
                below=range_days.below, within=range_days.within, above=range_days.above
            )
        ),
        weeks=[
            WeekAverageResponse(
                first_day=week.first_day,
                last_day=week.last_day,
                average=week.average,
                days=week.days,
            )
            for week in insight.weeks
        ],
        food_sources=[_source(source) for source in insight.food_sources],
        label_sources=[_source(source) for source in insight.label_sources],
    )


def _period(period: InsightsPeriod) -> InsightsPeriodResponse:
    return InsightsPeriodResponse(
        first_day=period.first_day,
        last_day=period.last_day,
        counted_days=period.counted_days,
        meal_count=period.meal_count,
        meals_with_values=period.meals_with_values,
        nutrients={code: _nutrient(insight) for code, insight in period.nutrients.items()},
    )


@router.get(
    "/micronutrients/insights",
    operation_id="getMicronutrientInsights",
    responses={400: error_response("Invalid range"), 404: error_response("Not found")},
)
async def read_micronutrient_insights(
    user_id: CurrentUserId,
    session: SessionDep,
    from_: Annotated[date, Query(alias="from", description="First local day (YYYY-MM-DD)")],
    to: Annotated[
        date, Query(description="Last local day (YYYY-MM-DD), inclusive; at most 366 days")
    ],
    nutrient: Annotated[
        list[Micronutrient] | None, Query(description="Only these nutrients; all by default")
    ] = None,
) -> MicronutrientInsightsResponse:
    """Vitamins and minerals over the period and the one before: weekly averages, days below,
    within or above the range, top sources and how many meals have values."""
    try:
        result = await micronutrient_insights(
            session, user_id, from_, to, now=utcnow(), nutrients=nutrient
        )
    except (RangeTooLongError, ValueError) as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(error)) from None
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return MicronutrientInsightsResponse(
        counting_rule=result.counting_rule,
        ranges={
            code: MicronutrientRangeModel(min=found.min, max=found.max, source=found.source)
            for code, found in result.ranges.items()
        },
        current=_period(result.current),
        previous=_period(result.previous),
    )
