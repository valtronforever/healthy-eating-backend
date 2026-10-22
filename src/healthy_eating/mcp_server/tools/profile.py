import uuid
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, EnergyUnit, NutrientField
from healthy_eating.services import settings as settings_service
from healthy_eating.services.settings import (
    InvalidSettingsError,
    SettingsChanges,
    UserSettings,
    get_settings,
    set_targets_from_chat,
    update_settings,
)
from healthy_eating.services.users import get_user

SET_TIMEZONE_DESCRIPTION = """\
Store the user's time zone in HealthyEating, so meals count for the right day and times
without a UTC offset are read correctly.
Call it when the user says where they live or which time zone they are in (for example
"I live in Berlin"), also in the same turn as log_meal and even if you already gave the meal
the right UTC offset. Also call it when you know the time zone from your context and
get_profile shows a different or no time zone. Never ask the user for their time zone just
for HealthyEating.
"""

SET_TARGETS_DESCRIPTION = """\
Store the user's daily target ranges in HealthyEating: energy in kcal (convert kJ first),
protein, fat and carbohydrates in g.
Call it only when the user states or confirms their own targets (for example "I'm aiming
for 1800 to 2100 kcal and at least 120 g protein"). Never suggest, ask for or change targets
on your own, and never comment on whether a target is high or low.
Targets are ranges, never limits or single numbers: give min, max or both. When the user
names a single number (for example "2000 kcal a day"), do not call this tool yet; ask which
range they want, such as from-to, at least or at most.
Targets you leave out stay as they are; put a target in `clear` to remove it.
When get_profile shows calm_mode, do not bring up calories or a calorie target: Calm mode
hides them, so this tool does not change the calorie target then.
"""


class TargetRange(BaseModel):
    min: float | None = Field(description="Lower bound, or null if open")
    max: float | None = Field(description="Upper bound, or null if open")


class Profile(BaseModel):
    user_id: uuid.UUID
    display_name: str | None = Field(description="Name shown in the app, if known")
    linked_logins: list[str] = Field(
        description="Social providers linked to this account, e.g. google, apple"
    )
    timezone: str | None = Field(
        description="The user's IANA time zone, e.g. 'Europe/Berlin'; null if not known yet"
    )
    energy_unit: EnergyUnit = Field(
        description="Unit the user sees energy in; always send values to tools in kcal"
    )
    calm_mode: bool = Field(
        description=(
            "The user turned on Calm mode in the app, which hides calories: do not bring up "
            "calories or calorie targets on your own"
        )
    )
    targets: dict[NutrientField, TargetRange | None] = Field(
        description=(
            "Daily target ranges the user set, in kcal and g; null if none. Targets are "
            "ranges, never limits: being over or under is not good or bad"
        )
    )


class TargetRangeInput(BaseModel):
    min: float | None = Field(default=None, ge=0, description="Lower bound; omit if open")
    max: float | None = Field(default=None, ge=0, description="Upper bound; omit if open")


class SetTargetsResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    targets: dict[NutrientField, TargetRange | None] = Field(
        description=(
            "All daily target ranges now stored, in kcal and g; null if none. Without "
            "calories_kcal in Calm mode"
        )
    )


TARGET_LABELS: dict[NutrientField, str] = {
    "calories_kcal": "calories",
    "protein_g": "protein",
    "fat_g": "fat",
    "carbohydrates_g": "carbohydrates",
}


def _amount(value: float, unit: str) -> str:
    return f"{value:g} {unit}"


def _describe_target(name: NutrientField, target: settings_service.TargetRange) -> str:
    unit = "kcal" if name == "calories_kcal" else "g"
    if target.min is not None and target.max is not None:
        text = f"{target.min:g} to {_amount(target.max, unit)}"
    elif target.min is not None:
        text = f"from {_amount(target.min, unit)}"
    else:
        assert target.max is not None
        text = f"up to {_amount(target.max, unit)}"
    # "1800 to 2100 kcal" names itself; macros need their name.
    return text if name == "calories_kcal" else f"{TARGET_LABELS[name]} {text}"


