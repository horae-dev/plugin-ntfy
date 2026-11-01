# SPDX-License-Identifier: Apache-2.0
"""Reminder state (#3): a rule is pushed once a day, across runs.

Each "run" is a fresh notifier (a new plugin process, as the host starts one per run) with
the core's per-run `dedupe_key` (a pipeline without an LLM step gets a new key every run),
sharing only the approved state directory."""

import fcntl
import functools
import json
import multiprocessing
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError

import horae_ntfy.plugin
from fakes import (
    TOKEN,
    Behaviour,
    FakeNtfy,
    Recorder,
    env,
    lan_notifier,
    push_in_process,
    recorded_notifier,
)
from horae_ntfy.http import DEADLINE_S
from horae_ntfy.message import sequence_id
from horae_ntfy.plugin import NtfyNotifier
from horae_ntfy.settings import parse
from horae_ntfy.state import (
    HOST_CALL_TIMEOUT_S,
    LOCK_FILE,
    LOCK_TIMEOUT_S,
    RETENTION,
    STATE_FILE,
    Sent,
    SentState,
    rule_fingerprint,
)

PARAMS: dict[str, Any] = {"server": "https://ntfy.example.org", "topic": "reminders"}
NIGHT = {"start": "22:00", "end": "07:00"}
NOON = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)  # 12:00 in Paris (CET)
INTENT = {"title": "⏰ Dentist", "body": "at 15:00", "link": None}
_keys = count(1)


def run_key() -> str:
    """The core's key for a notification of a new run (another run id, another key)."""
    return f"{next(_keys):064x}"


def call(paths: Paths, params: dict[str, Any] | None = None, **intent: Any) -> ApplyRequest:
    return ApplyRequest(
        paths,
        {"type": "notify.push", **INTENT, **intent},
        tool="ntfy.push",
        params=PARAMS if params is None else params,
        run_id="run",
        index=0,
        dedupe_key=run_key(),
    )


def push(
    notifier: NtfyNotifier, paths: Paths, params: dict[str, Any] | None = None, **intent: Any
) -> Applied:
    request = call(paths, params, **intent)

    async def go() -> Applied:
        return await notifier.apply(request)

    return anyio.run(go)


@pytest.fixture
def state(tmp_path: Path) -> Paths:
    tmp_path.chmod(0o700)
    return Paths(write=(str(tmp_path),))


def runs(
    recorder: Recorder, paths: Paths, instants: list[datetime], **kwargs: Any
) -> list[Applied]:
    return [push(recorded_notifier(recorder, now=at), paths, **kwargs) for at in instants]


def stored(paths: Paths) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(Path(paths.write[0], STATE_FILE).read_text())
    return document


def fingerprint(params: dict[str, Any] | None = None, **intent: Any) -> str:
    return rule_fingerprint(parse(PARAMS if params is None else params), {**INTENT, **intent})


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --- once a day per rule --------------------------------------------------------------------


def test_a_reminder_is_pushed_once_across_runs_of_a_day(state: Paths) -> None:
    """Twelve runs, every 5 minutes, each with its own core key: one push."""
    recorder = Recorder()
    applied = runs(recorder, state, [NOON + timedelta(minutes=5 * n) for n in range(12)])
    assert len(recorder.requests) == 1
    assert applied[0].details == {"delivery": "sent", "priority": 3, "id": "Zz99"}
    assert all(a.details == {"delivery": "duplicate", "priority": 3} for a in applied[1:])
    assert all(a.target == "https://ntfy.example.org/reminders" for a in applied)
    # The sequence id is the rule's, not the run's.
    assert recorder.json(0)["sequence_id"] == sequence_id(fingerprint())


def test_the_next_day_pushes_again(state: Paths) -> None:
    recorder = Recorder()
    runs(recorder, state, [NOON, NOON + timedelta(hours=3), NOON + timedelta(days=1)])
    assert len(recorder.requests) == 2
    assert recorder.json(0)["sequence_id"] == recorder.json(1)["sequence_id"]


@pytest.mark.parametrize("intent", [{"body": "at 16:00"}, {"title": "⏰ Doctor"}, {"title": None}])
def test_another_reminder_is_another_rule(state: Paths, intent: dict[str, Any]) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON), state)
    push(recorded_notifier(recorder, now=NOON), state, **intent)
    assert len(recorder.requests) == 2
    assert recorder.json(0)["sequence_id"] != recorder.json(1)["sequence_id"]


