import base64
import binascii
import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.nutrition.types import Confidence, MealSource, NutrientField
from healthy_eating.services.meals import LocalMeal, MealCursor, get_meal, list_meals

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
    created_at: datetime
    updated_at: datetime


class MealListResponse(BaseModel):
    meals: list[MealResponse] = Field(description="Newest first")
    next_cursor: str | None = Field(
        description="Pass as `cursor` to get the next page; null on the last page"
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