def _targets_message(
    changed: dict[NutrientField, settings_service.TargetRange | None],
) -> str:
    parts = [
        f"no {TARGET_LABELS[name]} target" if target is None else _describe_target(name, target)
        for name in NUTRIENT_FIELDS
        if name in changed
        for target in (changed[name],)
    ]
    return f"Saved targets to HealthyEating: {', '.join(parts)}."


def _shown_targets(settings: UserSettings) -> dict[NutrientField, TargetRange | None]:
    return {
        name: None if target is None else TargetRange(min=target.min, max=target.max)
        for name, target in settings.targets.items()
        if not (settings.calm_mode and name == "calories_kcal")
    }


class SetTimezoneResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    timezone: str


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_profile() -> Profile:
        """Return the signed-in user's Healthy Eating profile and settings."""
        user_id = current_user_id()
        async with db.session() as session:
            user = await get_user(session, user_id)
            settings = await get_settings(session, user_id)
        if user is None or settings is None:
            raise ToolError("User not found")
        return Profile(
            user_id=user.id,
            display_name=user.display_name,
            linked_logins=sorted(identity.provider for identity in user.identities),
            timezone=settings.timezone,
            energy_unit=settings.energy_unit,
            calm_mode=settings.calm_mode,
            targets={
                name: None if target is None else TargetRange(min=target.min, max=target.max)
                for name, target in settings.targets.items()
            },
        )

    @mcp.tool(
        description=SET_TIMEZONE_DESCRIPTION,
        annotations={
            "title": "Set time zone",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def set_timezone(
        timezone: Annotated[
            str,
            Field(
                min_length=1,
                max_length=64,
                description="IANA time zone name, e.g. 'Europe/Berlin' or 'America/New_York'",
            ),
        ],
    ) -> SetTimezoneResult:
        try:
            async with db.session() as session:
                settings = await update_settings(
                    session, current_user_id(), SettingsChanges(timezone=timezone)
                )
        except InvalidSettingsError as error:
            raise ToolError(f"Time zone not saved to HealthyEating: {error}") from None
        if settings is None:
            raise ToolError("User not found")
        return SetTimezoneResult(
            saved_to_healthy_eating=True,
            user_message=f"HealthyEating now uses your time zone {timezone}.",
            timezone=timezone,
        )

    @mcp.tool(
        description=SET_TARGETS_DESCRIPTION,
        annotations={
            "title": "Set daily targets",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def set_targets(
        calories_kcal: Annotated[
            TargetRangeInput | None, Field(description="Daily energy range in kcal")
        ] = None,
        protein_g: Annotated[
            TargetRangeInput | None, Field(description="Daily protein range in g")
        ] = None,
        fat_g: Annotated[TargetRangeInput | None, Field(description="Daily fat range in g")] = None,
        carbohydrates_g: Annotated[
            TargetRangeInput | None, Field(description="Daily carbohydrate range in g")
        ] = None,
        clear: Annotated[
            list[NutrientField] | None,
            Field(description="Targets to remove, only when the user asks to remove them"),
        ] = None,
    ) -> SetTargetsResult:
        given: dict[NutrientField, TargetRangeInput | None] = {
            "calories_kcal": calories_kcal,
            "protein_g": protein_g,
            "fat_g": fat_g,
            "carbohydrates_g": carbohydrates_g,
        }
        changed: dict[NutrientField, settings_service.TargetRange | None] = {
            name: settings_service.TargetRange(min=target.min, max=target.max)
            for name, target in given.items()
            if target is not None
        }
        clear = clear or []
        both = sorted(set(changed) & set(clear))
        if both:
            raise ToolError(
                f"Targets not saved to HealthyEating: {', '.join(both)} is both set and cleared"
            )
        changed |= {name: None for name in clear}
        try:
            async with db.session() as session:
                settings = await set_targets_from_chat(session, current_user_id(), changed)
        except InvalidSettingsError as error:
            raise ToolError(f"Targets not saved to HealthyEating: {error}") from None
        if settings is None:
            raise ToolError("User not found")
        return SetTargetsResult(
            saved_to_healthy_eating=True,
            user_message=_targets_message(changed),
            targets=_shown_targets(settings),
        )