@pytest.mark.parametrize(
    "params",
    [
        {**PARAMS, "topic": "other"},
        {**PARAMS, "priority": "high"},
        {**PARAMS, "server": "https://push.example.org"},
    ],
)
def test_another_entry_is_another_rule(state: Paths, params: dict[str, Any]) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON), state)
    push(recorded_notifier(recorder, now=NOON), state, params)
    assert len(recorder.requests) == 2


def test_the_rule_is_the_entry_and_the_text() -> None:
    settings = parse(PARAMS)
    intent = {"title": "a", "body": "b", "link": None}
    same = rule_fingerprint(parse({**PARAMS, "tags": ["x"]}), intent)
    assert rule_fingerprint(settings, intent) == same  # tags do not make another reminder
    assert rule_fingerprint(settings, {**intent, "body": "c"}) != same
    lone = rule_fingerprint(settings, {**intent, "body": "\ud800"})  # a lone surrogate hashes
    assert len(lone) == 64


def test_two_rules_with_the_same_text_collapse(state: Paths) -> None:
    """Documented limit: two entries (or pipelines) that push the same text to the same
    topic, server and priority are one rule; only the first is pushed that day."""
    recorder = Recorder()
    first = push(recorded_notifier(recorder, now=NOON), state, {**PARAMS, "tags": ["tooth"]})
    second = push(recorded_notifier(recorder, now=NOON), state, {**PARAMS, "tags": ["bell"]})
    assert (first.details["delivery"], second.details["delivery"]) == ("sent", "duplicate")
    assert len(recorder.requests) == 1


# --- a day, on the local wall clock (midnight and DST included) -----------------------------


@pytest.mark.parametrize(
    ("zone", "first", "second", "pushes"),
    [
        # Midnight: 23:45 and 00:05 in Paris (CET) are 20 minutes apart.
        ("Europe/Paris", "2026-10-29T22:45", "2026-10-29T23:05", 1),
        # 00:30 and 23:00 on the 30th in Paris, two UTC dates: one local day.
        ("Europe/Paris", "2026-10-29T23:30", "2026-10-30T22:00", 1),
        # Noon, then noon the next day: pushed again; 11:55 the next day is not.
        ("Europe/Paris", "2026-10-29T11:00", "2026-10-30T11:00", 2),
        ("Europe/Paris", "2026-10-29T11:00", "2026-10-30T10:55", 1),
        # A run starting a few seconds earlier the next day: the minute counts, no drift.
        ("Europe/Paris", "2026-10-29T11:00:40", "2026-10-30T11:00:10", 2),
        # Fall back (25 Oct 2026): 02:30 CEST then 02:30 CET, the same day.
        ("Europe/Paris", "2026-10-25T00:30", "2026-10-25T01:30", 1),
        # Noon on the 24th (CEST), noon on the 25th (CET): 25 hours, but a day on the clock.
        ("Europe/Paris", "2026-10-24T10:00", "2026-10-25T11:00", 2),
        ("Europe/Paris", "2026-10-24T10:00", "2026-10-25T10:55", 1),
        # Spring forward (29 Mar 2026): noon on the 28th (CET) and the 29th (CEST), 23 hours.
        ("Europe/Paris", "2026-03-28T11:00", "2026-03-29T10:00", 2),
        ("Europe/Paris", "2026-03-28T11:00", "2026-03-29T09:55", 1),
        # Spring forward: 01:59 CET then 03:00 CEST, the same day.
        ("Europe/Paris", "2026-03-29T00:59", "2026-03-29T01:00", 1),
        # New York's fall back (1 Nov 2026): 01:30 EDT then 01:30 EST, the same day.
        ("America/New_York", "2026-11-01T05:30", "2026-11-01T06:30", 1),
        # A half-hour zone (UTC+5:30): 23:45 and 00:15 local; 23:45 the next day.
        ("Asia/Kolkata", "2026-10-29T18:15", "2026-10-29T18:45", 1),
        ("Asia/Kolkata", "2026-10-29T18:15", "2026-10-30T18:15", 2),
    ],
)
def test_a_day_is_on_the_instance_wall_clock(
    state: Paths, zone: str, first: str, second: str, pushes: int
) -> None:
    recorder = Recorder()
    for at in (utc(first), utc(second)):
        push(recorded_notifier(recorder, now=at, environ=env(TZ=zone)), state)
    assert len(recorder.requests) == pushes


