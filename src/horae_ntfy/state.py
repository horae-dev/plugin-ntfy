# SPDX-License-Identifier: Apache-2.0
"""Reminder state: which rules were pushed and when, so a rule is pushed **once a day**.

**Why the plugin keeps it.** The core records each notification's `dedupe_key` as sent and
never sends a recorded key again, but a pipeline without an LLM step (the `remind` pipeline,
every 5 minutes) gets a key scoped to its run (`horae.apply.plan.dedupe_key`: no
`input_hash`, so the run id), so a reminder proposed again by the next run is a new key. And
only the notifier knows **when a push reaches the phone**: during quiet hours a push is
handed to ntfy now and delivered when they end, often the next day. So the notifier records,
for each rule it handles, the instant of its delivery, and skips a push of the same rule
that would reach the phone less than a day after the recorded one (`delivery: duplicate`,
still a success, so the core records its own key).

- **Rule**: the SHA-256 of the entry's server, topic and priority and the intent's title,
  body and link (`rule_fingerprint`). Only digests are stored, never the text. Two entries
  or pipelines that push the same text to the same topic are one rule.
- **A day** (`is_repeat`): the new delivery is earlier than the recorded one plus one day
  on the **local wall clock** of the instance time zone (`TZ`), to the minute. That covers
  the same local day, and a push across midnight (23:45, then 00:05) or a delayed push the
  morning after a daytime one. On the wall clock a daily reminder stays at its time: it
  does not move by the run interval when a run starts a few seconds earlier, nor by an hour
  after a daylight saving change (a 23 or 25 hour day).
- **Where**: `ntfy-sent.json` in the plugin's single `paths.write` directory, as approved in
  `horae.lock` (`state_dir`). No directory approved is `state_unavailable`, never a silent
  fall back to sending every time; an entry that wants no state says `dedupe: off`. The
  directory must be the plugin user's own, not writable by group or others, and on a
  **local** file system: `flock` and `fsync` are not reliable on NFS, SMB, FUSE or Docker
  Desktop shares.
- **Concurrent runs**: an exclusive `flock` on `ntfy-sent.lock` is held from the check to the
  write, around the publish, so a second run waits and then sees the first run's record. It
  waits at most `LOCK_TIMEOUT_S` (8 s), so that the wait plus its own publish (20 s at most)
  fits in the host's call timeout (30 s), then fails with `state_busy`.
- **At least once**: the record is written after ntfy accepted the push. A run killed in
  between leaves no record, and the next run pushes the rule again, with the same
  `sequence_id` (derived from the rule), which ntfy turns into a replacement.
- **Atomic and crash-safe**: written to a temporary file in the same directory, flushed to
  disk, then renamed over the old one; a crash leaves the old file or the new one. A
  temporary file a crash left behind is removed after an hour, under the lock. File work
  runs in a worker thread (`anyio.to_thread`), not on the event loop.
- **Fail closed**: a file the plugin cannot read (permission denied, a symbolic link) or in
  another `format` (a newer plugin) is `state_unavailable`; only corrupt or oversized content
  starts over (`state: "reset"` in the details: at worst a push is sent again), and bad
  entries are skipped one by one.
- **Bounded**: an entry is dropped once its delivery is more than `RETENTION` (7 days) in the
  past, and at most `MAX_ENTRIES` are kept, the latest deliveries; when that cap drops one
  still inside the retention, the details say `state: "evicted"`.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import anyio.to_thread
from horae_sdk import Paths, SinkError

from horae_ntfy.http import DEADLINE_S
from horae_ntfy.settings import Settings

STATE_FILE = "ntfy-sent.json"
LOCK_FILE = "ntfy-sent.lock"
TEMPORARY_PREFIX = ".ntfy-sent-"
TEMPORARY_SUFFIX = ".tmp"
FORMAT = 1
WINDOW = timedelta(days=1)
"""A rule is not pushed again until a day after its last delivery, on the local wall clock."""
RETENTION = timedelta(days=7)
MAX_ENTRIES = 4096
MAX_STATE_BYTES = 1024 * 1024
STALE_TEMPORARY_S = 3600.0
HOST_CALL_TIMEOUT_S = 30.0
"""The host's default `plugin_defaults.call_timeout_s`: the whole `apply` must fit in it."""
LOCK_MARGIN_S = 2.0
LOCK_TIMEOUT_S = HOST_CALL_TIMEOUT_S - DEADLINE_S - LOCK_MARGIN_S
"""8 s: the wait for another run, plus this run's own publish (at most `DEADLINE_S`, 20 s)
and a margin for the file work, stays under the host's call timeout."""
LOCK_POLL_S = 0.05
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(:[0-9]{2})?(Z|[+-][0-9:]{5})")
_SETUP = (
    "dedupe: daily (the default) needs one directory approved for the ntfy plugin in"
    " horae.lock: plugins.ntfy.permissions.paths.write: [<directory>] (or set dedupe: off)"
)


