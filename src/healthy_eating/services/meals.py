"""Meal logging: validates a nutrition estimate, decides what is missing, and saves it."""

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal
from healthy_eating.nutrition.types import (
    NUTRIENT_FIELDS,
    Confidence,
    MealSource,
    NutrientField,
)
from healthy_eating.services.local_time import (
    local_date_column,
    local_eaten_at,
    resolve_eaten_at,
    utc_window,
)
from healthy_eating.services.settings import get_time_zone

# Atwater factors: kcal per gram of protein, fat and carbohydrates.
KCAL_PER_G_PROTEIN = 4.0
KCAL_PER_G_FAT = 9.0
KCAL_PER_G_CARBS = 4.0

# Stated calories and calories derived from macros may differ by this much before we warn.
ENERGY_TOLERANCE_RATIO = 0.2
ENERGY_TOLERANCE_KCAL = 50.0

PORTION_QUESTION = (
    "How big was the portion? A weight in grams is best; otherwise a size such as "
    "'a full dinner plate' or '2 slices'."
)
INGREDIENTS_QUESTION = (
    "What are the main ingredients, and how was it cooked (for example fried in oil, "
    "with butter or a creamy sauce)?"
)
COMPLETE_DETAILS_QUESTION = (
    "To save the full nutrition values to HealthyEating: what were the main ingredients, "
    "and roughly how big was the portion?"
)


@dataclass(frozen=True)
class MealEstimate:
    dish_name: str
    source: MealSource = "estimated_by_assistant"
    source_note: str | None = None
    description: str | None = None
    portion_grams: float | None = None
    calories_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbohydrates_g: float | None = None
    # Values the assistant estimated itself, as opposed to values from the user's workflow.
    estimated_fields: tuple[NutrientField, ...] = ()
    confidence: Confidence = "medium"
    eaten_at: datetime | None = None
    replaces_meal_id: uuid.UUID | None = None
    open_questions: list[str] = field(default_factory=list)

    @property
    def missing_fields(self) -> list[NutrientField]:
        return [name for name in NUTRIENT_FIELDS if getattr(self, name) is None]


@dataclass(frozen=True)
class MealEvaluation:
    complete: bool
    # The estimate to save, with derived calories and normalized `estimated_fields`.
    meal: MealEstimate
    missing_fields: list[NutrientField]
    questions_for_user: list[str]
    warnings: list[str]


def energy_from_macros(protein_g: float, fat_g: float, carbohydrates_g: float) -> float:
    return (
        protein_g * KCAL_PER_G_PROTEIN + fat_g * KCAL_PER_G_FAT + carbohydrates_g * KCAL_PER_G_CARBS
    )


def evaluate_meal(estimate: MealEstimate) -> MealEvaluation:
    """Decide whether the meal can be saved or the user has to be asked first.

    Every meal needs all four values. Values from the user's own calculation are kept
    unchanged; anything else counts as estimated. A meal with all four values is saved even
    when the assistant still has open questions: the questions are passed on and the answer
    updates the same meal, so an unanswered question never loses a meal. A complete
    correction (`replaces_meal_id`) passes no questions on, so each meal is asked about once.
    """
    meal = estimate
    warnings = []

    protein, fat, carbs = meal.protein_g, meal.fat_g, meal.carbohydrates_g
    if meal.calories_kcal is None and protein is not None and fat is not None and carbs is not None:
        derived = energy_from_macros(protein, fat, carbs)
        meal = replace(
            meal,
            calories_kcal=round(derived),
            estimated_fields=(*meal.estimated_fields, "calories_kcal"),
        )
        warnings.append("Calories were derived from the macronutrients (4/9/4 kcal per g).")

    if meal.source == "estimated_by_assistant":
        estimated: tuple[NutrientField, ...] = NUTRIENT_FIELDS
    else:
        estimated = tuple(name for name in NUTRIENT_FIELDS if name in meal.estimated_fields)
    meal = replace(
        meal,
        estimated_fields=tuple(name for name in estimated if getattr(meal, name) is not None),
    )

    missing = meal.missing_fields
    questions = list(meal.open_questions)
    if meal.replaces_meal_id is not None and not missing:
        questions = []
    if missing and not questions:
        if meal.source == "from_conversation":
            questions.append(COMPLETE_DETAILS_QUESTION)
        else:
            if meal.portion_grams is None:
                questions.append(PORTION_QUESTION)
            questions.append(INGREDIENTS_QUESTION)

    # Derived calories match the macronutrients by definition; only check stated calories.
    if not missing and estimate.calories_kcal is not None:
        warnings.extend(_energy_mismatch(meal))

    return MealEvaluation(
        complete=not missing,
        meal=meal,
        missing_fields=missing,
        questions_for_user=questions,
        warnings=warnings,
    )


