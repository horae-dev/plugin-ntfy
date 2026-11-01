# SPDX-License-Identifier: Apache-2.0
"""The plugin: `ntfy.push`, a notifier that applies `notify.push` intents through ntfy.

The core's plugin host starts this module as an MCP server over stdio (`python -m
horae_ntfy`). A pipeline's `notify` step names `ntfy.push: {...}` (its trusted parameters:
`horae_ntfy.settings`); the core routes each `notify.push` intent to it and, after the run's
steps, applies it through the sink operation `ntfy.apply` (the SDK's `Notifier`). The core
journals the notification and records its `dedupe_key` as sent when `apply` returns, and
skips a key already sent, so the notifier publishes each call exactly once and never
retries a request that may have reached the server (`horae_ntfy.http`).

**Duplicates after an ambiguous failure: at least once.** When a publish may have reached the
server but `apply` did not finish (a timeout after sending, the host cancelling the call, the
process killed before the reminder state was written), nothing recorded the push, and the
next run publishes it again. Each publish therefore carries ntfy's `sequence_id`
(`message.sequence_id`), derived with `dedupe: daily` from the **rule** (`horae_ntfy.state`),
which is the same in every run, and with `dedupe: off` from the core's `dedupe_key`: ntfy
replaces a scheduled message with the same sequence id, and clients that support updates show
a delivered one updated in place, instead of adding a copy. This needs ntfy server v2.16 or
later (an older server ignores the field and shows both).

**Priority and quiet hours.** The entry's `priority` maps to ntfy's 1 to 5 (`min` 1, `low` 2,
`normal` 3, `high` 4, `critical` 5). During the entry's `quiet_hours` (local times in the
instance time zone, `TZ`), a push that is not `critical` is:

- `during: delay` (the default): handed to ntfy now with ntfy's scheduled delivery, so the
  phone gets it when they end. `delay` is a **duration** (`"<n>s"`, at least `MIN_DELAY`
  ahead), not an absolute time: the server counts it on its own clock, so a skew between
  the two clocks shifts the delivery a little instead of making ntfy refuse a time it
  thinks is too close or past. A 400 to a scheduled publish is `delay_refused`: the push is
  never re-sent at once, which would wake the phone. The message is not kept here: the
  server holds the message, in its message cache (a self-hosted server needs a persistent
  `cache-file` or database, or a restart loses the scheduled messages);
- `during: drop`: not sent at all. With `dedupe: daily` it is recorded in the reminder state
  as if sent, so the first run after quiet hours does not push it either.

Either way `apply` succeeds, so the core records the notification as handled and will not
send that `dedupe_key` again. A `critical` push is always sent at once.

**Once a day per rule.** With `dedupe: daily` (the default) a push of a rule (the entry and
the intent's text) is not sent again within a day of the last one, on the local wall clock
(`delivery: duplicate`), whatever its `dedupe_key`: the core gives a pipeline without an LLM
step a new key every run. `horae_ntfy.state` keeps that record in the plugin's approved
`paths.write` directory.

**What `apply` returns** (`Applied`): the target `<server>/<topic>` and `details`:
`delivery` (`sent`, `delayed`, `dropped` or `duplicate`); `priority`; the ntfy message `id`
when the server gave one; `until`, the UTC time a delayed push is due by this host's clock;
`click: "dropped"` when the intent's `obsidian://` link was not sent, until story #4; `state`
(`reset`, `evicted` or `not_saved`) when the reminder state was started over, had to drop a
live entry to stay bounded, or could not be written. Nothing in it comes from the proposer's
text, and the token is never in it.
"""

import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from horae_sdk import Applied, ApplyRequest, Notifier, Paths, Permissions, Plugin, SinkError

from horae_ntfy.http import Publisher
from horae_ntfy.message import is_obsidian, payload
from horae_ntfy.quiet import is_quiet, quiet_until, zone_from_env
from horae_ntfy.settings import DEFAULT_SERVER, TOKEN_SECRET, Settings, parse
from horae_ntfy.state import (
    LOCK_TIMEOUT_S,
    SentState,
    is_repeat,
    minute,
    rule_fingerprint,
    state_dir,
)