def test_the_day_needs_the_instance_zone(state: Paths) -> None:
    recorder = Recorder()
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(recorder, environ={"NTFY_TOKEN": TOKEN}), state)
    assert raised.value.kind == "config_invalid"
    assert "dedupe: daily needs TZ" in raised.value.message
    assert recorder.requests == []


# --- quiet hours ----------------------------------------------------------------------------

LATE = utc("2026-10-29T22:30")  # 23:30 in Paris: quiet until 07:00 on the 30th (06:00 UTC)


def test_a_delayed_reminder_counts_from_its_delivery(state: Paths) -> None:
    """Handed to ntfy at 23:30 for 07:00: the runs of the night and the morning after are
    duplicates (the phone gets it at 07:00 on the 30th); 07:05 the next day pushes again."""
    recorder = Recorder()
    quiet = {**PARAMS, "quiet_hours": NIGHT}
    applied = runs(
        recorder,
        state,
        [LATE, LATE + timedelta(minutes=5), utc("2026-10-30T06:05"), utc("2026-10-31T06:05")],
        params=quiet,
    )
    assert [a.details["delivery"] for a in applied] == ["delayed", "duplicate", "duplicate", "sent"]
    assert len(recorder.requests) == 2
    assert recorder.json(0)["delay"] == "27000s"
    assert "delay" not in recorder.json(1)


def test_a_reminder_sent_at_noon_is_not_delayed_to_the_next_morning(state: Paths) -> None:
    """Pushed at noon, proposed again at 23:30: delivering it at 07:00 would be a stale
    second push less than a day after the first."""
    recorder = Recorder()
    applied = runs(recorder, state, [NOON, LATE], params={**PARAMS, "quiet_hours": NIGHT})
    assert [a.details["delivery"] for a in applied] == ["sent", "duplicate"]
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("during", ["delay", "drop"])
def test_a_critical_reminder_is_sent_at_once_and_once(state: Paths, during: str) -> None:
    recorder = Recorder()
    params = {**PARAMS, "priority": "critical", "quiet_hours": {**NIGHT, "during": during}}
    applied = runs(recorder, state, [LATE, LATE + timedelta(minutes=5)], params=params)
    assert [a.details["delivery"] for a in applied] == ["sent", "duplicate"]
    assert len(recorder.requests) == 1
    assert "delay" not in recorder.json(0)


def test_a_dropped_reminder_is_recorded_like_a_sent_one(state: Paths) -> None:
    """Dropped during quiet hours: recorded, so it is not pushed right after them; it is
    pushed again a day after the drop."""
    recorder = Recorder()
    params = {**PARAMS, "quiet_hours": {**NIGHT, "during": "drop"}}
    instants = [
        LATE,
        LATE + timedelta(minutes=5),
        utc("2026-10-30T06:05"),  # 07:05 the morning after
        utc("2026-10-31T06:05"),  # 07:05 the day after
    ]
    applied = runs(recorder, state, instants, params=params)
    assert [a.details["delivery"] for a in applied] == ["dropped", "duplicate", "duplicate", "sent"]
    assert len(recorder.requests) == 1
    assert stored(state)["sent"] == {fingerprint(params): "2026-10-31T06:05:00+00:00"}


# --- concurrent runs and interrupted runs ---------------------------------------------------


def test_concurrent_runs_push_once(state: Paths, ntfy: FakeNtfy) -> None:
    """Five runs at once against a slow server: the lock is held around the publish, so the
    others wait and then find the first one's record."""
    ntfy.behaviour = Behaviour(delay_s=0.3)
    params = {"server": ntfy.url, "topic": "reminders"}
    results: list[Applied] = []

    async def one() -> None:
        results.append(await lan_notifier(now=NOON).apply(call(state, params)))

    async def all_at_once() -> None:
        async with anyio.create_task_group() as group:
            for _ in range(5):
                group.start_soon(one)

    anyio.run(all_at_once)
    assert len(ntfy.seen) == 1
    assert sorted(a.details["delivery"] for a in results) == ["duplicate"] * 4 + ["sent"]


