"""HTTP adapter for the Core terminal service."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import os
import signal
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .runtime import Conflict, Runtime
from .terminals import TerminalService

_LOCAL_ERROR = "terminal access requires authentication or a local loopback client"


def _address(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value.strip().split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _proxied(headers: Any) -> bool:
    """Whether a proxy header could have substituted or forwarded the peer identity."""
    for name in headers:
        name = name.lower()
        if name == "forwarded" or name.startswith("x-forwarded-") or name in ("x-real-ip", "x-client-ip"):
            return True
    return False


def _loopback(value: str | None) -> bool:
    address = _address(value)
    return address is not None and address.is_loopback


def _loopback_name(value: str | None) -> bool:
    if not value:
        return False
    name = value.strip().lower().rstrip(".")
    return name == "localhost" or _loopback(name)


def _hostname_key(value: str | None) -> str | None:
    if not value:
        return None
    address = _address(value)
    if address is not None:
        return str(address)
    return value.strip().lower().rstrip(".")


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _loopback_authority(value: str | None, scheme: str, port: int) -> str | None:
    """The normalized loopback host a Host header names, or None for a rebound one."""
    if not value or any(character in value for character in ",@/?#") or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit("//" + value)
        actual = parsed.port if parsed.port is not None else _default_port(scheme)
    except ValueError:
        return None
    if parsed.username or parsed.password or not _loopback_name(parsed.hostname) or actual != port:
        return None
    return _hostname_key(parsed.hostname)


def _loopback_origin(value: str, scheme: str, port: int, hostname: str) -> bool:
    try:
        parsed = urlsplit(value)
        actual = parsed.port if parsed.port is not None else _default_port(parsed.scheme)
    except ValueError:
        return False
    return (parsed.scheme == scheme and not parsed.username and not parsed.password
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
            and _loopback_name(parsed.hostname) and actual == port
            and _hostname_key(parsed.hostname) == hostname)


def routes(service: TerminalService, runtime: Runtime, wrap: Callable[..., Any], *, token: str, url: str) -> list[Route]:
    origin = urlsplit(url)
    allowed_origin = f"{origin.scheme}://{origin.netloc}"
    scheme = origin.scheme or "http"
    try:
        port = origin.port if origin.port is not None else _default_port(scheme)
    except ValueError:
        port = _default_port(scheme)

    def checked(handler: Callable[..., Any]) -> Callable[..., Any]:
        if not token:
            wrapped = wrap(handler)

            async def local(request: Request) -> Response:
                # This guard must stay outside `wrap`: its idempotency journal
                # replays a known mutation before the handler runs, so a remote
                # request with a guessed key must be refused first.
                if _proxied(request.headers):
                    return JSONResponse({"error": "terminal access through a proxy requires authentication"}, status_code=403)
                peer = request.client
                if peer is None or not _loopback(peer.host):
                    return JSONResponse({"error": _LOCAL_ERROR}, status_code=403)
                hostname = _loopback_authority(request.headers.get("host"), scheme, port)
                if hostname is None:
                    return JSONResponse({"error": "terminal host is not allowed"}, status_code=403)
                request_origin = request.headers.get("origin")
                if request_origin is not None and not _loopback_origin(request_origin, scheme, port, hostname):
                    return JSONResponse({"error": "terminal origin is not allowed"}, status_code=403)
                return await wrapped(request)
            return local

        async def guarded(request: Request) -> Response:
            request_origin = request.headers.get("origin")
            if request_origin and request_origin != allowed_origin:
                return JSONResponse({"error": "terminal origin is not allowed"}, status_code=403)
            return await handler(request)
        return wrap(guarded)

    async def body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 32768:
            raise ValueError("terminal request too large")
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise TypeError("expected an object")
        return value

    def text(data: dict[str, Any], name: str, limit: int = 512) -> str:
        value = data.get(name)
        if not isinstance(value, str) or not value or len(value) > limit:
            raise ValueError(f"invalid {name}")
        return value

    async def collection(request: Request) -> Response:
        if request.method == "GET":
            project_id = request.query_params.get("project_id", "")
            runtime.project(project_id)
            return JSONResponse(service.list(project_id), headers={"cache-control": "no-store"})
        data = await body(request)
        project_id = text(data, "project_id")
        project = runtime.project(project_id)
        try:
            workspace = Path(text(data, "workspace")).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("workspace does not exist") from exc
        session_id = data.get("session_id")
        if session_id is not None:
            if not isinstance(session_id, str):
                raise ValueError("invalid session_id")
            session = runtime.session(session_id)
            if session.project_id != project_id or workspace != Path(session.directory).resolve():
                raise ValueError("workspace does not match session")
        elif not workspace.is_relative_to(Path(project.path).resolve()):
            raise ValueError("workspace is outside project")
        term = service.create(project_id=project_id, workspace=str(workspace), creator=text(data, "client_id"),
                              session_id=session_id, rows=data.get("rows", 24), cols=data.get("cols", 80))
        return JSONResponse(term.metadata(), status_code=201, headers={"cache-control": "no-store"})

    async def resource(request: Request) -> Response:
        terminal_id = request.path_params["id"]
        if request.method == "PATCH":
            data = await body(request)
            if set(data) != {"name"}:
                raise ValueError("expected name")
            return JSONResponse(service.rename(terminal_id, data["name"]), headers={"cache-control": "no-store"})
        if request.method == "DELETE":
            return JSONResponse(await service.dismiss(terminal_id), headers={"cache-control": "no-store"})
        return JSONResponse(service.metadata(terminal_id), headers={"cache-control": "no-store"})

    async def attach(request: Request) -> Response:
        term = service.get(request.path_params["id"])
        data = await body(request)
        return JSONResponse({"attachment_id": term.attach(text(data, "client_id"))}, headers={"cache-control": "no-store"})

    async def action(request: Request) -> Response:
        term = service.get(request.path_params["id"])
        data = await body(request)
        aid = text(data, "attachment_id")
        operation = request.path_params["action"]
        if operation == "control":
            term.control(aid, text(data, "action"))
            result = term.metadata()
        elif operation == "detach":
            term.attachment(aid)
            term.detach(aid)
            result = {"detached": True}
        elif operation == "input":
            seq = data.get("input_seq")
            if type(seq) is not int or seq < 1:
                raise ValueError("invalid input_seq")
            try:
                value = base64.b64decode(text(data, "data", 22000), validate=True)
            except binascii.Error as exc:
                raise ValueError("invalid base64 input") from exc
            term.input(aid, seq, value)
            result = {"input_seq": seq}
        elif operation == "resize":
            term.resize(aid, data.get("rows"), data.get("cols"))
            result = term.metadata()
        elif operation == "signal":
            term.require_control(aid)
            name = text(data, "signal")
            signals = {"INT": signal.SIGINT, "TERM": signal.SIGTERM, "HUP": signal.SIGHUP}
            if name not in signals:
                raise ValueError("invalid signal")
            try:
                foreground = os.tcgetpgrp(term.fd)
                if foreground <= 0 or os.getsid(foreground) != term.pid:
                    raise Conflict("terminal foreground process is unavailable")
                os.killpg(foreground, signals[name])
            except OSError as exc:
                raise Conflict("terminal foreground process is unavailable") from exc
            result = {"signal": name}
        else:
            return JSONResponse({"error": "unknown action"}, status_code=404)
        return JSONResponse(result, headers={"cache-control": "no-store"})

    async def stream(request: Request) -> Response:
        term = service.get(request.path_params["id"])
        aid = request.query_params.get("attachment_id", "")
        attachment = term.attachment(aid)
        if attachment.connected:
            raise Conflict("attachment is already connected")
        raw_after = request.query_params.get("after")
        after = int(raw_after) if raw_after is not None else None
        if after is not None and (after < 0 or after > term.seq):
            raise ValueError("invalid terminal cursor")
        attachment.connected = True
        attachment.queue = asyncio.Queue(maxsize=256)
        floor, head = term.floor(), term.seq
        gap = floor > 1 and (after is None or after < floor - 1)
        replay = [frame for frame, _ in term.frames if after is None or gap or frame["seq"] > after]

        async def events() -> AsyncIterator[str]:
            try:
                if gap:
                    notice = {"version": 1, "type": "gap", "seq": floor - 1, "floor": floor, "head": head,
                              "discarded": term.discarded, "reset": base64.b64encode(b"\x1bc").decode(),
                              "rows": term.rows, "cols": term.cols}
                    yield f"event: gap\ndata: {json.dumps(notice)}\n\n"
                for frame in replay:
                    yield f"id: {frame['seq']}\nevent: {frame['type']}\ndata: {json.dumps(frame)}\n\n"
                while True:
                    try:
                        frame = await asyncio.wait_for(attachment.queue.get(), 15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if frame is None:
                        break
                    yield f"id: {frame['seq']}\nevent: {frame['type']}\ndata: {json.dumps(frame)}\n\n"
            finally:
                term.detach(aid)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"cache-control": "no-store", "x-accel-buffering": "no"})

    return [Route("/api/terminal", checked(collection), methods=["GET", "POST"]),
            Route("/api/terminal/{id}", checked(resource), methods=["GET", "PATCH", "DELETE"]),
            Route("/api/terminal/{id}/attach", checked(attach), methods=["POST"]),
            Route("/api/terminal/{id}/stream", checked(stream)),
            Route("/api/terminal/{id}/{action}", checked(action), methods=["POST"])]
