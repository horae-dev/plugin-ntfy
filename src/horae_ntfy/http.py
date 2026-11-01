# SPDX-License-Identifier: Apache-2.0
"""One bounded publish to an ntfy server, with the posture of the core's HTTP clients.

- **Explicit client**: `trust_env=False` (no ambient proxy, `.netrc` or `SSL_CERT_*`), TLS
  verified on the default trust store, **no redirect followed** (a 3xx is
  `redirect_refused`), the `httpx2` / `httpcore2` loggers pinned at `WARNING`.
- **Bounded**: connect 5 s, read and write 10 s, and one deadline of `DEADLINE_S` (20 s) for
  the whole publish, retry included: that is the worst case of one push. It is below the
  host's default 30 s `call_timeout_s`; with a `call_timeout_s` under 20 s the host can
  cancel a publish mid-request (the core then records the push as "maybe applied", and the
  `sequence_id` makes a re-run replace rather than duplicate it). The core applies a run's
  changes one after another, so a slow server holds the rest of the apply (head-of-line
  blocking) for up to 20 s per push. At most `MAX_RESPONSE_BYTES` of the answer is read
  (only the message id is used).
- **One retry**, only when the connection could not be made (`ConnectError`,
  `ConnectTimeout`): the request was never sent, so retrying cannot send the notification
  twice. A timeout or an error after the request was sent is not retried.
- **`http://` only to the LAN**: the host is resolved when connecting and the connection is
  refused (`host_refused`) unless every address is loopback, RFC 1918 or IPv6 ULA (not
  link-local or CGNAT); it then tries the checked addresses in order, with no second lookup.
  This reaches into `httpx2` internals (the transport's connection pool and its network
  backend): the version is pinned to one minor, and a missing attribute fails with kind
  `internal` (`self_test` checks it when the plugin starts).
- **The token** is sent only as `Authorization: Bearer <token>`. It is never logged, never in
  an error message (they are fixed texts written here) and never in the result; errors are
  raised outside any exception handler, so no `__context__` leads back to the request.

Failures are `SinkError` kinds: `auth_failed` (401, 403), `message_too_large` (413),
`rate_limited` (429), `server_unavailable` (5xx), `delay_refused` (400 to a scheduled
publish: never re-sent at once, which would wake the phone in quiet hours),
`publish_refused` (another 4xx), `redirect_refused` (3xx), `server_timeout`, `network_error`,
`host_refused`, `internal` (the LAN transport cannot be set up).
"""

import ipaddress
import json
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import anyio
import httpcore2
import httpx2
from horae_sdk import SinkError

from horae_ntfy.settings import Settings

DEADLINE_S = 20.0
RETRY_PAUSE_S = 0.5
MAX_RESPONSE_BYTES = 64 * 1024
TIMEOUT = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
LIMITS = httpx2.Limits(max_connections=2, max_keepalive_connections=0)
HTTP_LOGGERS = ("httpx2", "httpcore2")
USER_AGENT = "horae-plugin-ntfy"
_ID = re.compile(r"[A-Za-z0-9]{1,64}")

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
"""Resolves `(host, port)` to IP addresses (tests inject a fake one)."""
TransportFactory = Callable[[Settings], httpx2.AsyncBaseTransport]

LAN_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


class Refused(Exception):
    """A connection the notifier refuses to make (an `http://` server off the LAN)."""


def quiet_loggers() -> None:
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


