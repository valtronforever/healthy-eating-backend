import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.models import Meal
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.types import Confidence, MealSource, NutrientField
from healthy_eating.services.meals import (
    MealEstimate,
    MealNotFoundError,
    evaluate_meal,
    save_meal,
)

LOG_MEAL_DESCRIPTION = """\
Save a meal the user ate to HealthyEating: calories, protein, fat and carbohydrates.

HealthyEating is an addition to the user's existing way of tracking food, never a
replacement:
- First do everything the user's workflow already does, exactly as before: answer in
  their usual format with their own calculation method or project instructions, and keep
  using any other tools they track food with (for example a Google Sheets connector).
- Then call this tool and finish with one short line saying whether the meal was saved.

HealthyEating needs all four values for every meal:
- Numbers already calculated in this conversation (by the user's workflow, the user, a
  spreadsheet, a nutrition label or another app): pass them unchanged with
  source="from_conversation" and never alter them. Estimate the values that calculation
  did not produce and list them in `estimated_fields`.
- Nothing calculated yet (only a photo or a description): estimate all values yourself
  with source="estimated_by_assistant".
- If you cannot estimate a value reliably (portion size, ingredients or cooking method
  such as oil, butter or sauces are unknown), leave it empty and put your questions in
  `open_questions`.

Handling the result:
- status "needs_more_info": nothing was saved. Ask the user the returned questions as a
  short follow-up after your normal answer (never instead of it), then call again. If the
  user cannot answer, use your best estimate with confidence "low" and no `open_questions`.
- After every call, tell the user whether the meal was saved to HealthyEating by repeating
  `user_message`. If the call fails with an error, tell the user the meal was NOT saved to
  HealthyEating and show the error.

Also:
- Several dishes in one meal: save the total for the meal, unless the user tracks each
  dish separately.
- The user corrects a meal that was already saved: call again with `replaces_meal_id` set
  to the `meal_id` from the earlier result, so the meal is not counted twice.
- Meals eaten earlier (for example "save what I had for breakfast"): set `eaten_at`.
"""

FIELD_LABELS: dict[NutrientField, str] = {
    "calories_kcal": "calories",
    "protein_g": "protein",
    "fat_g": "fat",
    "carbohydrates_g": "carbohydrates",
}


class SavedMeal(BaseModel):
    meal_id: uuid.UUID = Field(description="Pass as replaces_meal_id to correct this meal")
    dish_name: str
    source: MealSource
    source_note: str | None
    eaten_at: datetime
    portion_grams: float | None
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    estimated_fields: list[NutrientField] = Field(
        description="Values estimated by the assistant rather than taken from the user's workflow"
    )
    confidence: Confidence


class LogMealResult(BaseModel):
    status: Literal["logged", "needs_more_info"]
    saved_to_healthy_eating: bool = Field(description="Whether the meal was saved")
    user_message: str = Field(
        description="Tell the user this, so they know whether the meal was saved to HealthyEating"
    )
    message: str = Field(description="What happened and what the assistant should do next")
    missing_fields: list[NutrientField] = Field(
        description="Nutrition values that are still unknown"
    )
    questions_for_user: list[str] = Field(
        description="Ask the user these questions after your normal answer, then call again"
    )
    warnings: list[str] = Field(description="Possible problems with the values")
    meal: SavedMeal | None = Field(description="The saved meal, when status is 'logged'")


def _saved_message(meal: MealEstimate) -> str:
    verb = "Updated in" if meal.replaces_meal_id else "Saved to"
    message = (
        f"{verb} HealthyEating: {meal.dish_name}, {meal.calories_kcal:g} kcal "
        f"(protein {meal.protein_g:g} g, fat {meal.fat_g:g} g, "
        f"carbohydrates {meal.carbohydrates_g:g} g)"
    )
    # Only call out estimates that complete someone else's calculation.
    if meal.source == "from_conversation" and meal.estimated_fields:
        labels = [FIELD_LABELS[name] for name in meal.estimated_fields]
        message += f"; {', '.join(labels)} estimated by the assistant"
    return message + "."


