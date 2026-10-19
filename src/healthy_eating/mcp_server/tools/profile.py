import uuid
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.types import EnergyUnit, NutrientField
from healthy_eating.services.settings import (
    InvalidSettingsError,
    SettingsChanges,
    get_settings,
    update_settings,
)
from healthy_eating.services.users import get_user

SET_TIMEZONE_DESCRIPTION = """\
Store the user's time zone in HealthyEating, so meals count for the right day and times
without a UTC offset are read correctly.
Call it when the user says where they are or which time zone they are in, or when you
know it from your context and get_profile shows a different or no time zone. Never ask
the user for their time zone just for HealthyEating.
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
    targets: dict[NutrientField, TargetRange | None] = Field(
        description=(
            "Daily target ranges the user set, in kcal and g; null if none. Targets are "
            "ranges, never limits: being over or under is not good or bad"
        )
    )


class SetTimezoneResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    timezone: str


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(annotations={"readOnlyHint": True})
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
            targets={
                name: None if target is None else TargetRange(min=target.min, max=target.max)
                for name, target in settings.targets.items()
            },
        )

    @mcp.tool(
        description=SET_TIMEZONE_DESCRIPTION,
        annotations={"title": "Set time zone", "readOnlyHint": False, "idempotentHint": True},
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