NAME = "ntfy"
"""The plugin's name, also the namespace of its refs (`ntfy.push`)."""
VERSION = "0.2.0"
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
MIN_DELAY = timedelta(seconds=30)
"""ntfy refuses a scheduled delivery less than ten seconds ahead (its default minimum); 30 s
leaves a margin for the time the request takes and for the two clocks counting it."""
_TOKEN_CHARS = frozenset(chr(code) for code in range(0x21, 0x7F))

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class NtfyNotifier(Notifier):
    """`ntfy.push`. `publisher`, `now` and `env` are seams for tests (a fake server's
    transport, a fake clock, a fake environment); `state_lock_timeout_s` bounds the wait for
    another run holding the reminder state."""

    def __init__(
        self,
        publisher: Publisher | None = None,
        *,
        now: Clock = utc_now,
        env: Mapping[str, str] | None = None,
        state_lock_timeout_s: float = LOCK_TIMEOUT_S,
    ) -> None:
        super().__init__([TOOL])
        self.state_lock_timeout_s = state_lock_timeout_s
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
        now = self._now().astimezone(UTC)
        delivery = now
        delay_until: datetime | None = None
        delay_s: int | None = None
        window = settings.quiet_hours
        if window is not None and not settings.critical:
            zone = zone_from_env(self._environ(), "quiet_hours")
            if is_quiet(now, window, zone):
                delay_until = max(quiet_until(now, window, zone), now + MIN_DELAY)
                delay_s = math.ceil((delay_until - now).total_seconds())
                if window.during == "drop":
                    details["delivery"] = "dropped"
                else:
                    details["delivery"] = "delayed"
                    delivery = delay_until
        daily = settings.dedupe == "daily"
        rule = rule_fingerprint(settings, request.intent)
        # The sequence id of a rule is the same in every run (`dedupe: daily`), so a push sent
        # again after an ambiguous failure replaces the first one on ntfy.
        sequence_key = rule if daily else request.dedupe_key
        # Built (and the intent checked) even when dropped: a bad intent fails the same way.
        message = payload(request.intent, settings, delay_s=delay_s, dedupe_key=sequence_key)
        if is_obsidian(request.intent.get("link")):
            details["click"] = "dropped"
        dropped = details["delivery"] == "dropped"
        if not daily:
            if not dropped:
                details.update(await self._publish(settings, message, token, delay_until))
            return Applied(target, details=details)
        # Once a day per rule, on the local wall clock (`horae_ntfy.state`).
        state = SentState(state_dir(request.paths), lock_timeout_s=self.state_lock_timeout_s)
        zone = zone_from_env(self._environ(), "dedupe: daily")
        async with state.locked() as sent:
            if sent.reset:
                details["state"] = "reset"
            previous = sent.entries.get(rule)
            if previous is not None and is_repeat(previous, delivery, zone):
                return Applied(target, details={**details, "delivery": "duplicate"})
            if not dropped:  # a dropped push is recorded too: not re-sent after quiet hours
                details.update(await self._publish(settings, message, token, delay_until))
            sent.entries[rule] = minute(delivery)
            try:
                await state.store(sent, now)
            except OSError:
                details["state"] = "not_saved"  # pushed: the core must still record it
            else:
                if sent.evicted:
                    details["state"] = "evicted"
        return Applied(target, details=details)

    async def _publish(
        self,
        settings: Settings,
        message: dict[str, Any],
        token: str | None,
        delay_until: datetime | None,
    ) -> dict[str, Any]:
        """Publishes `message`; the details it adds (the message `id`, `until`)."""
        details: dict[str, Any] = {}
        message_id = await self.publisher.publish(settings, message, token)
        if message_id is not None:
            details["id"] = message_id
        if delay_until is not None:
            details["until"] = delay_until.replace(microsecond=0).isoformat()
        return details


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
