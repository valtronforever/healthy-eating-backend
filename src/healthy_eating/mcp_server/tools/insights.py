"""MCP tool to read vitamin and mineral insights over weeks (#36)."""

from datetime import date, timedelta
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.mcp_server.tools.history import READING_RULES
from healthy_eating.mcp_server.tools.profile import MicronutrientRange
from healthy_eating.nutrition.micronutrients import Micronutrient
from healthy_eating.services.days import user_today
from healthy_eating.services.micronutrient_insights import (
    InsightsPeriod,
    Source,
    micronutrient_insights,
)
from healthy_eating.services.summaries import CountingRule, RangeTooLongError

# Without days given: the last four weeks, today included.
DEFAULT_DAYS = 28

GET_MICRONUTRIENT_INSIGHTS_DESCRIPTION = (
    """\
Vitamins and minerals over weeks from HealthyEating: per nutrient the average per day, weekly
averages, on how many days the total was below, within or above the user's range (their own
goal, else their life stage reference), the foods and supplements it came from, and how many
meals have values at all. The period before (same length) comes too, to compare. Use it when
the user asks how a vitamin or mineral went over a week, a month or longer ("how was my iron
this month?"); for single days use get_daily_summary. Pass `nutrients` when the user asks about
specific ones.

Values come only from meals with food items. Always say how much they cover (`meals_with_values`
of `meal_count`, and the days an average uses). State amounts and ranges as plain facts: below,
within and above are equally neutral, a change from the period before is a number, not progress
or a setback. Never advise what to eat or which supplement to take, and never suggest a goal.
"""
    + READING_RULES
)


class WeekAverage(BaseModel):
    first_day: date
    last_day: date
    average: float | None
    days: int = Field(description="Days the average uses")


class RangeDays(BaseModel):
    below: int
    within: int
    above: int


class SourceShare(BaseModel):
    name: str
    amount: float
    share: float = Field(description="Of all items' amounts of this nutrient (0 to 1)")
    meals: int


class NutrientInsight(BaseModel):
    average: float | None = Field(
        description="Per day, over the days on which every meal has a value"
    )
    days: int = Field(description="Days the average uses")
    range_days: RangeDays | None = Field(description="Null without a range")
    weeks: list[WeekAverage]
    food_sources: list[SourceShare] = Field(description="Top foods, from every meal")
    label_sources: list[SourceShare] = Field(
        description="Top supplements and packaged foods (label values)"
    )


class InsightsPeriodResult(BaseModel):
    first_day: date
    last_day: date
    counted_days: int
    meal_count: int
    meals_with_values: int = Field(description="Meals that have vitamin and mineral values")
    nutrients: dict[Micronutrient, NutrientInsight] = Field(
        description=(
            "The unit is the code's suffix (mg or ug); nutrients without values are left out"
        )
    )


class MicronutrientInsightsResult(BaseModel):
    counting_rule: CountingRule
    ranges: dict[Micronutrient, MicronutrientRange] = Field(
        description="The range that applies now; nutrients without one are left out"
    )
    current: InsightsPeriodResult
    previous: InsightsPeriodResult


def _sources(sources: list[Source]) -> list[SourceShare]:
    return [
        SourceShare(name=source.name, amount=source.amount, share=source.share, meals=source.meals)
        for source in sources
    ]


def _period(period: InsightsPeriod) -> InsightsPeriodResult:
    return InsightsPeriodResult(
        first_day=period.first_day,
        last_day=period.last_day,
        counted_days=period.counted_days,
        meal_count=period.meal_count,
        meals_with_values=period.meals_with_values,
        nutrients={
            code: NutrientInsight(
                average=None if insight.average is None else insight.average.average,
                days=0 if insight.average is None else insight.average.days,
                range_days=(
                    None
                    if insight.range_days is None
                    else RangeDays(
                        below=insight.range_days.below,
                        within=insight.range_days.within,
                        above=insight.range_days.above,
                    )
                ),
                weeks=[
                    WeekAverage(
                        first_day=week.first_day,
                        last_day=week.last_day,
                        average=week.average,
                        days=week.days,
                    )
                    for week in insight.weeks
                ],
                food_sources=_sources(insight.food_sources),
                label_sources=_sources(insight.label_sources),
            )
            for code, insight in period.nutrients.items()
        },
    )


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        name="get_micronutrient_insights",
        description=GET_MICRONUTRIENT_INSIGHTS_DESCRIPTION,
        annotations={
            "title": "Vitamin and mineral insights",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def get_micronutrient_insights(
        first_day: Annotated[
            date | None,
            Field(description="First local day (YYYY-MM-DD); empty: 4 weeks before last_day"),
        ] = None,
        last_day: Annotated[
            date | None, Field(description="Last local day (YYYY-MM-DD), inclusive; empty: today")
        ] = None,
        nutrients: Annotated[
            list[Micronutrient] | None,
            Field(description="Only these nutrients, e.g. ['iron_mg']; empty: all"),
        ] = None,
    ) -> MicronutrientInsightsResult:
        user_id = current_user_id()
        now = utcnow()
        async with db.session() as session:
            last = last_day or await user_today(session, user_id, now)
            first = first_day or last - timedelta(days=DEFAULT_DAYS - 1)
            try:
                result = await micronutrient_insights(
                    session, user_id, first, last, now=now, nutrients=nutrients
                )
            except (RangeTooLongError, ValueError) as error:
                raise ToolError(str(error)) from None
        if result is None:
            raise ToolError("User not found")
        return MicronutrientInsightsResult(
            counting_rule=result.counting_rule,
            ranges={
                code: MicronutrientRange(min=found.min, max=found.max, source=found.source)
                for code, found in result.ranges.items()
            },
            current=_period(result.current),
            previous=_period(result.previous),
        )
