from typing import Self

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from healthy_eating.api.deps import CurrentUserId, SessionDep
from healthy_eating.api.errors import error_response
from healthy_eating.nutrition.micronutrients import (
    LEGACY_REFERENCE_GROUPS,
    REFERENCE_INTAKES,
    LegacyReferenceGroup,
    Micronutrient,
    ReferenceGroup,
)
from healthy_eating.nutrition.types import NUTRIENT_FIELDS, EnergyUnit, NutrientField, WeightUnit
from healthy_eating.services.settings import (
    InvalidSettingsError,
    SettingsChanges,
    TargetRange,
    UserSettings,
    get_settings,
    update_settings,
    validate_time_zone,
)
from healthy_eating.services.unset import UNSET

router = APIRouter(tags=["account"])


class TargetRangeModel(BaseModel):
    """A daily target range. Either bound may be open (null), never both."""

    min: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("Give min, max or both; send null to remove the target")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min cannot exceed max")
        return self


class Targets(BaseModel):
    """Daily targets in canonical units; null means no target."""

    calories_kcal: TargetRangeModel | None = None
    protein_g: TargetRangeModel | None = None
    fat_g: TargetRangeModel | None = None
    carbohydrates_g: TargetRangeModel | None = None


class ReferenceRangeModel(BaseModel):
    min: float
    max: float | None


class SettingsResponse(BaseModel):
    timezone: str | None = Field(
        description="IANA time zone, e.g. 'Europe/Berlin'; null if unknown"
    )
    energy_unit: EnergyUnit = Field(description="Unit to show energy in; values stay in kcal")
    calm_mode: bool
    targets: Targets
    weight_tracking: bool = Field(
        description=(
            "Opt-in: off until the first weigh-in from the chat or the app, or until the user "
            "turns it on. Off hides weight everywhere; the entries stay until deleted"
        )
    )
    weight_unit: WeightUnit = Field(description="Unit to show weight in; values stay in kg")
    reference_group: ReferenceGroup | None = Field(
        description=(
            "Life stage for the vitamin and mineral reference values; null when off (the default)"
        )
    )
    micronutrient_references: dict[Micronutrient, ReferenceRangeModel] = Field(
        description=(
            "The group's reference range per vitamin and mineral (unit: the code's suffix): `min` "
            "is the RDA or AI, `max` the upper limit where it applies to food, else null. US "
            "Dietary Reference Intakes for the life stage. Empty when reference_group is null"
        )
    )


class SettingsUpdate(BaseModel):
    """Only the fields present are changed. null clears the time zone or a target."""

    timezone: str | None = None
    energy_unit: EnergyUnit | None = None
    calm_mode: bool | None = None
    targets: Targets | None = None
    weight_tracking: bool | None = None
    weight_unit: WeightUnit | None = None
    reference_group: ReferenceGroup | LegacyReferenceGroup | None = Field(
        default=None,
        description=(
            "null turns the reference values off. The old names `adult_women` and `adult_men` "
            "are still accepted and stored as `women_31_50` and `men_31_50`"
        ),
    )

    @field_validator("timezone")
    @classmethod
    def _check_time_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_time_zone(value)
        except InvalidSettingsError as error:
            raise ValueError(str(error)) from None

    @model_validator(mode="after")
    def _check_not_null(self) -> Self:
        for name in ("energy_unit", "calm_mode", "targets", "weight_tracking", "weight_unit"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def to_changes(self) -> SettingsChanges:
        fields = self.model_fields_set
        targets: dict[NutrientField, TargetRange | None] = {}
        if self.targets is not None:
            for name in NUTRIENT_FIELDS:
                if name not in self.targets.model_fields_set:
                    continue
                target: TargetRangeModel | None = getattr(self.targets, name)
                targets[name] = None if target is None else TargetRange(target.min, target.max)
        return SettingsChanges(
            timezone=self.timezone if "timezone" in fields else UNSET,
            energy_unit=self.energy_unit if self.energy_unit is not None else UNSET,
            calm_mode=self.calm_mode if self.calm_mode is not None else UNSET,
            targets=targets,
            weight_tracking=self.weight_tracking if self.weight_tracking is not None else UNSET,
            weight_unit=self.weight_unit if self.weight_unit is not None else UNSET,
            reference_group=(
                _current_group(self.reference_group) if "reference_group" in fields else UNSET
            ),
        )


def _current_group(group: ReferenceGroup | LegacyReferenceGroup | None) -> ReferenceGroup | None:
    if group in LEGACY_REFERENCE_GROUPS:
        return LEGACY_REFERENCE_GROUPS[group]
    return group  # type: ignore[return-value]


def _response(settings: UserSettings) -> SettingsResponse:
    targets = {
        name: None if target is None else TargetRangeModel(min=target.min, max=target.max)
        for name, target in settings.targets.items()
    }
    return SettingsResponse(
        timezone=settings.timezone,
        energy_unit=settings.energy_unit,
        calm_mode=settings.calm_mode,
        targets=Targets(**targets),
        weight_tracking=settings.weight_tracking,
        weight_unit=settings.weight_unit,
        reference_group=settings.reference_group,
        micronutrient_references={
            code: ReferenceRangeModel(min=reference.min, max=reference.max)
            for code, reference in (
                REFERENCE_INTAKES[settings.reference_group] if settings.reference_group else {}
            ).items()
        },
    )


@router.get(
    "/me/settings", operation_id="getSettings", responses={404: error_response("Not found")}
)
async def read_settings(user_id: CurrentUserId, session: SessionDep) -> SettingsResponse:
    """The signed-in user's settings."""
    settings = await get_settings(session, user_id)
    if settings is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _response(settings)


@router.patch(
    "/me/settings", operation_id="updateSettings", responses={404: error_response("Not found")}
)
async def change_settings(
    changes: SettingsUpdate, user_id: CurrentUserId, session: SessionDep
) -> SettingsResponse:
    """Change some of the signed-in user's settings and return all of them."""
    settings = await update_settings(session, user_id, changes.to_changes())
    if settings is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _response(settings)