def _energy_mismatch(meal: MealEstimate) -> list[str]:
    assert meal.calories_kcal is not None
    assert meal.protein_g is not None
    assert meal.fat_g is not None
    assert meal.carbohydrates_g is not None
    derived = energy_from_macros(meal.protein_g, meal.fat_g, meal.carbohydrates_g)
    difference = abs(derived - meal.calories_kcal)
    if difference <= ENERGY_TOLERANCE_KCAL:
        return []
    if difference <= ENERGY_TOLERANCE_RATIO * max(meal.calories_kcal, derived):
        return []
    return [
        f"Calories ({meal.calories_kcal:.0f} kcal) do not match the macronutrients "
        f"({derived:.0f} kcal from 4/9/4 kcal per g of protein/fat/carbohydrates)."
    ]


class MealNotFoundError(Exception):
    """The meal to correct does not exist or belongs to another user."""


async def save_meal(session: AsyncSession, user_id: uuid.UUID, meal: MealEstimate) -> Meal:
    """Store a complete meal. With `replaces_meal_id`, the existing meal is updated in place."""
    missing = meal.missing_fields
    if missing:
        raise ValueError(f"Meal is incomplete, missing: {', '.join(missing)}")

    values: dict[str, object] = {
        "dish_name": meal.dish_name,
        "description": meal.description,
        "source": meal.source,
        "source_note": meal.source_note,
        "portion_grams": meal.portion_grams,
        "calories_kcal": meal.calories_kcal,
        "protein_g": meal.protein_g,
        "fat_g": meal.fat_g,
        "carbohydrates_g": meal.carbohydrates_g,
        "estimated_fields": list(meal.estimated_fields),
        "confidence": meal.confidence,
    }
    # A correction keeps the original time unless it gives a new one.
    if meal.eaten_at is not None or meal.replaces_meal_id is None:
        time_zone = await get_time_zone(session, user_id)
        values["eaten_at"], values["utc_offset_minutes"] = resolve_eaten_at(
            meal.eaten_at, time_zone, now=utcnow()
        )

    if meal.replaces_meal_id is None:
        row = Meal(user_id=user_id, **values)
        session.add(row)
    else:
        found = await session.scalar(
            select(Meal)
            .where(Meal.id == meal.replaces_meal_id, Meal.user_id == user_id)
            .with_for_update()
        )
        if found is None:
            raise MealNotFoundError
        row = found
        for name, value in values.items():
            setattr(row, name, value)

    await session.flush()
    return row


@dataclass(frozen=True)
class LocalMeal:
    meal: Meal
    # When the meal was eaten, in its local time (see services/local_time.py).
    eaten_at: datetime


@dataclass(frozen=True)
class MealCursor:
    """Position after the last meal of a page, for keyset pagination (newest first)."""

    eaten_at: datetime
    meal_id: uuid.UUID


@dataclass(frozen=True)
class MealPage:
    meals: list[LocalMeal]
    next_cursor: MealCursor | None


def _local(meal: Meal, time_zone: str | None) -> LocalMeal:
    return LocalMeal(
        meal=meal,
        eaten_at=local_eaten_at(meal.eaten_at, meal.utc_offset_minutes, time_zone),
    )


async def list_meals(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    first_day: date | None = None,
    last_day: date | None = None,
    after: MealCursor | None = None,
    limit: int = 50,
) -> MealPage:
    """The user's meals eaten on the local days first_day..last_day (inclusive), newest first."""
    time_zone = await get_time_zone(session, user_id)
    query = select(Meal).where(Meal.user_id == user_id)
    start, end = utc_window(first_day, last_day)
    local_date = local_date_column(time_zone)
    if start is not None:
        query = query.where(Meal.eaten_at >= start, local_date >= first_day)
    if end is not None:
        query = query.where(Meal.eaten_at < end, local_date <= last_day)
    if after is not None:
        query = query.where(tuple_(Meal.eaten_at, Meal.id) < tuple_(after.eaten_at, after.meal_id))
    query = query.order_by(Meal.eaten_at.desc(), Meal.id.desc()).limit(limit + 1)

    rows = list(await session.scalars(query))
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = MealCursor(eaten_at=rows[-1].eaten_at, meal_id=rows[-1].id)
    return MealPage(meals=[_local(row, time_zone) for row in rows], next_cursor=next_cursor)


async def get_meal(
    session: AsyncSession, user_id: uuid.UUID, meal_id: uuid.UUID
) -> LocalMeal | None:
    meal = await session.scalar(select(Meal).where(Meal.id == meal_id, Meal.user_id == user_id))
    if meal is None:
        return None
    return _local(meal, await get_time_zone(session, user_id))


async def delete_meal(session: AsyncSession, user_id: uuid.UUID, meal_id: uuid.UUID) -> bool:
    """Delete one of the user's meals. False if it does not exist or belongs to someone else."""
    deleted = await session.scalar(
        delete(Meal).where(Meal.id == meal_id, Meal.user_id == user_id).returning(Meal.id)
    )
    return deleted is not None