def test_two_processes_push_once(state: Paths, ntfy: FakeNtfy) -> None:
    """Two plugin processes released together by a barrier: `flock` is per open file, across
    processes, so one pushes and the other finds its record."""
    ntfy.behaviour = Behaviour(delay_s=0.3)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    workers = [
        context.Process(target=push_in_process, args=(barrier, state.write[0], ntfy.url, results))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    deliveries = sorted(results.get(timeout=60) for _ in workers)
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0
    assert deliveries == ["duplicate", "sent"]
    assert len(ntfy.seen) == 1


def test_a_run_cancelled_mid_publish_is_pushed_again_with_the_same_sequence_id(
    state: Paths, ntfy: FakeNtfy
) -> None:
    """At least once: the host cancels the call (or kills the plugin) after ntfy got the push
    but before the state was written. The next run, with a new core key, pushes the rule
    again with the same sequence id, so ntfy replaces the first copy instead of adding one."""
    ntfy.behaviour = Behaviour(delay_s=1.0)
    params = {"server": ntfy.url, "topic": "reminders"}

    async def cancelled() -> None:
        with anyio.move_on_after(0.3) as scope:
            await lan_notifier(now=NOON).apply(call(state, params))
        assert scope.cancelled_caught

    anyio.run(cancelled)
    assert len(ntfy.seen) == 1  # ntfy received it
    assert not Path(state.write[0], STATE_FILE).exists()  # nothing recorded
    ntfy.behaviour = Behaviour()
    applied = push(lan_notifier(now=NOON + timedelta(minutes=5)), state, params)
    assert applied.details["delivery"] == "sent"
    first, again = ntfy.seen[0].json(), ntfy.seen[1].json()
    assert first["sequence_id"] == again["sequence_id"] == sequence_id(fingerprint(params))
    assert push(lan_notifier(now=NOON), state, params).details["delivery"] == "duplicate"


def test_a_run_holding_the_state_too_long_is_state_busy(state: Paths) -> None:
    recorder = Recorder()
    fd = os.open(os.path.join(state.write[0], LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        notifier = NtfyNotifier(recorded_notifier(recorder).publisher, now=lambda: NOON, env=env())
        notifier.state_lock_timeout_s = 0.15
        with pytest.raises(SinkError) as raised:
            push(notifier, state)
    finally:
        os.close(fd)
    assert raised.value.kind == "state_busy"
    assert "another run held the reminder state for over 0.15 s" in raised.value.message
    assert recorder.requests == []
    push(notifier, state)  # released: it goes out
    assert len(recorder.requests) == 1


def test_the_lock_wait_and_a_publish_fit_in_the_host_call_timeout() -> None:
    assert LOCK_TIMEOUT_S == 8.0
    assert LOCK_TIMEOUT_S + DEADLINE_S < HOST_CALL_TIMEOUT_S


def test_the_worst_case_wait_then_publish_fits_in_the_host_call_timeout(
    state: Paths, ntfy: FakeNtfy
) -> None:
    """The real ratio, scaled down 10x: another run holds the lock for almost the whole
    wait, then this run's publish hits its own deadline; it still fails before the host
    would have cancelled the call."""
    scale = 0.1
    ntfy.behaviour = Behaviour(delay_s=DEADLINE_S * scale + 1.0)  # slower than the deadline
    fd = os.open(os.path.join(state.write[0], LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(LOCK_TIMEOUT_S * scale * 0.9, os.close, (fd,)).start()
    notifier = lan_notifier(now=NOON, deadline_s=DEADLINE_S * scale)
    notifier.state_lock_timeout_s = LOCK_TIMEOUT_S * scale
    started = time.monotonic()
    with pytest.raises(SinkError) as raised:
        push(notifier, state, {"server": ntfy.url, "topic": "reminders"})
    elapsed = time.monotonic() - started
    assert raised.value.kind == "server_timeout"
    assert LOCK_TIMEOUT_S * scale * 0.9 + DEADLINE_S * scale <= elapsed
    assert elapsed < HOST_CALL_TIMEOUT_S * scale


# --- the state file -------------------------------------------------------------------------


def test_a_missing_file_is_created_private_and_holds_no_text(state: Paths) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON + timedelta(seconds=42)), state)
    path = Path(state.write[0], STATE_FILE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    for plain in ("Dentist", "15:00", "reminders"):
        assert plain not in text
    assert stored(state) == {"format": 1, "sent": {fingerprint(): "2026-10-29T11:00:00+00:00"}}
    assert sorted(os.listdir(state.write[0])) == [STATE_FILE, LOCK_FILE]  # no temporary left


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'{"sent": {}}',
        b'{"format": "1", "sent": {}}',
        b'{"format": true, "sent": {}}',
        b'{"format": 1, "sent": []}',
        b"{" + b" " * (1024 * 1024) + b"}",
    ],
)
def test_corrupt_content_starts_over(state: Paths, content: bytes) -> None:
    Path(state.write[0], STATE_FILE).write_bytes(content)
    recorder = Recorder()
    applied = runs(recorder, state, [NOON, NOON + timedelta(minutes=5)])
    assert applied[0].details["state"] == "reset"
    assert applied[0].details["delivery"] == "sent"
    assert applied[1].details == {"delivery": "duplicate", "priority": 3}
    assert len(recorder.requests) == 1
    assert stored(state)["format"] == 1


@pytest.mark.parametrize("version", [0, 2, 99])
def test_an_unknown_format_is_refused_not_reset(state: Paths, version: int) -> None:
    """A file in another format (a newer plugin wrote it) is never thrown away."""
    path = Path(state.write[0], STATE_FILE)
    content = json.dumps({"format": version, "sent": {"x": "y"}})
    path.write_text(content)
    recorder = Recorder()
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(recorder, now=NOON), state)
    assert raised.value.kind == "state_unavailable"
    assert "upgrade the plugin" in raised.value.message
    assert path.read_text() == content
    assert recorder.requests == []


def test_bad_entries_are_skipped_one_by_one(state: Paths) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON), state)
    document = stored(state)
    document["sent"].update(
        {
            "nothex": "2026-10-29T11:00:00+00:00",
            "a" * 64: "2026-13-40T11:00:00+00:00",
            "b" * 64: 5,
            "c" * 64: "<script>",
            "d" * 64: "2026-10-29T11:00:00",  # no offset
        }
    )
    Path(state.write[0], STATE_FILE).write_text(json.dumps(document))
    applied = push(recorded_notifier(recorder, now=NOON), state)
    assert applied.details == {"delivery": "duplicate", "priority": 3}  # no reset
    push(recorded_notifier(recorder, now=NOON), state, body="another")
    assert len(stored(state)["sent"]) == 2  # the bad ones are gone


