# SPDX-License-Identifier: Apache-2.0
"""The plugin: `ntfy.push`, a notifier that applies `notify.push` intents through ntfy.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_ntfy`). A pipeline's `notify` step names `ntfy.push: {...}` (its trusted parameters:
`horae_ntfy.settings`); the core routes each `notify.push` intent to it and, after the run's
steps, applies it through the sink operation `ntfy.apply` (the SDK's `Notifier`). The core
journals the notification and records its `dedupe_key` as sent when `apply` returns, and
skips a key already sent, so the notifier publishes each call exactly once and never
retries a request that may have reached the server (`horae_ntfy.http`).

**Priority and quiet hours.** The entry's `priority` maps to ntfy's 1 to 5 (`min` 1, `low` 2,
`normal` 3, `high` 4, `critical` 5). During the entry's `quiet_hours` (local times in the
instance time zone, `TZ`), a push that is not `critical` is:

- `during: delay` (the default): handed to ntfy now with ntfy's scheduled delivery
  (`delay`: the Unix time the quiet hours end, at least ten seconds ahead), so the phone gets
  it when they end. Nothing is kept in the plugin: the server holds the message (it needs
  ntfy's message cache, on by default);
- `during: drop`: not sent at all.

Either way `apply` succeeds, so the core records the notification as handled and will not
send that `dedupe_key` again. A `critical` push is always sent at once.

**What `apply` returns** (`Applied`): the target `<server>/<topic>` and `details`
(`delivery`: `sent`, `delayed` or `dropped`; `priority`; the ntfy message `id` when the
server gave one; `until`, the UTC time a delayed push is due). Nothing in it comes from the
proposer's text, and the token is never in it.
"""

import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from horae_sdk import Applied, ApplyRequest, Notifier, Paths, Permissions, Plugin, SinkError

from horae_ntfy.http import Publisher
from horae_ntfy.message import payload
from horae_ntfy.quiet import is_quiet, quiet_until, zone_from_env
from horae_ntfy.settings import DEFAULT_SERVER, TOKEN_SECRET, Settings, parse

NAME = "ntfy"
"""The plugin's name, also the namespace of its refs (`ntfy.push`)."""
VERSION = "0.1.0"
TOOL = f"{NAME}.push"
COMMAND = ["python3", "-m", "horae_ntfy"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(
    intents=["notify.push"],
    network=[DEFAULT_SERVER.removeprefix("https://")],
    secrets=[TOKEN_SECRET],
)
"""`notify.push` (applied, never proposed), the default public server `ntfy.sh` and the access
token. A self-hosted server's host is approved in `horae.lock` (README, "Your own server")."""
MIN_DELAY = timedelta(seconds=10)
"""ntfy refuses a scheduled delivery less than ten seconds ahead."""
_TOKEN_CHARS = frozenset(chr(code) for code in range(0x21, 0x7F))

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class NtfyNotifier(Notifier):
    """`ntfy.push`. `publisher`, `now` and `env` are seams for tests (a fake server's
    transport, a fake clock, a fake environment)."""

    def __init__(
        self,
        publisher: Publisher | None = None,
        *,
        now: Clock = utc_now,
        env: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__([TOOL])
        self.publisher = publisher or Publisher()
        self._now = now
        self._env = env

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def identity(self, paths: Paths) -> str:
        return NAME

    def _token(self, settings: Settings) -> str | None:
        if settings.auth == "none":
            return None
        token = self._environ().get(TOKEN_SECRET)
        if not token:
            raise SinkError("secret_missing", f"{TOKEN_SECRET} is not set (or set auth: none)")
        if len(token) > 512 or not set(token) <= _TOKEN_CHARS:
            raise SinkError("secret_invalid", f"{TOKEN_SECRET} is not a valid access token")
        return token

    async def apply(self, request: ApplyRequest) -> Applied:
        if request.intent.get("type") != "notify.push":
            raise SinkError("not_supported", f"{TOOL} applies notify.push intents only")
        settings = parse(request.params)
        token = self._token(settings)
        target = f"{settings.server}/{settings.topic}"
        details: dict[str, Any] = {"delivery": "sent", "priority": settings.ntfy_priority}
        delay_until: datetime | None = None
        window = settings.quiet_hours
        if window is not None and not settings.critical:
            zone = zone_from_env(self._environ())
            now = self._now().astimezone(UTC)
            if is_quiet(now, window, zone):
                delay_until = max(quiet_until(now, window, zone), now + MIN_DELAY)
                details["delivery"] = "dropped" if window.during == "drop" else "delayed"
        # Built (and the intent checked) even when dropped: a bad intent fails the same way.
        message = payload(
            request.intent,
            settings,
            delay_until=None if delay_until is None else math.ceil(delay_until.timestamp()),
        )
        if details["delivery"] == "dropped":
            return Applied(target, details=details)
        message_id = await self.publisher.publish(settings, message, token)
        if message_id is not None:
            details["id"] = message_id
        if delay_until is not None:
            details["until"] = delay_until.replace(microsecond=0).isoformat()
        return Applied(target, details=details)


def build(notifier: NtfyNotifier | None = None) -> Plugin:
    """The plugin, applying pushes through `notifier` (a default one when omitted)."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Sends Horae notifications through ntfy, with priorities and quiet hours.",
    )
    plugin.add(notifier or NtfyNotifier())
    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_ntfy --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=">=0,<0.2",  # the core is 0.0.x until v0.1; nothing checks it yet
        license="Apache-2.0",
        homepage="https://github.com/horae-dev/plugin-ntfy",
    )
