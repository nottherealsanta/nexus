"""Gateway routes: pairing, device auth, host listing, views, event streams, commands, voice.

Every route except ``/healthz`` and ``/v1/pair`` needs a device bearer token, and
every host route re-checks the device against the current state file (revoke,
disallow, expiry, scope) before talking to the host. The gateway forwards what the
host's browser surface produced and never reduces events itself. Errors are the
single shape ``{"error": code, "message": human}`` and never echo a request,
token, SDP or audio payload.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import msgspec

from ..host import protocol as p
from ..util import redact_secrets
from . import policy
from .httpd import Request, RequestHead, Response, Stream, error_response, json_response
from .sse import FrameParser, FrameQueue, FrameTooLarge, event_name, is_relayed, sse_event
from .store import Device, GatewayError, GatewayStore, HostEntry, PairingError, clean_name
from .upstream import Upstream, UpstreamError, workspace_state

API_VERSION = 1
MAX_PAIR_BODY = 4096
MAX_VOICE_BODY = 8 * 1024 * 1024
MAX_SESSIONS = 500
SSE_RETRY_MS = 3000

_SESSION = r"(?P<session>[A-Za-z0-9_.:-]{1,128})"
_HOST = r"/v1/hosts/(?P<host>[0-9a-f]{16})"
#: (name, pattern, allowed methods)
_ROUTES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = tuple(
    (name, re.compile(pattern + r"\Z"), frozenset(methods))
    for name, pattern, methods in (
        ("health", r"/healthz", ("GET",)),
        ("pair", r"/v1/pair", ("POST",)),
        ("me", r"/v1/me", ("GET",)),
        ("hosts", r"/v1/hosts", ("GET",)),
        ("sessions", _HOST + r"/sessions", ("GET",)),
        ("view", _HOST + r"/sessions/" + _SESSION + r"/view", ("GET",)),
        ("events", _HOST + r"/sessions/" + _SESSION + r"/events", ("GET",)),
        ("command", _HOST + r"/command", ("POST",)),
        ("voice", _HOST + r"/voice", ("POST",)),
    )
)
_PUBLIC = frozenset({"health", "pair"})
_ROUTE_SCOPE = {"hosts": "read", "sessions": "read", "view": "read", "events": "read", "voice": "voice"}
_BODY_LIMITS = {"pair": MAX_PAIR_BODY, "command": policy.MAX_COMMAND_BYTES, "voice": MAX_VOICE_BODY}
_DIGITS = re.compile(r"[0-9]{1,16}\Z")


class _Reply(Exception):
    """Unwinds a handler with a ready response."""

    def __init__(self, response: Response) -> None:
        super().__init__(response.status)
        self.response = response


def _fail(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> _Reply:
    return _Reply(error_response(status, code, message, headers))


def public_hosts(public_url: str) -> frozenset[str]:
    """The ``Host`` values the externally advertised HTTPS name may arrive as."""
    if not public_url:
        return frozenset()
    try:
        parts = urlsplit(public_url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise GatewayError("the public URL is not valid") from exc
    if (
        parts.scheme != "https"
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", host)
    ):
        raise GatewayError("the public URL must be https://HOST[:PORT] with no path")
    hosts = {host, f"{host}:{port}" if port else f"{host}:443"}
    if port in (None, 443):
        hosts.add(f"{host}:443")
    return frozenset(hosts)


class _FailureWindow:
    """Allow at most ``limit`` recorded failures per ``window`` seconds."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float]) -> None:
        self._limit, self._window, self._clock = limit, window, clock
        self._times: list[float] = []

    def blocked(self) -> float:
        """Seconds until another attempt is allowed; ``0`` when allowed now."""
        now = self._clock()
        self._times = [t for t in self._times if now - t < self._window]
        if len(self._times) < self._limit:
            return 0.0
        return max(1.0, self._window - (now - self._times[0]))

    def record(self) -> None:
        self._times.append(self._clock())


