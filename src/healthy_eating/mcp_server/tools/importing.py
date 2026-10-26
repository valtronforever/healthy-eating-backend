"""MCP tool for importing earlier food history (healthy-eating-backend#27)."""

from datetime import date, datetime
from typing import Annotated

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.mcp_server.context import current_client_id, current_user_id
from healthy_eating.mcp_server.tools.meals import MealItemInput
from healthy_eating.nutrition.types import Confidence, MealSource, TrackedNutrientField
from healthy_eating.services.food_matching import FoodItem
from healthy_eating.services.meal_import import (
    MAX_IMPORT_MEALS,
    MAX_IMPORTED_FROM_LENGTH,
    ImportResult,
    import_meals,
)
from healthy_eating.services.meals import (
    MAX_CALORIES_KCAL,
    MAX_DESCRIPTION_LENGTH,
    MAX_DISH_NAME_LENGTH,
    MAX_ITEMS,
    MAX_MACRONUTRIENT_G,
    MAX_PORTION_GRAMS,
    MAX_SODIUM_MG,
    MealEstimate,
)

IMPORT_MEALS_DESCRIPTION = """\
Import the user's earlier food history into HealthyEating: meals from a ChatGPT project, a
spreadsheet, another app or notes that the user pasted or that are in this conversation. Use
it when the user asks to import, move, copy or add their history to HealthyEating; for a meal
eaten now, use log_meal.

- Send each meal of the history as one entry. If the history only has totals per day, send one
  entry per day named "Day total".
- Keep the numbers from the history unchanged (source "from_conversation"); estimate values the
  history does not have and list them in `estimated_fields`. Every entry needs calories,
  protein, fat and carbohydrates (calories may be left out when the three macros are given).
- Set `eaten_at` from the history, with the user's UTC offset if known; when only the date is
  known, use 12:00 that day.
- When the history names the foods and amounts of a meal, also pass `items` (as in log_meal)
  for vitamins and minerals; leave them out for day totals or when the foods are unclear.
- At most 100 entries per call: for a longer history, call again with the next entries. Entries
  already in HealthyEating are skipped, so a repeated call never counts a meal twice, and days
  that already have meals logged in HealthyEating are left as they are.
- Finish with the one-line `user_message`; it says how many meals were imported and for which
  dates.
"""


class HistoryMeal(BaseModel):
    dish_name: str = Field(
        min_length=1,
        max_length=MAX_DISH_NAME_LENGTH,
        description="Name of the dish as in the history, or 'Day total'",
    )
    eaten_at: datetime = Field(
        description=(
            "When it was eaten, ISO 8601 with the user's UTC offset if known; 12:00 that day "
            "when the history has only the date"
        )
    )
    calories_kcal: float | None = Field(
        default=None, ge=0, le=MAX_CALORIES_KCAL, description="Energy, kcal"
    )
    protein_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    fat_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    carbohydrates_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    fiber_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    sugar_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    sodium_mg: float | None = Field(default=None, ge=0, le=MAX_SODIUM_MG)
    estimated_fields: list[TrackedNutrientField] = Field(
        default_factory=list, description="Values you estimated because the history lacks them"
    )
    source: MealSource = Field(
        default="from_conversation",
        description="'from_conversation' when the history has the numbers (the usual case)",
    )
    confidence: Confidence = "medium"
    portion_grams: float | None = Field(default=None, gt=0, le=MAX_PORTION_GRAMS)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    items: list[MealItemInput] | None = Field(
        default=None,
        max_length=MAX_ITEMS,
        description="The meal's foods with grams, for vitamins and minerals (as in log_meal)",
    )


class RejectedEntry(BaseModel):
    index: int = Field(description="Position of the entry in `meals`, from 0")
    reason: str


class ImportMealsResult(BaseModel):
    saved_to_healthy_eating: bool = Field(description="Whether any meal was imported")
    user_message: str = Field(description="Tell the user this in one short line")
    imported: int
    skipped_duplicates: int = Field(description="Entries already in HealthyEating")
    days_already_logged: list[date] = Field(
        description="Days left as they were because meals were already logged there"
    )
    rejected: list[RejectedEntry] = Field(description="Entries not imported, with the reason")
    first_day: date | None
    last_day: date | None


def _count(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _user_message(result: ImportResult, imported_from: str) -> str:
    parts = []
    if result.imported and result.first_day and result.last_day:
        days = (
            result.first_day.isoformat()
            if result.first_day == result.last_day
            else f"{result.first_day.isoformat()} to {result.last_day.isoformat()}"
        )
        parts.append(
            f"Imported {_count(len(result.imported), 'meal', 'meals')} from {imported_from} into "
            f"HealthyEating ({days})"
        )
    else:
        parts.append("Nothing new was imported into HealthyEating")
    if result.duplicates:
        parts.append(f"{_count(result.duplicates, 'entry', 'entries')} already there")
    if result.days_already_logged:
        parts.append(
            f"{_count(len(result.days_already_logged), 'day', 'days')} that already had meals "
            "logged left as they were"
        )
    if result.rejected:
        parts.append(f"{_count(len(result.rejected), 'entry', 'entries')} not imported")
    return "; ".join(parts) + "."


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        name="import_meals",
        description=IMPORT_MEALS_DESCRIPTION,
        annotations={
            "title": "Import food history",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def import_meals_tool(
        meals: Annotated[
            list[HistoryMeal],
            Field(min_length=1, max_length=MAX_IMPORT_MEALS, description="Up to 100 entries"),
        ],
        imported_from: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_IMPORTED_FROM_LENGTH,
                description="Where the history comes from, e.g. 'ChatGPT project', 'Google Sheets'",
            ),
        ],
    ) -> ImportMealsResult:
        estimates = [
            MealEstimate(
                dish_name=meal.dish_name,
                source=meal.source,
                description=meal.description,
                portion_grams=meal.portion_grams,
                calories_kcal=meal.calories_kcal,
                protein_g=meal.protein_g,
                fat_g=meal.fat_g,
                carbohydrates_g=meal.carbohydrates_g,
                fiber_g=meal.fiber_g,
                sugar_g=meal.sugar_g,
                sodium_mg=meal.sodium_mg,
                estimated_fields=tuple(meal.estimated_fields),
                confidence=meal.confidence,
                eaten_at=meal.eaten_at,
                items=(
                    tuple(FoodItem(i.name, i.grams, i.food_id) for i in meal.items)
                    if meal.items is not None
                    else None
                ),
            )
            for meal in meals
        ]
        async with db.session() as session:
            result = await import_meals(
                session,
                current_user_id(),
                estimates,
                imported_from=imported_from,
                client_id=current_client_id(),
                now=utcnow(),
            )
        return ImportMealsResult(
            saved_to_healthy_eating=bool(result.imported),
            user_message=_user_message(result, imported_from),
            imported=len(result.imported),
            skipped_duplicates=result.duplicates,
            days_already_logged=result.days_already_logged,
            rejected=[RejectedEntry(index=r.index, reason=r.reason) for r in result.rejected],
            first_day=result.first_day,
            last_day=result.last_day,
        )
