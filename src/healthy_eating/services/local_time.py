"""Local time: the user's time zone, the local time of a meal and local days.

A meal's local time uses, in this order: the UTC offset stored with the meal (the offset the
user gave, or the one of their time zone when it was logged), the user's current time zone,
UTC. Storing the offset keeps a meal on the same local day when the user travels or changes
their time zone later.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from sqlalchemy import Date, case, cast, func
from sqlalchemy.sql.elements import ColumnElement

from healthy_eating.db.models import Meal

# UTC offsets in use range from -12:00 to +14:00. Range queries first select meals by
# eaten_at (indexed) with this margin, then by the exact local date.
LOCAL_DAY_MARGIN = timedelta(days=1)


@cache
def _time_zone_names() -> frozenset[str]:
    return frozenset(available_timezones())


def is_valid_time_zone(name: str) -> bool:
    """Whether `name` is an IANA time zone name, such as "Europe/Berlin"."""
    return name in _time_zone_names()


def zone_info(name: str | None) -> ZoneInfo:
    """The user's time zone, or UTC when it is not known."""
    return ZoneInfo(name) if name else ZoneInfo("UTC")


def offset_minutes(moment: datetime) -> int | None:
    offset = moment.utcoffset()
    return None if offset is None else int(offset.total_seconds() // 60)


def resolve_eaten_at(
    eaten_at: datetime | None, time_zone: str | None, *, now: datetime
) -> tuple[datetime, int | None]:
    """When a meal was eaten, as a UTC-aware time plus the UTC offset of its local time.

    `eaten_at` is None for "just now". A time without an offset is read in the user's time
    zone; without a time zone it is stored as UTC with the offset unknown (None).
    """
    if eaten_at is None:
        eaten_at = now
        if time_zone is None:
            return eaten_at, None
        return eaten_at, offset_minutes(eaten_at.astimezone(zone_info(time_zone)))
    if eaten_at.utcoffset() is None:
        if time_zone is None:
            return eaten_at.replace(tzinfo=UTC), None
        eaten_at = eaten_at.replace(tzinfo=zone_info(time_zone))
    return eaten_at.astimezone(UTC), offset_minutes(eaten_at)


def local_eaten_at(
    eaten_at: datetime, utc_offset_minutes: int | None, time_zone: str | None
) -> datetime:
    """The meal's time in its local time, with the UTC offset attached."""
    if utc_offset_minutes is not None:
        return eaten_at.astimezone(timezone(timedelta(minutes=utc_offset_minutes)))
    return eaten_at.astimezone(zone_info(time_zone))


def local_date_column(time_zone: str | None) -> ColumnElement[date]:
    """SQL expression for the local date of `Meal.eaten_at`, following `local_eaten_at`."""
    utc_wall_time = func.timezone("UTC", Meal.eaten_at)
    with_offset = utc_wall_time + func.make_interval(0, 0, 0, 0, 0, Meal.utc_offset_minutes)
    in_time_zone = func.timezone(time_zone or "UTC", Meal.eaten_at)
    return cast(
        case((Meal.utc_offset_minutes.is_not(None), with_offset), else_=in_time_zone),
        Date,
    )


def utc_window(first: date | None, last: date | None) -> tuple[datetime | None, datetime | None]:
    """A UTC range of eaten_at that contains every meal of the local days first..last."""
    start = end = None
    if first is not None:
        start = datetime.combine(first, time(), UTC) - LOCAL_DAY_MARGIN
    if last is not None:
        end = datetime.combine(last + timedelta(days=1), time(), UTC) + LOCAL_DAY_MARGIN
    return start, end
