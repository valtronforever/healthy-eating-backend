import base64
import binascii
import uuid
from datetime import date, datetime
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, model_validator

from healthy_eating.api.deps import CurrentUserId, SessionDep, bearer_scheme
from healthy_eating.api.errors import error_response
from healthy_eating.db.base import utcnow
from healthy_eating.nutrition.micronutrients import ItemMatchStatus, Micronutrient
from healthy_eating.nutrition.types import (
    OPTIONAL_NUTRIENT_FIELDS,
    Confidence,
    MealSource,
    TrackedNutrientField,
)
from healthy_eating.services.connections import client_of_access_token
from healthy_eating.services.meals import (
    MAX_CALORIES_KCAL,
    MAX_DISH_NAME_LENGTH,
    MAX_MACRONUTRIENT_G,
    MAX_PORTION_GRAMS,
    MAX_SODIUM_MG,
    LocalMeal,
    MealCursor,
    MealEdit,
    delete_meal,
    edit_meal,
    get_meal,
    list_meals,
)
from healthy_eating.services.unset import UNSET
from healthy_eating.services.usual_meals import MealIdTakenError, relog_meal, usual_meals

router = APIRouter(tags=["meals"])


class MealItemResponse(BaseModel):
    name: str
    grams: float
    status: ItemMatchStatus = Field(
        description="'matched': its vitamins and minerals come from `food_description`"
    )
    food_description: str | None = Field(description="The USDA food used, when matched")


class LabelItemResponse(BaseModel):
    name: str
    grams: float | None = Field(description="Weight, if known (a capsule has none)")
    values: dict[Micronutrient, float] = Field(
        description="Vitamins and minerals from the label, for the amount taken"
    )


class MealResponse(BaseModel):
    id: uuid.UUID
    dish_name: str
    description: str | None
    eaten_at: datetime = Field(
        description=(
            "When the meal was eaten, in its local time with the UTC offset: the offset the "
            "user gave, or the one of their time zone"
        )
    )
    local_date: date = Field(description="The user's local day the meal counts for")
    portion_grams: float | None
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbohydrates_g: float
    fiber_g: float | None = Field(description="Optional; null when the meal has no value")
    sugar_g: float | None = Field(description="Optional, part of the carbohydrates")
    sodium_mg: float | None = Field(description="Optional, in mg")
    source: MealSource = Field(
        description=(
            "'from_conversation': values came from the user's own workflow; "
            "'estimated_by_assistant': the assistant estimated them"
        )
    )
    source_note: str | None
    micronutrients: dict[Micronutrient, float] | None = Field(
        description=(
            "Vitamins and minerals of the matched food items (USDA FoodData Central); the unit is "
            "the suffix (mg or ug). Null when the meal has no items; a nutrient no matched item "
            "has a value for is left out"
        )
    )
    items: list[MealItemResponse] = Field(
        description="The meal's food items matched against USDA reference data, in order"
    )
    label_items: list[LabelItemResponse] = Field(
        description=(
            "Items whose vitamins and minerals come from their label or the user (supplements, "
            "packaged foods), in order; included in `micronutrients`"
        )
    )
    imported_from: str | None = Field(
        description=(
            "Set for meals imported from the user's earlier tracking (e.g. 'ChatGPT project'); "
            "null for meals logged as they were eaten"
        )
    )
    estimated_fields: list[TrackedNutrientField] = Field(
        description="Values the assistant estimated rather than took from the user's workflow"
    )
    confidence: Confidence
    client_name: str | None = Field(description="The AI assistant that saved the meal, if known")
    created_at: datetime
    updated_at: datetime


class MealListResponse(BaseModel):
    meals: list[MealResponse] = Field(description="Newest first")
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page"
    )


class MealUpdate(BaseModel):
    """Only the fields present are changed; portion_grams, fiber_g, sugar_g and sodium_mg may be
    null (removes the value).

    Changed nutrition values become the user's own and lose their "estimated" mark.
    """

    model_config = ConfigDict(extra="forbid")

    dish_name: str | None = Field(default=None, min_length=1, max_length=MAX_DISH_NAME_LENGTH)
    portion_grams: float | None = Field(default=None, gt=0, le=MAX_PORTION_GRAMS)
    calories_kcal: float | None = Field(default=None, ge=0, le=MAX_CALORIES_KCAL)
    protein_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    fat_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    carbohydrates_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    fiber_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    sugar_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    sodium_mg: float | None = Field(default=None, ge=0, le=MAX_SODIUM_MG)
    eaten_at: datetime | None = Field(
        default=None, description="ISO 8601; without an offset it is read in the user's time zone"
    )

    @model_validator(mode="after")
    def _check_not_null(self) -> Self:
        for name in self.model_fields_set - {"portion_grams", *OPTIONAL_NUTRIENT_FIELDS}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def to_edit(self) -> MealEdit:
        given = {name: getattr(self, name) for name in self.model_fields_set}
        return MealEdit(**{name: given.get(name, UNSET) for name in type(self).model_fields})


class MealUpdateResponse(BaseModel):
    meal: MealResponse
    warnings: list[str] = Field(
        description="Possible problems with the values, e.g. calories that do not match the macros"
    )


