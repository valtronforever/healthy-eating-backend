"""Day completeness: whether the user logged every meal of a local day, and which days count.

A forgotten meal makes a day look like a low-calorie day and distorts averages. Users (in
the app) or the assistant (when the user says "that's all for today") can mark a local day
as complete or partial.

Which days count for averages:
- once the user has marked any day as complete, only complete days with at least one meal;
- otherwise every day with at least one meal, except days marked partial.
A day without meals never counts, even when marked complete, so an accidental mark on an
empty day cannot pull averages down.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from healthy_eating.db.models import DayStatusRecord
from healthy_eating.nutrition.types import DayStatus, StatusSetter
from healthy_eating.services.local_time import zone_info
from healthy_eating.services.settings import get_time_zone

# How far after the user's "today" a day may be marked: covers a time zone that is not set
# yet (UTC) while the user's local day has already moved on.
FUTURE_MARGIN = timedelta(days=1)


class FutureDayError(ValueError):
    """The day to mark has not started yet."""


@dataclass(frozen=True)
class DayStatusInfo:
    local_date: date
    status: DayStatus
    set_by: StatusSetter
    updated_at: datetime


def _info(record: DayStatusRecord) -> DayStatusInfo:
    return DayStatusInfo(
        local_date=record.local_date,
        status="complete" if record.status == "complete" else "partial",
        set_by="assistant" if record.set_by == "assistant" else "user",
        updated_at=record.updated_at,
    )


def local_today(time_zone: str | None, now: datetime) -> date:
    return now.astimezone(zone_info(time_zone)).date()


async def user_today(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> date:
    return local_today(await get_time_zone(session, user_id), now)


async def set_day_status(
    session: AsyncSession,
    user_id: uuid.UUID,
    local_date: date,
    status: DayStatus,
    set_by: StatusSetter,
    *,
    now: datetime,
) -> DayStatusInfo:
    if local_date > await user_today(session, user_id, now) + FUTURE_MARGIN:
        raise FutureDayError(f"{local_date.isoformat()} has not started yet")
    statement = (
        insert(DayStatusRecord)
        .values(user_id=user_id, local_date=local_date, status=status, set_by=set_by)
        .on_conflict_do_update(
            index_elements=[DayStatusRecord.user_id, DayStatusRecord.local_date],
            set_={"status": status, "set_by": set_by, "updated_at": func.now()},
        )
        .returning(DayStatusRecord)
    )
    record = (await session.scalars(statement)).one()
    return _info(record)


async def clear_day_status(session: AsyncSession, user_id: uuid.UUID, local_date: date) -> bool:
    """Remove the mark of a day. False if the day was not marked."""
    removed = await session.scalar(
        delete(DayStatusRecord)
        .where(DayStatusRecord.user_id == user_id, DayStatusRecord.local_date == local_date)
        .returning(DayStatusRecord.local_date)
    )
    return removed is not None


async def day_statuses(
    session: AsyncSession, user_id: uuid.UUID, first_day: date, last_day: date
) -> dict[date, DayStatusInfo]:
    records = await session.scalars(
        select(DayStatusRecord).where(
            DayStatusRecord.user_id == user_id,
            DayStatusRecord.local_date >= first_day,
            DayStatusRecord.local_date <= last_day,
        )
    )
    return {record.local_date: _info(record) for record in records}


async def counts_complete_days_only(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether the user has ever marked a day complete (see the module docstring)."""
    return bool(
        await session.scalar(
            select(
                exists().where(
                    DayStatusRecord.user_id == user_id, DayStatusRecord.status == "complete"
                )
            )
        )
    )


def day_counts(status: DayStatus | None, meal_count: int, *, complete_days_only: bool) -> bool:
    """Whether a day counts for averages (see the module docstring)."""
    if meal_count == 0:
        return False
    if complete_days_only:
        return status == "complete"
    return status != "partial"
