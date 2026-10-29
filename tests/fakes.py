# SPDX-License-Identifier: Apache-2.0
"""Shared fakes: an ntfy HTTP server on 127.0.0.1, an in-memory transport, a fake clock.

Nothing here touches the network: the fake server listens on loopback only, and the
notifier reaches it through the `http://` LAN path with a fake resolver that answers
127.0.0.1 for the synthetic host `ntfy.example.org`. The token is a synthetic value.
"""

import contextlib
import json
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx2

from horae_ntfy.http import Publisher
from horae_ntfy.plugin import NtfyNotifier
from horae_ntfy.settings import Settings

TOKEN = "tk_synthetictesttoken0123456789ab"  # noqa: S105 - a fake token, for tests only
HOST = "ntfy.example.org"
FIXED_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Behaviour:
    status: int = 200
    body: bytes = b'{"id":"Ab12Cd34","time":1700000000,"event":"message","topic":"t"}'
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    delay_s: float = 0.0


class FakeNtfy:
    """A loopback HTTP server recording every request and answering with `behaviour`."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.behaviour = Behaviour()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                fake.seen.append(
                    Seen("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                behaviour = fake.behaviour
                if behaviour.delay_s:
                    time.sleep(behaviour.delay_s)
                self.send_response(behaviour.status)
                for name, value in behaviour.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(behaviour.body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self.wfile.write(behaviour.body)  # the client may stop reading

            def log_message(self, format: str, *args: Any) -> None:
                pass  # quiet: stderr is checked for secrets in some tests

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def __enter__(self) -> "FakeNtfy":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


async def loopback(host: str, port: int) -> Sequence[str]:
    """The fake resolver: the synthetic host is on loopback; anything else is unknown."""
    if host == HOST:
        return ["127.0.0.1"]
    raise OSError("unknown host")


def env(**extra: str) -> dict[str, str]:
    return {"TZ": "Europe/Paris", "NTFY_TOKEN": TOKEN, **extra}


def lan_notifier(
    *, now: datetime = FIXED_NOW, environ: dict[str, str] | None = None, **publisher: Any
) -> NtfyNotifier:
    """A notifier reaching the fake server over real sockets (the `http://` LAN path)."""
    return NtfyNotifier(
        Publisher(resolver=loopback, retry_pause_s=0.0, **publisher),
        now=lambda: now,
        env=env() if environ is None else environ,
    )


Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """An in-memory transport for the `https://` path: records requests, answers `handler`."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.handler = handler or (lambda request: httpx2.Response(200, json={"id": "Zz99"}))

    def __call__(self, settings: Settings) -> httpx2.AsyncBaseTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            answer = self.handler(request)
            # A streamed answer, as a real transport gives (the publisher streams it).
            stream = httpx2.ByteStream(answer.content)
            return httpx2.Response(answer.status_code, headers=answer.headers, stream=stream)

        return httpx2.MockTransport(handle)

    def json(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content.decode("utf-8"))


def recorded_notifier(
    recorder: Recorder, *, now: datetime = FIXED_NOW, environ: dict[str, str] | None = None
) -> NtfyNotifier:
    return NtfyNotifier(
        Publisher(transport=recorder, retry_pause_s=0.0),
        now=lambda: now,
        env=env() if environ is None else environ,
    )