def _meal_response(local: LocalMeal) -> MealResponse:
    meal = local.meal
    return MealResponse(
        id=meal.id,
        dish_name=meal.dish_name,
        description=meal.description,
        eaten_at=local.eaten_at,
        local_date=local.eaten_at.date(),
        portion_grams=meal.portion_grams,
        calories_kcal=meal.calories_kcal,
        protein_g=meal.protein_g,
        fat_g=meal.fat_g,
        carbohydrates_g=meal.carbohydrates_g,
        fiber_g=meal.fiber_g,
        sugar_g=meal.sugar_g,
        sodium_mg=meal.sodium_mg,
        source=meal.source,
        source_note=meal.source_note,
        imported_from=meal.imported_from,
        micronutrients=meal.micronutrients,
        items=[
            MealItemResponse(
                name=item.name,
                grams=item.grams,
                status=item.status,
                food_description=item.food_description,
            )
            for item in meal.items
            if item.status != "label" and item.grams is not None
        ],
        label_items=[
            LabelItemResponse(name=item.name, grams=item.grams, values=item.label_values or {})
            for item in meal.items
            if item.status == "label"
        ],
        estimated_fields=meal.estimated_fields,
        confidence=meal.confidence,
        client_name=local.client_name,
        created_at=meal.created_at,
        updated_at=meal.updated_at,
    )


def _encode_cursor(cursor: MealCursor) -> str:
    raw = f"{cursor.eaten_at.isoformat()}|{cursor.meal_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str) -> MealCursor:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        eaten_at, meal_id = raw.split("|")
        cursor = MealCursor(eaten_at=datetime.fromisoformat(eaten_at), meal_id=uuid.UUID(meal_id))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid cursor") from None
    if cursor.eaten_at.utcoffset() is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid cursor")
    return cursor


@router.get(
    "/meals", operation_id="listMeals", responses={400: error_response("Invalid range or cursor")}
)
async def read_meals(
    user_id: CurrentUserId,
    session: SessionDep,
    from_: Annotated[
        date | None,
        Query(alias="from", description="First local day (YYYY-MM-DD), inclusive"),
    ] = None,
    to: Annotated[date | None, Query(description="Last local day (YYYY-MM-DD), inclusive")] = None,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MealListResponse:
    """The signed-in user's meals on the given local days, newest first."""
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="`from` is after `to`")
    page = await list_meals(
        session,
        user_id,
        first_day=from_,
        last_day=to,
        after=None if cursor is None else _decode_cursor(cursor),
        limit=limit,
    )
    return MealListResponse(
        meals=[_meal_response(meal) for meal in page.meals],
        next_cursor=None if page.next_cursor is None else _encode_cursor(page.next_cursor),
    )


@router.get(
    "/meals/{meal_id}", operation_id="getMeal", responses={404: error_response("Not found")}
)
async def read_meal(
    meal_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep
) -> MealResponse:
    """One of the signed-in user's meals."""
    meal = await get_meal(session, user_id, meal_id)
    if meal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return _meal_response(meal)


@router.patch(
    "/meals/{meal_id}", operation_id="updateMeal", responses={404: error_response("Not found")}
)
async def update_meal(
    meal_id: uuid.UUID, update: MealUpdate, user_id: CurrentUserId, session: SessionDep
) -> MealUpdateResponse:
    """Correct one of the signed-in user's meals in place (same meal id)."""
    edited = await edit_meal(session, user_id, meal_id, update.to_edit())
    if edited is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return MealUpdateResponse(meal=_meal_response(edited.meal), warnings=edited.warnings)


class UsualMealResponse(BaseModel):
    meal: MealResponse = Field(
        description="The latest meal with this name: what a re-log copies (use its id)"
    )
    times: int = Field(description="How often the dish was eaten in the last 90 days")


class UsualMealsResponse(BaseModel):
    meals: list[UsualMealResponse] = Field(
        description="Dishes eaten at least twice in the last 90 days, most often eaten first"
    )


class MealRelog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Id for the new meal, chosen by the client; sending the same request again returns "
            "that meal instead of saving a second one"
        ),
    )
    eaten_at: datetime | None = Field(
        default=None,
        description="ISO 8601; default now. Without an offset it is read in the user's time zone",
    )


BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]


@router.get("/usual-meals", operation_id="listUsualMeals")
async def read_usual_meals(user_id: CurrentUserId, session: SessionDep) -> UsualMealsResponse:
    """The user's usual meals, to log one again with one tap."""
    meals = await usual_meals(session, user_id, now=utcnow())
    return UsualMealsResponse(
        meals=[
            UsualMealResponse(meal=_meal_response(item.template), times=item.times)
            for item in meals
        ]
    )


@router.post(
    "/meals/{meal_id}/relog",
    status_code=status.HTTP_201_CREATED,
    operation_id="relogMeal",
    responses={
        404: error_response("Not found"),
        409: error_response("The id is taken by another meal"),
    },
)
async def relog(
    meal_id: uuid.UUID,
    body: MealRelog,
    user_id: CurrentUserId,
    session: SessionDep,
    credentials: BearerCredentials,
) -> MealResponse:
    """Log one of the user's meals again: same values and provenance, a new time.

    The app never creates meals from scratch; this copies an existing one.
    """
    client_id = (
        None
        if credentials is None
        else await client_of_access_token(session, credentials.credentials)
    )
    try:
        meal = await relog_meal(
            session,
            user_id,
            meal_id,
            new_meal_id=body.id,
            eaten_at=body.eaten_at,
            client_id=client_id,
            now=utcnow(),
        )
    except MealIdTakenError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Choose another id") from None
    if meal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return _meal_response(meal)


@router.delete(
    "/meals/{meal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteMeal",
    responses={404: error_response("Not found")},
)
async def remove_meal(meal_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep) -> Response:
    """Delete one of the signed-in user's meals."""
    if not await delete_meal(session, user_id, meal_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
