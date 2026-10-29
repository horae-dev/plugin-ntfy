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
Sunday of March is 03:00 CEST). It walks forward a minute at a time, at most a day and a few
hours of minutes: simple enough to be obviously right in every zone, including half-hour
offsets.
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
        raise SinkError("config_invalid", "quiet_hours need TZ (the instance time zone)")
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


def is_quiet(now: datetime, window: QuietHours, zone: ZoneInfo) -> bool:
    """Whether the aware instant `now` falls in the quiet window."""
    return in_window(_wall(now, zone), window)


def quiet_until(now: datetime, window: QuietHours, zone: ZoneInfo) -> datetime:
    """The first minute boundary after `now` (UTC) that is not quiet."""
    instant = now.astimezone(UTC).replace(second=0, microsecond=0)
    for _ in range(_MAX_MINUTES):
        instant += timedelta(minutes=1)
        if not in_window(_wall(instant, zone), window):
            return instant
    raise AssertionError("a quiet window never ends")  # pragma: no cover - start != end
