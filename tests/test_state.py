# SPDX-License-Identifier: Apache-2.0
"""Reminder state (#3): a push is sent once per rule and local day, across runs.

Each "run" is a fresh notifier (a new plugin process, as the host starts one per run) with
the core's per-run `dedupe_key` (a pipeline without an LLM step gets a new key every run),
sharing only the approved state directory."""

import fcntl
import json
import os
import stat
from datetime import UTC, date, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError

from fakes import TOKEN, Behaviour, FakeNtfy, Recorder, env, lan_notifier, recorded_notifier
from horae_ntfy.message import sequence_id
from horae_ntfy.plugin import NtfyNotifier
from horae_ntfy.settings import parse
from horae_ntfy.state import (
    LOCK_FILE,
    RETENTION_DAYS,
    STATE_FILE,
    Sent,
    SentState,
    rule_fingerprint,
    sent_key,
)

PARAMS: dict[str, Any] = {"server": "https://ntfy.example.org", "topic": "reminders"}
NIGHT = {"start": "22:00", "end": "07:00"}
NOON = datetime(2026, 10, 29, 11, 0, tzinfo=UTC)  # 12:00 in Paris (CET)
_keys = count(1)


def run_key() -> str:
    """The core's key for a notification of a new run (another run id, another key)."""
    return f"{next(_keys):064x}"


def push(
    notifier: NtfyNotifier,
    paths: Paths,
    params: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    **intent: Any,
) -> Applied:
    call = ApplyRequest(
        paths,
        {"type": "notify.push", "title": "⏰ Dentist", "body": "at 15:00", "link": None, **intent},
        tool="ntfy.push",
        params=PARAMS if params is None else params,
        run_id="run",
        index=0,
        dedupe_key=key or run_key(),
    )

    async def go() -> Applied:
        return await notifier.apply(call)

    return anyio.run(go)


@pytest.fixture
def state(tmp_path: Path) -> Paths:
    return Paths(write=(str(tmp_path),))


def runs(
    recorder: Recorder, paths: Paths, instants: list[datetime], **kwargs: Any
) -> list[Applied]:
    return [push(recorded_notifier(recorder, now=at), paths, **kwargs) for at in instants]


def stored(paths: Paths) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(Path(paths.write[0], STATE_FILE).read_text())
    return document


# --- once per rule and day ------------------------------------------------------------------


def test_a_reminder_is_pushed_once_across_runs_of_a_day(state: Paths) -> None:
    """Twelve runs, every 5 minutes, each with its own core key: one push."""
    recorder = Recorder()
    instants = [NOON + timedelta(minutes=5 * n) for n in range(12)]
    first_key = run_key()
    applied = [push(recorded_notifier(recorder, now=instants[0]), state, key=first_key)]
    applied += runs(recorder, state, instants[1:])
    assert len(recorder.requests) == 1
    assert applied[0].details == {"delivery": "sent", "priority": 3, "id": "Zz99"}
    assert all(a.details == {"delivery": "duplicate", "priority": 3} for a in applied[1:])
    assert all(a.target == "https://ntfy.example.org/reminders" for a in applied)
    # The push still carries the sequence id of its own notification.
    assert recorder.json(0)["sequence_id"] == sequence_id(first_key)


def test_the_next_local_day_pushes_again(state: Paths) -> None:
    recorder = Recorder()
    runs(recorder, state, [NOON, NOON + timedelta(hours=3), NOON + timedelta(days=1)])
    assert len(recorder.requests) == 2


@pytest.mark.parametrize("intent", [{"body": "at 16:00"}, {"title": "⏰ Doctor"}, {"title": None}])
def test_another_reminder_is_another_rule(state: Paths, intent: dict[str, Any]) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON), state)
    push(recorded_notifier(recorder, now=NOON), state, **intent)
    assert len(recorder.requests) == 2


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


