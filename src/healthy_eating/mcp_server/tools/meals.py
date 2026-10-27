import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal
from healthy_eating.mcp_server.context import current_client_id, current_user_id
from healthy_eating.mcp_server.tools.usual import Per100g, per_100g
from healthy_eating.nutrition.micronutrients import MICRONUTRIENTS, ItemMatchStatus, Micronutrient
from healthy_eating.nutrition.types import (
    NUTRIENT_FIELDS,
    Confidence,
    MealSource,
    NutrientField,
    TrackedNutrientField,
)
from healthy_eating.services.food_matching import FoodItem, ItemMatch
from healthy_eating.services.meal_search import KnownMeal, earlier_known_meal
from healthy_eating.services.meals import (
    MAX_CALORIES_KCAL,
    MAX_DESCRIPTION_LENGTH,
    MAX_DISH_NAME_LENGTH,
    MAX_ITEMS,
    MAX_MACRONUTRIENT_G,
    MAX_PORTION_GRAMS,
    MAX_SODIUM_MG,
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
- Nothing calculated yet (only a photo or a description): for a packaged or branded product,
  or a dish the user may have logged before, first look for its known values with find_meals
  (the same food at another weight: relog_meal with portion_grams). Otherwise estimate all
  values yourself with source="estimated_by_assistant".
- Something is unclear (portion size, drinks, sauces, oil or butter): if you can still give
  a rough estimate, pass all four values with confidence "low" and put your questions in
  `open_questions`. The meal is saved now; when the user answers, call again with
  `replaces_meal_id` if the answer changes the values. Ask about a meal only once: after
  the user's answer, save your best estimate without new questions.
- Only if you cannot estimate a value at all, leave it empty and put your questions in
  `open_questions`; then nothing is saved until the user answers.
- Fiber, sugar and sodium are optional: pass them when they were calculated or you can
  estimate them, otherwise leave them out (never ask the user for them). The same rules
  apply: values you estimate are listed in `estimated_fields` with
  source="from_conversation".

Handling the result:
- Always give your normal answer first, including any estimate you can make, then the
  one-line `user_message`, then the questions in `questions_for_user` (if any). Never ask
  instead of answering.
- status "logged": the meal was saved. If `questions_for_user` is not empty, ask them after
  your answer. `earlier_meal` is set when an earlier meal with this name had known values that
  differ from your estimate; its question is one of them.
- status "needs_more_info": nothing was saved. Ask the returned questions, then call again.
  If the user cannot answer, use your best estimate with confidence "low".
- If the call fails with an error, tell the user the meal was NOT saved to HealthyEating,
  say why, and call again if the error says how to fix the input.

Also:
- Several dishes in one meal: save the total for the meal, unless the user tracks each
  dish separately.
- The user corrects a meal that was already saved: call again with `replaces_meal_id` set
  to the `meal_id` from the earlier result, so the meal is not counted twice.
- Meals eaten earlier ("yesterday evening", "this morning", "at lunch"): always set
  `eaten_at`, with the user's UTC offset if you know it. Without it the meal is saved as
  eaten right now and counts for the wrong day. Never ask the user for their offset or time
  zone. Leave `eaten_at` empty only for meals eaten just now.

Vitamins and minerals:
- Also pass `items`: each food of the meal with its weight in grams, named plainly as in a
  food table ("white rice, cooked", "chicken breast, grilled", "olive oil"). HealthyEating
  looks up their vitamins and minerals in USDA reference data; this never changes the
  calories and macronutrients you pass. Leave `items` out only when you cannot tell the foods.
- The result says which reference food each item matched. If an item matched nothing or the
  wrong food, you may look it up with find_foods and correct the meal with `replaces_meal_id`
  and the item's `food_id`. Do not mention vitamins and minerals unless the user asks.
- Supplements and packaged foods: pass the item with `label_values`, the vitamins and minerals
  printed on the label or stated by the user, for the amount taken (2 capsules = twice the
  label), converted to the unit in each key (vitamin D: 40 IU = 1 ug). Leave out a value you
  cannot read or convert exactly (for example vitamin A or E given only in IU); never estimate
  label values. Calories stay as for any meal (often near 0 for supplements).
"""

FIELD_LABELS: dict[NutrientField, str] = {
    "calories_kcal": "calories",
    "protein_g": "protein",
    "fat_g": "fat",
    "carbohydrates_g": "carbohydrates",
}
MAX_FIBER_OR_SUGAR_G = MAX_MACRONUTRIENT_G
# Upper bound for one label value in mg or ug (high-dose supplements stay far below).
MAX_LABEL_VALUE = 100_000


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
    fiber_g: float | None
    sugar_g: float | None
    sodium_mg: float | None
    estimated_fields: list[TrackedNutrientField] = Field(
        description="Values estimated by the assistant rather than taken from the user's workflow"
    )
    confidence: Confidence


class MealItemInput(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=MAX_DISH_NAME_LENGTH,
        description="The food, plainly, e.g. 'white rice, cooked', 'chicken breast, grilled'",
    )
    grams: float | None = Field(
        default=None,
        gt=0,
        le=MAX_PORTION_GRAMS,
        description=(
            "Weight eaten, g; without it (and without label_values) the item gets no values"
        ),
    )
    food_id: int | None = Field(
        default=None, description="A food_id from find_foods, when the name matched wrongly"
    )
    # Any keys: nutrients HealthyEating does not track (biotin, omega-3...) are left out and
    # reported back, so one of them never costs the user the whole meal.
    label_values: dict[str, Annotated[float, Field(ge=0, le=MAX_LABEL_VALUE)]] | None = Field(
        default=None,
        description=(
            "Only for supplements or packaged foods: vitamins and minerals printed on the label "
            "or stated by the user, for the amount taken, keyed by "
            + ", ".join(MICRONUTRIENTS)
            + " (the unit is the key's suffix, mg or ug). Other nutrients are not tracked and "
            "are left out"
        ),
    )


def food_item(item: MealItemInput) -> FoodItem:
    """The item to save; label values HealthyEating does not track are left out."""
    values: dict[Micronutrient, float] | None = None
    if item.label_values is not None:
        values = {
            code: item.label_values[code] for code in MICRONUTRIENTS if code in item.label_values
        }
    return FoodItem(item.name, item.grams, item.food_id, values)


def untracked_values(item: MealItemInput) -> list[str]:
    return sorted(key for key in item.label_values or {} if key not in MICRONUTRIENTS)


class ItemResult(BaseModel):
    name: str
    grams: float | None
    status: ItemMatchStatus = Field(
        description="'matched': vitamins and minerals come from `matched_food`; 'label': from "
        "the label values you passed; 'weak' or 'none': this item has none (see find_foods)"
    )
    matched_food: str | None = Field(description="The USDA food used, when matched")
    food_id: int | None = Field(description="Its food_id, when matched")
    closest_food: str | None = Field(description="With 'weak': the closest food, not used")
    untracked_values: list[str] = Field(
        default_factory=list,
        description="label_values keys HealthyEating does not track; they were left out",
    )


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
        description=(
            "Ask the user these after your normal answer. With status 'logged' the meal is "
            "already saved: call again with replaces_meal_id if the answer changes the values. "
            "With 'needs_more_info' call again with the answers."
        )
    )
    warnings: list[str] = Field(description="Possible problems with the values")
    meal: SavedMeal | None = Field(description="The saved meal, when status is 'logged'")
    items: list[ItemResult] = Field(
        default_factory=list, description="How each item matched the USDA reference data"
    )
    earlier_meal: "EarlierMeal | None" = Field(
        default=None,
        description=(
            "An earlier meal with this name whose values were known (label, the user's numbers) "
            "and differ from this estimate; its question is in questions_for_user"
        ),
    )


class EarlierMeal(BaseModel):
    meal_id: uuid.UUID = Field(description="The earlier meal; relog_meal can log it again")
    dish_name: str
    eaten_at: datetime = Field(description="Local time with the UTC offset")
    per_100g: Per100g
    source_note: str | None


LogMealResult.model_rebuild()


def _earlier_meal(known: KnownMeal) -> EarlierMeal:
    return EarlierMeal(
        meal_id=known.template.meal.id,
        dish_name=known.template.meal.dish_name,
        eaten_at=known.template.eaten_at,
        per_100g=per_100g(known.per_100g),
        source_note=known.template.meal.source_note,
    )


def _earlier_question(known: KnownMeal) -> str:
    when = known.template.eaten_at.date().isoformat()
    calories = known.per_100g["calories_kcal"]
    return (
        f"On {when} you logged {known.template.meal.dish_name} with known values "
        f"({calories:g} kcal per 100 g). Use those for this meal too?"
    )


def _item_results(matches: list[ItemMatch] | None, untracked: list[list[str]]) -> list[ItemResult]:
    results = []
    for index, match in enumerate(matches or []):
        matched = match.status == "matched" and match.food is not None
        results.append(
            ItemResult(
                name=match.item.name,
                grams=match.item.grams,
                status=match.status,
                matched_food=match.food.description if matched and match.food else None,
                food_id=match.food.id if matched and match.food else None,
                closest_food=(
                    match.food.description if match.status == "weak" and match.food else None
                ),
                untracked_values=untracked[index] if index < len(untracked) else [],
            )
        )
    return results


def _saved_message(meal: MealEstimate, *, refinable: bool = False) -> str:
    verb = "Updated in" if meal.replaces_meal_id else "Saved to"
    message = (
        f"{verb} HealthyEating: {meal.dish_name}, {meal.calories_kcal:g} kcal "
        f"(protein {meal.protein_g:g} g, fat {meal.fat_g:g} g, "
        f"carbohydrates {meal.carbohydrates_g:g} g)"
    )
    # Only call out estimates that complete someone else's calculation, for the values shown.
    shown = [name for name in NUTRIENT_FIELDS if name in meal.estimated_fields]
    if meal.source == "from_conversation" and shown:
        labels = [FIELD_LABELS[name] for name in shown]
        message += f"; {', '.join(labels)} estimated by the assistant"
    message += "."
    if refinable:
        message += " I'll update it if your answer changes the estimate."
    return message


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
        fiber_g=row.fiber_g,
        sugar_g=row.sugar_g,
        sodium_mg=row.sodium_mg,
        estimated_fields=row.estimated_fields,
        confidence=row.confidence,
    )


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        description=LOG_MEAL_DESCRIPTION,
        annotations={
            "title": "Save a meal",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def log_meal(
        dish_name: Annotated[
            str,
            Field(
                min_length=1,
                max_length=MAX_DISH_NAME_LENGTH,
                description="Name of the dish, e.g. 'Caesar salad'",
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
            list[TrackedNutrientField] | None,
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
                max_length=MAX_DESCRIPTION_LENGTH,
                description="Visible or stated ingredients, cooking method, portion notes",
            ),
        ] = None,
        portion_grams: Annotated[
            float | None, Field(gt=0, le=MAX_PORTION_GRAMS, description="Portion weight in grams")
        ] = None,
        calories_kcal: Annotated[
            float | None,
            Field(ge=0, le=MAX_CALORIES_KCAL, description="Energy of the whole portion, kcal"),
        ] = None,
        protein_g: Annotated[
            float | None,
            Field(ge=0, le=MAX_MACRONUTRIENT_G, description="Protein in the whole portion, g"),
        ] = None,
        fat_g: Annotated[
            float | None,
            Field(ge=0, le=MAX_MACRONUTRIENT_G, description="Fat in the whole portion, g"),
        ] = None,
        carbohydrates_g: Annotated[
            float | None,
            Field(
                ge=0, le=MAX_MACRONUTRIENT_G, description="Carbohydrates in the whole portion, g"
            ),
        ] = None,
        fiber_g: Annotated[
            float | None,
            Field(
                ge=0, le=MAX_FIBER_OR_SUGAR_G, description="Optional: fiber in the whole portion, g"
            ),
        ] = None,
        sugar_g: Annotated[
            float | None,
            Field(
                ge=0,
                le=MAX_FIBER_OR_SUGAR_G,
                description="Optional: sugars in the whole portion, g (part of the carbohydrates)",
            ),
        ] = None,
        sodium_mg: Annotated[
            float | None,
            Field(
                ge=0,
                le=MAX_SODIUM_MG,
                description="Optional: sodium in the whole portion, mg (not salt: salt is 2.5x)",
            ),
        ] = None,
        eaten_at: Annotated[
            datetime | None,
            Field(
                description=(
                    "Only for meals eaten earlier: when, as ISO 8601, with the user's UTC offset "
                    "if known (e.g. '2026-10-18T08:30:00+02:00'); a time without an offset is "
                    "read in the user's time zone. Leave empty for 'just now'."
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
        items: Annotated[
            list[MealItemInput] | None,
            Field(
                max_length=MAX_ITEMS,
                description=(
                    "The meal's foods with grams, for vitamins and minerals; with "
                    "replaces_meal_id, new items replace the old ones and no items keeps them"
                ),
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
                fiber_g=fiber_g,
                sugar_g=sugar_g,
                sodium_mg=sodium_mg,
                estimated_fields=tuple(estimated_fields or ()),
                confidence=confidence,
                eaten_at=eaten_at,
                replaces_meal_id=replaces_meal_id,
                open_questions=open_questions or [],
                items=(tuple(food_item(i) for i in items) if items is not None else None),
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
                    "Nothing was saved. Give your normal answer first, unchanged and with any "
                    "estimate you can make. Then tell the user `user_message`, ask the "
                    "questions below and call log_meal again."
                ),
                missing_fields=evaluation.missing_fields,
                questions_for_user=evaluation.questions_for_user,
                warnings=evaluation.warnings,
                meal=None,
            )

        meal = evaluation.meal
        try:
            async with db.session() as session:
                result = await save_meal(session, current_user_id(), meal, current_client_id())
                saved = _saved_meal(result.meal)
                # Only for a new meal: a correction already settled its values.
                earlier = (
                    await earlier_known_meal(session, current_user_id(), result.meal, now=utcnow())
                    if replaces_meal_id is None
                    else None
                )
        except MealNotFoundError:
            raise ToolError(
                "Not saved to HealthyEating: the meal to correct (replaces_meal_id) was not "
                "found. Save it as a new meal instead."
            ) from None

        questions = list(evaluation.questions_for_user)
        message = (
            "The meal was saved. Keep your normal answer, add `user_message` as one line "
            "and mention any warnings."
        )
        if earlier is not None:
            questions.append(_earlier_question(earlier))
            message += (
                " `earlier_meal` has known values for this food. If the user agrees to use "
                "them, call log_meal with replaces_meal_id and the values per 100 g scaled to "
                "this portion (source 'from_conversation', not estimated)."
            )
        if questions:
            message += (
                " Then ask `questions_for_user`; if the answer changes the values, call "
                "log_meal again with replaces_meal_id."
            )
        return LogMealResult(
            status="logged",
            saved_to_healthy_eating=True,
            user_message=_saved_message(meal, refinable=bool(questions)),
            message=message,
            missing_fields=[],
            questions_for_user=questions,
            warnings=evaluation.warnings,
            meal=saved,
            items=_item_results(result.matches, [untracked_values(i) for i in items or []]),
            earlier_meal=_earlier_meal(earlier) if earlier is not None else None,
        )