@dataclass(slots=True)
class Limits:
    keepalive: float = 15.0
    recheck: float = 0.5
    max_streams: int = 32
    max_streams_per_device: int = 4
    sse_queue_events: int = 256
    sse_queue_bytes: int = 4 * 1024 * 1024
    max_frame_bytes: int = 1024 * 1024
    pair_failures: int = 8
    pair_window: float = 60.0
    upstream_concurrency: int = 8


class GatewayApp:
    def __init__(
        self,
        store: GatewayStore,
        upstream: Upstream,
        *,
        public_url: str = "",
        probe: Callable[[Path], str] = workspace_state,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._upstream = upstream
        self._probe = probe
        self._limits = limits or Limits()
        self._public = public_hosts(public_url)
        self._loopback: frozenset[str] = frozenset()
        self._pair_failures = _FailureWindow(self._limits.pair_failures, self._limits.pair_window, clock)
        self._upstream_slots = asyncio.Semaphore(self._limits.upstream_concurrency)
        self._streams = 0
        self._device_streams: dict[str, int] = {}

    def bind_port(self, port: int) -> None:
        self._loopback = frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})

    @property
    def stream_count(self) -> int:
        return self._streams

    # -- Handler protocol --------------------------------------------------

    def body_limit(self, head: RequestHead) -> int:
        return _BODY_LIMITS.get(head.ctx.get("route", ""), 0)

    async def pre_body(self, head: RequestHead) -> Response | None:
        """Reject before any body is read: host, origin, route, auth, scope."""
        try:
            self._gate(head)
        except _Reply as reply:
            return reply.response
        return None

    def _gate(self, head: RequestHead) -> None:
        headers = head.headers
        if headers.get("host", "").lower() not in self._loopback | self._public:
            raise _fail(421, "invalid_host", "this host name is not served here")
        if "origin" in headers:
            raise _fail(403, "origin_forbidden", "browser origins are not accepted")
        path = head.path
        for name, pattern, methods in _ROUTES:
            match = pattern.match(path)
            if match:
                break
        else:
            raise _fail(404, "not_found", "no such route")
        if head.method not in methods:
            raise _fail(405, "method_not_allowed", "method not allowed", {"Allow": ", ".join(sorted(methods))})
        head.ctx["route"] = name
        head.ctx["params"] = match.groupdict()
        if name == "pair":
            wait = self._pair_failures.blocked()
            if wait:
                raise _fail(429, "rate_limited", "too many failed pairing attempts", {"Retry-After": str(int(wait) + 1)})
            self._check_content_type(head, "application/json", required=False)
        if name in _PUBLIC:
            return
        device = self._authenticate(head)
        head.ctx["device"] = device
        if "host" in head.ctx["params"]:
            entry = self._store.host(head.ctx["params"]["host"])
            if entry is None or entry.host_id not in device.host_ids:
                raise _fail(404, "unknown_host", "no such workspace host")
            head.ctx["entry"] = entry
        scope = _ROUTE_SCOPE.get(name)
        if scope is not None and scope not in device.scopes:
            raise _fail(403, "scope_required", f"this device lacks the {scope} scope")
        if name == "command":
            self._check_content_type(head, "application/json", required=True)
        elif name == "voice":
            self._check_content_type(head, "audio/wav", required=True)

    @staticmethod
    def _check_content_type(head: RequestHead, expected: str, *, required: bool) -> None:
        value = head.headers.get("content-type", "")
        if not value and not required:
            return
        if value.split(";", 1)[0].strip().lower() != expected:
            raise _fail(415, "unsupported_media_type", f"expected {expected}")

    def _authenticate(self, head: RequestHead) -> Device:
        scheme, _, token = head.headers.get("authorization", "").partition(" ")
        device = self._store.authenticate(token.strip()) if scheme.lower() == "bearer" else None
        if device is None:
            raise _fail(401, "unauthorized", "a valid device token is required", {"WWW-Authenticate": 'Bearer realm="nexus-gateway"'})
        return device

    async def handle(self, request: Request, stream: Stream) -> Response | None:
        head = request.head
        route = head.ctx["route"]
        try:
            if route == "health":
                return json_response(200, {"ok": True, "version": API_VERSION})
            if route == "pair":
                return await self._pair(request)
            device: Device = head.ctx["device"]
            if route == "me":
                return json_response(200, {
                    "device_id": device.device_id, "name": device.name,
                    "scopes": list(device.scopes), "expires_at": int(device.expires_at),
                })
            if route == "hosts":
                return self._hosts(device)
            entry: HostEntry = head.ctx["entry"]
            params = head.ctx["params"]
            if route == "sessions":
                return await self._sessions(device, entry)
            if route == "view":
                return await self._view(device, entry, params["session"])
            if route == "events":
                return await self._events(head, stream, device, entry, params["session"])
            if route == "command":
                return await self._command(request, device, entry)
            return await self._voice(request, device, entry)
        except _Reply as reply:
            return reply.response
        except UpstreamError as exc:
            return error_response(exc.status, exc.code, exc.message)

    # -- authorization -----------------------------------------------------

    def _recheck(self, device: Device, entry: HostEntry, scope: str) -> Device:
        """Re-validate against the current state file immediately before acting."""
        current = self._store.check_device(device.device_id, host_id=entry.host_id, scope=scope)
        if current is None:
            raise _fail(403, "access_revoked", "this device no longer has access")
        return current

    # -- pairing and identity ----------------------------------------------

    async def _pair(self, request: Request) -> Response:
        try:
            data = msgspec.json.decode(request.body)
            code, name = data["code"], data.get("device_name", "")
            if not isinstance(code, str) or len(code) > 64 or not isinstance(name, str):
                raise ValueError
        except (msgspec.DecodeError, KeyError, TypeError, ValueError, AttributeError):
            return error_response(400, "bad_request", "expected {code, device_name}")
        try:
            device, token = await asyncio.to_thread(self._store.redeem, code, clean_name(name, ""))
        except PairingError:
            self._pair_failures.record()
            return error_response(403, "invalid_code", "the pairing code is invalid or expired")
        except GatewayError:
            return error_response(503, "unavailable", "pairing is unavailable")
        return json_response(200, {
            "device_id": device.device_id, "token": token, "scopes": list(device.scopes),
            "expires_at": int(device.expires_at), "gateway_id": self._store.gateway_id,
        })

    def _hosts(self, device: Device) -> Response:
        rows = [
            {
                "host_id": entry.host_id,
                "workspace": entry.workspace,
                "label": entry.label,
                "state": self._probe(Path(entry.workspace)),
            }
            for entry in self._store.hosts()
            if entry.host_id in device.host_ids
        ]
        return json_response(200, rows)

    # -- host reads --------------------------------------------------------

    async def _host_command(self, entry: HostEntry, command: p.Command, *, idempotent: bool) -> bytes:
        async with self._upstream_slots:
            response = await self._upstream.request(
                entry.workspace, "POST", "/v1/web/command",
                content=p.encode_command(command), idempotent=idempotent,
            )
        if response.status == 200:
            return response.body
        if response.status == 413:
            raise _fail(502, "response_too_large", "the host response is too large")
        if response.status in (400, 403):
            raise _fail(400, "host_rejected", "the host rejected the request")
        raise _fail(502, "host_unavailable", "the workspace host failed")

    async def _ensure_open(self, entry: HostEntry, session: str) -> None:
        """Load an existing session in the host (never creates one)."""
        body = await self._host_command(entry, p.SessionOpen(session=session, create=False, recover=True), idempotent=True)
        try:
            result = p.decode_result(body)
        except (msgspec.DecodeError, msgspec.ValidationError) as exc:
            raise _fail(502, "host_unavailable", "the workspace host failed") from exc
        if not isinstance(result, p.SessionOpenResult):
            raise _fail(404, "session_unavailable", "the session is unavailable")

    async def _sessions(self, device: Device, entry: HostEntry) -> Response:
        self._recheck(device, entry, "read")
        body = await self._host_command(entry, p.SessionList(), idempotent=True)
        try:
            result = msgspec.json.decode(body)
        except msgspec.DecodeError as exc:
            raise _fail(502, "host_unavailable", "the workspace host failed") from exc
        if not isinstance(result, dict) or result.get("type") != "SessionListResult":
            raise _fail(502, "host_unavailable", "the workspace host failed")
        sessions = result.get("sessions") if isinstance(result.get("sessions"), list) else []
        result["truncated"] = len(sessions) > MAX_SESSIONS
        result["sessions"] = sessions[:MAX_SESSIONS]
        return json_response(200, result)

    async def _view(self, device: Device, entry: HostEntry, session: str) -> Response:
        self._recheck(device, entry, "read")
        await self._ensure_open(entry, session)
        async with self._upstream_slots:
            response = await self._upstream.request(
                entry.workspace, "GET", "/v1/web/session-view", params={"session": session}, idempotent=True,
            )
        if response.status == 200:
            return Response(200, response.body, {"Content-Type": "application/json"})
        if response.status in (400, 404):
            raise _fail(404, "session_unavailable", "the session is unavailable")
        raise _fail(502, "host_unavailable", "the workspace host failed")

    # -- event stream ------------------------------------------------------

    def _from_seq(self, head: RequestHead) -> int:
        query = parse_qs(head.query).get("from_seq")
        raw = query[0] if query else head.headers.get("last-event-id", "0") or "0"
        if not _DIGITS.fullmatch(raw):
            raise _fail(400, "bad_request", "from_seq must be a non-negative integer")
        return int(raw)

    def _claim_stream(self, device_id: str) -> bool:
        limits = self._limits
        if self._streams >= limits.max_streams or self._device_streams.get(device_id, 0) >= limits.max_streams_per_device:
            return False
        self._streams += 1
        self._device_streams[device_id] = self._device_streams.get(device_id, 0) + 1
        return True

    def _release_stream(self, device_id: str) -> None:
        self._streams -= 1
        remaining = self._device_streams.get(device_id, 1) - 1
        if remaining > 0:
            self._device_streams[device_id] = remaining
        else:
            self._device_streams.pop(device_id, None)

    async def _events(
        self, head: RequestHead, stream: Stream, device: Device, entry: HostEntry, session: str
    ) -> Response | None:
        from_seq = self._from_seq(head)
        self._recheck(device, entry, "read")
        if not self._claim_stream(device.device_id):
            return error_response(429, "too_many_streams", "too many open event streams")
        try:
            await self._ensure_open(entry, session)
            async with self._upstream.stream(
                entry.workspace, "/v1/web/session-events", {"session": session, "from_seq": str(from_seq)}
            ) as upstream:
                started = await stream.start(200, {
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "X-Accel-Buffering": "no",
                })
                if started and await stream.write(f"retry: {SSE_RETRY_MS}\n\n".encode("ascii")):
                    await self._relay(upstream, stream, device, entry)
        except _Reply as reply:
            if not stream.started:
                return reply.response
            await stream.write(_resync("host_failed"))
        except UpstreamError as exc:
            if not stream.started:
                return error_response(exc.status, exc.code, exc.message)
            await stream.write(_resync("host_unavailable"))
        finally:
            self._release_stream(device.device_id)
        return None

    async def _relay(self, upstream, stream: Stream, device: Device, entry: HostEntry) -> None:  # noqa: ANN001
        limits = self._limits
        queue = FrameQueue(limits.sse_queue_events, limits.sse_queue_bytes)
        state = {"reason": "upstream_closed", "error": False, "revoked": False}
        parser = FrameParser(limits.max_frame_bytes)

        async def pump() -> None:
            try:
                async for chunk in upstream.aiter_bytes():
                    for frame in parser.feed(chunk):
                        if not is_relayed(frame):
                            continue
                        if event_name(frame) == "error":
                            state["error"] = True
                        if not queue.push(frame):
                            return
            except FrameTooLarge:
                state["reason"] = "frame_too_large"
            except Exception:  # noqa: BLE001, S110 - an upstream drop ends the stream with a resync
                pass
            finally:
                queue.close()

        async def watch() -> None:
            # Revocation, disallow and expiry are rechecked against the state
            # file on a short timer, even while a write to a slow client blocks.
            while True:
                await asyncio.sleep(limits.recheck)
                if self._store.check_device(device.device_id, host_id=entry.host_id, scope="read") is None:
                    state["revoked"] = True
                    queue.close()
                    await asyncio.sleep(limits.recheck)
                    stream.abort()
                    return

        loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(pump()), asyncio.create_task(watch())]
        last_write = loop.time()
        try:
            while not stream.disconnected.is_set():
                if state["revoked"]:
                    await stream.write(sse_event("error", '{"error":"access_revoked"}'))
                    return
                if stream.closing:
                    await stream.write(_resync("shutdown"))
                    return
                kind, frame = await queue.get(limits.recheck)
                if kind == "frame" and frame is not None:
                    if not await stream.write(frame):
                        return
                    last_write = loop.time()
                elif kind == "end":
                    if state["revoked"]:
                        continue
                    if not state["error"]:
                        await stream.write(_resync(state["reason"]))
                    return
                elif kind == "overflow":
                    await stream.write(_resync("slow_consumer"))
                    return
                elif loop.time() - last_write >= limits.keepalive:
                    if not await stream.write(b": keepalive\n\n"):
                        return
                    last_write = loop.time()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    # -- commands and voice ------------------------------------------------

    async def _command(self, request: Request, device: Device, entry: HostEntry) -> Response:
        try:
            command = p.decode_command(request.body)
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
            return error_response(400, "malformed_command", "the command could not be decoded")
        try:
            command = policy.authorize(command, device.scopes, device.device_id)
        except policy.CommandDenied as exc:
            return error_response(403, exc.code, exc.message)
        except policy.CommandInvalid as exc:
            return error_response(400, "invalid_command", exc.message)
        scope = policy.required_scope(command) or "read"
        self._recheck(device, entry, scope)
        body = await self._host_command(entry, command, idempotent=policy.is_read_only(command))
        return Response(200, _sanitize_result(body), {"Content-Type": "application/json"})

    async def _voice(self, request: Request, device: Device, entry: HostEntry) -> Response:
        values = parse_qs(request.head.query).get("request_id", [])
        request_id = values[0] if len(values) == 1 else ""
        try:
            valid = len(request_id) == 36 and str(uuid.UUID(request_id)) == request_id.lower()
        except ValueError:
            valid = False
        if not valid:
            return error_response(400, "bad_request", "request_id must be a UUID")
        if not request.body:
            return error_response(400, "bad_request", "an audio/wav body is required")
        self._recheck(device, entry, "voice")
        async with self._upstream_slots:
            response = await self._upstream.request(
                entry.workspace, "POST", "/v1/web/voice", params={"request_id": request_id},
                content=request.body, content_type="audio/wav", idempotent=False,
            )
        if response.status == 200:
            return Response(200, response.body, {"Content-Type": "application/json"})
        if response.status in (400, 415):
            return error_response(400, "host_rejected", "the host rejected the audio")
        if response.status == 413:
            return error_response(413, "body_too_large", "the audio is too large")
        return error_response(502, "host_unavailable", "the workspace host failed")


def _resync(reason: str) -> bytes:
    return sse_event("resync", msgspec.json.encode({"reason": reason}).decode())


def _sanitize_result(body: bytes) -> bytes:
    """Bound and redact a host ``ErrorResult``; every other result is passed through."""
    if b"ErrorResult" not in body[:64]:
        return body
    try:
        result = p.decode_result(body)
    except (msgspec.DecodeError, msgspec.ValidationError):
        return p.encode_result(p.ErrorResult(kind="error", message="the host returned an unreadable error"))
    if not isinstance(result, p.ErrorResult):
        return body
    kind = re.sub(r"[^A-Za-z0-9_.:-]", "", result.kind)[:64] or "error"
    return p.encode_result(p.ErrorResult(kind=kind, message=redact_secrets(result.message)[:400]))


__all__ = ["API_VERSION", "GatewayApp", "Limits", "public_hosts"]
