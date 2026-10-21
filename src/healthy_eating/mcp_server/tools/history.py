"""MCP tools to read back saved meals and daily totals, and to delete a meal."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.types import DayStatus, MealSource, TrackedNutrientField
from healthy_eating.services.days import user_today
from healthy_eating.services.meals import delete_meal, get_meal, list_meals
from healthy_eating.services.summaries import CountingRule, NutrientTotal, daily_summaries

# Longest range the read tools accept, to keep results small for the model.
MAX_TOOL_DAYS = 31
MAX_TOOL_MEALS = 100

READING_RULES = """
Healthy Eating's numbers are what was saved here. If the user tracks food elsewhere too (a
project with its own totals, a spreadsheet), keep answering in their usual format and do not
replace their own totals unless they ask for Healthy Eating's. Report numbers neutrally:
never call a day or a food good or bad, and never say the user went over or under a target.
"""

LIST_MEALS_DESCRIPTION = (
    """\
List the meals saved in HealthyEating for the user's local days, newest first, with their
`meal_id`s. Use it when the user asks what they logged, or to find the `meal_id` of a meal to
correct (log_meal with replaces_meal_id) or to delete (delete_meal).
"""
    + READING_RULES
)

GET_DAILY_SUMMARY_DESCRIPTION = (
    """\
Totals per local day from HealthyEating: calories, protein, fat and carbohydrates, number of
meals, how much of the calories the assistant estimated, and whether the day is marked
complete. Use it when the user asks how much they have eaten today or on other days.
"""
    + READING_RULES
)

DELETE_MEAL_DESCRIPTION = """\
Delete a meal from HealthyEating. Only when the user asks to remove a meal, for example one
that was saved twice or never eaten. To change a meal's values, use log_meal with
`replaces_meal_id` instead of deleting it. Get the `meal_id` from an earlier log_meal result or
from list_meals. When the user asks to remove a duplicate and the copies are the same, delete
the later copy without asking. Ask which one only when the matching meals differ and it is
unclear which one the user means. Finish with the one-line `user_message`.
"""


def _day_range(first_day: date | None, last_day: date | None, today: date) -> tuple[date, date]:
    first = first_day or last_day or today
    last = last_day or first_day or today
    if first > last:
        raise ToolError("first_day is after last_day")
    if (last - first).days >= MAX_TOOL_DAYS:
        raise ToolError(f"Ask for at most {MAX_TOOL_DAYS} days at a time")
    return first, last


FirstDay = Annotated[
    date | None,
    Field(description="First local day (YYYY-MM-DD); leave both days empty for today"),
]
LastDay = Annotated[date | None, Field(description="Last local day (YYYY-MM-DD), inclusive")]


class SavedMealItem(BaseModel):
    meal_id: uuid.UUID
    dish_name: str
    eaten_at: datetime = Field(description="Local time with the UTC offset")
    local_date: date
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    fiber_g: float | None
    sugar_g: float | None
    sodium_mg: float | None
    source: MealSource
    estimated_fields: list[TrackedNutrientField]


class MealList(BaseModel):
    first_day: date
    last_day: date
    meals: list[SavedMealItem]
    more: bool = Field(description="True if there are more meals than shown; ask for fewer days")


class DayTotals(BaseModel):
    date: date
    meal_count: int
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    fiber_g: float | None = Field(description="Sum over the meals that have a value; else null")
    sugar_g: float | None
    sodium_mg: float | None = Field(description="In mg")
    estimated_calories_share: float | None = Field(
        description="Share of the calories the assistant estimated (0 to 1)"
    )
    status: DayStatus | None = Field(description="'complete', 'partial' or not marked")
    counted: bool = Field(description="Whether the day counts for averages")


class DailySummary(BaseModel):
    days: list[DayTotals] = Field(description="Oldest first")
    counting_rule: CountingRule


def _amount(total: NutrientTotal | None) -> float | None:
    return None if total is None else total.amount


class DeleteMealResult(BaseModel):
    deleted_from_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        name="list_meals",
        description=LIST_MEALS_DESCRIPTION,
        annotations={"title": "List saved meals", "readOnlyHint": True, "openWorldHint": False},
    )
    async def list_meals_tool(first_day: FirstDay = None, last_day: LastDay = None) -> MealList:
        user_id = current_user_id()
        async with db.session() as session:
            first, last = _day_range(
                first_day, last_day, await user_today(session, user_id, utcnow())
            )
            page = await list_meals(
                session, user_id, first_day=first, last_day=last, limit=MAX_TOOL_MEALS
            )
        return MealList(
            first_day=first,
            last_day=last,
            meals=[
                SavedMealItem(
                    meal_id=item.meal.id,
                    dish_name=item.meal.dish_name,
                    eaten_at=item.eaten_at,
                    local_date=item.eaten_at.date(),
                    calories_kcal=item.meal.calories_kcal,
                    protein_g=item.meal.protein_g,
                    fat_g=item.meal.fat_g,
                    carbohydrates_g=item.meal.carbohydrates_g,
                    fiber_g=item.meal.fiber_g,
                    sugar_g=item.meal.sugar_g,
                    sodium_mg=item.meal.sodium_mg,
                    source=item.meal.source,
                    estimated_fields=item.meal.estimated_fields,
                )
                for item in page.meals
            ],
            more=page.next_cursor is not None,
        )

    @mcp.tool(
        name="get_daily_summary",
        description=GET_DAILY_SUMMARY_DESCRIPTION,
        annotations={"title": "Daily totals", "readOnlyHint": True, "openWorldHint": False},
    )
    async def get_daily_summary(
        first_day: FirstDay = None, last_day: LastDay = None
    ) -> DailySummary:
        user_id = current_user_id()
        async with db.session() as session:
            first, last = _day_range(
                first_day, last_day, await user_today(session, user_id, utcnow())
            )
            summaries = await daily_summaries(session, user_id, first, last)
        return DailySummary(
            days=[
                DayTotals(
                    date=day.local_date,
                    meal_count=day.meal_count,
                    calories_kcal=day.calories_kcal,
                    protein_g=day.protein_g,
                    fat_g=day.fat_g,
                    carbohydrates_g=day.carbohydrates_g,
                    fiber_g=_amount(day.optional.get("fiber_g")),
                    sugar_g=_amount(day.optional.get("sugar_g")),
                    sodium_mg=_amount(day.optional.get("sodium_mg")),
                    estimated_calories_share=day.estimated_calories_share,
                    status=day.status,
                    counted=day.counted,
                )
                for day in summaries.days
            ],
            counting_rule=summaries.counting_rule,
        )

    @mcp.tool(
        name="delete_meal",
        description=DELETE_MEAL_DESCRIPTION,
        annotations={
            "title": "Delete a meal",
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        },
    )
    async def delete_meal_tool(
        meal_id: Annotated[uuid.UUID, Field(description="meal_id of the meal to delete")],
    ) -> DeleteMealResult:
        user_id = current_user_id()
        async with db.session() as session:
            meal = await get_meal(session, user_id, meal_id)
            if meal is None or not await delete_meal(session, user_id, meal_id):
                raise ToolError(
                    "Not deleted from HealthyEating: no saved meal has this meal_id. "
                    "Use list_meals to find it."
                )
        return DeleteMealResult(
            deleted_from_healthy_eating=True,
            user_message=f"Deleted from HealthyEating: {meal.meal.dish_name}.",
        )
