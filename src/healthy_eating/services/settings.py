"""User settings: time zone, energy unit, calm mode and daily nutrition targets."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import User
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, EnergyUnit, NutrientField
from healthy_eating.services.local_time import is_valid_time_zone


class _Unset(Enum):
    UNSET = "unset"


UNSET = _Unset.UNSET


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


@dataclass(frozen=True)
class SettingsChanges:
    """Settings to change. UNSET leaves a setting as it is; None clears it."""

    timezone: str | _Unset | None = UNSET
    energy_unit: EnergyUnit | _Unset = UNSET
    calm_mode: bool | _Unset = UNSET
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


async def update_settings(
    session: AsyncSession, user_id: uuid.UUID, changes: SettingsChanges
) -> UserSettings | None:
    """Apply the changes and return the new settings, or None if the user does not exist."""
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        return None
    if not isinstance(changes.timezone, _Unset):
        user.timezone = None if changes.timezone is None else validate_time_zone(changes.timezone)
    if not isinstance(changes.energy_unit, _Unset):
        user.energy_unit = changes.energy_unit
    if not isinstance(changes.calm_mode, _Unset):
        user.calm_mode = changes.calm_mode
    for name, target in changes.targets.items():
        if target is not None:
            target.validate()
        setattr(user, f"target_{name}_min", None if target is None else target.min)
        setattr(user, f"target_{name}_max", None if target is None else target.max)
    await session.flush()
    return settings_of(user)
