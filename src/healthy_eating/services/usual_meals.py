"""Usual meals: the meals a user eats again, and re-logging one (healthy-eating-backend#26).

A dish is usual when its name (case and spacing ignored) was eaten at least twice in the last
90 days. The latest of those meals is the template, so a correction of it counts. Imported
history does not count: it may hold day totals instead of dishes.

Re-logging copies the template's name, description, portion, values (optional nutrients too),
its food items and their vitamins and minerals, `source`, `source_note`, `estimated_fields` and
`confidence` (decision healthy-eating-ios#13):
only the time is new, and the new meal is saved by the client that asked for it. It works from
any existing meal; new food is still only logged in the chat. A client-chosen id makes a repeated
request (a double tap, a retried call) return the same meal instead of saving a second one.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import Meal, MealItem
from healthy_eating.services.local_time import resolve_eaten_at
from healthy_eating.services.meals import LocalMeal, get_meal, local_meal, with_client_name
from healthy_eating.services.settings import get_time_zone

USUAL_DAYS = 90
MIN_TIMES = 2
MAX_USUAL_MEALS = 20


def dish_key(dish_name: str) -> str:
    """Names that differ only in case or spacing are the same dish."""
    return " ".join(dish_name.casefold().split())


@dataclass(frozen=True)
class UsualMeal:
    # The latest meal with this name: what a re-log copies.
    template: LocalMeal
    # How often the dish was eaten in the last USUAL_DAYS days.
    times: int


async def usual_meals(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime, limit: int = MAX_USUAL_MEALS
) -> list[UsualMeal]:
    """The user's usual meals, most often eaten first (then most recently eaten)."""
    query = (
        select(Meal)
        .where(Meal.user_id == user_id, Meal.eaten_at >= now - timedelta(days=USUAL_DAYS))
        # Imported history (services/meal_import.py) may hold day totals, not dishes.
        .where(Meal.imported_from.is_(None))
        .order_by(Meal.eaten_at.desc(), Meal.id.desc())
    )
    rows = list(await session.execute(with_client_name(query)))
    latest: dict[str, tuple[Meal, str | None]] = {}
    times: dict[str, int] = {}
    for meal, client_name in rows:
        key = dish_key(meal.dish_name)
        latest.setdefault(key, (meal, client_name))
        times[key] = times.get(key, 0) + 1
    keys = [key for key, count in times.items() if count >= MIN_TIMES]
    keys.sort(key=lambda key: (-times[key], -latest[key][0].eaten_at.timestamp()))
    time_zone = await get_time_zone(session, user_id)
    usual = []
    for key in keys[:limit]:
        meal, client_name = latest[key]
        usual.append(UsualMeal(template=local_meal(meal, time_zone, client_name), times=times[key]))
    return usual


class MealIdTakenError(Exception):
    """The id chosen for the new meal belongs to another user's meal."""


async def relog_meal(
    session: AsyncSession,
    user_id: uuid.UUID,
    meal_id: uuid.UUID,
    *,
    new_meal_id: uuid.UUID | None,
    eaten_at: datetime | None,
    client_id: str | None,
    now: datetime,
) -> LocalMeal | None:
    """Log one of the user's meals again; None if `meal_id` is not one of their meals."""
    if new_meal_id is not None:
        existing = await session.get(Meal, new_meal_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise MealIdTakenError
            return await get_meal(session, user_id, new_meal_id)
    original = await session.scalar(select(Meal).where(Meal.id == meal_id, Meal.user_id == user_id))
    if original is None:
        return None
    time_zone = await get_time_zone(session, user_id)
    when, offset = resolve_eaten_at(eaten_at, time_zone, now=now)
    copy = Meal(
        id=new_meal_id or uuid.uuid4(),
        user_id=user_id,
        client_id=client_id,
        dish_name=original.dish_name,
        description=original.description,
        source=original.source,
        source_note=original.source_note,
        eaten_at=when,
        utc_offset_minutes=offset,
        portion_grams=original.portion_grams,
        calories_kcal=original.calories_kcal,
        protein_g=original.protein_g,
        fat_g=original.fat_g,
        carbohydrates_g=original.carbohydrates_g,
        fiber_g=original.fiber_g,
        sugar_g=original.sugar_g,
        sodium_mg=original.sodium_mg,
        estimated_fields=list(original.estimated_fields),
        confidence=original.confidence,
        micronutrients=(
            dict(original.micronutrients) if original.micronutrients is not None else None
        ),
        items=[
            MealItem(
                position=item.position,
                name=item.name,
                grams=item.grams,
                status=item.status,
                food_id=item.food_id,
                food_description=item.food_description,
                label_values=dict(item.label_values) if item.label_values is not None else None,
            )
            for item in original.items
        ],
    )
    session.add(copy)
    await session.flush()
    return await get_meal(session, user_id, copy.id)
