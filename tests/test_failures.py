# SPDX-License-Identifier: Apache-2.0
"""Failures: every one is a `SinkError` with a fixed message, bounded in time and size, and
the token never shows in an error, a log, stderr or a result."""

import logging
import traceback
from collections.abc import Callable, Sequence
from typing import Any

import anyio
import httpcore2
import httpx2
import pytest
from horae_sdk import Applied, ApplyRequest, Paths, SinkError

from fakes import TOKEN, Behaviour, FakeNtfy, Recorder, lan_notifier, loopback, recorded_notifier
from horae_ntfy.http import (
    MAX_RESPONSE_BYTES,
    LanOnlyNetwork,
    Publisher,
    default_transport,
    lan_address,
    lan_only,
    self_test,
    system_resolver,
)
from horae_ntfy.plugin import NtfyNotifier
from horae_ntfy.settings import parse


def push(params: dict[str, Any]) -> ApplyRequest:
    intent = {"type": "notify.push", "title": "T", "body": "B", "link": None}
    return ApplyRequest(Paths(), intent, tool="ntfy.push", params=params)


def apply(notifier: NtfyNotifier, params: dict[str, Any]) -> Applied:
    async def run() -> Applied:
        return await notifier.apply(push(params))

    return anyio.run(run)


def failure(notifier: NtfyNotifier, params: dict[str, Any]) -> SinkError:
    with pytest.raises(SinkError) as raised:
        apply(notifier, params)
    error = raised.value
    assert error.__cause__ is None
    assert TOKEN not in repr(error)
    assert TOKEN not in error.message
    return error


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "publish_refused"),
        (401, "auth_failed"),
        (403, "auth_failed"),
        (404, "publish_refused"),
        (413, "message_too_large"),
        (429, "rate_limited"),
        (500, "server_unavailable"),
        (502, "server_unavailable"),
        (507, "server_unavailable"),
    ],
)
def test_an_error_status_is_a_sink_error(ntfy: FakeNtfy, status: int, kind: str) -> None:
    # The answer echoes the token, as a hostile or verbose server might: it is never kept.
    ntfy.behaviour = Behaviour(status=status, body=f'{{"error":"bad {TOKEN}"}}'.encode())
    error = failure(lan_notifier(), {"server": ntfy.url, "topic": "t"})
    assert error.kind == kind
    assert len(ntfy.seen) == 1  # never retried: the server may have taken it


def test_a_redirect_is_not_followed(ntfy: FakeNtfy) -> None:
    ntfy.behaviour = Behaviour(
        status=307, body=b"", headers={"Location": "https://elsewhere.example.net/"}
    )
    assert failure(lan_notifier(), {"server": ntfy.url, "topic": "t"}).kind == "redirect_refused"
    assert len(ntfy.seen) == 1


def test_an_oversized_answer_is_not_read_past_the_cap(ntfy: FakeNtfy) -> None:
    ntfy.behaviour = Behaviour(body=b'{"id":"Ab12"' + b" " * (MAX_RESPONSE_BYTES * 4) + b"}")
    applied = apply(lan_notifier(), {"server": ntfy.url, "topic": "t"})
    assert applied.details == {"delivery": "sent", "priority": 3}  # delivered, id not read


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[1,2]", b'{"id": 5}', b'{"id":"../x"}', b"\xff\xfe", b"[" * 100_000],
)
def test_an_unreadable_answer_still_counts_as_delivered(ntfy: FakeNtfy, body: bytes) -> None:
    ntfy.behaviour = Behaviour(body=body)
    applied = apply(lan_notifier(), {"server": ntfy.url, "topic": "t"})
    assert "id" not in applied.details


def test_an_encoded_answer_is_not_decoded(ntfy: FakeNtfy) -> None:
    ntfy.behaviour = Behaviour(headers={"Content-Encoding": "gzip"}, body=b"\x1f\x8b junk")
    assert "id" not in apply(lan_notifier(), {"server": ntfy.url, "topic": "t"}).details


def test_a_slow_server_hits_the_deadline(ntfy: FakeNtfy) -> None:
    ntfy.behaviour = Behaviour(delay_s=1.0)
    error = failure(lan_notifier(deadline_s=0.2), {"server": ntfy.url, "topic": "t"})
    assert error.kind == "server_timeout"


