# SPDX-License-Identifier: Apache-2.0
"""`ntfy.push` end to end against a fake ntfy server: delivery, headers, auth, priorities,
quiet hours (with a fake clock) and the critical bypass."""

import math
from datetime import UTC, datetime
from typing import Any

import anyio
import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError

from fakes import (
    FIXED_NOW,
    HOST,
    TOKEN,
    FakeNtfy,
    Recorder,
    env,
    lan_notifier,
    recorded_notifier,
)
from horae_ntfy.plugin import NtfyNotifier

NIGHT = {"start": "22:00", "end": "07:00"}
# 2026-10-29 23:30 in Paris (CET, UTC+1) is 22:30 UTC; the night ends at 06:00 UTC.
LATE = datetime(2026, 10, 29, 22, 30, tzinfo=UTC)
MORNING = datetime(2026, 10, 30, 6, 0, tzinfo=UTC)


def request(params: dict[str, Any], **intent: Any) -> ApplyRequest:
    return ApplyRequest(
        Paths(),
        {"type": "notify.push", "title": "Today", "body": "3 tasks due", "link": None, **intent},
        tool="ntfy.push",
        params=params,
        run_id="run-1",
        index=0,
        dedupe_key="k" * 64,
    )


def apply(notifier: NtfyNotifier, params: dict[str, Any], **intent: Any) -> Applied:
    async def run() -> Applied:
        return await notifier.apply(request(params, **intent))

    return anyio.run(run)


def test_delivers_over_http_to_the_lan_server(ntfy: FakeNtfy) -> None:
    applied = apply(lan_notifier(), {"server": ntfy.url, "topic": "reminders"})
    assert applied.target == f"{ntfy.url}/reminders"
    assert applied.after_sha256 is None
    assert applied.details == {"delivery": "sent", "priority": 3, "id": "Ab12Cd34"}
    [seen] = ntfy.seen
    assert (seen.method, seen.path) == ("POST", "/")
    assert seen.json() == {
        "topic": "reminders",
        "message": "3 tasks due",
        "title": "Today",
        "priority": 3,
    }
    assert seen.headers["host"] == f"{HOST}:{ntfy.port}"
    assert seen.headers["authorization"] == f"Bearer {TOKEN}"
    assert seen.headers["content-type"] == "application/json"
    assert seen.headers["accept-encoding"] == "identity"
    assert seen.headers["user-agent"] == "horae-plugin-ntfy"
    assert not {"x-markdown", "markdown", "x-actions", "x-click"} & set(seen.headers)


def test_an_https_server_gets_the_same_request() -> None:
    recorder = Recorder()
    applied = apply(
        recorded_notifier(recorder),
        {"server": "https://ntfy.example.org", "topic": "t", "tags": ["warning"]},
    )
    [sent] = recorder.requests
    assert str(sent.url) == "https://ntfy.example.org/"
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert recorder.json()["tags"] == ["warning"]
    assert applied.details["id"] == "Zz99"


def test_the_default_server_is_ntfy_sh() -> None:
    recorder = Recorder()
    apply(recorded_notifier(recorder), {"topic": "t"})
    assert str(recorder.requests[0].url) == "https://ntfy.sh/"


@pytest.mark.parametrize(
    ("priority", "expected"), [("min", 1), ("low", 2), ("normal", 3), ("high", 4), ("critical", 5)]
)
def test_priority_mapping(ntfy: FakeNtfy, priority: str, expected: int) -> None:
    applied = apply(lan_notifier(), {"server": ntfy.url, "topic": "t", "priority": priority})
    assert ntfy.seen[0].json()["priority"] == expected
    assert applied.details["priority"] == expected


def test_auth_none_sends_no_token_even_when_one_is_set(ntfy: FakeNtfy) -> None:
    apply(lan_notifier(), {"server": ntfy.url, "topic": "t", "auth": "none"})
    assert "authorization" not in ntfy.seen[0].headers


@pytest.mark.parametrize(
    ("environ", "kind"),
    [
        ({"TZ": "UTC"}, "secret_missing"),
        ({"NTFY_TOKEN": ""}, "secret_missing"),
        ({"NTFY_TOKEN": "tk_with space_inside"}, "secret_invalid"),
        ({"NTFY_TOKEN": "tk_line\r\nX-Evil: 1"}, "secret_invalid"),
        ({"NTFY_TOKEN": "tk_" + "x" * 600}, "secret_invalid"),
    ],
)
def test_a_missing_or_malformed_token_sends_nothing(
    ntfy: FakeNtfy, environ: dict[str, str], kind: str
) -> None:
    with pytest.raises(SinkError) as raised:
        apply(lan_notifier(environ=environ), {"server": ntfy.url, "topic": "t"})
    assert raised.value.kind == kind
    assert "NTFY_TOKEN" in raised.value.message
    assert ntfy.seen == []


