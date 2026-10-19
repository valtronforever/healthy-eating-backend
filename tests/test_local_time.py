from datetime import UTC, date, datetime

from healthy_eating.services.local_time import (
    is_valid_time_zone,
    local_eaten_at,
    resolve_eaten_at,
    utc_window,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def test_iana_names_are_valid_time_zones() -> None:
    assert is_valid_time_zone("Europe/Berlin")
    assert is_valid_time_zone("UTC")
    assert not is_valid_time_zone("Mars/Olympus_Mons")
    assert not is_valid_time_zone("")
    assert not is_valid_time_zone("../etc/passwd")


def test_time_without_offset_is_read_in_the_users_time_zone() -> None:
    # 18 October 2026: Berlin is on summer time (UTC+2).
    eaten_at, offset = resolve_eaten_at(datetime(2026, 10, 18, 8, 30), "Europe/Berlin", now=NOW)
    assert eaten_at == datetime(2026, 10, 18, 6, 30, tzinfo=UTC)
    assert offset == 120


def test_time_without_offset_follows_daylight_saving_time() -> None:
    eaten_at, offset = resolve_eaten_at(datetime(2026, 12, 1, 8, 30), "Europe/Berlin", now=NOW)
    assert eaten_at == datetime(2026, 12, 1, 7, 30, tzinfo=UTC)
    assert offset == 60


def test_time_without_offset_and_time_zone_is_stored_as_utc() -> None:
    eaten_at, offset = resolve_eaten_at(datetime(2026, 10, 18, 8, 30), None, now=NOW)
    assert eaten_at == datetime(2026, 10, 18, 8, 30, tzinfo=UTC)
    assert offset is None


def test_given_offset_wins_over_the_time_zone() -> None:
    given = datetime.fromisoformat("2026-10-18T08:30:00-04:00")
    eaten_at, offset = resolve_eaten_at(given, "Europe/Berlin", now=NOW)
    assert eaten_at == datetime(2026, 10, 18, 12, 30, tzinfo=UTC)
    assert offset == -240


def test_just_now_records_the_offset_of_the_time_zone() -> None:
    assert resolve_eaten_at(None, "Europe/Berlin", now=NOW) == (NOW, 60)
    assert resolve_eaten_at(None, None, now=NOW) == (NOW, None)


def test_local_time_prefers_the_stored_offset() -> None:
    eaten_at = datetime(2026, 10, 17, 16, 0, tzinfo=UTC)
    tokyo = local_eaten_at(eaten_at, 540, "America/Los_Angeles")
    assert tokyo.isoformat() == "2026-10-18T01:00:00+09:00"
    los_angeles = local_eaten_at(eaten_at, None, "America/Los_Angeles")
    assert los_angeles.isoformat() == "2026-10-17T09:00:00-07:00"
    assert local_eaten_at(eaten_at, None, None).isoformat() == "2026-10-17T16:00:00+00:00"


def test_utc_window_covers_every_offset() -> None:
    start, end = utc_window(date(2026, 10, 18), date(2026, 10, 18))
    assert start == datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    assert utc_window(None, None) == (None, None)
