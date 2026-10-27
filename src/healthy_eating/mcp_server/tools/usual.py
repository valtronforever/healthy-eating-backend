"""MCP tools for meals eaten again: usual meals, earlier meals with known values, re-logging.

Usual meals: healthy-eating-backend#26. Earlier meals with known values and re-logging at
another weight: owner decision 2026-10-27 (services/meal_search.py).
"""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.mcp_server.context import current_client_id, current_user_id
from healthy_eating.nutrition.types import Confidence, MealSource, TrackedNutrientField
from healthy_eating.services.meal_search import find_known_meals
from healthy_eating.services.meals import MAX_PORTION_GRAMS
from healthy_eating.services.usual_meals import (
    MealIdTakenError,
    PortionUnknownError,
    relog_meal,
    usual_meals,
)

LIST_USUAL_MEALS_DESCRIPTION = """\
List the user's usual meals in HealthyEating: dishes they ate at least twice in the last 90
days, most often first, each with the `meal_id` of its latest version and its values. Use it
when the user says they ate one of their usual meals again ("my usual breakfast", "same lunch
as yesterday") to find the meal to log again with relog_meal.
"""

RELOG_MEAL_DESCRIPTION = """\
Log a meal the user already logged once more in HealthyEating, with the same values and
provenance and a new time. Use it when the user ate the same as before ("same breakfast as
yesterday", "my usual oatmeal again", "the Danone yogurt again, 200 g"); get the `meal_id` from
find_meals, list_usual_meals or list_meals.
- Same food, another weight: pass `portion_grams`; every value scales to it. This needs the
  earlier meal's portion weight (find_meals results always have one).
- Anything else differs (ingredients, recipe): use log_meal instead.
Pass `request_id` (any new UUID) so a retried call saves the meal only once. Set `eaten_at` when
the user says when they ate it. Finish with the one-line `user_message`.
"""

FIND_MEALS_DESCRIPTION = """\
Find earlier meals of the user whose values are known: taken from a nutrition label, the user's
own numbers or their workflow, never estimated, with the portion weight. Each result gives its
values per 100 g. Use it before you estimate a packaged or branded product, or a dish the user
has logged before, so known values win over a new estimate.
- Search with the product, brand or dish name as the user said it. If nothing is found, try once
  more with the name in another language or script (for example "Danone" and "Данон").
- The same food again, at any weight: relog_meal with its `meal_id` and `portion_grams`.
- The food is part of a bigger meal: in log_meal, use its values per 100 g for that part (source
  "from_conversation", source_note "earlier <name>") and estimate only the rest.
- Say in one short line that the earlier values were used. No result: estimate as usual.
"""


class UsualMealItem(BaseModel):
    meal_id: uuid.UUID = Field(description="Pass to relog_meal to log this meal again")
    dish_name: str
    times: int = Field(description="How often it was eaten in the last 90 days")
    last_eaten_at: datetime = Field(description="Local time with the UTC offset")
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    source: MealSource
    estimated_fields: list[TrackedNutrientField]


class UsualMealList(BaseModel):
    meals: list[UsualMealItem] = Field(description="Most often eaten first")


class Per100g(BaseModel):
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    fiber_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = Field(default=None, description="mg per 100 g")


class KnownMealItem(BaseModel):
    meal_id: uuid.UUID = Field(
        description="Pass to relog_meal (with portion_grams) to log it again"
    )
    dish_name: str
    last_eaten_at: datetime = Field(description="Local time with the UTC offset")
    times: int = Field(description="How often it was eaten in the last year")
    portion_grams: float = Field(description="Weight of that earlier portion, g")
    per_100g: Per100g
    source_note: str | None = Field(
        description="Where the values came from, e.g. 'nutrition label'"
    )
    confidence: Confidence


class KnownMealList(BaseModel):
    meals: list[KnownMealItem] = Field(description="Best match first; empty when none is known")


def per_100g(values: dict[str, float]) -> Per100g:
    return Per100g.model_validate(values)


class RelogMealResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    meal_id: uuid.UUID = Field(description="The new meal; pass as replaces_meal_id to correct it")
    eaten_at: datetime = Field(description="Local time with the UTC offset")
    local_date: date


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        name="list_usual_meals",
        description=LIST_USUAL_MEALS_DESCRIPTION,
        annotations={"title": "List usual meals", "readOnlyHint": True, "openWorldHint": False},
    )
    async def list_usual_meals_tool() -> UsualMealList:
        async with db.session() as session:
            meals = await usual_meals(session, current_user_id(), now=utcnow())
        return UsualMealList(
            meals=[
                UsualMealItem(
                    meal_id=item.template.meal.id,
                    dish_name=item.template.meal.dish_name,
                    times=item.times,
                    last_eaten_at=item.template.eaten_at,
                    calories_kcal=item.template.meal.calories_kcal,
                    protein_g=item.template.meal.protein_g,
                    fat_g=item.template.meal.fat_g,
                    carbohydrates_g=item.template.meal.carbohydrates_g,
                    source=item.template.meal.source,
                    estimated_fields=item.template.meal.estimated_fields,
                )
                for item in meals
            ]
        )

    @mcp.tool(
        name="find_meals",
        description=FIND_MEALS_DESCRIPTION,
        annotations={"title": "Find earlier meals", "readOnlyHint": True, "openWorldHint": False},
    )
    async def find_meals_tool(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="Product, brand or dish name, e.g. 'Danone yogurt'",
            ),
        ],
    ) -> KnownMealList:
        async with db.session() as session:
            found = await find_known_meals(session, current_user_id(), query, now=utcnow())
        return KnownMealList(
            meals=[
                KnownMealItem(
                    meal_id=item.template.meal.id,
                    dish_name=item.template.meal.dish_name,
                    last_eaten_at=item.template.eaten_at,
                    times=item.times,
                    portion_grams=item.template.meal.portion_grams or 0,
                    per_100g=per_100g(item.per_100g),
                    source_note=item.template.meal.source_note,
                    confidence=item.template.meal.confidence,
                )
                for item in found
            ]
        )

    @mcp.tool(
        name="relog_meal",
        description=RELOG_MEAL_DESCRIPTION,
        annotations={
            "title": "Log a meal again",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def relog_meal_tool(
        meal_id: Annotated[uuid.UUID, Field(description="meal_id of the meal to log again")],
        request_id: Annotated[
            uuid.UUID | None,
            Field(description="Any new UUID; the same request_id saves the meal only once"),
        ] = None,
        eaten_at: Annotated[
            datetime | None,
            Field(
                description=(
                    "When it was eaten, ISO 8601 with the user's UTC offset if known; leave "
                    "empty for just now"
                )
            ),
        ] = None,
        portion_grams: Annotated[
            float | None,
            Field(
                gt=0,
                le=MAX_PORTION_GRAMS,
                description="Only when the weight differs: the new portion in grams",
            ),
        ] = None,
    ) -> RelogMealResult:
        try:
            async with db.session() as session:
                meal = await relog_meal(
                    session,
                    current_user_id(),
                    meal_id,
                    new_meal_id=request_id,
                    eaten_at=eaten_at,
                    client_id=current_client_id(),
                    now=utcnow(),
                    portion_grams=portion_grams,
                )
        except MealIdTakenError:
            raise ToolError("Not saved to HealthyEating: use another request_id.") from None
        except PortionUnknownError:
            raise ToolError(
                "Not saved to HealthyEating: the earlier meal has no portion weight to scale "
                "from. Use log_meal with the values for this portion."
            ) from None
        if meal is None:
            raise ToolError(
                "Not saved to HealthyEating: no saved meal has this meal_id. Use "
                "list_usual_meals or list_meals to find it."
            )
        row = meal.meal
        return RelogMealResult(
            saved_to_healthy_eating=True,
            user_message=(
                f"Saved to HealthyEating again: {row.dish_name}"
                + (f" ({row.portion_grams:g} g)" if portion_grams is not None else "")
                + f", {row.calories_kcal:g} kcal "
                f"(protein {row.protein_g:g} g, fat {row.fat_g:g} g, "
                f"carbohydrates {row.carbohydrates_g:g} g)."
            ),
            meal_id=row.id,
            eaten_at=meal.eaten_at,
            local_date=meal.eaten_at.date(),
        )