def _saved_meal(row: Meal) -> SavedMeal:
    return SavedMeal(
        meal_id=row.id,
        dish_name=row.dish_name,
        source=row.source,
        source_note=row.source_note,
        eaten_at=row.eaten_at,
        portion_grams=row.portion_grams,
        calories_kcal=row.calories_kcal,
        protein_g=row.protein_g,
        fat_g=row.fat_g,
        carbohydrates_g=row.carbohydrates_g,
        estimated_fields=row.estimated_fields,
        confidence=row.confidence,
    )


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        description=LOG_MEAL_DESCRIPTION,
        annotations={"title": "Save a meal", "readOnlyHint": False, "idempotentHint": False},
    )
    async def log_meal(
        dish_name: Annotated[
            str,
            Field(
                min_length=1, max_length=200, description="Name of the dish, e.g. 'Caesar salad'"
            ),
        ],
        source: Annotated[
            MealSource,
            Field(
                description=(
                    "'from_conversation' if values were already calculated before (the user's "
                    "workflow, a spreadsheet, a label, another app); 'estimated_by_assistant' "
                    "if you estimated all values now"
                )
            ),
        ],
        estimated_fields: Annotated[
            list[NutrientField] | None,
            Field(
                description=(
                    "With source='from_conversation': the values you estimated yourself "
                    "because the existing calculation did not produce them"
                )
            ),
        ] = None,
        source_note: Annotated[
            str | None,
            Field(
                max_length=200,
                description=(
                    "Where existing numbers came from, e.g. 'ChatGPT project instructions', "
                    "'Google Sheets log', 'nutrition label'"
                ),
            ),
        ] = None,
        confidence: Annotated[
            Confidence,
            Field(description="How reliable the values are; 'high' for labels and weighed food"),
        ] = "medium",
        description: Annotated[
            str | None,
            Field(
                max_length=1000,
                description="Visible or stated ingredients, cooking method, portion notes",
            ),
        ] = None,
        portion_grams: Annotated[
            float | None, Field(gt=0, le=5000, description="Portion weight in grams")
        ] = None,
        calories_kcal: Annotated[
            float | None, Field(ge=0, le=10000, description="Energy of the whole portion, kcal")
        ] = None,
        protein_g: Annotated[
            float | None, Field(ge=0, le=1000, description="Protein in the whole portion, g")
        ] = None,
        fat_g: Annotated[
            float | None, Field(ge=0, le=1000, description="Fat in the whole portion, g")
        ] = None,
        carbohydrates_g: Annotated[
            float | None,
            Field(ge=0, le=1000, description="Carbohydrates in the whole portion, g"),
        ] = None,
        eaten_at: Annotated[
            AwareDatetime | None,
            Field(
                description=(
                    "When the meal was eaten, ISO 8601 with timezone offset. "
                    "Leave empty for 'just now'."
                )
            ),
        ] = None,
        replaces_meal_id: Annotated[
            uuid.UUID | None,
            Field(description="meal_id of a previously saved meal that this call corrects"),
        ] = None,
        open_questions: Annotated[
            list[str] | None,
            Field(
                max_length=5,
                description="Questions for the user about values you could not estimate",
            ),
        ] = None,
    ) -> LogMealResult:
        evaluation = evaluate_meal(
            MealEstimate(
                dish_name=dish_name,
                source=source,
                source_note=source_note,
                description=description,
                portion_grams=portion_grams,
                calories_kcal=calories_kcal,
                protein_g=protein_g,
                fat_g=fat_g,
                carbohydrates_g=carbohydrates_g,
                estimated_fields=tuple(estimated_fields or ()),
                confidence=confidence,
                eaten_at=eaten_at,
                replaces_meal_id=replaces_meal_id,
                open_questions=open_questions or [],
            )
        )

        if not evaluation.complete:
            return LogMealResult(
                status="needs_more_info",
                saved_to_healthy_eating=False,
                user_message=(
                    f"Not saved to HealthyEating yet: I need a few more details about {dish_name}."
                ),
                message=(
                    "Nothing was saved. Give your normal answer first, unchanged. Then tell the "
                    "user `user_message`, ask the questions below and call log_meal again."
                ),
                missing_fields=evaluation.missing_fields,
                questions_for_user=evaluation.questions_for_user,
                warnings=evaluation.warnings,
                meal=None,
            )

        meal = evaluation.meal
        try:
            async with db.session() as session:
                row = await save_meal(session, current_user_id(), meal)
                saved = _saved_meal(row)
        except MealNotFoundError:
            raise ToolError(
                "Not saved to HealthyEating: the meal to correct (replaces_meal_id) was not "
                "found. Save it as a new meal instead."
            ) from None

        return LogMealResult(
            status="logged",
            saved_to_healthy_eating=True,
            user_message=_saved_message(meal),
            message=(
                "The meal was saved. Keep your normal answer, add `user_message` as one line "
                "and mention any warnings."
            ),
            missing_fields=[],
            questions_for_user=[],
            warnings=evaluation.warnings,
            meal=saved,
        )
