import uuid
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.micronutrients import (
    MICRONUTRIENT_INFO,
    MICRONUTRIENTS,
    Micronutrient,
    ReferenceGroup,
)
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, EnergyUnit, NutrientField
from healthy_eating.services.settings import (
    InvalidSettingsError,
    SettingsChanges,
    UserSettings,
    get_settings,
    micronutrient_ranges,
    set_targets_from_chat,
    update_settings,
    visible_targets,
)
from healthy_eating.services.settings import TargetRange as ServiceTargetRange
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
protein, fat and carbohydrates in g, and goals for vitamins and minerals in
`vitamins_and_minerals` (unit: the code's suffix, mg or ug = µg; convert IU only where exact,
for example vitamin D 40 IU = 1 µg).
Call it only when the user states or confirms their own targets or goals (for example "I'm
aiming for 1800 to 2100 kcal and at least 120 g protein", or "my doctor told me to get 50 µg
vitamin D a day"). Never suggest, ask for or change targets or goals on your own, and never
comment on whether one is high or low.
Targets are ranges, never limits or single numbers: give min, max or both. When the user
names a single number for calories or a macro (for example "2000 kcal a day"), do not call
this tool yet; ask which range they want, such as from-to, at least or at most. An amount of a
vitamin or mineral the user should get or reach each day is a lower bound: save it as min
(for example vitamin_d_ug min 50). Ask only when it is unclear whether it is a lower or an
upper bound.
Targets and goals you leave out stay as they are; put one in `clear` to remove it (a removed
vitamin or mineral goal falls back to the reference range of the user's life stage).
When get_profile shows calm_mode, do not bring up calories or a calorie target, and leave
calories_kcal out of this call (a call with it is rejected): Calm mode hides calories, so
save only the other targets.
"""


class TargetRange(BaseModel):
    min: float | None = Field(description="Lower bound, or null if open")
    max: float | None = Field(description="Upper bound, or null if open")


class MicronutrientRange(BaseModel):
    min: float | None = Field(description="Lower bound, or null if open")
    max: float | None = Field(description="Upper bound, or null if open")
    source: Literal["personal", "reference"]


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
            "Daily target ranges the user set, in kcal and g; null if none. Without "
            "calories_kcal in Calm mode. Targets are ranges, never limits: being over or "
            "under is not good or bad"
        )
    )
    reference_group: ReferenceGroup | None = Field(
        description="Life stage the user picked for vitamin and mineral references; null if none"
    )
    vitamin_and_mineral_ranges: dict[Micronutrient, MicronutrientRange] = Field(
        description=(
            "The daily range that applies per vitamin and mineral (unit: the code's suffix): "
            "the user's own goal (source personal), else the reference for their life stage "
            "(source reference). Nutrients with neither are left out. Ranges, never limits: "
            "being over or under is not good or bad, and never bring them up unless the user "
            "asks"
        )
    )


class TargetRangeInput(BaseModel):
    min: float | None = Field(default=None, description="Lower bound; omit if open")
    max: float | None = Field(default=None, description="Upper bound; omit if open")


class SetTargetsResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    targets: dict[NutrientField, TargetRange | None] = Field(
        description=(
            "All daily target ranges now stored, in kcal and g; null if none. Without "
            "calories_kcal in Calm mode"
        )
    )
    vitamin_and_mineral_goals: dict[Micronutrient, TargetRange] = Field(
        description="All of the user's own vitamin and mineral goals now stored"
    )


TARGET_LABELS: dict[NutrientField, str] = {
    "calories_kcal": "calories",
    "protein_g": "protein",
    "fat_g": "fat",
    "carbohydrates_g": "carbohydrates",
}


def _micronutrient_label(code: Micronutrient) -> str:
    # "vitamin D", "thiamin (B1)", "iron": lower case inside a sentence.
    label = MICRONUTRIENT_INFO[code].label
    return label[0].lower() + label[1:]


def _number(value: float, digits: int = 1) -> str:
    # At most `digits` decimals and never scientific notation: "2100", "250.5", "2000000".
    rounded = round(value, digits)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.{digits}f}".rstrip("0")


def _amount(value: float, unit: str, digits: int = 1) -> str:
    return f"{_number(value, digits)} {unit}"


def _describe_range(target: ServiceTargetRange, unit: str, digits: int = 1) -> str:
    if target.min is not None and target.max is not None:
        return f"{_number(target.min, digits)} to {_amount(target.max, unit, digits)}"
    if target.min is not None:
        return f"from {_amount(target.min, unit, digits)}"
    assert target.max is not None
    return f"up to {_amount(target.max, unit, digits)}"


def _describe_target(name: NutrientField, target: ServiceTargetRange) -> str:
    text = _describe_range(target, "kcal" if name == "calories_kcal" else "g")
    # "1800 to 2100 kcal" names itself; macros need their name.
    return text if name == "calories_kcal" else f"{TARGET_LABELS[name]} {text}"


def _describe_goal(code: Micronutrient, goal: ServiceTargetRange) -> str:
    return f"{_micronutrient_label(code)} {_describe_range(goal, MICRONUTRIENT_INFO[code].unit, 2)}"


def _targets_message(
    targets: dict[NutrientField, ServiceTargetRange],
    goals: dict[Micronutrient, ServiceTargetRange],
    clear: list[NutrientField | Micronutrient],
) -> str:
    parts = [
        _describe_target(name, targets[name])
        if name in targets
        else f"no {TARGET_LABELS[name]} target"
        for name in NUTRIENT_FIELDS
        if name in targets or name in clear
    ]
    parts += [
        _describe_goal(code, goals[code])
        if code in goals
        else f"no own {_micronutrient_label(code)} goal"
        for code in MICRONUTRIENTS
        if code in goals or code in clear
    ]
    noun = (
        "targets" if any(name in targets or name in clear for name in NUTRIENT_FIELDS) else "goals"
    )
    return f"Saved {noun} to HealthyEating: {', '.join(parts)}."


def _shown_ranges(settings: UserSettings) -> dict[Micronutrient, MicronutrientRange]:
    return {
        code: MicronutrientRange(min=found.min, max=found.max, source=found.source)
        for code, found in micronutrient_ranges(settings).items()
    }


def _shown_goals(settings: UserSettings) -> dict[Micronutrient, TargetRange]:
    return {
        code: TargetRange(min=goal.min, max=goal.max)
        for code, goal in settings.micronutrient_goals.items()
    }


def _shown_targets(
    targets: dict[NutrientField, ServiceTargetRange | None],
) -> dict[NutrientField, TargetRange | None]:
    return {
        name: None if target is None else TargetRange(min=target.min, max=target.max)
        for name, target in targets.items()
    }


def _target(nutrient: str, unit: str) -> str:
    return (
        f"Daily {nutrient} range in {unit}. Omit it (or send null) to leave it unchanged; "
        "to remove it, list it in clear"
    )


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
            targets=_shown_targets(visible_targets(settings)),
            reference_group=settings.reference_group,
            vitamin_and_mineral_ranges=_shown_ranges(settings),
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
            TargetRangeInput | None, Field(description=_target("energy", "kcal"))
        ] = None,
        protein_g: Annotated[
            TargetRangeInput | None, Field(description=_target("protein", "g"))
        ] = None,
        fat_g: Annotated[TargetRangeInput | None, Field(description=_target("fat", "g"))] = None,
        carbohydrates_g: Annotated[
            TargetRangeInput | None, Field(description=_target("carbohydrate", "g"))
        ] = None,
        vitamins_and_minerals: Annotated[
            dict[Micronutrient, TargetRangeInput] | None,
            Field(
                description=(
                    "Daily goal ranges for vitamins and minerals by code, in the code's unit "
                    "(mg or ug = µg). Leave a code out to leave its goal unchanged; to remove "
                    "one, list it in clear"
                )
            ),
        ] = None,
        clear: Annotated[
            list[NutrientField | Micronutrient] | None,
            Field(description="Targets or goals to remove, only when the user asks to remove them"),
        ] = None,
    ) -> SetTargetsResult:
        given: dict[NutrientField, TargetRangeInput | None] = {
            "calories_kcal": calories_kcal,
            "protein_g": protein_g,
            "fat_g": fat_g,
            "carbohydrates_g": carbohydrates_g,
        }
        targets = {
            name: ServiceTargetRange(min=target.min, max=target.max)
            for name, target in given.items()
            if target is not None
        }
        goals = {
            code: ServiceTargetRange(min=goal.min, max=goal.max)
            for code, goal in (vitamins_and_minerals or {}).items()
        }
        clear = list(dict.fromkeys(clear or []))
        try:
            async with db.session() as session:
                settings = await set_targets_from_chat(
                    session, current_user_id(), targets, clear, goals
                )
        except InvalidSettingsError as error:
            raise ToolError(f"Targets not saved to HealthyEating: {error}") from None
        if settings is None:
            raise ToolError("User not found")
        return SetTargetsResult(
            saved_to_healthy_eating=True,
            user_message=_targets_message(targets, goals, clear),
            targets=_shown_targets(visible_targets(settings)),
            vitamin_and_mineral_goals=_shown_goals(settings),
        )