async def system_resolver(host: str, port: int) -> Sequence[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def lan_address(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in LAN_NETWORKS if network.version == address.version)


class LanOnlyNetwork(httpcore2.AsyncNetworkBackend):
    """Resolves when connecting; refuses unless every address is on the LAN."""

    def __init__(self, inner: httpcore2.AsyncNetworkBackend, resolver: Resolver) -> None:
        self.inner = inner
        self._resolve = resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore2.AsyncNetworkStream:
        addresses: Sequence[str] = ()
        failure: Exception | None = None
        try:
            with anyio.fail_after(timeout):
                addresses = await self._resolve(host, port)
        except TimeoutError:
            failure = httpcore2.ConnectTimeout("resolving the server timed out")
        except OSError:
            failure = httpcore2.ConnectError("cannot resolve the server")
        if failure is not None:
            raise failure
        if not addresses or not all(lan_address(address) for address in addresses):
            raise Refused("an http:// server that resolves outside the LAN")
        last: Exception | None = None
        for address in addresses:
            try:
                return await self.inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore2.ConnectError, httpcore2.ConnectTimeout) as error:
                last = error
        assert last is not None  # noqa: S101 - addresses is not empty
        raise last

    async def connect_unix_socket(  # pragma: no cover - no transport sets `uds`
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore2.AsyncNetworkStream:
        raise Refused("a unix socket")

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - retries are off
        await self.inner.sleep(seconds)


def _https_transport() -> httpx2.AsyncHTTPTransport:
    return httpx2.AsyncHTTPTransport(verify=True, trust_env=False, limits=LIMITS, retries=0)


def lan_only(transport: httpx2.AsyncHTTPTransport, resolver: Resolver) -> None:
    """Puts `LanOnlyNetwork` in front of `transport`'s network backend (httpx2 internals);
    `SinkError("internal")` if they are not what this version expects."""
    pool = getattr(transport, "_pool", None)
    backend = getattr(pool, "_network_backend", None)
    if not isinstance(pool, httpcore2.AsyncConnectionPool) or not isinstance(
        backend, httpcore2.AsyncNetworkBackend
    ):
        raise SinkError("internal", "this httpx2 version cannot restrict http:// to the LAN")
    pool._network_backend = LanOnlyNetwork(backend, resolver)


def self_test() -> None:
    """Checks at startup that the `http://` LAN restriction can be set up with this httpx2
    (`SinkError("internal")` otherwise), rather than on the first push."""
    lan_only(_https_transport(), system_resolver)


def default_transport(settings: Settings, resolver: Resolver) -> httpx2.AsyncBaseTransport:
    transport = _https_transport()
    if settings.scheme == "http":
        lan_only(transport, resolver)
    return transport


def _status_error(status: int, *, delayed: bool = False) -> SinkError:
    if 300 <= status < 400:
        return SinkError("redirect_refused", f"the ntfy server answered {status} (a redirect)")
    if status in (401, 403):
        return SinkError("auth_failed", f"the ntfy server refused the credentials ({status})")
    if status == 413:
        return SinkError("message_too_large", "the ntfy server refused the message size (413)")
    if status == 400 and delayed:
        return SinkError("delay_refused", "the ntfy server refused the scheduled delivery (400)")
    if status == 429:
        return SinkError("rate_limited", "the ntfy server is rate limiting (429)")
    if status >= 500:
        return SinkError("server_unavailable", f"the ntfy server failed ({status})")
    return SinkError("publish_refused", f"the ntfy server refused the message ({status})")


def _message_id(raw: bytes) -> str | None:
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    message_id = answer.get("id") if isinstance(answer, dict) else None
    return message_id if isinstance(message_id, str) and _ID.fullmatch(message_id) else None


class Publisher:
    """Publishes one message (`publish`); `transport` and `resolver` are seams for tests."""

    def __init__(
        self,
        *,
        transport: TransportFactory | None = None,
        resolver: Resolver = system_resolver,
        deadline_s: float = DEADLINE_S,
        retry_pause_s: float = RETRY_PAUSE_S,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self.deadline_s = deadline_s
        self.retry_pause_s = retry_pause_s

    def _client(self, settings: Settings) -> httpx2.AsyncClient:
        transport = (
            self._transport(settings)
            if self._transport is not None
            else default_transport(settings, self._resolver)
        )
        return httpx2.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT,
            limits=LIMITS,
        )

    async def _send(
        self, client: httpx2.AsyncClient, settings: Settings, body: bytes, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        async with client.stream("POST", f"{settings.server}/", content=body, headers=headers) as r:
            raw = bytearray()
            if not r.headers.get("content-encoding"):
                async for chunk in r.aiter_raw():
                    raw += chunk
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raw.clear()  # over the cap: the id is not needed, stop reading
                        break
            return r.status_code, bytes(raw)

    async def publish(
        self, settings: Settings, message: dict[str, Any], token: str | None
    ) -> str | None:
        """POST `message` as JSON; the ntfy message id (`None` when the answer has none)."""
        quiet_loggers()
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        failure: SinkError | None = None
        outcome: tuple[int, bytes] | None = None
        try:
            with anyio.fail_after(self.deadline_s):
                async with self._client(settings) as client:
                    for attempt in (1, 2):
                        try:
                            outcome = await self._send(client, settings, body, headers)
                            break
                        except (httpx2.ConnectError, httpx2.ConnectTimeout):
                            if attempt == 2:
                                failure = SinkError(
                                    "network_error", "cannot connect to the ntfy server"
                                )
                        if failure is None:
                            await anyio.sleep(self.retry_pause_s)
        except TimeoutError:
            failure = SinkError("server_timeout", "the ntfy server did not answer in time")
        except httpx2.TimeoutException:
            failure = SinkError("server_timeout", "the ntfy server did not answer in time")
        except Refused:
            failure = SinkError("host_refused", "an http:// server must resolve to the LAN")
        except httpx2.HTTPError:
            failure = SinkError("network_error", "the connection to the ntfy server failed")
        if failure is not None:
            raise failure
        if outcome is None:  # pragma: no cover - every path above sets one or the other
            raise SinkError("network_error", "the ntfy server gave no answer")
        status, raw = outcome
        if not 200 <= status < 300:
            raise _status_error(status, delayed="delay" in message)
        return _message_id(raw)
