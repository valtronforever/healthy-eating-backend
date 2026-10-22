"""MCP tools to log, read back and delete body weight (decision in healthy-eating-ios#14)."""

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.types import WeightSource
from healthy_eating.services.days import user_today
from healthy_eating.services.settings import get_settings
from healthy_eating.services.unset import UNSET
from healthy_eating.services.weights import (
    InvalidWeightError,
    LocalWeight,
    WeightInput,
    add_weights,
    delete_weight,
    edit_weight,
    weight_history,
)

KG_PER_LB = 0.45359237
MAX_TOOL_DAYS = 90

NEUTRAL_RULES = """
Weight is sensitive. Report it as a plain number: never praise or warn about a change, never
call it good, bad, progress or a setback, and never suggest a goal, a diet or a target weight.
"""

LOG_WEIGHT_DESCRIPTION = (
    """\
Save the user's body weight to HealthyEating. Call it every time the user states their weight
or asks you to change, note or remember it, in any language and however short (for example "I
weighed 80.4 kg this morning", "scale says 177 lb", "my weight is 56 kg now", "weight 56"). Do
this even when you also keep their weight in your memory, notes or another tool: those do not
reach HealthyEating. Never ask for the weight. Pass the number and unit exactly as the user gave
them; HealthyEating stores kg. Set `measured_at` when
they say when it was ("this morning", "yesterday"), with their UTC offset if you know it. To
correct a weight saved earlier in the chat, pass its `weight_id` as `replaces_weight_id`.
Finish with the one-line `user_message` as given: it names HealthyEating, so the user knows
where the weight was saved.
"""
    + NEUTRAL_RULES
)

LIST_WEIGHTS_DESCRIPTION = (
    """\
List the user's weigh-ins saved in HealthyEating with the smoothed trend, newest first, for
local days (default: the last 30). Use it when the user asks about their weight or to find the
`weight_id` of a weigh-in to correct or delete. Prefer the trend over single weigh-ins, which
vary from day to day. Returns nothing while the user has weight tracking turned off.
"""
    + NEUTRAL_RULES
)

DELETE_WEIGHT_DESCRIPTION = """\
Delete one weigh-in from HealthyEating, only when the user asks to remove it (for example one
saved by mistake). Get the `weight_id` from log_weight or list_weights. Finish with the
one-line `user_message`.
"""


class LogWeightResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    weight_id: uuid.UUID = Field(description="Pass as replaces_weight_id to correct it")
    weight_kg: float
    measured_at: datetime = Field(description="Local time with the UTC offset")
    weight_tracking_turned_on: bool = Field(
        description="True when this weigh-in turned weight tracking on"
    )


class WeighIn(BaseModel):
    weight_id: uuid.UUID
    weight_kg: float
    measured_at: datetime = Field(description="Local time with the UTC offset")
    source: WeightSource


class WeightList(BaseModel):
    weight_tracking: bool = Field(description="False: the user turned weight off; nothing listed")
    first_day: date
    last_day: date
    weigh_ins: list[WeighIn] = Field(description="Newest first")
    latest_trend_kg: float | None = Field(description="Smoothed trend, less noisy than weigh-ins")
    weekly_change_kg: float | None = Field(
        description="Trend change per week over the last 28 days; report it neutrally"
    )


class DeleteWeightResult(BaseModel):
    deleted_from_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")


def _shown(weight_kg: float, unit: Literal["kg", "lb"]) -> str:
    value = weight_kg / KG_PER_LB if unit == "lb" else weight_kg
    return f"{round(value, 1):g} {unit}"