def test_the_process_environment_holds_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOKEN", TOKEN)
    recorder = Recorder()
    notifier = NtfyNotifier(recorded_notifier(recorder).publisher)
    apply(notifier, {"topic": "t"})
    assert recorder.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_an_https_link_becomes_the_click_action(ntfy: FakeNtfy) -> None:
    prefix = "https://notes.example.org/daily/"
    apply(
        lan_notifier(),
        {"server": ntfy.url, "topic": "t", "link_prefix": prefix},
        link=prefix + "2026-10-29",
    )
    assert ntfy.seen[0].json()["click"] == prefix + "2026-10-29"


def test_a_link_outside_the_prefix_sends_nothing(ntfy: FakeNtfy) -> None:
    with pytest.raises(SinkError, match="link_prefix"):
        apply(
            lan_notifier(),
            {"server": ntfy.url, "topic": "t", "link_prefix": "https://notes.example.org/a/"},
            link="https://notes.example.org/b/x",
        )
    assert ntfy.seen == []


def test_only_notify_push_is_applied() -> None:
    async def run() -> None:
        await lan_notifier().apply(
            ApplyRequest(Paths(), {"type": "http.post", "url": "https://x.example.org/"})
        )

    with pytest.raises(SinkError) as raised:
        anyio.run(run)
    assert raised.value.kind == "not_supported"


def test_identity_is_constant() -> None:
    assert lan_notifier().identity(Paths()) == "ntfy"


# --- quiet hours ----------------------------------------------------------------------------


def test_outside_quiet_hours_a_push_goes_now(ntfy: FakeNtfy) -> None:
    applied = apply(
        lan_notifier(now=FIXED_NOW), {"server": ntfy.url, "topic": "t", "quiet_hours": NIGHT}
    )
    assert "delay" not in ntfy.seen[0].json()
    assert applied.details["delivery"] == "sent"


def test_during_quiet_hours_a_push_is_delayed_to_their_end(ntfy: FakeNtfy) -> None:
    applied = apply(
        lan_notifier(now=LATE), {"server": ntfy.url, "topic": "t", "quiet_hours": NIGHT}
    )
    assert ntfy.seen[0].json()["delay"] == str(int(MORNING.timestamp()))
    assert applied.details == {
        "delivery": "delayed",
        "priority": 3,
        "id": "Ab12Cd34",
        "until": "2026-10-30T06:00:00+00:00",
    }


def test_a_delay_is_at_least_ten_seconds(ntfy: FakeNtfy) -> None:
    almost = datetime(2026, 10, 30, 5, 59, 55, 500000, tzinfo=UTC)  # 06:59:55.5 in Paris
    apply(lan_notifier(now=almost), {"server": ntfy.url, "topic": "t", "quiet_hours": NIGHT})
    assert ntfy.seen[0].json()["delay"] == str(math.ceil(almost.timestamp() + 10))


def test_during_quiet_hours_drop_sends_nothing(ntfy: FakeNtfy) -> None:
    applied = apply(
        lan_notifier(now=LATE),
        {"server": ntfy.url, "topic": "t", "quiet_hours": {**NIGHT, "during": "drop"}},
    )
    assert ntfy.seen == []
    assert applied.details == {"delivery": "dropped", "priority": 3}
    assert applied.target == f"{ntfy.url}/t"


@pytest.mark.parametrize("during", ["delay", "drop"])
def test_a_critical_push_ignores_quiet_hours(ntfy: FakeNtfy, during: str) -> None:
    applied = apply(
        lan_notifier(now=LATE),
        {
            "server": ntfy.url,
            "topic": "t",
            "priority": "critical",
            "quiet_hours": {**NIGHT, "during": during},
        },
    )
    sent = ntfy.seen[0].json()
    assert "delay" not in sent
    assert sent["priority"] == 5
    assert applied.details["delivery"] == "sent"


def test_a_critical_push_needs_no_time_zone(ntfy: FakeNtfy) -> None:
    environ = {"NTFY_TOKEN": TOKEN}
    apply(
        lan_notifier(now=LATE, environ=environ),
        {"server": ntfy.url, "topic": "t", "priority": "critical", "quiet_hours": NIGHT},
    )
    assert len(ntfy.seen) == 1


def test_quiet_hours_without_a_time_zone_send_nothing(ntfy: FakeNtfy) -> None:
    with pytest.raises(SinkError) as raised:
        apply(
            lan_notifier(environ={"NTFY_TOKEN": TOKEN}),
            {"server": ntfy.url, "topic": "t", "quiet_hours": NIGHT},
        )
    assert raised.value.kind == "config_invalid"
    assert ntfy.seen == []


def test_quiet_hours_follow_the_instance_zone(ntfy: FakeNtfy) -> None:
    # 22:30 UTC is 18:30 in New York: not quiet there.
    apply(
        lan_notifier(now=LATE, environ=env(TZ="America/New_York")),
        {"server": ntfy.url, "topic": "t", "quiet_hours": NIGHT},
    )
    assert "delay" not in ntfy.seen[0].json()


def test_a_dropped_push_still_checks_its_intent(ntfy: FakeNtfy) -> None:
    with pytest.raises(SinkError) as raised:
        apply(
            lan_notifier(now=LATE),
            {"server": ntfy.url, "topic": "t", "quiet_hours": {**NIGHT, "during": "drop"}},
            link="https://evil.example.net/",
        )
    assert raised.value.kind == "intent_invalid"
