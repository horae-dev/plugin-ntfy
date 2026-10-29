# SPDX-License-Identifier: Apache-2.0
"""The `ntfy.push` entry's trusted parameters, checked on every apply.

A pipeline's `notify` entry configures the notifier:

```yaml
- notify:
    - ntfy.push:
        server: https://ntfy.example.org   # default https://ntfy.sh
        topic: horae-reminders             # required
        priority: high                     # min | low | normal (default) | high | critical
        tags: [alarm_clock]                # ntfy tags (emoji short codes), at most 8
        auth: token                        # token (default: NTFY_TOKEN, Bearer) | none
        quiet_hours: { start: "22:00", end: "07:00", during: delay }   # during: delay | drop
        link_prefix: https://notes.example.org/daily/                  # read by the core
```

These parameters are **trusted** (the pipeline author's, never a proposer's), but a typo must
not silently turn a feature off (`quiet_hour:` would disable quiet hours), so any unknown key
or invalid value is `SinkError("config_invalid")`, naming the parameter and never echoing its
value. `link_prefix` is the core's (it routes a `link` only under it); the notifier checks
the link against it again.

**Server.** `https://<host>[:port]` with no path, query, fragment or user info. The host is a
lowercase DNS name as `permissions.network` writes it (no IP literal, no `localhost`). An
`http://` server is accepted only for the LAN: the connection is refused unless the host
resolves to loopback, RFC 1918 or IPv6 ULA addresses (`horae_ntfy.http`), the rule the core
applies to an `http://` LLM endpoint. The token then travels in clear on that network.
"""

import re
from dataclasses import dataclass
from datetime import time
from typing import Any, Literal
from urllib.parse import urlsplit

from horae_sdk import SinkError

DEFAULT_SERVER = "https://ntfy.sh"
TOKEN_SECRET = "NTFY_TOKEN"  # noqa: S105 - the name of the secret, not its value
"""The declared secret holding the access token (`permissions.secrets`)."""

PRIORITIES: dict[str, int] = {"min": 1, "low": 2, "normal": 3, "high": 4, "critical": 5}
"""Horae priority name -> ntfy priority (1 to 5). Only `critical` bypasses quiet hours."""
CRITICAL = "critical"
MAX_TAGS = 8

_KEYS = {"server", "topic", "priority", "tags", "auth", "quiet_hours", "link_prefix"}
_QUIET_KEYS = {"start", "end", "during"}
_TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")  # ntfy's own topic rule
_TAG = re.compile(r"[a-z0-9+][a-z0-9_+-]{0,31}")
_HOST = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
_LOCALHOST = re.compile(r"(^|\.)localhost$")
_HHMM = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")

During = Literal["delay", "drop"]


def _invalid(name: str, why: str) -> SinkError:
    return SinkError("config_invalid", f"ntfy.push parameter {name}: {why}")


@dataclass(frozen=True)
class QuietHours:
    """Local wall-clock times in the instance time zone; `start > end` crosses midnight."""

    start: time
    end: time
    during: During = "delay"


@dataclass(frozen=True)
class Settings:
    server: str
    """`scheme://host[:port]`, no trailing slash."""
    scheme: Literal["https", "http"]
    host: str
    topic: str
    priority: str
    tags: tuple[str, ...]
    auth: Literal["token", "none"]
    quiet_hours: QuietHours | None
    link_prefix: str | None

    @property
    def ntfy_priority(self) -> int:
        return PRIORITIES[self.priority]

    @property
    def critical(self) -> bool:
        return self.priority == CRITICAL


def _server(value: object) -> tuple[str, Literal["https", "http"], str]:
    if not isinstance(value, str) or len(value) > 300:
        raise _invalid("server", "must be an https:// URL")
    if not value.isascii() or any(ch <= " " or ch == "\x7f" for ch in value):
        raise _invalid("server", "must be an https:// URL without spaces or controls")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise _invalid("server", "is not a valid URL") from None
    scheme = parts.scheme
    if scheme not in ("https", "http"):
        raise _invalid("server", "must be https:// (or http:// for a LAN server)")
    host = parts.hostname or ""
    if "@" in parts.netloc or parts.query or parts.fragment or "#" in value or "?" in value:
        raise _invalid("server", "has user info, a query or a fragment")
    if parts.path not in ("", "/"):
        raise _invalid("server", "has a path (ntfy publishes at the server root)")
    if parts.netloc != parts.netloc.lower() or not _HOST.fullmatch(host):
        raise _invalid("server", "host must be a lowercase DNS name (no IP literal)")
    if _LOCALHOST.search(host) or len(host) > 253:
        raise _invalid("server", "host must not be localhost")
    if port is not None and not 1 <= port <= 65535:
        raise _invalid("server", "has an invalid port")
    netloc = host if port is None else f"{host}:{port}"
    if parts.netloc != netloc:
        raise _invalid("server", "has an invalid port")
    return f"{scheme}://{netloc}", "https" if scheme == "https" else "http", host


def _clock(value: object, name: str) -> time:
    match = _HHMM.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _invalid(f"quiet_hours.{name}", 'must be "HH:MM" (24-hour)')
    return time(int(match.group(1)), int(match.group(2)))


def _quiet(value: object) -> QuietHours | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid("quiet_hours", "must be an object with start and end")
    unknown = sorted(str(key) for key in set(value) - _QUIET_KEYS)
    if unknown:
        raise _invalid("quiet_hours", f"unknown key {unknown[0]!r:.40}")
    if "start" not in value or "end" not in value:
        raise _invalid("quiet_hours", "needs start and end")
    start, end = _clock(value["start"], "start"), _clock(value["end"], "end")
    if start == end:
        raise _invalid("quiet_hours", "start and end must differ")
    during = value.get("during", "delay")
    if during not in ("delay", "drop"):
        raise _invalid("quiet_hours.during", "must be delay or drop")
    return QuietHours(start, end, "delay" if during == "delay" else "drop")


def parse(params: dict[str, Any]) -> Settings:
    """The entry's parameters as `Settings`, or `SinkError("config_invalid")`."""
    unknown = sorted(str(key) for key in set(params) - _KEYS)
    if unknown:
        raise _invalid(f"{unknown[0]!r:.40}", "unknown parameter")
    server, scheme, host = _server(params.get("server", DEFAULT_SERVER))
    topic = params.get("topic")
    if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
        raise _invalid("topic", "required, 1 to 64 of A-Z a-z 0-9 _ -")
    priority = params.get("priority", "normal")
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise _invalid("priority", f"must be one of {', '.join(PRIORITIES)}")
    tags = params.get("tags", [])
    if (
        not isinstance(tags, list)
        or len(tags) > MAX_TAGS
        or not all(isinstance(tag, str) and _TAG.fullmatch(tag) for tag in tags)
        or len(set(tags)) != len(tags)
    ):
        raise _invalid("tags", f"at most {MAX_TAGS} distinct short codes (a-z 0-9 _ + -)")
    auth = params.get("auth", "token")
    if auth not in ("token", "none"):
        raise _invalid("auth", "must be token or none")
    prefix = params.get("link_prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise _invalid("link_prefix", "must be a string")
    return Settings(
        server=server,
        scheme=scheme,
        host=host,
        topic=topic,
        priority=priority,
        tags=tuple(tags),
        auth="token" if auth == "token" else "none",
        quiet_hours=_quiet(params.get("quiet_hours")),
        link_prefix=prefix,
    )