def _weigh_in(item: LocalWeight) -> WeighIn:
    return WeighIn(
        weight_id=item.entry.id,
        weight_kg=item.entry.weight_kg,
        measured_at=item.measured_at,
        source=item.entry.source,
    )


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        description=LOG_WEIGHT_DESCRIPTION,
        annotations={
            "title": "Save a weigh-in",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    )
    async def log_weight(
        weight: Annotated[float, Field(gt=0, description="The number the user gave")],
        unit: Annotated[
            Literal["kg", "lb"], Field(description="The unit the user gave; default kg")
        ] = "kg",
        measured_at: Annotated[
            datetime | None,
            Field(
                description=(
                    "When it was measured, ISO 8601 with the user's UTC offset if known; leave "
                    "empty for just now"
                )
            ),
        ] = None,
        replaces_weight_id: Annotated[
            uuid.UUID | None,
            Field(description="weight_id of a weigh-in saved earlier that this call corrects"),
        ] = None,
    ) -> LogWeightResult:
        weight_kg = round(weight * KG_PER_LB, 2) if unit == "lb" else weight
        user_id = current_user_id()
        try:
            async with db.session() as session:
                settings = await get_settings(session, user_id)
                was_on = settings is not None and settings.weight_tracking
                if replaces_weight_id is None:
                    [saved] = await add_weights(
                        session,
                        user_id,
                        [WeightInput(weight_kg=weight_kg, measured_at=measured_at)],
                        "assistant",
                        now=utcnow(),
                    )
                else:
                    edited = await edit_weight(
                        session,
                        user_id,
                        replaces_weight_id,
                        weight_kg=weight_kg,
                        measured_at=UNSET if measured_at is None else measured_at,
                        now=utcnow(),
                    )
                    if edited is None:
                        raise ToolError(
                            "Not saved to HealthyEating: the weigh-in to correct "
                            "(replaces_weight_id) was not found. Save it as a new weigh-in."
                        )
                    saved = edited
        except InvalidWeightError as error:
            raise ToolError(f"Not saved to HealthyEating: {error}.") from None

        verb = "Updated in" if replaces_weight_id else "Saved to"
        message = f"{verb} HealthyEating: {_shown(saved.entry.weight_kg, unit)}."
        turned_on = replaces_weight_id is None and not was_on
        if turned_on:
            message += (
                " Weight tracking is now on; you can turn it off in the Healthy Eating app's "
                "settings."
            )
        return LogWeightResult(
            saved_to_healthy_eating=True,
            user_message=message,
            weight_id=saved.entry.id,
            weight_kg=saved.entry.weight_kg,
            measured_at=saved.measured_at,
            weight_tracking_turned_on=turned_on,
        )

    @mcp.tool(
        name="list_weights",
        description=LIST_WEIGHTS_DESCRIPTION,
        annotations={"title": "List weigh-ins", "readOnlyHint": True, "openWorldHint": False},
    )
    async def list_weights_tool(
        first_day: Annotated[
            date | None, Field(description="First local day (YYYY-MM-DD); default 29 days ago")
        ] = None,
        last_day: Annotated[
            date | None, Field(description="Last local day (YYYY-MM-DD); default today")
        ] = None,
    ) -> WeightList:
        user_id = current_user_id()
        async with db.session() as session:
            last = last_day or await user_today(session, user_id, utcnow())
            first = first_day or last - timedelta(days=29)
            if first > last:
                raise ToolError("first_day is after last_day")
            if (last - first).days >= MAX_TOOL_DAYS:
                raise ToolError(f"Ask for at most {MAX_TOOL_DAYS} days at a time")
            settings = await get_settings(session, user_id)
            if settings is None or not settings.weight_tracking:
                return WeightList(
                    weight_tracking=False,
                    first_day=first,
                    last_day=last,
                    weigh_ins=[],
                    latest_trend_kg=None,
                    weekly_change_kg=None,
                )
            history = await weight_history(session, user_id, first, last)
        return WeightList(
            weight_tracking=True,
            first_day=first,
            last_day=last,
            weigh_ins=[_weigh_in(item) for item in reversed(history.entries)],
            latest_trend_kg=history.latest_trend_kg,
            weekly_change_kg=history.weekly_change_kg,
        )

    @mcp.tool(
        name="delete_weight",
        description=DELETE_WEIGHT_DESCRIPTION,
        annotations={
            "title": "Delete a weigh-in",
            "readOnlyHint": False,
            "destructiveHint": True,
            "openWorldHint": False,
        },
    )
    async def delete_weight_tool(
        weight_id: Annotated[uuid.UUID, Field(description="weight_id of the weigh-in to delete")],
    ) -> DeleteWeightResult:
        async with db.session() as session:
            deleted = await delete_weight(session, current_user_id(), weight_id)
        if not deleted:
            raise ToolError(
                "Not deleted from HealthyEating: no weigh-in with this weight_id. Use "
                "list_weights to find it."
            )
        return DeleteWeightResult(
            deleted_from_healthy_eating=True,
            user_message="Deleted the weigh-in from HealthyEating.",
        )