def test_an_http_server_off_the_lan_is_refused() -> None:
    async def public(host: str, port: int) -> list[str]:
        return ["93.184.215.14", "10.0.0.2"]

    notifier = NtfyNotifier(Publisher(resolver=public), env={"NTFY_TOKEN": TOKEN})
    error = failure(notifier, {"server": "http://ntfy.example.org", "topic": "t"})
    assert error.kind == "host_refused"


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.10", "::1", "fd00::1", "::ffff:10.0.0.1"],
)
def test_lan_addresses(address: str) -> None:
    assert lan_address(address)


@pytest.mark.parametrize(
    "address",
    ["93.184.215.14", "169.254.169.254", "100.64.0.1", "fe80::1", "2001:db8::1", "nope"],
)
def test_addresses_off_the_lan(address: str) -> None:
    assert not lan_address(address)


def test_an_unknown_lan_host_is_a_network_error() -> None:
    notifier = NtfyNotifier(
        Publisher(resolver=loopback, retry_pause_s=0.0), env={"NTFY_TOKEN": TOKEN}
    )
    error = failure(notifier, {"server": "http://unknown.example.org", "topic": "t"})
    assert error.kind == "network_error"


def test_a_slow_resolver_is_a_timeout() -> None:
    async def stuck(host: str, port: int) -> list[str]:
        await anyio.sleep(10)
        return []  # pragma: no cover

    notifier = NtfyNotifier(Publisher(resolver=stuck, deadline_s=0.2), env={"NTFY_TOKEN": TOKEN})
    error = failure(notifier, {"server": "http://ntfy.example.org", "topic": "t"})
    assert error.kind == "server_timeout"


def test_a_closed_port_is_retried_once_then_a_network_error(ntfy: FakeNtfy) -> None:
    url = ntfy.url
    ntfy.__exit__()  # nothing listens on the port any more
    assert failure(lan_notifier(), {"server": url, "topic": "t"}).kind == "network_error"


def raising(error: Callable[[httpx2.Request], Exception]) -> Recorder:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error(request)

    return Recorder(handler)


def test_a_connect_failure_is_retried_once() -> None:
    recorder = raising(lambda request: httpx2.ConnectError(f"no route {TOKEN}", request=request))
    error = failure(
        recorded_notifier(recorder), {"server": "https://ntfy.example.org", "topic": "t"}
    )
    assert error.kind == "network_error"
    assert len(recorder.requests) == 2


def test_a_connect_failure_then_success_delivers() -> None:
    calls: list[int] = []

    def flaky(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx2.ConnectTimeout("slow", request=request)
        return httpx2.Response(200, json={"id": "Ok1"})

    applied = apply(
        recorded_notifier(Recorder(flaky)), {"server": "https://ntfy.example.org", "topic": "t"}
    )
    assert applied.details["id"] == "Ok1"
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx2.ReadTimeout, "server_timeout"),
        (httpx2.WriteTimeout, "server_timeout"),
        (httpx2.ReadError, "network_error"),
        (httpx2.RemoteProtocolError, "network_error"),
    ],
)
def test_a_failure_after_sending_is_not_retried(
    error: type[httpx2.RequestError], kind: str
) -> None:
    recorder = raising(lambda request: error(f"boom {TOKEN}", request=request))
    assert (
        failure(
            recorded_notifier(recorder), {"server": "https://ntfy.example.org", "topic": "t"}
        ).kind
        == kind
    )
    assert len(recorder.requests) == 1


SCENARIOS: list[tuple[str, Behaviour]] = [
    ("ok", Behaviour()),
    ("unauthorized", Behaviour(status=401, body=TOKEN.encode())),
    ("server error", Behaviour(status=500, body=TOKEN.encode())),
    ("redirect", Behaviour(status=302, body=b"", headers={"Location": f"https://x/{TOKEN}"})),
    ("slow", Behaviour(delay_s=0.5)),
]


