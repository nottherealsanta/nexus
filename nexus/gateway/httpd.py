"""A small, strictly bounded HTTP/1.1 server for the gateway (loopback bind only).

It is independent of the daemon's ``HTTPSSEServer`` on purpose: the gateway is
reachable from a tailnet through a TLS-terminating proxy, so its limits are its
own. Everything a peer can make it allocate or wait for is bounded: header
bytes and count, body bytes (per route, decided from the headers before any body
is read), the time to receive a head and a body, concurrent connections,
requests per connection, the time to write, and the drain on shutdown. A
request is parsed strictly (no folding, no chunked bodies, no duplicate headers,
origin-form targets only) and no request data is ever echoed in an error.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Self

import msgspec

DEFAULT_MAX_HEADER_BYTES = 16 * 1024
DEFAULT_MAX_HEADERS = 64
DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_REQUESTS = 100
DEFAULT_HEAD_TIMEOUT = 10.0
DEFAULT_BODY_TIMEOUT = 20.0
#: A body must arrive at no less than this rate (bytes/second) on top of the base timeout.
MIN_BODY_RATE = 32 * 1024
DEFAULT_WRITE_TIMEOUT = 10.0
DEFAULT_DRAIN_TIMEOUT = 5.0
MAX_TARGET_BYTES = 4096

_REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict", 413: "Content Too Large",
    415: "Unsupported Media Type", 417: "Expectation Failed", 421: "Misdirected Request",
    429: "Too Many Requests", 431: "Request Header Fields Too Large", 500: "Internal Server Error",
    502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
}
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


@dataclass(slots=True)
class RequestHead:
    method: str
    target: str
    path: str
    query: str
    version: str
    headers: dict[str, str]
    keep_alive: bool
    content_length: int = 0
    #: Scratch space the handler fills in ``pre_body`` and reads in ``handle``.
    ctx: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Request:
    head: RequestHead
    body: bytes


@dataclass(slots=True)
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def json_response(status: int, value: object, headers: dict[str, str] | None = None) -> Response:
    return Response(status, msgspec.json.encode(value), {"Content-Type": "application/json", **(headers or {})})


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    """The one error shape: a stable code and a human sentence, never request data."""
    return json_response(status, {"error": code, "message": message}, headers)


class Handler(Protocol):
    def body_limit(self, head: RequestHead) -> int: ...

    async def pre_body(self, head: RequestHead) -> Response | None: ...

    async def handle(self, request: Request, stream: Stream) -> Response | None: ...


class Stream:
    """The raw connection for a streaming response (SSE)."""

    def __init__(self, server: HTTPServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._server = server
        self._reader = reader
        self._writer = writer
        self.started = False
        self.disconnected = asyncio.Event()
        self._watch: asyncio.Task[None] | None = None

    @property
    def closing(self) -> bool:
        return self._server.closing

    async def start(self, status: int, headers: dict[str, str]) -> bool:
        head = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}"]
        head.extend(f"{k}: {v}" for k, v in {**SECURITY_HEADERS, **headers}.items())
        head.append("Connection: close")
        ok = await self.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        if ok:
            self.started = True
            self._watch = asyncio.create_task(self._watch_peer())
        return ok

    async def write(self, data: bytes) -> bool:
        return await self._server._write(self._writer, data)

    def abort(self) -> None:
        """Drop the connection now, unblocking a write stuck on a slow client."""
        transport = self._writer.transport
        if transport is not None:
            transport.abort()

    async def _watch_peer(self) -> None:
        # A client sends nothing after its request; EOF or an error means it left.
        try:
            await self._reader.read(1)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        self.disconnected.set()

    async def aclose(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watch


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class HTTPServer:
    def __init__(
        self,
        handler: Handler,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_headers: int = DEFAULT_MAX_HEADERS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        head_timeout: float = DEFAULT_HEAD_TIMEOUT,
        body_timeout: float = DEFAULT_BODY_TIMEOUT,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        if not _is_loopback(host):
            raise ValueError("the gateway listens on a loopback address only")
        self._handler = handler
        self.host = host
        self._requested_port = int(port)
        self.max_header_bytes = int(max_header_bytes)
        self.max_headers = int(max_headers)
        self.max_connections = int(max_connections)
        self.max_requests = int(max_requests)
        self.head_timeout = float(head_timeout)
        self.body_timeout = float(body_timeout)
        self.write_timeout = float(write_timeout)
        self.drain_timeout = float(drain_timeout)
        self.port = 0
        self.closing = False
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def connection_count(self) -> int:
        return len(self._writers)

    async def start(self) -> Self:
        self._server = await asyncio.start_server(
            self._on_connection,
            host=self.host,
            port=self._requested_port,
            limit=self.max_header_bytes,
            backlog=self.max_connections,
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def aclose(self) -> None:
        """Stop accepting, let in-flight requests finish (bounded), then cut the rest."""
        if self.closing:
            return
        self.closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), self.drain_timeout)
        for writer in list(self._writers):
            writer.close()
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2.0)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2.0)

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- connections -------------------------------------------------------

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if self.closing or len(self._writers) >= self.max_connections:
            await self._write(writer, _render(503, error_response(503, "busy", "the gateway is busy").body, {"Content-Type": "application/json"}))
            # A peer may already have sent a request body. Read a small, timed
            # amount after replying so closing does not reset the response. This
            # path deliberately creates no background drain task.
            with contextlib.suppress(TimeoutError, ConnectionError, OSError):
                await asyncio.wait_for(reader.read(64 * 1024), min(self.drain_timeout, 0.25))
            writer.close()
            return
        self._writers.add(writer)
        if task is not None:
            self._tasks.add(task)
        try:
            with contextlib.suppress(Exception):
                await self._serve(reader, writer)
        finally:
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2.0)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        served = 0
        while served < self.max_requests and not self.closing:
            head = await self._read_head(reader, writer)
            if head is None:
                return
            served += 1
            self._begin()
            try:
                if not await self._dispatch(head, reader, writer):
                    return
            finally:
                self._end()

    def _begin(self) -> None:
        self._inflight += 1
        self._idle.clear()

    def _end(self) -> None:
        self._inflight -= 1
        if self._inflight <= 0:
            self._inflight = 0
            self._idle.set()

    async def _dispatch(self, head: RequestHead, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Serve one request; return whether the connection may be reused."""
        keep = head.keep_alive and not self.closing
        try:
            early = await self._handler.pre_body(head)
            if early is not None:
                await self._respond(writer, early, keep_alive=False)
                return False
            limit = self._handler.body_limit(head)
            if head.content_length > limit:
                await self._respond(writer, error_response(413, "body_too_large", "the request body is too large"), keep_alive=False)
                return False
            body = b""
            if head.content_length:
                timeout = self.body_timeout + head.content_length / MIN_BODY_RATE
                try:
                    body = await asyncio.wait_for(reader.readexactly(head.content_length), timeout)
                except TimeoutError:
                    await self._respond(writer, error_response(408, "request_timeout", "the request body arrived too slowly"), keep_alive=False)
                    return False
                except asyncio.IncompleteReadError:
                    return False
            stream = Stream(self, reader, writer)
            try:
                response = await self._handler.handle(Request(head, body), stream)
            finally:
                await stream.aclose()
            if response is None:  # the handler streamed its own response
                return False
            return await self._respond(writer, response, keep_alive=keep)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError):
            return False
        except Exception:  # noqa: BLE001 - a handler bug is a generic 500, never a traceback
            await self._respond(writer, error_response(500, "internal_error", "internal error"), keep_alive=False)
            return False

    async def _respond(self, writer: asyncio.StreamWriter, response: Response, *, keep_alive: bool) -> bool:
        headers = {**SECURITY_HEADERS, **response.headers}
        data = _render(response.status, response.body, headers, keep_alive=keep_alive)
        return await self._write(writer, data) and keep_alive

    async def _write(self, writer: asyncio.StreamWriter, data: bytes) -> bool:
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), self.write_timeout)
            return True
        except (TimeoutError, ConnectionError, OSError, RuntimeError):
            return False

    # -- parsing -----------------------------------------------------------

    async def _read_head(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> RequestHead | None:
        async def refuse(status: int, code: str, message: str) -> None:
            await self._respond(writer, error_response(status, code, message), keep_alive=False)

        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.head_timeout)
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                await refuse(400, "bad_request", "truncated request")
            return None
        except asyncio.LimitOverrunError:
            await refuse(431, "headers_too_large", "request headers are too large")
            return None
        except TimeoutError:
            await refuse(408, "request_timeout", "the request head arrived too slowly")
            return None
        except (ConnectionError, OSError):
            return None
        if len(raw) > self.max_header_bytes:
            await refuse(431, "headers_too_large", "request headers are too large")
            return None
        parsed = _parse_head(raw[:-4], self.max_headers)
        if isinstance(parsed, tuple):
            await refuse(*parsed)
            return None
        return parsed


