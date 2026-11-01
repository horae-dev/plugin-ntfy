# SPDX-License-Identifier: Apache-2.0
"""Quiet hours: local wall-clock windows in the instance time zone, DST-safe.

The plugin host sets `TZ` to `horae.yaml`'s `timezone` in every plugin's environment, so the
window is read in the instance's zone (`zone_from_env`). An instant is **quiet** when its
local wall-clock time (minute precision) is in `[start, end)`; with `start > end` the window
crosses midnight (`22:00`-`07:00`: from 22:00 to 06:59 inclusive).

Wall-clock times make daylight saving time unsurprising: on the night clocks go back, the
repeated hour is quiet on both passes when it is in the window; on the night they go forward,
the skipped hour simply never happens. `quiet_until` is the first instant (on a minute
boundary, UTC) whose wall-clock time is outside the window: when `end` itself is skipped by a
spring-forward gap, that is the moment the gap ends (e.g. `end: "02:30"` in Paris on the last
Sunday of March is 03:00 CEST). When `end` falls **inside the repeated hour** of a fall-back
night, the window ends at its **later** occurrence (`end: "02:30"` in Paris on the last
Sunday of October is 02:30 CET, not 02:30 CEST): between the two, the clock comes back into
the window, so ending at the first would wake the phone in the middle of the night. The
stretch between the two occurrences is quiet too (`is_quiet`).

`quiet_until` walks forward a minute at a time, at most a day and a few hours of minutes:
simple enough to be obviously right in every zone, including half-hour offsets.
"""

import os
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from horae_sdk import SinkError

from horae_ntfy.settings import QuietHours

_MAX_MINUTES = 26 * 60  # a window is shorter than a day; DST adds at most a couple of hours


def zone_from_env(env: Mapping[str, str] | None = None) -> ZoneInfo:
    """The instance time zone from `TZ` (an IANA name; a leading `:` is allowed)."""
    name = (os.environ if env is None else env).get("TZ", "").removeprefix(":")
    if not name:
        raise SinkError("config_invalid", "quiet_hours and dedupe need TZ (the instance time zone)")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SinkError("config_invalid", "TZ is not a known IANA time zone") from None


def in_window(wall: time, window: QuietHours) -> bool:
    """Whether the local wall-clock time `wall` is quiet."""
    if window.start < window.end:
        return window.start <= wall < window.end
    return wall >= window.start or wall < window.end


def _wall(instant: datetime, zone: ZoneInfo) -> time:
    local = instant.astimezone(zone)
    return time(local.hour, local.minute)


def _later_occurrence(instant: datetime, window: QuietHours, zone: ZoneInfo) -> datetime | None:
    """When `instant` is the first pass of a repeated (fall-back) wall-clock time and the
    clock comes back into the window before its second pass, that second pass (UTC);
    `None` otherwise."""
    local = instant.astimezone(zone)
    if local.fold:
        return None
    later = local.replace(fold=1).astimezone(UTC)
    if later <= instant or not in_window(_wall(later - timedelta(minutes=1), zone), window):
        return None
    return later


def _overlap_end(now: datetime, window: QuietHours, zone: ZoneInfo) -> datetime | None:
    """When `now` lies between the first and the later occurrence of `end` on a fall-back
    night, that later occurrence (UTC); `None` otherwise."""
    local = now.astimezone(zone)
    first = local.replace(
        hour=window.end.hour, minute=window.end.minute, second=0, microsecond=0, fold=0
    ).astimezone(UTC)
    later = _later_occurrence(first, window, zone)
    if later is not None and first <= now.astimezone(UTC) < later:
        return later
    return None


def is_quiet(now: datetime, window: QuietHours, zone: ZoneInfo) -> bool:
    """Whether the aware instant `now` falls in the quiet window."""
    return in_window(_wall(now, zone), window) or _overlap_end(now, window, zone) is not None


def quiet_until(now: datetime, window: QuietHours, zone: ZoneInfo) -> datetime:
    """The first minute boundary after `now` (UTC) that is not quiet (on a fall-back night,
    the later occurrence of an `end` in the repeated hour)."""
    overlap = _overlap_end(now, window, zone)
    if overlap is not None:
        return overlap
    instant = now.astimezone(UTC).replace(second=0, microsecond=0)
    for _ in range(_MAX_MINUTES):
        instant += timedelta(minutes=1)
        if not in_window(_wall(instant, zone), window):
            return _later_occurrence(instant, window, zone) or instant
    raise AssertionError("a quiet window never ends")  # pragma: no cover - start != end
