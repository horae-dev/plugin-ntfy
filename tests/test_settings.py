# SPDX-License-Identifier: Apache-2.0
"""The entry's trusted parameters: defaults, and every invalid value refused by name."""

from datetime import time
from typing import Any

import pytest
from horae_sdk import SinkError

from horae_ntfy.settings import PRIORITIES, QuietHours, parse


def test_defaults() -> None:
    settings = parse({"topic": "reminders"})
    assert settings.server == "https://ntfy.sh"
    assert (settings.scheme, settings.host) == ("https", "ntfy.sh")
    assert settings.priority == "normal"
    assert settings.ntfy_priority == 3
    assert settings.tags == ()
    assert settings.auth == "token"
    assert settings.quiet_hours is None
    assert settings.link_prefix is None
    assert not settings.critical


def test_everything_set() -> None:
    settings = parse(
        {
            "server": "https://ntfy.example.org:8443/",
            "topic": "Horae_alerts-1",
            "priority": "critical",
            "tags": ["warning", "+1", "alarm_clock"],
            "auth": "none",
            "quiet_hours": {"start": "22:30", "end": "06:45", "during": "drop"},
            "link_prefix": "https://notes.example.org/daily/",
        }
    )
    assert settings.server == "https://ntfy.example.org:8443"
    assert settings.critical
    assert settings.ntfy_priority == 5
    assert settings.quiet_hours == QuietHours(time(22, 30), time(6, 45), "drop")


def test_priorities_map_to_ntfy_one_to_five() -> None:
    assert {name: parse({"topic": "t", "priority": name}).ntfy_priority for name in PRIORITIES} == {
        "min": 1,
        "low": 2,
        "normal": 3,
        "high": 4,
        "critical": 5,
    }


def test_an_http_server_is_accepted_for_the_lan_path() -> None:
    settings = parse({"topic": "t", "server": "http://ntfy:8080"})
    assert (settings.scheme, settings.server) == ("http", "http://ntfy:8080")


@pytest.mark.parametrize(
    "server",
    [
        "ftp://ntfy.example.org",
        "https://127.0.0.1",
        "https://[::1]",
        "https://localhost",
        "https://ntfy.localhost",
        "https://NTFY.example.org",
        "https://user:pw@ntfy.example.org",
        "https://ntfy.example.org/base",
        "https://ntfy.example.org/?x=1",
        "https://ntfy.example.org/#top",
        "https://ntfy.example.org:0",
        "https://ntfy.example.org:99999",
        "https://ntfy.example.org:080",
        "https://ntfy.example.org :443",
        "https://ntfyé.example.org",
        "https://ntfy_x.example.org",
        "https://",
        42,
        "https://" + "a" * 300,
    ],
)
def test_invalid_servers_are_refused(server: Any) -> None:
    with pytest.raises(SinkError) as raised:
        parse({"topic": "t", "server": server})
    assert raised.value.kind == "config_invalid"
    assert "server" in raised.value.message


@pytest.mark.parametrize(
    ("params", "name"),
    [
        ({}, "topic"),
        ({"topic": ""}, "topic"),
        ({"topic": "a/b"}, "topic"),
        ({"topic": "x" * 65}, "topic"),
        ({"topic": "t", "priority": "urgent"}, "priority"),
        ({"topic": "t", "priority": 5}, "priority"),
        ({"topic": "t", "tags": "warning"}, "tags"),
        ({"topic": "t", "tags": ["Warning"]}, "tags"),
        ({"topic": "t", "tags": ["a", "a"]}, "tags"),
        ({"topic": "t", "tags": [str(i) for i in range(9)]}, "tags"),
        ({"topic": "t", "auth": "basic"}, "auth"),
        ({"topic": "t", "link_prefix": 3}, "link_prefix"),
        ({"topic": "t", "quiet_hour": {"start": "22:00", "end": "07:00"}}, "quiet_hour"),
        ({"topic": "t", "quiet_hours": "22:00-07:00"}, "quiet_hours"),
        ({"topic": "t", "quiet_hours": {"start": "22:00"}}, "quiet_hours"),
        ({"topic": "t", "quiet_hours": {"start": "22:00", "end": "22:00"}}, "quiet_hours"),
        ({"topic": "t", "quiet_hours": {"start": "24:00", "end": "07:00"}}, "quiet_hours.start"),
        ({"topic": "t", "quiet_hours": {"start": "22:00", "end": "7:00"}}, "quiet_hours.end"),
        ({"topic": "t", "quiet_hours": {"start": "22:00", "end": 7}}, "quiet_hours.end"),
        (
            {"topic": "t", "quiet_hours": {"start": "22:00", "end": "07:00", "tz": "UTC"}},
            "quiet_hours",
        ),
        (
            {"topic": "t", "quiet_hours": {"start": "22:00", "end": "07:00", "during": "queue"}},
            "quiet_hours.during",
        ),
    ],
)
def test_invalid_parameters_are_refused_by_name(params: dict[str, Any], name: str) -> None:
    with pytest.raises(SinkError) as raised:
        parse(params)
    assert raised.value.kind == "config_invalid"
    assert f"parameter {name}" in raised.value.message or f"'{name}'" in raised.value.message


def test_the_message_never_echoes_a_value() -> None:
    with pytest.raises(SinkError) as raised:
        parse({"topic": "t", "server": "https://ntfy.example.org/secret-path-value"})
    assert "secret-path-value" not in raised.value.message
