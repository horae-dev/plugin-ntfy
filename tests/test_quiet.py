# SPDX-License-Identifier: Apache-2.0
"""Quiet hours on the wall clock of the instance time zone: midnight, DST, odd offsets."""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from horae_sdk import SinkError

from horae_ntfy.quiet import in_window, is_quiet, quiet_until, zone_from_env
from horae_ntfy.settings import QuietHours

PARIS = ZoneInfo("Europe/Paris")
NIGHT = QuietHours(time(22, 0), time(7, 0))
NAP = QuietHours(time(13, 0), time(14, 30))


def local(
    zone: ZoneInfo,
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    second: int = 0,
    *,
    fold: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=zone, fold=fold)


@pytest.mark.parametrize(
    ("wall", "quiet"),
    [
        (time(21, 59), False),
        (time(22, 0), True),
        (time(23, 59), True),
        (time(0, 0), True),
        (time(6, 59), True),
        (time(7, 0), False),
        (time(12, 0), False),
    ],
)
def test_a_window_crossing_midnight(wall: time, quiet: bool) -> None:
    assert in_window(wall, NIGHT) is quiet


@pytest.mark.parametrize(
    ("wall", "quiet"),
    [(time(12, 59), False), (time(13, 0), True), (time(14, 29), True), (time(14, 30), False)],
)
def test_a_window_within_the_day(wall: time, quiet: bool) -> None:
    assert in_window(wall, NAP) is quiet


def test_quiet_until_the_next_morning() -> None:
    now = local(PARIS, 2026, 10, 20, 23, 15, 42)
    assert is_quiet(now, NIGHT, PARIS)
    assert quiet_until(now, NIGHT, PARIS) == local(PARIS, 2026, 10, 21, 7, 0)


def test_quiet_until_the_same_morning() -> None:
    now = local(PARIS, 2026, 10, 21, 3, 0)
    assert quiet_until(now, NIGHT, PARIS) == local(PARIS, 2026, 10, 21, 7, 0)


def test_the_last_quiet_minute() -> None:
    now = local(PARIS, 2026, 10, 21, 6, 59, 59)
    assert is_quiet(now, NIGHT, PARIS)
    assert quiet_until(now, NIGHT, PARIS) == local(PARIS, 2026, 10, 21, 7, 0)


def test_the_window_follows_the_zone_not_utc() -> None:
    # 21:30 UTC is 22:30 in Paris in winter (quiet), 21:30 in London (not quiet).
    instant = datetime(2026, 1, 15, 21, 30, tzinfo=UTC)
    assert is_quiet(instant, NIGHT, PARIS)
    assert not is_quiet(instant, NIGHT, ZoneInfo("Europe/London"))


def test_spring_forward_shortens_the_night() -> None:
    # Paris, 2026-03-29: 02:00 CET jumps to 03:00 CEST. The night is an hour shorter, and the
    # end is still 07:00 on the wall clock.
    now = local(PARIS, 2026, 3, 28, 23, 0)
    until = quiet_until(now, NIGHT, PARIS)
    assert until == local(PARIS, 2026, 3, 29, 7, 0)
    assert until.astimezone(UTC) == datetime(2026, 3, 29, 5, 0, tzinfo=UTC)
    assert (until - now).total_seconds() == 7 * 3600  # 8 wall-clock hours, 7 real ones


def test_an_end_inside_the_spring_gap_ends_when_the_gap_does() -> None:
    window = QuietHours(time(1, 0), time(2, 30))  # 02:30 does not exist on 2026-03-29
    now = local(PARIS, 2026, 3, 29, 1, 45)
    until = quiet_until(now, window, PARIS)
    assert until.astimezone(PARIS).timetuple()[:5] == (2026, 3, 29, 3, 0)
    assert until == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)


def test_fall_back_keeps_the_repeated_hour_quiet_on_both_passes() -> None:
    # Paris, 2026-10-25: 03:00 CEST goes back to 02:00 CET; 02:15 happens twice.
    window = QuietHours(time(2, 0), time(3, 0))
    first = local(PARIS, 2026, 10, 25, 2, 15, fold=0)
    second = local(PARIS, 2026, 10, 25, 2, 15, fold=1)
    assert first.astimezone(UTC) != second.astimezone(UTC)
    assert is_quiet(first, window, PARIS)
    assert is_quiet(second, window, PARIS)
    # From the first pass, 03:00 is two real hours away: the repeated hour is quiet too.
    assert quiet_until(first, window, PARIS) == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)
    assert quiet_until(second, window, PARIS) == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)


def test_fall_back_lengthens_the_night() -> None:
    now = local(PARIS, 2026, 10, 24, 22, 0)
    until = quiet_until(now, NIGHT, PARIS)
    assert until == local(PARIS, 2026, 10, 25, 7, 0)
    assert (until - now).total_seconds() == 10 * 3600  # 9 wall-clock hours, 10 real ones


def test_a_new_york_dst_night() -> None:
    zone = ZoneInfo("America/New_York")  # 2026-03-08 02:00 EST -> 03:00 EDT
    now = local(zone, 2026, 3, 7, 23, 30)
    assert quiet_until(now, NIGHT, zone) == local(zone, 2026, 3, 8, 7, 0)


def test_a_half_hour_offset_zone() -> None:
    zone = ZoneInfo("Asia/Kolkata")  # UTC+05:30
    instant = datetime(2026, 6, 1, 16, 45, tzinfo=UTC)  # 22:15 in Kolkata
    assert is_quiet(instant, NIGHT, zone)
    assert quiet_until(instant, NIGHT, zone) == datetime(2026, 6, 2, 1, 30, tzinfo=UTC)


@pytest.mark.parametrize("value", ["Europe/Paris", ":Europe/Paris"])
def test_the_zone_comes_from_tz(value: str) -> None:
    assert zone_from_env({"TZ": value}) == PARIS


@pytest.mark.parametrize("environ", [{}, {"TZ": ""}, {"TZ": "Mars/Olympus"}, {"TZ": "../etc"}])
def test_a_missing_or_unknown_zone_is_a_config_error(environ: dict[str, str]) -> None:
    with pytest.raises(SinkError) as raised:
        zone_from_env(environ)
    assert raised.value.kind == "config_invalid"


def test_the_process_environment_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert zone_from_env() == ZoneInfo("Asia/Tokyo")
