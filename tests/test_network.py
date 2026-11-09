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


def run(
    recorder: Recorder,
    network: list[str] | None,
    server: str = SERVER,
    network_local: list[str] | None = None,
) -> dict[str, Any]:
    arguments = {
        "paths": state_paths().to_wire(),
        "intent": {"type": "notify.push", "title": "T", "body": "B", "link": None},
        "tool": TOOL,
        "params": {"server": server, "topic": "t"},
        "dedupe_key": "0" * 64,
    }
    plugin = build(recorded_notifier(recorder))
    return anyio.run(
        lambda: operation(
            plugin, "ntfy.apply", arguments, network=network, network_local=network_local
        ),
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


LAN = "http://ntfy.lan:8080"


def test_http_on_a_network_local_grant_is_published_to() -> None:
    recorder = Recorder()
    result = run(recorder, [], LAN, ["ntfy.lan:8080"])
    assert result["target"] == f"{LAN}/t"
    assert str(recorder.requests[0].url) == f"{LAN}/"


def test_http_without_a_port_means_port_80() -> None:
    recorder = Recorder()
    assert run(recorder, [], "http://ntfy.lan", ["ntfy.lan:80"])["target"] == "http://ntfy.lan/t"
    assert len(recorder.requests) == 1
    refused = run(Recorder(), [], "http://ntfy.lan", ["ntfy.lan:8080"])
    assert refused["error"]["kind"] == "config_invalid"


@pytest.mark.parametrize(
    ("server", "local"),
    [
        ("http://ntfy.lan:8081", ["ntfy.lan:8080"]),  # another port
        ("http://ntfy.lan", ["ntfy.lan:8080"]),  # the default port 80 is not 8080
        ("http://other.lan:8080", ["ntfy.lan:8080"]),  # another host
        ("http://ntfy.lan:8080", []),  # no grant
        ("http://ntfy.lan.evil.example.org:8080", ["ntfy.lan:8080"]),  # a public look-alike
    ],
)
def test_http_without_an_exact_grant_is_refused_without_contact(
    server: str, local: list[str]
) -> None:
    recorder = Recorder()
    result = run(recorder, [], server, local)
    assert result["error"]["kind"] == "config_invalid"
    assert "network_local" in result["error"]["message"]
    assert recorder.requests == []


def test_http_on_a_public_host_is_refused_even_when_approved_for_https() -> None:
    recorder = Recorder()
    result = run(recorder, ["ntfy.example.org"], "http://ntfy.example.org", ["ntfy.lan:80"])
    assert result["error"]["kind"] == "config_invalid"
    assert recorder.requests == []


def test_a_lan_http_server_is_matched_by_its_host() -> None:
    refused = run(Recorder(), ["ntfy.sh"], "http://ntfy.example.org:8080")
    assert refused["error"]["kind"] == "config_invalid"


def test_an_older_core_keeps_the_configured_http_server() -> None:
    recorder = Recorder()
    assert run(recorder, None, LAN)["target"] == f"{LAN}/t"
    assert len(recorder.requests) == 1


def test_https_on_a_local_grant_is_published_to() -> None:
    recorder = Recorder()
    server = "https://ntfy.lan:8443"
    assert run(recorder, [], server, ["ntfy.lan:8443"])["target"] == f"{server}/t"
    refused = run(Recorder(), [], server, ["ntfy.lan:443"])
    assert refused["error"]["kind"] == "config_invalid"


def test_a_public_https_server_still_needs_network() -> None:
    recorder = Recorder()
    result = run(recorder, [], SERVER, ["ntfy.lan:80"])
    assert result["error"]["kind"] == "config_invalid"
    assert recorder.requests == []