# --- the local day, DST included ------------------------------------------------------------


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("zone", "first", "second", "pushes"),
    [
        # Paris (CET, UTC+1): 23:30 on the 29th and 00:30 on the 30th, one UTC date.
        ("Europe/Paris", "2026-10-29T22:30", "2026-10-29T23:30", 2),
        # 00:30 and 23:00 on the 30th in Paris, two UTC dates: one local day.
        ("Europe/Paris", "2026-10-29T23:30", "2026-10-30T22:00", 1),
        # Fall back (25 Oct 2026): 02:30 CEST then 02:30 CET, the same day.
        ("Europe/Paris", "2026-10-25T00:30", "2026-10-25T01:30", 1),
        # The midnight before the fall back: 23:55 CEST on the 24th, 00:05 on the 25th.
        ("Europe/Paris", "2026-10-24T21:55", "2026-10-24T22:05", 2),
        # Spring forward (29 Mar 2026): 01:59 CET then 03:00 CEST, the same day.
        ("Europe/Paris", "2026-03-29T00:59", "2026-03-29T01:00", 1),
        # The midnight before it: 23:58 CET on the 28th, 00:02 on the 29th.
        ("Europe/Paris", "2026-03-28T22:58", "2026-03-28T23:02", 2),
        # New York's fall back (1 Nov 2026): 01:30 EDT then 01:30 EST, the same day.
        ("America/New_York", "2026-11-01T05:30", "2026-11-01T06:30", 1),
        # A half-hour zone (UTC+5:30): 23:45 and 00:15 local.
        ("Asia/Kolkata", "2026-10-29T18:15", "2026-10-29T18:45", 2),
    ],
)
def test_the_day_is_the_instance_local_day(
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
    assert recorder.requests == []


# --- quiet hours ----------------------------------------------------------------------------

LATE = utc("2026-10-29T22:30")  # 23:30 in Paris: quiet until 07:00 on the 30th (06:00 UTC)


def test_a_delayed_reminder_counts_for_its_delivery_day(state: Paths) -> None:
    """Handed to ntfy at 23:30 for 07:00: the runs of the night and the morning after are
    duplicates (the phone gets it at 07:00 on the 30th); the next day pushes again."""
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


def test_a_reminder_sent_today_and_delayed_to_tomorrow_is_two_days(state: Paths) -> None:
    recorder = Recorder()
    runs(recorder, state, [NOON, LATE], params={**PARAMS, "quiet_hours": NIGHT})
    assert len(recorder.requests) == 2  # one on the 29th, one at 07:00 on the 30th


@pytest.mark.parametrize("during", ["delay", "drop"])
def test_a_critical_reminder_is_sent_at_once_and_once(state: Paths, during: str) -> None:
    recorder = Recorder()
    params = {**PARAMS, "priority": "critical", "quiet_hours": {**NIGHT, "during": during}}
    applied = runs(recorder, state, [LATE, LATE + timedelta(minutes=5)], params=params)
    assert [a.details["delivery"] for a in applied] == ["sent", "duplicate"]
    assert len(recorder.requests) == 1
    assert "delay" not in recorder.json(0)


def test_a_dropped_reminder_is_not_recorded(state: Paths) -> None:
    """Dropped during quiet hours (nothing reaches the phone, nothing is recorded): the first
    run after them sends it, once."""
    recorder = Recorder()
    params = {**PARAMS, "quiet_hours": {**NIGHT, "during": "drop"}}
    late = [LATE, LATE + timedelta(minutes=5)]
    assert [a.details["delivery"] for a in runs(recorder, state, late, params=params)] == [
        "dropped",
        "dropped",
    ]
    assert not Path(state.write[0], STATE_FILE).exists()
    morning = [utc("2026-10-30T06:05"), utc("2026-10-30T06:10")]
    applied = runs(recorder, state, morning, params=params)
    assert [a.details["delivery"] for a in applied] == ["sent", "duplicate"]
    assert len(recorder.requests) == 1


# --- concurrent runs ------------------------------------------------------------------------


def test_concurrent_runs_push_once(state: Paths, ntfy: FakeNtfy) -> None:
    """Five runs at once against a slow server: the lock is held around the publish, so the
    others wait and then find the first one's key."""
    ntfy.behaviour = Behaviour(delay_s=0.3)
    params = {"server": ntfy.url, "topic": "reminders"}
    results: list[Applied] = []

    async def one() -> None:
        call = ApplyRequest(
            state,
            {"type": "notify.push", "body": "at 15:00", "link": None},
            tool="ntfy.push",
            params=params,
            dedupe_key=run_key(),
        )
        results.append(await lan_notifier(now=NOON).apply(call))

    async def all_at_once() -> None:
        async with anyio.create_task_group() as group:
            for _ in range(5):
                group.start_soon(one)

    anyio.run(all_at_once)
    assert len(ntfy.seen) == 1
    assert sorted(a.details["delivery"] for a in results) == ["duplicate"] * 4 + ["sent"]


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
    assert recorder.requests == []
    push(notifier, state)  # released: it goes out
    assert len(recorder.requests) == 1


# --- the state file -------------------------------------------------------------------------


def test_a_missing_file_is_created_private_and_holds_no_text(state: Paths) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON), state)
    path = Path(state.write[0], STATE_FILE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    for plain in ("Dentist", "15:00", "reminders"):
        assert plain not in text
    fingerprint = rule_fingerprint(parse(PARAMS), {"title": "⏰ Dentist", "body": "at 15:00"})
    assert stored(state) == {
        "format": 1,
        "sent": {sent_key(fingerprint, date(2026, 10, 29)): "2026-10-29T11:00:00+00:00"},
    }
    assert sorted(os.listdir(state.write[0])) == [STATE_FILE, LOCK_FILE]  # no temporary left


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'{"format": 2, "sent": {}}',
        b'{"format": 1, "sent": []}',
        b"{" + b" " * (1024 * 1024) + b"}",
    ],
)
def test_a_corrupt_file_starts_over(state: Paths, content: bytes) -> None:
    Path(state.write[0], STATE_FILE).write_bytes(content)
    recorder = Recorder()
    applied = runs(recorder, state, [NOON, NOON + timedelta(minutes=5)])
    assert applied[0].details["state"] == "reset"
    assert applied[0].details["delivery"] == "sent"
    assert applied[1].details == {"delivery": "duplicate", "priority": 3}
    assert len(recorder.requests) == 1
    assert stored(state)["format"] == 1


