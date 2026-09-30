"""The HTTP/SSE surface transport (PLAN sections 14.9 and 14.12).

``uds`` is the local CLI link; this module is the network-shaped peer. It serves
the same :mod:`nexus.host.protocol` command/result structs and the same
:class:`~nexus.events.Event` envelopes, but over HTTP/1.1 on loopback:

* **commands as POST** — ``POST /v1/command`` carries one JSON
  :data:`~nexus.host.protocol.Command` and returns one JSON
  :data:`~nexus.host.protocol.Result`, the exact structs the Unix socket frames;
* **events as Server-Sent Events** — ``GET /v1/events?session=...`` attaches a
  stream. SSE's ``Last-Event-ID`` maps one-to-one onto a session log's ``seq``
  (PLAN §14.9), so a dropped connection resumes with no gap: the reconnect
  passes the last delivered ``seq`` and the stream continues at ``seq + 1`` from
  the authoritative log.

Security is built in, not retrofitted (PLAN §14.12). The server binds
``127.0.0.1`` only (a non-loopback host is refused at construction), requires a
bearer token compared in constant time on every request, checks a strict
``Origin`` allowlist on every request including preflight, and never emits
``Access-Control-Allow-Credentials``. Requiring the header is deliberate: a
request with *no* ``Origin`` is refused (403), not treated as same-origin, which
closes the browser-family gap where a cross-site form or navigation would omit
it. A non-browser client must therefore send an allowlisted ``Origin`` too. It
bounds everything a hostile peer could
make it allocate — header bytes and count, body bytes, requests per connection,
concurrent connections, and the SSE backlog by both count and bytes. No request
body, header value, token, or facade secret is ever echoed in a response.

A slow client can never stall a turn: the subscription is drained into a
bounded, non-blocking buffer and a consumer that falls behind is dropped, so
back-pressure never reaches the producer and closing the view never cancels the
turn (the session already guarantees this; the transport preserves it).

The server owns no runtime. It wraps a
:class:`~nexus.host.facade.HostFacade`, never a manager; :meth:`aclose` stops
accepting and closes every active connection but does not shut the facade down,
which remains the daemon's decision.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import re
import secrets
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

import msgspec

from ...events import Event
from ...util import new_id
from .. import protocol as p
from ..web import MAX_WEB_RESPONSE, BrowserRoutes
from . import TransportError

#: The only address family this surface ever listens on.
LOOPBACK_HOST = "127.0.0.1"
#: One JSON command in, one JSON result out.
COMMAND_PATH = "/v1/command"
#: A Server-Sent Events stream for one session.
EVENTS_PATH = "/v1/events"

#: Upper bound on one request's request-line + header block.
DEFAULT_MAX_HEADER_BYTES = 16 * 1024
#: Upper bound on one request body (a command, never an upload).
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
# The browser voice route carries one bounded PCM WAV recording.
MAX_WEB_VOICE_BODY_BYTES = 8 * 1024 * 1024
#: Upper bound on the number of header fields.
DEFAULT_MAX_HEADERS = 64
#: Upper bound on simultaneous client connections.
DEFAULT_MAX_CONNECTIONS = 64
#: Upper bound on requests served on one keep-alive connection.
DEFAULT_MAX_REQUESTS_PER_CONNECTION = 100
#: Bound on an SSE subscription's backlog, in events ...
DEFAULT_SSE_QUEUE_EVENTS = 256
#: ... and in bytes, so a few huge events cannot defeat the event bound.
DEFAULT_SSE_QUEUE_BYTES = 4 * 1024 * 1024
#: Seconds of SSE silence before a comment keep-alive is written.
DEFAULT_KEEPALIVE = 15.0
#: Bound on reading one request (line, headers, or body).
DEFAULT_READ_TIMEOUT = 30.0
#: Bound on draining one write; a slower client is disconnected.
DEFAULT_WRITE_TIMEOUT = 10.0
#: SSE reconnect hint, in milliseconds.
DEFAULT_RETRY_MS = 3000

_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    415: "Unsupported Media Type",
    421: "Misdirected Request",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

#: An HTTP token, used to validate method and header names.
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

#: One encoded SSE frame, queued for a slow socket. Bytes, not events, so the
#: backlog bound also bounds memory when an event is large.
_Frame = bytes


class HTTPSSEError(TransportError):
    """The HTTP/SSE server could not be configured, started, or reached."""


def _normalize_origin(origin: str) -> str:
    """Lower-case an origin for comparison; ``Origin`` carries no path."""
    return origin.strip().rstrip("/").lower()


def loopback_origins(port: int) -> frozenset[str]:
    """The same-origin aliases a loopback listener must accept."""
    return frozenset(
        {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error_body(message: str) -> bytes:
    """A generic failure body; never a request body or a secret."""
    return msgspec.json.encode({"error": message})


def _error_frame(message: str) -> bytes:
    return b"event: error\ndata: " + _error_body(message) + b"\n\n"


def _sse_frame(event: Event) -> bytes:
    """Encode one event as an SSE frame; ``id`` is the log ``seq``."""
    data = msgspec.json.encode(event)
    return (
        b"id: "
        + str(int(event.seq)).encode("ascii")
        + b"\nevent: "
        + event.type.encode("utf-8")
        + b"\ndata: "
        + data
        + b"\n\n"
    )


def _bearer(header: str) -> str | None:
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _parse_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return None


@dataclass(slots=True)
class _Request:
    method: str
    target: str
    path: str
    query: str
    version: str
    headers: dict[str, str]
    body: bytes
    keep_alive: bool


@dataclass(slots=True)
class _RequestError:
    status: int
    message: str


class _StreamBuffer:
    """A bounded, non-blocking hand-off from a subscription to a socket.

    The producer never awaits the socket: it appends an encoded frame and is
    told ``False`` the moment the backlog is full, at which point it stops and
    the connection is dropped. A keep-alive deadline lets the writer emit a
    comment during idle periods without ever blocking the producer.
    """

    __slots__ = (
        "_bytes",
        "_error",
        "_events",
        "_items",
        "_max_bytes",
        "_max_events",
        "_signal",
        "_state",
    )

    def __init__(self, max_events: int, max_bytes: int) -> None:
        self._items: deque[_Frame] = deque()
        self._bytes = 0
        self._events = 0
        self._max_events = max(1, max_events)
        self._max_bytes = max(1, max_bytes)
        self._signal = asyncio.Event()
        self._state = "open"
        self._error = "slow_consumer"

    def push(self, frame: _Frame) -> bool:
        if self._state != "open":
            return False
        if (
            self._events >= self._max_events
            or self._bytes + len(frame) > self._max_bytes
        ):
            self._state = "overflow"
            self._signal.set()
            return False
        self._items.append(frame)
        self._events += 1
        self._bytes += len(frame)
        self._signal.set()
        return True

    def fail(self, message: str) -> None:
        """End the stream with one error frame if there is room for it."""
        if self._state == "open" and self._events < self._max_events:
            frame = _error_frame(message)
            if self._bytes + len(frame) <= self._max_bytes:
                self._items.append(frame)
                self._bytes += len(frame)
        self._state = "closed"
        self._signal.set()

    def close(self) -> None:
        if self._state == "open":
            self._state = "closed"
        self._signal.set()

    async def get(self, timeout: float | None) -> tuple[str, _Frame | None]:
        """Return ``("frame", bytes)``, ``("keepalive", None)``, ``("end", None)``."""
        while True:
            if self._items:
                frame = self._items.popleft()
                self._events -= 1
                self._bytes -= len(frame)
                return "frame", frame
            if self._state == "overflow":
                return "error", _error_frame(self._error)
            if self._state == "closed":
                return "end", None
            self._signal.clear()
            # Re-check to close the window between clear() and await.
            if self._items or self._state != "open":
                continue
            try:
                await asyncio.wait_for(self._signal.wait(), timeout)
            except TimeoutError:
                return "keepalive", None


class HTTPSSEServer:
    """A loopback HTTP/1.1 server exposing one :class:`HostFacade` as JSON + SSE."""

    def __init__(
        self,
        facade: Any,
        *,
        host: str = LOOPBACK_HOST,
        port: int = 0,
        token: str | None = None,
        allowed_origins: Iterable[str] | None = None,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_headers: int = DEFAULT_MAX_HEADERS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_requests_per_connection: int = DEFAULT_MAX_REQUESTS_PER_CONNECTION,
        sse_queue_events: int = DEFAULT_SSE_QUEUE_EVENTS,
        sse_queue_bytes: int = DEFAULT_SSE_QUEUE_BYTES,
        keepalive: float = DEFAULT_KEEPALIVE,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        retry_ms: int = DEFAULT_RETRY_MS,
        on_shutdown: Any | None = None,
        web_workspace: str = "",
    ) -> None:
        if not _is_loopback(host):
            raise ValueError(
                f"the HTTP/SSE surface may only bind a loopback address, got {host!r}"
            )
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("token must be a non-empty string")
        for name, value in (
            ("max_header_bytes", max_header_bytes),
            ("max_body_bytes", max_body_bytes),
            ("max_headers", max_headers),
            ("max_connections", max_connections),
            ("max_requests_per_connection", max_requests_per_connection),
            ("sse_queue_events", sse_queue_events),
            ("sse_queue_bytes", sse_queue_bytes),
        ):
            if int(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if keepalive <= 0 or read_timeout <= 0 or write_timeout <= 0:
            raise ValueError("timeouts must be positive")

        self.facade = facade
        self.host = host
        self._requested_port = int(port)
        self._token = token or secrets.token_urlsafe(32)
        self._allowed: frozenset[str] | None = (
            frozenset(_normalize_origin(origin) for origin in allowed_origins)
            if allowed_origins is not None
            else None
        )
        self.max_header_bytes = int(max_header_bytes)
        self.max_body_bytes = int(max_body_bytes)
        self.max_headers = int(max_headers)
        self.max_connections = int(max_connections)
        self.max_requests_per_connection = int(max_requests_per_connection)
        self.sse_queue_events = int(sse_queue_events)
        self.sse_queue_bytes = int(sse_queue_bytes)
        self.keepalive = float(keepalive)
        self.read_timeout = float(read_timeout)
        self.write_timeout = float(write_timeout)
        self.retry_ms = int(retry_ms)
        self._on_shutdown = on_shutdown
        self._web_workspace = web_workspace

        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._web: BrowserRoutes | None = None

    # -- introspection -----------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    @property
    def origins(self) -> frozenset[str]:
        return self._allowed or frozenset()

    @property
    def started(self) -> bool:
        return self._server is not None

    @property
    def connection_count(self) -> int:
        return len(self._writers)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> Self:
        """Bind the loopback socket and begin accepting connections."""
        if self._server is not None:
            return self
        if self._closed:
            raise HTTPSSEError("server is closed")
        self._server = await asyncio.start_server(
            self._on_connection,
            host=self.host,
            port=self._requested_port,
            limit=self.max_header_bytes,
            backlog=self.max_connections,
        )
        sock = self._server.sockets[0]
        self._port = int(sock.getsockname()[1])
        if self._allowed is None:
            self._allowed = loopback_origins(self._port)
        self._web = BrowserRoutes(
            self.facade,
            workspace=self._web_workspace,
            port=self._port,
        )
        return self

    async def aclose(self) -> None:
        """Stop accepting and close every active connection, gracefully."""
        if self._closed:
            return
        self._closed = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        # Connections are closed and their handlers cancelled *before* the
        # server is awaited: since Python 3.12 ``wait_closed`` waits for every
        # active handler, so it would otherwise block on a quiet socket.
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()
        pending = [task for task in list(self._tasks) if task is not asyncio.current_task()]
        if pending:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), timeout=2.0
                )
        self._writers.clear()
        self._tasks.clear()
        if server is not None:
            with contextlib.suppress(Exception):
                await server.wait_closed()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.aclose()
        return False

    # -- auth / origin -----------------------------------------------------

    def _authorized(self, request: _Request) -> bool:
        presented = _bearer(request.headers.get("authorization", ""))
        if presented is None and request.path == EVENTS_PATH:
            # ``EventSource`` cannot set a header, so the SSE endpoint also
            # accepts the token as a query parameter; it is compared the same way.
            values = parse_qs(request.query).get("access_token")
            presented = values[0] if values else None
        if not presented:
            return False
        return hmac.compare_digest(
            presented.encode("utf-8"), self._token.encode("utf-8")
        )

    def _origin_allowed(self, origin: str) -> bool:
        """Strict allowlist, and a *required* header.

        A missing/blank ``Origin`` is refused by design rather than assumed to
        be same-origin: an absent header is not proof of a local caller, and
        treating it as one would let a cross-site navigation through. The
        allowlist itself is never ``None`` once the server has started.
        """
        if self._allowed is None or not origin.strip():
            return False
        return _normalize_origin(origin) in self._allowed

    def _cors(self, origin: str) -> dict[str, str]:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}

    def _preflight(self, origin: str) -> dict[str, str]:
        headers = self._cors(origin)
        headers.update(
            {
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Authorization, Content-Type, Last-Event-ID",
                "Access-Control-Max-Age": "600",
            }
        )
        return headers

    # -- connections -------------------------------------------------------

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._writers) >= self.max_connections:
            await self._write_response(
                writer, 503, _error_body("too many connections"), keep_alive=False
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        task = asyncio.current_task()
        self._writers.add(writer)
        if task is not None:
            self._tasks.add(task)
        try:
            # A broken peer ends its own connection; it is never a crash.
            with contextlib.suppress(Exception):
                await self._serve_connection(reader, writer)
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        served = 0
        while served < self.max_requests_per_connection and not self._closed:
            request = await self._read_request(reader)
            if request is None:
                return
            if isinstance(request, _RequestError):
                await self._write_response(
                    writer,
                    request.status,
                    _error_body(request.message),
                    keep_alive=False,
                )
                return
            served += 1
            # Browser pages and local assets share this listener but use a
            # separate cookie/CSRF path. Peer routes below retain bearer auth.
            if self._web is not None and await self._web.route(request, writer, self):
                return
            origin_header = request.headers.get("origin", "")
            if request.method == "OPTIONS":
                if not self._origin_allowed(origin_header):
                    await self._write_response(
                        writer, 403, _error_body("origin not allowed"), keep_alive=False
                    )
                    return
                await self._write_response(
                    writer,
                    204,
                    b"",
                    headers=self._preflight(origin_header.strip()),
                    keep_alive=request.keep_alive,
                )
                if not request.keep_alive:
                    return
                continue
            if not self._authorized(request):
                await self._write_response(
                    writer,
                    401,
                    _error_body("unauthorized"),
                    headers={"WWW-Authenticate": 'Bearer realm="nexus"'},
                    keep_alive=False,
                )
                return
            if not self._origin_allowed(origin_header):
                await self._write_response(
                    writer, 403, _error_body("origin not allowed"), keep_alive=False
                )
                return
            reusable = await self._route(request, writer, origin_header.strip())
            if not reusable:
                return

    # -- HTTP parse --------------------------------------------------------

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> _Request | _RequestError | None:
        try:
            raw = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), self.read_timeout
            )
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                return None
            return _RequestError(400, "truncated request")
        except asyncio.LimitOverrunError:
            return _RequestError(431, "request headers too large")
        except TimeoutError:
            return _RequestError(408, "request timeout")
        except (ConnectionError, OSError):
            return None
        if len(raw) > self.max_header_bytes:
            return _RequestError(431, "request headers too large")

        lines = raw[:-4].split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) != 3:
            return _RequestError(400, "malformed request line")
        try:
            method = parts[0].decode("ascii")
            target = parts[1].decode("latin-1")
            version = parts[2].decode("ascii")
        except UnicodeDecodeError:
            return _RequestError(400, "malformed request line")
        if not method or not _TOKEN.fullmatch(method):
            return _RequestError(400, "malformed request line")
        if version not in ("HTTP/1.1", "HTTP/1.0"):
            return _RequestError(400, "unsupported http version")
        if not target or "\x00" in target:
            return _RequestError(400, "malformed request target")
        try:
            split = urlsplit(target)
        except ValueError:
            return _RequestError(400, "malformed request target")
        if split.scheme or split.netloc:
            return _RequestError(400, "absolute target not supported")

        headers: dict[str, str] = {}
        seen: set[str] = set()
        for line in lines[1:]:
            if len(headers) >= self.max_headers:
                return _RequestError(431, "too many headers")
            if b":" not in line:
                return _RequestError(400, "malformed header")
            name_raw, _, value_raw = line.partition(b":")
            try:
                name = name_raw.strip().decode("ascii")
                value = value_raw.strip().decode("latin-1")
            except UnicodeDecodeError:
                return _RequestError(400, "malformed header")
            if not _TOKEN.fullmatch(name):
                return _RequestError(400, "malformed header")
            key = name.lower()
            if key in seen:
                return _RequestError(400, "duplicate header")
            seen.add(key)
            headers[key] = value

        if "transfer-encoding" in headers:
            return _RequestError(400, "unsupported transfer-encoding")
        length = 0
        raw_length = headers.get("content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                return _RequestError(400, "malformed content-length")
            if length < 0:
                return _RequestError(400, "malformed content-length")

        # Authenticate before buffering the larger voice body; the route checks again.
        if method == "POST" and split.path == "/v1/web/voice" and self._web is not None:
            if not self._web._host_ok(headers.get("host", "")):
                return _RequestError(421, "invalid host")
            active = self._web._session(headers)
            if not active:
                return _RequestError(401, "unauthorized")
            if not self._web._origin_ok(headers.get("origin", "")) or not self._web._csrf_matches(
                headers.get("x-csrf-token", ""), active[1].csrf
            ):
                return _RequestError(403, "forbidden")

        route_body_limit = (
            MAX_WEB_VOICE_BODY_BYTES
            if split.path == "/v1/web/voice"
            else self.max_body_bytes
        )
        if length > route_body_limit:
            return _RequestError(413, "request body too large")

        body = b""
        if length:
            try:
                body = await asyncio.wait_for(
                    reader.readexactly(length), self.read_timeout
                )
            except asyncio.IncompleteReadError:
                return _RequestError(400, "truncated request body")
            except TimeoutError:
                return _RequestError(408, "request timeout")
            except (ConnectionError, OSError):
                return None

        connection = headers.get("connection", "").lower()
        if version == "HTTP/1.1":
            keep_alive = "close" not in connection
        else:
            keep_alive = "keep-alive" in connection
        return _Request(
            method=method,
            target=target,
            path=split.path,
            query=split.query,
            version=version,
            headers=headers,
            body=body,
            keep_alive=keep_alive,
        )

    # -- routing -----------------------------------------------------------

    async def _route(
        self, request: _Request, writer: asyncio.StreamWriter, origin: str
    ) -> bool:
        if request.path == COMMAND_PATH:
            if request.method != "POST":
                await self._write_response(
                    writer,
                    405,
                    _error_body("method not allowed"),
                    headers={"Allow": "POST", **self._cors(origin)},
                    keep_alive=False,
                )
                return False
            return await self._handle_command(request, writer, origin)
        if request.path == EVENTS_PATH:
            if request.method != "GET":
                await self._write_response(
                    writer,
                    405,
                    _error_body("method not allowed"),
                    headers={"Allow": "GET", **self._cors(origin)},
                    keep_alive=False,
                )
                return False
            await self._handle_events(request, writer, origin)
            return False
        await self._write_response(
            writer,
            404,
            _error_body("not found"),
            headers=self._cors(origin),
            keep_alive=False,
        )
        return False

    async def _handle_command(
        self, request: _Request, writer: asyncio.StreamWriter, origin: str
    ) -> bool:
        content_type = request.headers.get("content-type", "")
        if content_type and "application/json" not in content_type.lower():
            await self._write_response(
                writer,
                415,
                _error_body("expected application/json"),
                headers=self._cors(origin),
                keep_alive=False,
            )
            return False
        try:
            command = p.decode_command(request.body)
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
            await self._write_response(
                writer,
                400,
                _error_body("malformed command"),
                headers=self._cors(origin),
                keep_alive=False,
            )
            return False
        # Browser listener startup is owner-only over the UDS, never an HTTP
        # peer command. The command remains in the shared wire union only so the
        # daemon's local control socket can return its one-use launch URL.
        if isinstance(command, getattr(p, "WebLaunch", ())):
            await self._write_response(
                writer, 403, _error_body("command unavailable over HTTP"),
                headers=self._cors(origin), keep_alive=False,
            )
            return False
        try:
            result = await self.facade.handle(command)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never leak a facade failure
            await self._write_response(
                writer,
                500,
                _error_body("internal error"),
                headers=self._cors(origin),
                keep_alive=False,
            )
            return False
        data = p.encode_result(result)
        if len(data) > MAX_WEB_RESPONSE:
            await self._write_response(
                writer,
                413,
                _error_body("command result too large"),
                headers=self._cors(origin),
                keep_alive=False,
            )
            return False
        if (
            isinstance(command, p.Shutdown)
            and isinstance(result, p.ShutdownResult)
            and result.stopping
            and callable(self._on_shutdown)
        ):
            with contextlib.suppress(Exception):
                self._on_shutdown(result.reason)
        await self._write_response(
            writer,
            200,
            data,
            headers={"Content-Type": "application/json", **self._cors(origin)},
            keep_alive=request.keep_alive,
        )
        return request.keep_alive

    # -- SSE ---------------------------------------------------------------

    async def _handle_events(
        self, request: _Request, writer: asyncio.StreamWriter, origin: str
    ) -> None:
        params = parse_qs(request.query)
        session = (params.get("session") or [""])[0]
        if not session:
            await self._write_response(
                writer,
                400,
                _error_body("session is required"),
                headers=self._cors(origin),
                keep_alive=False,
            )
            return
        from_seq = 0
        raw_from = (params.get("from_seq") or [None])[0]
        if raw_from is not None:
            try:
                from_seq = int(raw_from)
            except ValueError:
                from_seq = -1
            if from_seq < 0:
                await self._write_response(
                    writer,
                    400,
                    _error_body("from_seq must be a non-negative integer"),
                    headers=self._cors(origin),
                    keep_alive=False,
                )
                return
        follow = True
        raw_follow = (params.get("follow") or [None])[0]
        if raw_follow is not None:
            parsed = _parse_bool(raw_follow)
            if parsed is None:
                await self._write_response(
                    writer,
                    400,
                    _error_body("follow must be a boolean"),
                    headers=self._cors(origin),
                    keep_alive=False,
                )
                return
            follow = parsed
        start = max(from_seq, self._last_event_id(request))
        client_id = (params.get("client_id") or [None])[0] or new_id()

        head = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            **self._cors(origin),
        }
        if not await self._write_head(writer, 200, head):
            return
        await self._write_raw(writer, f"retry: {self.retry_ms}\n\n".encode("ascii"))

        buffer = _StreamBuffer(self.sse_queue_events, self.sse_queue_bytes)
        producer = asyncio.ensure_future(
            self._produce(buffer, session, start, follow, client_id)
        )
        try:
            await self._stream(buffer, writer)
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer

    def _last_event_id(self, request: _Request) -> int:
        raw = request.headers.get("last-event-id") or (
            parse_qs(request.query).get("last_event_id") or [None]
        )[0]
        if raw is None:
            return 0
        try:
            value = int(raw)
        except ValueError:
            return 0
        return max(0, value)

    async def _produce(
        self,
        buffer: _StreamBuffer,
        session: str,
        from_seq: int,
        follow: bool,
        client_id: str,
    ) -> None:
        try:
            async for event in self.facade.subscribe(
                session, from_seq, follow=follow, client_id=client_id
            ):
                try:
                    frame = _sse_frame(event)
                except Exception:  # noqa: BLE001 - an unencodable event ends the stream
                    buffer.fail("unencodable event")
                    return
                if not buffer.push(frame):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed subscription ends the stream
            buffer.fail("subscription failed")
        finally:
            buffer.close()

    async def _stream(
        self, buffer: _StreamBuffer, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            kind, frame = await buffer.get(self.keepalive)
            if kind == "frame" and frame is not None:
                if not await self._write_raw(writer, frame):
                    return
            elif kind == "keepalive":
                if not await self._write_raw(writer, b": keepalive\n\n"):
                    return
            elif kind == "error":
                await self._write_raw(writer, frame or _error_frame("slow_consumer"))
                return
            else:
                return

    # -- writes ------------------------------------------------------------

    async def _write_raw(self, writer: asyncio.StreamWriter, data: bytes) -> bool:
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self.write_timeout)
            return True
        except (TimeoutError, ConnectionError, OSError, RuntimeError):
            return False

    async def _write_head(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        headers: dict[str, str],
    ) -> bool:
        head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}"]
        head.extend(f"{name}: {value}" for name, value in headers.items())
        head.append("Connection: close")
        return await self._write_raw(
            writer, ("\r\n".join(head) + "\r\n\r\n").encode("latin-1")
        )

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes = b"",
        *,
        headers: dict[str, str] | None = None,
        keep_alive: bool = False,
    ) -> bool:
        out = dict(headers or {})
        out["Content-Length"] = str(len(body))
        out["Connection"] = "keep-alive" if keep_alive else "close"
        if body and "Content-Type" not in out:
            out["Content-Type"] = "application/json"
        head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}"]
        head.extend(f"{name}: {value}" for name, value in out.items())
        return await self._write_raw(
            writer, ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body
        )


__all__ = [
    "COMMAND_PATH",
    "DEFAULT_KEEPALIVE",
    "DEFAULT_MAX_BODY_BYTES",
    "MAX_WEB_VOICE_BODY_BYTES",
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_MAX_HEADERS",
    "DEFAULT_MAX_HEADER_BYTES",
    "DEFAULT_MAX_REQUESTS_PER_CONNECTION",
    "DEFAULT_READ_TIMEOUT",
    "DEFAULT_RETRY_MS",
    "DEFAULT_SSE_QUEUE_BYTES",
    "DEFAULT_SSE_QUEUE_EVENTS",
    "DEFAULT_WRITE_TIMEOUT",
    "EVENTS_PATH",
    "LOOPBACK_HOST",
    "HTTPSSEError",
    "HTTPSSEServer",
    "loopback_origins",
]
