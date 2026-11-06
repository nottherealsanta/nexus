"""HTTP adapter for the Core terminal service."""
from __future__ import annotations

import asyncio
import base64
import binascii
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


def routes(service: TerminalService, runtime: Runtime, wrap: Callable[..., Any], *, token: str, url: str) -> list[Route]:
    origin = urlsplit(url)
    allowed_origin = f"{origin.scheme}://{origin.netloc}"

    def checked(handler: Callable[..., Any]) -> Callable[..., Any]:
        async def guarded(request: Request) -> Response:
            if not token:
                return JSONResponse({"error": "terminal access requires authentication"}, status_code=403)
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
        if request.method == "DELETE" and terminal_id in service.terminals:
            await service.get(terminal_id).close()
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
                os.killpg(os.tcgetpgrp(term.fd), signals[name])
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
            Route("/api/terminal/{id}", checked(resource), methods=["GET", "DELETE"]),
            Route("/api/terminal/{id}/attach", checked(attach), methods=["POST"]),
            Route("/api/terminal/{id}/stream", checked(stream)),
            Route("/api/terminal/{id}/{action}", checked(action), methods=["POST"])]