def test_a_symlinked_state_file_is_refused(state: Paths, tmp_path_factory: Any) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "target.json"
    elsewhere.write_text('{"format": 1, "sent": {}}')
    link = Path(state.write[0], STATE_FILE)
    link.symlink_to(elsewhere)
    recorder = Recorder()
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(recorder, now=NOON), state)
    assert raised.value.kind == "state_unavailable"
    assert elsewhere.read_text() == '{"format": 1, "sent": {}}'  # untouched
    assert link.is_symlink()
    assert recorder.requests == []


def test_a_state_file_that_is_not_a_regular_file_is_refused(state: Paths) -> None:
    os.mkfifo(Path(state.write[0], STATE_FILE), 0o600)
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(Recorder(), now=NOON), state)
    assert raised.value.kind == "state_unavailable"


def test_an_unreadable_state_file_is_refused_not_reset(state: Paths) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    path = Path(state.write[0], STATE_FILE)
    path.write_text('{"format": 1, "sent": {}}')
    path.chmod(0)
    recorder = Recorder()
    try:
        with pytest.raises(SinkError) as raised:
            push(recorded_notifier(recorder, now=NOON), state)
    finally:
        path.chmod(0o600)
    assert raised.value.kind == "state_unavailable"
    assert "permission denied" in raised.value.message
    assert recorder.requests == []