def test_bad_entries_are_skipped_one_by_one(state: Paths) -> None:
    recorder = Recorder()
    push(recorded_notifier(recorder, now=NOON), state)
    document = stored(state)
    document["sent"].update(
        {
            "nothex:2026-10-29": "2026-10-29T11:00:00+00:00",
            "a" * 64 + ":2026-13-40": "2026-10-29T11:00:00+00:00",
            "b" * 64 + ":2026-10-29": 5,
            "c" * 64 + ":2026-10-29": "<script>",
        }
    )
    Path(state.write[0], STATE_FILE).write_text(json.dumps(document))
    applied = push(recorded_notifier(recorder, now=NOON), state)
    assert applied.details == {"delivery": "duplicate", "priority": 3}  # no reset
    push(recorded_notifier(recorder, now=NOON), state, body="another")
    assert len(stored(state)["sent"]) == 2  # the bad ones are gone


def test_a_symlinked_state_file_is_not_followed(state: Paths, tmp_path_factory: Any) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "target.json"
    elsewhere.write_text('{"format": 1, "sent": {}}')
    link = Path(state.write[0], STATE_FILE)
    link.symlink_to(elsewhere)
    recorder = Recorder()
    applied = push(recorded_notifier(recorder, now=NOON), state)
    assert applied.details["state"] == "reset"
    assert elsewhere.read_text() == '{"format": 1, "sent": {}}'  # untouched
    assert not link.is_symlink()  # replaced by the plugin's own file


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


def test_an_unreadable_state_file_starts_over(state: Paths) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    path = Path(state.write[0], STATE_FILE)
    path.write_text('{"format": 1, "sent": {}}')
    path.chmod(0)
    applied = push(recorded_notifier(Recorder(), now=NOON), state)
    assert applied.details["state"] == "reset"


def test_old_entries_expire(state: Paths) -> None:
    today = date(2026, 10, 29)
    directory = state.write[0]
    old = {
        sent_key(f"{n:064x}", today - timedelta(days=age)): "2026-10-01T00:00:00+00:00"
        for n, age in enumerate([RETENTION_DAYS + 3, RETENTION_DAYS, RETENTION_DAYS - 1, 0])
    }
    Path(directory, STATE_FILE).write_text(json.dumps({"format": 1, "sent": old}))
    push(recorded_notifier(Recorder(), now=NOON), state)
    days = sorted(key[65:] for key in stored(state)["sent"])
    assert days == ["2026-10-23", "2026-10-29", "2026-10-29"]  # 6 days old is kept, 7 is not


def test_the_state_is_bounded(tmp_path: Path) -> None:
    state = SentState(str(tmp_path), max_entries=3)
    sent = Sent(
        {
            sent_key(f"{n:064x}", date(2026, 10, 20 + n)): f"2026-10-{20 + n}T00:00:00+00:00"
            for n in range(6)
        }
    )
    state.save(sent, date(2026, 10, 25))
    kept = json.loads((tmp_path / STATE_FILE).read_text())["sent"]
    assert sorted(key[65:] for key in kept) == ["2026-10-23", "2026-10-24", "2026-10-25"]


# --- where the state lives ------------------------------------------------------------------


@pytest.mark.parametrize("write", [(), ("/a", "/b")])
def test_dedupe_needs_one_approved_directory(write: tuple[str, ...]) -> None:
    recorder = Recorder()
    with pytest.raises(SinkError) as raised:
        push(recorded_notifier(recorder, now=NOON), Paths(write=write))
    assert raised.value.kind == "state_unavailable"
    assert "dedupe: off" in raised.value.message
    assert recorder.requests == []


def test_the_approved_entry_must_be_a_directory(tmp_path: Path) -> None:
    afile = tmp_path / "file"
    afile.write_text("")
    for entry in (str(afile), str(tmp_path / "missing")):
        with pytest.raises(SinkError) as raised:
            push(recorded_notifier(Recorder(), now=NOON), Paths(write=(entry,)))
        assert raised.value.kind == "state_unavailable"


def test_a_read_only_directory_is_state_unavailable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    tmp_path.chmod(0o500)
    recorder = Recorder()
    try:
        with pytest.raises(SinkError) as raised:
            push(recorded_notifier(recorder, now=NOON), Paths(write=(str(tmp_path),)))
    finally:
        tmp_path.chmod(0o700)
    assert raised.value.kind == "state_unavailable"
    assert recorder.requests == []


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
