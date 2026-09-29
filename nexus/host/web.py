"""Browser-only HTTP routes and short-lived launch/session credentials.

The peer API remains bearer authenticated. This module handles only the local
browser surface and is intentionally independent of Textual and the runtime.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import mimetypes
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

import msgspec

from . import protocol as p

WEB_ROOT = Path(__file__).resolve().parents[1] / "ui" / "web"
TICKET_TTL = 60.0
COOKIE_TTL = 12 * 60 * 60.0
MAX_WEB_RESPONSE = 32 * 1024 * 1024
COOKIE_NAME = "nexus_web"
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


@dataclass
class _BrowserSession:
    csrf: str
    expires: float


class BrowserRoutes:
    """Routes attached to one loopback listener; credentials die on restart."""

    def __init__(self, facade, *, workspace: str, port: int):
        self.facade = facade
        self.workspace = workspace
        self.port = port
        self._tickets: dict[str, float] = {}
        self._sessions: dict[str, _BrowserSession] = {}
        self._lock = asyncio.Lock()

    def issue_ticket(self) -> str:
        now = time.monotonic()
        self._expire(now)
        if len(self._tickets) >= 256:
            oldest = min(self._tickets, key=self._tickets.get)
            del self._tickets[oldest]
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = now + TICKET_TTL
        return ticket

    def _expire(self, now: float) -> None:
        self._tickets = {k: v for k, v in self._tickets.items() if v > now}
        self._sessions = {k: v for k, v in self._sessions.items() if v.expires > now}

    @staticmethod
    def _json(value) -> bytes:
        return msgspec.json.encode(value)

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        }

    def _host_ok(self, value: str) -> bool:
        return value.lower() in {
            f"127.0.0.1:{self.port}", f"localhost:{self.port}",
            f"[::1]:{self.port}", "127.0.0.1", "localhost", "[::1]",
        }

    def _origin_ok(self, origin: str) -> bool:
        return origin in {
            f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}",
            f"http://[::1]:{self.port}",
        }

    def _cookie(self, headers: dict[str, str]) -> str | None:
        for part in headers.get("cookie", "").split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == COOKIE_NAME:
                return value
        return None

    @staticmethod
    def _csrf_matches(presented: str, expected: str) -> bool:
        # Header values are decoded as latin-1 by the HTTP parser. Comparing
        # encoded bytes keeps compare_digest safe for arbitrary non-ASCII input.
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))

    def _session(self, headers: dict[str, str]) -> tuple[str, _BrowserSession] | None:
        now = time.monotonic()
        self._expire(now)
        key = self._cookie(headers)
        value = self._sessions.get(key or "")
        return (key, value) if key and value else None

    async def route(self, request, writer, server) -> bool:
        """Serve a browser route; return False for paths owned by peer API."""
        path = request.path
        is_session_page = bool(re.fullmatch(r"/s/[A-Za-z0-9_.:-]{1,128}(/a/[A-Za-z0-9_.:%-]{1,384})?", path))
        is_web = path == "/" or is_session_page or path.startswith(
            ("/assets/", "/styles/", "/js/", "/v1/web/")
        )
        if not is_web:
            return False
        common = self._headers()
        if not self._host_ok(request.headers.get("host", "")):
            await server._write_response(writer, 421, self._json({"error": "invalid host"}), headers=common)
            return True
        if not path.startswith("/v1/web/") and (".." in path.split("/") or "\\" in path):
            await server._write_response(writer, 404, self._json({"error": "not found"}), headers=common)
            return True
        origin = request.headers.get("origin", "")
        if path == "/v1/web/ticket/redeem":
            if request.method != "POST" or not self._origin_ok(origin):
                await server._write_response(writer, 403, self._json({"error": "forbidden"}), headers=common)
                return True
            try:
                body = msgspec.json.decode(request.body)
                ticket = body.get("ticket") if isinstance(body, dict) else None
            except (msgspec.DecodeError, AttributeError):
                ticket = None
            async with self._lock:
                expiry = self._tickets.pop(ticket, 0) if isinstance(ticket, str) else 0
                if expiry <= time.monotonic():
                    await server._write_response(writer, 401, self._json({"error": "invalid launch ticket"}), headers=common)
                    return True
                key, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                if len(self._sessions) >= 512:
                    oldest = min(self._sessions, key=lambda item: self._sessions[item].expires)
                    del self._sessions[oldest]
                self._sessions[key] = _BrowserSession(csrf, time.monotonic() + COOKIE_TTL)
            common["Set-Cookie"] = f"{COOKIE_NAME}={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age={int(COOKIE_TTL)}"
            common["Content-Type"] = "application/json"
            await server._write_response(writer, 200, self._json({"csrf": csrf, "workspace": self.workspace}), headers=common)
            return True
        if path == "/v1/web/logout":
            active = self._session(request.headers)
            if request.method != "POST" or not active or not self._origin_ok(origin) or not self._csrf_matches(request.headers.get("x-csrf-token", ""), active[1].csrf):
                await server._write_response(writer, 403, self._json({"error": "forbidden"}), headers=common)
                return True
            self._sessions.pop(active[0], None)
            common["Set-Cookie"] = f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            await server._write_response(writer, 204, headers=common)
            return True
        if path.startswith("/v1/web/"):
            active = self._session(request.headers)
            if not active:
                await server._write_response(writer, 401, self._json({"error": "unauthorized"}), headers=common)
                return True
            if request.method not in ("GET", "HEAD") and (
                not self._origin_ok(origin)
                or not self._csrf_matches(request.headers.get("x-csrf-token", ""), active[1].csrf)
            ):
                await server._write_response(writer, 403, self._json({"error": "forbidden"}), headers=common)
                return True
            if path == "/v1/web/bootstrap" and request.method == "GET":
                common["Content-Type"] = "application/json"
                await server._write_response(writer, 200, self._json({"schema_version": 1, "csrf": active[1].csrf, "workspace": self.workspace}), headers=common)
                return True
            params = parse_qs(request.query)
            session = (params.get("session") or [""])[0]
            if path == "/v1/web/session-view" and request.method == "GET":
                if not _SESSION_ID.fullmatch(session):
                    await server._write_response(writer, 400, self._json({"error": "invalid session"}), headers=common)
                    return True
                try:
                    payload = self.facade.web_snapshot(session)
                    data = self._json(payload)
                except Exception:  # noqa: BLE001 - facade failures map to a generic unavailable response
                    await server._write_response(writer, 404, self._json({"error": "session unavailable"}), headers=common)
                    return True
                if len(data) > MAX_WEB_RESPONSE:
                    await server._write_response(writer, 413, self._json({"error": "view too large"}), headers=common)
                else:
                    common.update({"Content-Type": "application/json"})
                    await server._write_response(writer, 200, data, headers=common)
                return True
            if path == "/v1/web/command" and request.method == "POST":
                try:
                    command = p.decode_command(request.body)
                    if isinstance(command, (p.Shutdown, getattr(p, "WebLaunch", p.Shutdown))):
                        await server._write_response(writer, 403, self._json({"error": "command unavailable to browser"}), headers=common)
                        return True
                    result = await self.facade.handle(command)
                    data = p.encode_result(result)
                except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
                    await server._write_response(writer, 400, self._json({"error": "malformed command"}), headers=common)
                    return True
                except Exception:  # noqa: BLE001 - never expose command/facade details to the browser
                    await server._write_response(writer, 500, self._json({"error": "internal error"}), headers=common)
                    return True
                if len(data) > MAX_WEB_RESPONSE:
                    await server._write_response(writer, 413, self._json({"error": "command result too large"}), headers=common)
                    return True
                common["Content-Type"] = "application/json"
                await server._write_response(writer, 200, data, headers=common)
                return True
            if path == "/v1/web/session-events" and request.method == "GET":
                if not _SESSION_ID.fullmatch(session):
                    await server._write_response(writer, 400, self._json({"error": "invalid session"}), headers=common)
                    return True
                try:
                    from_seq = int((params.get("from_seq") or ["0"])[0])
                    if from_seq < 0:
                        raise ValueError
                except ValueError:
                    await server._write_response(writer, 400, self._json({"error": "invalid from_seq"}), headers=common)
                    return True
                await self._stream(session, from_seq, writer, server)
                return True
            if path == "/v1/web/workspace-events" and request.method == "GET":
                await self._workspace_stream(writer, server)
                return True
            await server._write_response(writer, 404, self._json({"error": "not found"}), headers=common)
            return True
        if request.method not in ("GET", "HEAD"):
            await server._write_response(writer, 405, self._json({"error": "method not allowed"}), headers=common)
            return True
        relative = "index.html" if path == "/" or is_session_page else path.lstrip("/")
        root = WEB_ROOT.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
            data = target.read_bytes()
        except (ValueError, OSError):
            await server._write_response(writer, 404, self._json({"error": "not found"}), headers=common)
            return True
        common.update({"Content-Type": mimetypes.guess_type(target.name)[0] or "application/octet-stream", "Cache-Control": "no-cache"})
        await server._write_response(writer, 200, b"" if request.method == "HEAD" else data, headers=common)
        return True

    async def _stream(self, session, from_seq, writer, server):
        head = {**self._headers(), "Content-Type": "text/event-stream", "X-Accel-Buffering": "no"}
        if not await server._write_head(writer, 200, head):
            return
        if not await server._write_raw(writer, b"retry: 1000\n\n"):
            return
        iterator = self.facade.subscribe_web(session, from_seq).__aiter__()
        pending = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=server.keepalive)
                if not done:
                    if not await server._write_raw(writer, b": keepalive\n\n"):
                        break
                    continue
                try:
                    frame = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                seq = int(frame["seq"])
                body = msgspec.json.encode(frame)
                if len(body) > 256 * 1024:
                    data = msgspec.json.encode({"schema_version": 1, "session": session, "seq": seq, "resync": True})
                else:
                    data = body
                packet = b"id: " + str(seq).encode("ascii") + b"\nevent: view\ndata: " + data + b"\n\n"
                if not await server._write_raw(writer, packet):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - convert stream failures into a resync event
            await server._write_raw(writer, b"event: error\ndata: {" + b'"resync":true}' + b"\n\n")
        finally:
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            with contextlib.suppress(Exception):
                await iterator.aclose()

    async def _workspace_stream(self, writer, server):
        if not hasattr(self.facade, "subscribe_workspace"):
            await server._write_response(writer, 501, self._json({"error": "workspace stream unavailable"}), headers=self._headers())
            return
        head = {**self._headers(), "Content-Type": "text/event-stream", "X-Accel-Buffering": "no"}
        if not await server._write_head(writer, 200, head):
            return
        if not await server._write_raw(writer, b"retry: 1000\n\n"):
            return
        iterator = self.facade.subscribe_workspace().__aiter__()
        pending = None
        event_id = 0
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=server.keepalive)
                if not done:
                    if not await server._write_raw(writer, b": keepalive\n\n"):
                        break
                    continue
                try:
                    frame = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                event_id = int(frame.get("revision", event_id + 1))
                packet = b"id: " + str(event_id).encode("ascii") + b"\nevent: workspace\ndata: " + msgspec.json.encode(frame) + b"\n\n"
                if len(packet) > 512 * 1024 or not await server._write_raw(writer, packet):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001, S110 - disconnected workspace streams need no response
            pass
        finally:
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pending
            with contextlib.suppress(Exception):
                await iterator.aclose()


__all__ = ["COOKIE_NAME", "WEB_ROOT", "BrowserRoutes"]