def test_a_state_that_cannot_be_saved_still_reports_the_push(
    state: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(*args: Any, **kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full)
    recorder = Recorder()
    applied = push(recorded_notifier(recorder, now=NOON), state)
    assert applied.details["delivery"] == "sent"
    assert applied.details["state"] == "not_saved"
    assert sorted(os.listdir(state.write[0])) == [LOCK_FILE]  # the temporary is removed


def test_stale_temporary_files_are_swept(state: Paths) -> None:
    directory = Path(state.write[0])
    stale, fresh, other = (
        directory / ".ntfy-sent-crash.tmp",
        directory / ".ntfy-sent-busy.tmp",
        directory / "unrelated.tmp",
    )
    for path in (stale, fresh, other):
        path.write_text("")
    two_hours_ago = time.time() - 7200
    os.utime(stale, (two_hours_ago, two_hours_ago))
    os.utime(other, (two_hours_ago, two_hours_ago))
    push(recorded_notifier(Recorder(), now=NOON), state)
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_old_entries_expire(state: Paths) -> None:
    def stamp(at: datetime) -> str:
        return at.isoformat()

    old = {
        f"{n:064x}": stamp(NOON - age)
        for n, age in enumerate(
            [RETENTION + timedelta(days=3), RETENTION, RETENTION - timedelta(minutes=1)]
        )
    }
    Path(state.write[0], STATE_FILE).write_text(json.dumps({"format": 1, "sent": old}))
    applied = push(recorded_notifier(Recorder(), now=NOON), state)
    assert "state" not in applied.details  # expiry is not eviction
    assert sorted(stored(state)["sent"].values()) == [
        "2026-10-22T11:01:00+00:00",  # 7 days less a minute ago: kept
        "2026-10-29T11:00:00+00:00",
    ]


def test_the_state_is_bounded(tmp_path: Path) -> None:
    state = SentState(str(tmp_path), max_entries=3)
    sent = Sent({f"{n:064x}": NOON - timedelta(hours=n) for n in range(6)})
    state.save(sent, NOON)
    assert sent.evicted
    kept = json.loads((tmp_path / STATE_FILE).read_text())["sent"]
    assert sorted(kept) == [f"{n:064x}" for n in range(3)]  # the latest deliveries


def test_evicting_a_live_entry_is_reported(state: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(horae_ntfy.plugin, "SentState", functools.partial(SentState, max_entries=1))
    recorder = Recorder()
    first = push(recorded_notifier(recorder, now=NOON), state)
    second = push(recorded_notifier(recorder, now=NOON), state, body="another")
    assert "state" not in first.details
    assert second.details["state"] == "evicted"
    assert push(recorded_notifier(recorder, now=NOON), state).details["delivery"] == "sent"


# --- where the state lives ------------------------------------------------------------------


@pytest.mark.parametrize("write", [(), ("/a", "/b")])
def test_dedupe_needs_one_approved_directory(write: tuple[str, ...]) -> None:
    recorder = Recorder()
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(recorder, now=NOON), Paths(write=write))
    assert raised.value.kind == "state_unavailable"
    assert "plugins.ntfy.permissions.paths.write" in raised.value.message
    assert "horae.lock" in raised.value.message
    assert "dedupe: off" in raised.value.message
    assert recorder.requests == []


def test_the_approved_entry_must_be_a_directory(tmp_path: Path) -> None:
    afile = tmp_path / "file"
    afile.write_text("")
    for entry in (str(afile), str(tmp_path / "missing")):
        with pytest.raises(SinkError) as raised:
            push(recorded_notifier(Recorder(), now=NOON), Paths(write=(entry,)))
        assert raised.value.kind == "state_unavailable"


def test_a_read_only_directory_is_state_unavailable(state: Paths) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    directory = Path(state.write[0])
    directory.chmod(0o500)
    recorder = Recorder()
    try:
        with pytest.raises(SinkError) as raised:
            push(recorded_notifier(recorder, now=NOON), state)
    finally:
        directory.chmod(0o700)
    assert raised.value.kind == "state_unavailable"
    assert recorder.requests == []


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_shared_writable_directory_is_state_unavailable(state: Paths, mode: int) -> None:
    directory = Path(state.write[0])
    directory.chmod(mode)
    recorder = Recorder()
    try:
        with pytest.raises(SinkError) as raised:
            push(recorded_notifier(recorder, now=NOON), state)
    finally:
        directory.chmod(0o700)
    assert raised.value.kind == "state_unavailable"
    assert "not be writable by group or others" in raised.value.message
    assert recorder.requests == []


def test_a_directory_of_another_user_is_state_unavailable(
    state: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(state.write[0]).st_uid + 1)
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(Recorder(), now=NOON), state)
    assert raised.value.kind == "state_unavailable"


def test_dedupe_off_keeps_no_state_and_pushes_every_time() -> None:
    recorder = Recorder()
    runs(recorder, Paths(), [NOON, NOON], params={**PARAMS, "dedupe": "off"})
    assert len(recorder.requests) == 2


def test_a_failed_push_is_not_recorded(state: Paths) -> None:
    """The publish fails: nothing recorded (nor by the core), so the next run sends it."""
    failing = Recorder(lambda request: httpx2.Response(503))
    with pytest.raises(SinkError):
        push(recorded_notifier(failing, now=NOON), state)
    recorder = Recorder()
    assert push(recorded_notifier(recorder, now=NOON), state).details["delivery"] == "sent"
