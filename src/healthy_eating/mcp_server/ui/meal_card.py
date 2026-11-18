"""Data for the meal card: what the chat shows next to a saved meal, beyond the tool result."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.base import utcnow
from healthy_eating.db.models import Meal
from healthy_eating.services.days import user_today
from healthy_eating.services.local_time import local_eaten_at
from healthy_eating.services.settings import get_settings, visible_targets
from healthy_eating.services.summaries import daily_summaries


async def meal_card_data(
    session: AsyncSession, user_id: uuid.UUID, meal: Meal, *, updated: bool
) -> dict[str, Any]:
    """The meal's local time, its day's totals, the targets the user sees and display settings.

    Calm mode leaves calories out of the day and the targets, like the app does; the card also
    hides the meal's own calories then.
    """
    settings = await get_settings(session, user_id)
    time_zone = settings.timezone if settings else None
    calm_mode = settings.calm_mode if settings else False
    eaten_at = local_eaten_at(meal.eaten_at, meal.utc_offset_minutes, time_zone)
    local_date = eaten_at.date()
    today = await user_today(session, user_id, utcnow())
    summary = (await daily_summaries(session, user_id, local_date, local_date)).days[0]

    day_totals: dict[str, Any] = {
        "meal_count": summary.meal_count,
        "protein_g": summary.protein_g,
        "fat_g": summary.fat_g,
        "carbohydrates_g": summary.carbohydrates_g,
    }
    if not calm_mode:
        day_totals["calories_kcal"] = summary.calories_kcal
    targets = {
        name: {"min": target.min, "max": target.max}
        for name, target in (visible_targets(settings).items() if settings else [])
        if target is not None and not target.is_open
    }
    return {
        "updated": updated,
        "calm_mode": calm_mode,
        "energy_unit": settings.energy_unit if settings else "kcal",
        "eaten_at_local": eaten_at.isoformat(),
        "local_date": local_date.isoformat(),
        "today": today.isoformat(),
        "day_totals": day_totals,
        "targets": targets,
    }
