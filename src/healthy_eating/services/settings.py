"""User settings: time zone, energy and weight units, calm mode, weight tracking and targets."""

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import User
from healthy_eating.nutrition.micronutrients import REFERENCE_GROUPS, ReferenceGroup
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, EnergyUnit, NutrientField, WeightUnit
from healthy_eating.services.local_time import is_valid_time_zone
from healthy_eating.services.unset import UNSET, Unset


class InvalidSettingsError(ValueError):
    """A setting has a value that cannot be stored."""


@dataclass(frozen=True)
class TargetRange:
    """A daily target as a range; either bound may be open. Targets are never single numbers."""

    min: float | None = None
    max: float | None = None

    def validate(self) -> None:
        for bound in (self.min, self.max):
            if bound is not None and bound < 0:
                raise InvalidSettingsError("Targets cannot be negative")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise InvalidSettingsError("The lower bound of a target cannot exceed the upper bound")

    @property
    def is_open(self) -> bool:
        return self.min is None and self.max is None


@dataclass(frozen=True)
class UserSettings:
    timezone: str | None
    energy_unit: EnergyUnit
    calm_mode: bool
    # None when the user has no target for that value.
    targets: dict[NutrientField, TargetRange | None]
    weight_tracking: bool = False
    weight_unit: WeightUnit = "kg"
    # Vitamin and mineral reference values; None = off.
    reference_group: ReferenceGroup | None = None


@dataclass(frozen=True)
class SettingsChanges:
    """Settings to change. UNSET leaves a setting as it is; None clears it."""

    timezone: str | Unset | None = UNSET
    energy_unit: EnergyUnit | Unset = UNSET
    calm_mode: bool | Unset = UNSET
    weight_tracking: bool | Unset = UNSET
    weight_unit: WeightUnit | Unset = UNSET
    reference_group: ReferenceGroup | Unset | None = UNSET
    # Only the targets listed here change; None clears a target.
    targets: Mapping[NutrientField, TargetRange | None] = field(default_factory=dict)


def settings_of(user: User) -> UserSettings:
    targets: dict[NutrientField, TargetRange | None] = {}
    for name in NUTRIENT_FIELDS:
        target = TargetRange(
            min=getattr(user, f"target_{name}_min"), max=getattr(user, f"target_{name}_max")
        )
        targets[name] = None if target.is_open else target
    return UserSettings(
        timezone=user.timezone,
        energy_unit="kj" if user.energy_unit == "kj" else "kcal",
        calm_mode=user.calm_mode,
        targets=targets,
        weight_tracking=user.weight_tracking,
        weight_unit="lb" if user.weight_unit == "lb" else "kg",
        reference_group=(
            user.reference_group if user.reference_group in REFERENCE_GROUPS else None
        ),
    )


def validate_time_zone(name: str) -> str:
    if not is_valid_time_zone(name):
        raise InvalidSettingsError(
            f"Unknown time zone {name!r}; use an IANA name such as 'Europe/Berlin'"
        )
    return name


async def get_settings(session: AsyncSession, user_id: uuid.UUID) -> UserSettings | None:
    user = await session.get(User, user_id)
    return None if user is None else settings_of(user)


async def get_time_zone(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    time_zone: str | None = await session.scalar(select(User.timezone).where(User.id == user_id))
    return time_zone


def visible_targets(settings: UserSettings) -> dict[NutrientField, TargetRange | None]:
    """The targets the user sees: Calm mode hides the calorie target, like the app does."""
    return {
        name: target
        for name, target in settings.targets.items()
        if not (settings.calm_mode and name == "calories_kcal")
    }


def _apply(user: User, changes: SettingsChanges) -> None:
    if not isinstance(changes.timezone, Unset):
        user.timezone = None if changes.timezone is None else validate_time_zone(changes.timezone)
    if not isinstance(changes.energy_unit, Unset):
        user.energy_unit = changes.energy_unit
    if not isinstance(changes.calm_mode, Unset):
        user.calm_mode = changes.calm_mode
    if not isinstance(changes.weight_tracking, Unset):
        user.weight_tracking = changes.weight_tracking
    if not isinstance(changes.weight_unit, Unset):
        user.weight_unit = changes.weight_unit
    if not isinstance(changes.reference_group, Unset):
        user.reference_group = changes.reference_group
    for name, target in changes.targets.items():
        if target is not None:
            target.validate()
        setattr(user, f"target_{name}_min", None if target is None else target.min)
        setattr(user, f"target_{name}_max", None if target is None else target.max)


async def update_settings(
    session: AsyncSession, user_id: uuid.UUID, changes: SettingsChanges
) -> UserSettings | None:
    """Apply the changes and return the new settings, or None if the user does not exist."""
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        return None
    _apply(user, changes)
    await session.flush()
    return settings_of(user)


def _is_single_number(target: TargetRange) -> bool:
    # Bounds that look the same once rounded for the user ("2000 to 2000 kcal") count too.
    return (
        target.min is not None
        and target.max is not None
        and round(target.min, 1) == round(target.max, 1)
    )


async def set_targets_from_chat(
    session: AsyncSession,
    user_id: uuid.UUID,
    targets: Mapping[NutrientField, TargetRange],
    clear: Collection[NutrientField] = (),
) -> UserSettings | None:
    """Set and clear targets the user gave in the chat (MCP), or None if the user does not exist.

    Stricter than the app: a target needs a bound and is never a single number, so the
    assistant asks the user for a range. In Calm mode the calorie target is neither shown nor
    changed, like on the app's targets screen. Nothing is saved when any target is rejected.
    """
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        return None
    if not targets and not clear:
        raise InvalidSettingsError("No target given")
    both = sorted(set(targets) & set(clear))
    if both:
        raise InvalidSettingsError(f"{', '.join(both)} is both set and cleared")
    if user.calm_mode and ("calories_kcal" in targets or "calories_kcal" in clear):
        raise InvalidSettingsError(
            "Calm mode is on, so the calorie target is not shown or changed and nothing was "
            "saved. Call set_targets again right away with the same targets without "
            "calories_kcal (do not ask the user first), then tell the user the calorie target "
            "can be changed in the iOS app after turning Calm mode off"
        )
    for target in targets.values():
        if target.is_open:
            raise InvalidSettingsError("A target needs a lower bound, an upper bound or both")
        if _is_single_number(target):
            raise InvalidSettingsError(
                "A target is a range, never a single number; ask the user for a range "
                "(for example '1800 to 2100 kcal' or 'at least 120 g protein')"
            )
        target.validate()
    changes: dict[NutrientField, TargetRange | None] = {**targets}
    changes |= dict.fromkeys(clear)
    _apply(user, SettingsChanges(targets=changes))
    await session.flush()
    return settings_of(user)