def rule_fingerprint(settings: Settings, intent: dict[str, Any]) -> str:
    """The SHA-256 hex of the rule: where and how it is pushed, and what it says."""
    rule = {
        "server": settings.server,
        "topic": settings.topic,
        "priority": settings.priority,
        "title": intent.get("title"),
        "body": intent.get("body"),
        "link": intent.get("link"),
    }
    text = json.dumps(rule, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def minute(instant: datetime) -> datetime:
    """`instant` in UTC, on the minute."""
    return instant.astimezone(UTC).replace(second=0, microsecond=0)


def _wall(instant: datetime, zone: ZoneInfo) -> datetime:
    """The local wall-clock time (naive) of `instant` in `zone`, to the minute."""
    return instant.astimezone(zone).replace(tzinfo=None, second=0, microsecond=0)


def is_repeat(previous: datetime, delivery: datetime, zone: ZoneInfo) -> bool:
    """Whether a push delivered at `delivery` repeats one delivered (or scheduled) at
    `previous`: earlier than a day after it on the local wall clock, to the minute. Two
    deliveries on the same local date always are."""
    return _wall(delivery, zone) < _wall(previous, zone) + WINDOW


def state_dir(paths: Paths) -> str:
    """The real path of the single approved `paths.write` directory."""
    if len(paths.write) != 1:
        raise SinkError("state_unavailable", _SETUP)
    real = paths.allowed(paths.write[0], write=True)
    if real is None or not os.path.isdir(real):
        raise SinkError(
            "state_unavailable",
            "the paths.write entry approved for the ntfy plugin in horae.lock is not an"
            " existing directory: create it (mode 0700) or set dedupe: off",
        )
    return real


def _check_directory(directory: str) -> None:
    try:
        info = os.stat(directory)
    except OSError:
        info = None
    if info is None or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise SinkError(
            "state_unavailable",
            "the reminder state directory must belong to the user running the ntfy plugin and"
            " not be writable by group or others (chown it; chmod 0700)",
        )


@dataclass
class Sent:
    """The rules handled (fingerprint -> delivery instant, UTC), as read under the lock."""

    entries: dict[str, datetime] = field(default_factory=dict)
    reset: bool = False
    """The file held corrupt or oversized content and was started over."""
    evicted: bool = False
    """The last `save` dropped an entry still inside the retention (the cap was reached)."""


class SentState:
    """The state file in `directory`; `locked()` reads it under the lock, `store` writes it."""

    def __init__(
        self,
        directory: str,
        *,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        retention: timedelta = RETENTION,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.path = os.path.join(directory, STATE_FILE)
        self.lock_path = os.path.join(directory, LOCK_FILE)
        self.directory = directory
        self.lock_timeout_s = lock_timeout_s
        self.retention = retention
        self.max_entries = max_entries

    @contextlib.asynccontextmanager
    async def locked(self) -> AsyncIterator[Sent]:
        """Holds the exclusive lock for the block and yields the state as read under it."""
        fd = await anyio.to_thread.run_sync(self._open_lock)
        try:
            deadline = time.monotonic() + self.lock_timeout_s
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise SinkError(
                        "state_busy",
                        f"another run held the reminder state for over {self.lock_timeout_s:g} s"
                        " (it is pushing to a slow or unreachable ntfy server); the next run"
                        " tries this push again",
                    )
                await anyio.sleep(LOCK_POLL_S)
            yield await anyio.to_thread.run_sync(self._load)
        finally:
            os.close(fd)  # releases the lock

    async def store(self, sent: Sent, now: datetime) -> None:
        """`save`, in a worker thread."""
        await anyio.to_thread.run_sync(self.save, sent, now)

    def _open_lock(self) -> int:
        _check_directory(self.directory)
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
            return os.open(self.lock_path, flags, 0o600)
        except OSError:
            raise SinkError(
                "state_unavailable",
                "cannot open the reminder state lock ntfy-sent.lock: the approved directory"
                " must be writable by the ntfy plugin",
            ) from None

    def _sweep(self) -> None:
        """Removes the temporary files a crash left behind (older than an hour)."""
        cutoff = time.time() - STALE_TEMPORARY_S
        with contextlib.suppress(OSError):
            for name in os.listdir(self.directory):
                if name.startswith(TEMPORARY_PREFIX) and name.endswith(TEMPORARY_SUFFIX):
                    path = os.path.join(self.directory, name)
                    with contextlib.suppress(OSError):
                        if os.lstat(path).st_mtime < cutoff:
                            os.unlink(path)

    def _read(self) -> bytes | None:
        """The file's bytes; `None` when it does not exist."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            fd = -1
        if fd >= 0:
            try:
                with os.fdopen(fd, "rb") as file:
                    if stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        return file.read(MAX_STATE_BYTES + 1)
            except OSError:
                pass
        raise SinkError(
            "state_unavailable",
            "cannot read the reminder state file ntfy-sent.json (permission denied, a symbolic"
            " link or not a regular file): give it to the plugin's user with mode 0600, or"
            " remove it",
        )

    def _load(self) -> Sent:
        self._sweep()
        raw = self._read()
        if raw is None:
            return Sent()
        try:
            document = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_STATE_BYTES else None
        except (UnicodeDecodeError, ValueError):
            document = None
        if not isinstance(document, dict):
            return Sent(reset=True)
        version = document.get("format")
        if isinstance(version, int) and not isinstance(version, bool) and version != FORMAT:
            raise SinkError(
                "state_unavailable",
                "the reminder state file ntfy-sent.json is in a format this version of the"
                " plugin does not read (written by a newer version?): upgrade the plugin,"
                " or remove the file",
            )
        sent = document.get("sent")
        if isinstance(version, bool) or version != FORMAT or not isinstance(sent, dict):
            return Sent(reset=True)
        entries: dict[str, datetime] = {}
        for fingerprint, stamp in sent.items():
            at = _instant(stamp)
            if _FINGERPRINT.fullmatch(fingerprint) and at is not None:
                entries[fingerprint] = at
        return Sent(entries)

    def save(self, sent: Sent, now: datetime) -> None:
        """Writes `sent` (call it under `locked`), without the entries past the retention and
        past `max_entries`. Raises `OSError` when it cannot be written."""
        cutoff = now - self.retention
        # By delivery; a tie keeps the dict order, where the entry just added is last.
        live = sorted(
            ((key, at) for key, at in sent.entries.items() if at > cutoff), key=lambda kv: kv[1]
        )
        kept = live[-self.max_entries :] if self.max_entries else []
        sent.evicted = len(kept) < len(live)
        sent.entries = dict(kept)
        stamps = {key: minute(at).isoformat() for key, at in sent.entries.items()}
        data = json.dumps({"format": FORMAT, "sent": stamps}, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(
            prefix=TEMPORARY_PREFIX, suffix=TEMPORARY_SUFFIX, dir=self.directory
        )
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        _sync_directory(self.directory)


def _instant(stamp: object) -> datetime | None:
    """A stored delivery instant (aware, UTC), or `None` when it is not one."""
    if not isinstance(stamp, str) or not _STAMP.fullmatch(stamp):
        return None
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return at.astimezone(UTC)


def _try_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:  # EWOULDBLOCK: another open file holds it
        return False
    return True


def _sync_directory(directory: str) -> None:
    """Makes the rename durable (best effort: not every file system supports it)."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
