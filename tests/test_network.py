# SPDX-License-Identifier: Apache-2.0
"""The server's host is checked against the network permissions approved in `horae.lock`
(horae-sdk 0.5): refused when not approved, trusted as before when an older core sent no list."""

from typing import Any

import anyio
import pytest
from horae_sdk.testing import operation

from fakes import Recorder, recorded_notifier, state_paths
from horae_ntfy import TOOL, build

SERVER = "https://ntfy.example.org"


def run(recorder: Recorder, network: list[str] | None, server: str = SERVER) -> dict[str, Any]:
    arguments = {
        "paths": state_paths().to_wire(),
        "intent": {"type": "notify.push", "title": "T", "body": "B", "link": None},
        "tool": TOOL,
        "params": {"server": server, "topic": "t"},
        "dedupe_key": "0" * 64,
    }
    plugin = build(recorded_notifier(recorder))
    return anyio.run(
        lambda: operation(plugin, "ntfy.apply", arguments, network=network),
    )


def test_an_approved_host_is_published_to() -> None:
    recorder = Recorder()
    result = run(recorder, ["ntfy.example.org"])
    assert result["target"] == f"{SERVER}/t"
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("network", [[], ["ntfy.sh"]])
def test_an_unapproved_host_is_refused_without_contact(network: list[str]) -> None:
    recorder = Recorder()
    result = run(recorder, network)
    assert result["error"]["kind"] == "config_invalid"
    assert "ntfy.example.org" in result["error"]["message"]
    assert recorder.requests == []


def test_an_older_core_without_a_list_keeps_the_configured_server() -> None:
    recorder = Recorder()
    result = run(recorder, None)
    assert result["target"] == f"{SERVER}/t"
    assert len(recorder.requests) == 1


def test_a_lan_http_server_is_matched_by_its_host() -> None:
    refused = run(Recorder(), ["ntfy.sh"], "http://ntfy.example.org:8080")
    assert refused["error"]["kind"] == "config_invalid"
