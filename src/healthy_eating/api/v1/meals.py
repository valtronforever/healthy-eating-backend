import base64
import binascii
import uuid
from datetime import date, datetime
from typing import Annotated, Self

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.nutrition.types import Confidence, MealSource, NutrientField
from healthy_eating.services.meals import (
    MAX_CALORIES_KCAL,
    MAX_DISH_NAME_LENGTH,
    MAX_MACRONUTRIENT_G,
    MAX_PORTION_GRAMS,
    LocalMeal,
    MealCursor,
    MealEdit,
    delete_meal,
    edit_meal,
    get_meal,
    list_meals,
)
from healthy_eating.services.unset import UNSET

router = APIRouter(tags=["meals"])


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
    source: MealSource = Field(
        description=(
            "'from_conversation': values came from the user's own workflow; "
            "'estimated_by_assistant': the assistant estimated them"
        )
    )
    source_note: str | None
    estimated_fields: list[NutrientField] = Field(
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
    """Only the fields present are changed; only portion_grams may be null (removes it).

    Changed nutrition values become the user's own and lose their "estimated" mark.
    """

    model_config = ConfigDict(extra="forbid")

    dish_name: str | None = Field(default=None, min_length=1, max_length=MAX_DISH_NAME_LENGTH)
    portion_grams: float | None = Field(default=None, gt=0, le=MAX_PORTION_GRAMS)
    calories_kcal: float | None = Field(default=None, ge=0, le=MAX_CALORIES_KCAL)
    protein_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    fat_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    carbohydrates_g: float | None = Field(default=None, ge=0, le=MAX_MACRONUTRIENT_G)
    eaten_at: datetime | None = Field(
        default=None, description="ISO 8601; without an offset it is read in the user's time zone"
    )

    @model_validator(mode="after")
    def _check_not_null(self) -> Self:
        for name in self.model_fields_set - {"portion_grams"}:
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
        source=meal.source,
        source_note=meal.source_note,
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


@router.get("/meals")
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


@router.get("/meals/{meal_id}")
async def read_meal(
    meal_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep
) -> MealResponse:
    """One of the signed-in user's meals."""
    meal = await get_meal(session, user_id, meal_id)
    if meal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return _meal_response(meal)


@router.patch("/meals/{meal_id}")
async def update_meal(
    meal_id: uuid.UUID, update: MealUpdate, user_id: CurrentUserId, session: SessionDep
) -> MealUpdateResponse:
    """Correct one of the signed-in user's meals in place (same meal id)."""
    edited = await edit_meal(session, user_id, meal_id, update.to_edit())
    if edited is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return MealUpdateResponse(meal=_meal_response(edited.meal), warnings=edited.warnings)


@router.delete("/meals/{meal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_meal(meal_id: uuid.UUID, user_id: CurrentUserId, session: SessionDep) -> Response:
    """Delete one of the signed-in user's meals."""
    if not await delete_meal(session, user_id, meal_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Meal not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