def _render(status: int, body: bytes, headers: dict[str, str], *, keep_alive: bool = False) -> bytes:
    out = dict(headers)
    out["Content-Length"] = str(len(body))
    out["Connection"] = "keep-alive" if keep_alive else "close"
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}"]
    lines.extend(f"{k}: {v}" for k, v in out.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _parse_head(raw: bytes, max_headers: int) -> RequestHead | tuple[int, str, str]:
    def bad(message: str = "malformed request") -> tuple[int, str, str]:
        return 400, "bad_request", message

    lines = raw.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3:
        return bad()
    try:
        method, target, version = (part.decode("ascii") for part in parts)
    except UnicodeDecodeError:
        return bad()
    if not _TOKEN.match(method) or version not in ("HTTP/1.1", "HTTP/1.0"):
        return bad()
    if not target.startswith("/") or len(target) > MAX_TARGET_BYTES or "#" in target:
        return bad("malformed request target")
    if _CONTROL.search(target) or "\t" in target or any(ord(ch) > 126 for ch in target):
        return bad("malformed request target")
    path, _, query = target.partition("?")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if len(headers) >= max_headers:
            return 431, "headers_too_large", "too many request headers"
        if line[:1] in (b" ", b"\t") or b":" not in line:
            return bad("malformed header")
        name_raw, _, value_raw = line.partition(b":")
        try:
            name, value = name_raw.decode("ascii"), value_raw.strip().decode("latin-1")
        except UnicodeDecodeError:
            return bad("malformed header")
        key = name.lower()
        if not _TOKEN.match(name) or key in headers or _CONTROL.search(value):
            return bad("malformed header")
        headers[key] = value
    if "transfer-encoding" in headers or "expect" in headers or "upgrade" in headers:
        return bad("unsupported request framing")
    length = 0
    if "content-length" in headers:
        raw_length = headers["content-length"]
        if not (raw_length.isascii() and raw_length.isdigit() and len(raw_length) <= 10):
            return bad("malformed content-length")
        length = int(raw_length)
    connection = headers.get("connection", "").lower()
    keep_alive = "close" not in connection if version == "HTTP/1.1" else "keep-alive" in connection
    return RequestHead(method, target, path, query, version, headers, keep_alive, length)


__all__ = [
    "SECURITY_HEADERS",
    "Handler",
    "HTTPServer",
    "Request",
    "RequestHead",
    "Response",
    "Stream",
    "error_response",
    "json_response",
]