@pytest.mark.parametrize(("name", "behaviour"), SCENARIOS, ids=[name for name, _ in SCENARIOS])
def test_the_token_never_reaches_logs_stderr_or_results(
    ntfy: FakeNtfy,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
    name: str,
    behaviour: Behaviour,
) -> None:
    caplog.set_level(logging.DEBUG)
    for logger in ("httpx2", "httpcore2"):
        logging.getLogger(logger).setLevel(logging.DEBUG)  # the publisher pins them back
    ntfy.behaviour = behaviour
    seen: list[str] = []
    try:
        applied = apply(lan_notifier(deadline_s=0.2), {"server": ntfy.url, "topic": "t"})
        seen.append(repr(applied.to_wire()))
    except SinkError as error:
        seen.extend([repr(error), error.message, repr(error.__context__)])
    out, err = capfd.readouterr()
    assert ntfy.seen[0].headers["authorization"] == f"Bearer {TOKEN}"  # it was sent...
    for text in [*seen, caplog.text, out, err]:
        assert TOKEN not in text  # ...and shows nowhere else
    assert logging.getLogger("httpx2").level == logging.WARNING


def test_the_system_resolver_answers_for_localhost() -> None:
    addresses = anyio.run(system_resolver, "localhost", 80)
    assert addresses
    assert all(lan_address(address) for address in addresses)


def test_an_https_server_connects_directly() -> None:
    """Only `http://` goes through the LAN check: an `https://` server's connections are
    made by the default backend (the egress proxy of M5 will police them)."""
    https = default_transport(parse({"server": "https://ntfy.example.org", "topic": "t"}), loopback)
    http = default_transport(parse({"topic": "t", "server": "http://ntfy"}), loopback)
    assert not isinstance(https._pool._network_backend, LanOnlyNetwork)  # type: ignore[attr-defined]
    assert isinstance(http._pool._network_backend, LanOnlyNetwork)  # type: ignore[attr-defined]


# --- LAN addresses in order --------------------------------------------------------------------


class Inner(httpcore2.AsyncNetworkBackend):
    """A network backend that refuses every address except `good`, recording the attempts."""

    def __init__(self, good: str | None) -> None:
        self.good = good
        self.tried: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore2.AsyncNetworkStream:
        self.tried.append(host)
        if host != self.good:
            raise httpcore2.ConnectError("refused")
        return object()  # type: ignore[return-value]


def connect(inner: Inner, addresses: Sequence[str]) -> object:
    async def resolve(host: str, port: int) -> Sequence[str]:
        return addresses

    async def run() -> object:
        return await LanOnlyNetwork(inner, resolve).connect_tcp("ntfy.lan", 80, timeout=1.0)

    return anyio.run(run)


def test_each_checked_lan_address_is_tried_in_order() -> None:
    inner = Inner(good="10.0.0.3")
    connect(inner, ["fd00::1", "10.0.0.2", "10.0.0.3", "10.0.0.4"])
    assert inner.tried == ["fd00::1", "10.0.0.2", "10.0.0.3"]


def test_when_no_lan_address_answers_the_last_error_is_raised() -> None:
    inner = Inner(good=None)
    with pytest.raises(httpcore2.ConnectError):
        connect(inner, ["10.0.0.2", "10.0.0.3"])
    assert inner.tried == ["10.0.0.2", "10.0.0.3"]


# --- httpx2 internals -----------------------------------------------------------------------


def test_the_startup_self_test_passes_with_the_pinned_httpx2() -> None:
    self_test()


def test_missing_httpx2_internals_are_an_internal_error_not_a_type_error() -> None:
    transport = httpx2.AsyncHTTPTransport()
    del transport._pool
    with pytest.raises(SinkError) as raised:
        lan_only(transport, loopback)
    assert raised.value.kind == "internal"


def test_the_plugin_refuses_to_start_when_the_self_test_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import horae_ntfy.__main__ as entry

    def broken() -> None:
        raise SinkError("internal", "this httpx2 version cannot restrict http:// to the LAN")

    monkeypatch.setattr(entry, "self_test", broken)
    with pytest.raises(SystemExit) as raised:
        entry.main([])
    assert "internal" in str(raised.value.code)


# --- an unexpected exception ----------------------------------------------------------------


def test_an_unexpected_exception_carries_no_token_in_its_traceback() -> None:
    def crash(request: httpx2.Request) -> httpx2.Response:
        raise RuntimeError("an unexpected bug in a transport")

    with pytest.raises(RuntimeError) as raised:
        apply(
            recorded_notifier(Recorder(crash)), {"server": "https://ntfy.example.org", "topic": "t"}
        )
    text = "".join(traceback.format_exception(raised.value))
    assert "unexpected bug" in text
    assert TOKEN not in text
    assert TOKEN not in repr(raised.value.__context__)
