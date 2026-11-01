# SPDX-License-Identifier: Apache-2.0
"""Reminder state: which pushes went out, so a push is sent **once per rule and local day**.

**Why the plugin keeps it.** The core records each notification's `dedupe_key` as sent and
never sends a recorded key again, but a pipeline without an LLM step (the `remind` pipeline,
every 5 minutes) gets a key scoped to its run (`horae.apply.plan.dedupe_key`: no
`input_hash`, so the run id), so a reminder proposed again by the next run is a new key. And
only the notifier knows the **day a push reaches the phone**: during quiet hours a push is
handed to ntfy now and delivered when they end, often the next day. So the notifier records,
for each push it hands to ntfy, a `(rule, delivery day)` key, and skips a push whose key is
already there (`delivery: duplicate`, still a success, so the core records its own key).

- **Rule**: the SHA-256 of the entry's server, topic and priority and the intent's title,
  body and link (`rule_fingerprint`). Only digests are stored, never the text.
- **Day**: the local date, in the instance time zone (`TZ`), of the delivery: now, or when
  quiet hours end for a delayed push. DST needs nothing special: a date is a date.
- **Where**: `ntfy-sent.json` in the plugin's single `paths.write` directory, as approved in
  `horae.lock` (`state_dir`). No directory approved is `state_unavailable`, never a silent
  fall back to sending every time; an entry that wants no state says `dedupe: off`.
- **Concurrent runs**: an exclusive `flock` on `ntfy-sent.lock` is held from the check to the
  write, around the publish, so a second run waits (up to `LOCK_TIMEOUT_S`, above the
  publish's 20 s deadline, then `state_busy`) and then sees the first run's key.
- **Atomic and crash-safe**: written to a temporary file in the same directory, flushed to
  disk, then renamed over the old one; a crash leaves the old file or the new one.
- **Corruption-tolerant**: a missing file is empty; an unreadable, oversized or malformed one
  starts over (`state: "reset"` in the details: at worst one push is sent again), and bad
  entries are skipped one by one. Files are opened without following symbolic links.
- **Bounded**: entries older than `RETENTION_DAYS` local days are dropped on every write, and
  at most `MAX_ENTRIES` are kept (the newest).
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import anyio
from horae_sdk import Paths, SinkError

from horae_ntfy.settings import Settings

STATE_FILE = "ntfy-sent.json"
LOCK_FILE = "ntfy-sent.lock"
FORMAT = 1
RETENTION_DAYS = 7
MAX_ENTRIES = 4096
MAX_STATE_BYTES = 1024 * 1024
LOCK_TIMEOUT_S = 30.0
"""Above the publish deadline (`horae_ntfy.http.DEADLINE_S`, 20 s) another run holds it for."""
LOCK_POLL_S = 0.05
_KEY = re.compile(r"[0-9a-f]{64}:[0-9]{4}-[0-9]{2}-[0-9]{2}")
_STAMP = re.compile(r"[0-9T:+.Z-]{1,40}")


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


def sent_key(fingerprint: str, day: date) -> str:
    return f"{fingerprint}:{day.isoformat()}"


def state_dir(paths: Paths) -> str:
    """The real path of the single approved `paths.write` directory."""
    if len(paths.write) != 1:
        raise SinkError(
            "state_unavailable",
            "dedupe: daily needs exactly one paths.write directory approved in horae.lock"
            " (or set dedupe: off)",
        )
    real = paths.allowed(paths.write[0], write=True)
    if real is None or not os.path.isdir(real):
        raise SinkError("state_unavailable", "the approved paths.write entry is not a directory")
    return real


def _day(key: str) -> date | None:
    try:
        return date.fromisoformat(key[65:])
    except ValueError:
        return None


@dataclass
class Sent:
    """The keys sent (key -> UTC time it was sent), as read under the lock."""

    entries: dict[str, str] = field(default_factory=dict)
    reset: bool = False
    """The file could not be read and was started over."""


class SentState:
    """The state file in `directory`; `locked()` reads it under the lock, `save` writes it."""

    def __init__(
        self,
        directory: str,
        *,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        retention_days: int = RETENTION_DAYS,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self.path = os.path.join(directory, STATE_FILE)
        self.lock_path = os.path.join(directory, LOCK_FILE)
        self.directory = directory
        self.lock_timeout_s = lock_timeout_s
        self.retention_days = retention_days
        self.max_entries = max_entries

    @contextlib.asynccontextmanager
    async def locked(self) -> AsyncIterator[Sent]:
        """Holds the exclusive lock for the block and yields the state as read under it."""
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open(self.lock_path, flags, 0o600)
        except OSError:
            fd = -1
        if fd < 0:
            raise SinkError("state_unavailable", "cannot open the reminder state lock")
        try:
            deadline = time.monotonic() + self.lock_timeout_s
            while not _try_lock(fd):
                if time.monotonic() >= deadline:
                    raise SinkError("state_busy", "another run holds the reminder state")
                await anyio.sleep(LOCK_POLL_S)
            yield self._load()
        finally:
            os.close(fd)  # releases the lock

    def _read(self) -> bytes | None:
        """The file's bytes; `None` when it does not exist; `b""` when it cannot be read."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            return b""
        try:
            with os.fdopen(fd, "rb") as file:
                return file.read(MAX_STATE_BYTES + 1)
        except OSError:
            return b""

    def _load(self) -> Sent:
        raw = self._read()
        if raw is None:
            return Sent()
        try:
            document = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_STATE_BYTES else None
        except (UnicodeDecodeError, ValueError):
            document = None
        if not (
            isinstance(document, dict)
            and document.get("format") == FORMAT
            and isinstance(document.get("sent"), dict)
        ):
            return Sent(reset=True)
        entries = {
            key: stamp
            for key, stamp in document["sent"].items()
            if isinstance(key, str)
            and _KEY.fullmatch(key)
            and _day(key) is not None
            and isinstance(stamp, str)
            and _STAMP.fullmatch(stamp)
        }
        return Sent(entries)

    def save(self, sent: Sent, today: date) -> None:
        """Writes `sent` (call it under `locked`), without the entries older than the
        retention and past `max_entries`. Raises `OSError` when it cannot be written."""
        cutoff = today - timedelta(days=self.retention_days)
        kept = [(key, at) for key, at in sent.entries.items() if key[65:] > cutoff.isoformat()]
        kept.sort(key=lambda item: (item[0][65:], item[1]))  # oldest day, then oldest send
        sent.entries = dict(kept[-self.max_entries :])
        data = json.dumps({"format": FORMAT, "sent": sent.entries}, sort_keys=True).encode()
        fd, temporary = tempfile.mkstemp(prefix=".ntfy-sent-", suffix=".tmp", dir=self.directory)
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
