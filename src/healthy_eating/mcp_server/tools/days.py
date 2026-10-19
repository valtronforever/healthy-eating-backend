from datetime import date
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from healthy_eating.db import Database
from healthy_eating.db.base import utcnow
from healthy_eating.mcp_server.context import current_user_id
from healthy_eating.nutrition.types import DayStatus
from healthy_eating.services.days import FutureDayError, set_day_status, user_today

SET_DAY_STATUS_DESCRIPTION = """\
Mark a day in HealthyEating as complete (every meal of that day is logged) or partial
(some meals are missing). Averages count complete days, so this keeps statistics honest.

Call it only when the user says so, for example "that's all for today" or "I'm done
eating for the day" (complete), or "I forgot to log lunch yesterday and don't remember it"
(partial). Never ask the user whether a day is complete. Finish with the one-line
`user_message`.
"""


class SetDayStatusResult(BaseModel):
    saved_to_healthy_eating: bool
    user_message: str = Field(description="Tell the user this in one short line")
    date: date
    status: DayStatus


def register(mcp: FastMCP, db: Database) -> None:
    @mcp.tool(
        name="set_day_status",
        description=SET_DAY_STATUS_DESCRIPTION,
        annotations={
            "title": "Mark a day",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    async def set_day_status_tool(
        status: Annotated[
            DayStatus,
            Field(description="'complete' if every meal is logged, 'partial' if some are missing"),
        ],
        day: Annotated[
            date | None,
            Field(description="The user's local date (YYYY-MM-DD); leave empty for today"),
        ] = None,
    ) -> SetDayStatusResult:
        user_id = current_user_id()
        now = utcnow()
        try:
            async with db.session() as session:
                local_date = day or await user_today(session, user_id, now)
                info = await set_day_status(
                    session, user_id, local_date, status, "assistant", now=now
                )
        except FutureDayError as error:
            raise ToolError(f"Not saved to HealthyEating: {error}.") from None
        label = "complete" if info.status == "complete" else "partial (some meals missing)"
        return SetDayStatusResult(
            saved_to_healthy_eating=True,
            user_message=f"Marked {info.local_date.isoformat()} as {label} in HealthyEating.",
            date=info.local_date,
            status=info.status,
        )
