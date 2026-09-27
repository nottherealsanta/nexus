"""Browser launch tickets, cookie/CSRF auth, and projection HTTP routes."""
from __future__ import annotations

import asyncio
import contextlib

import msgspec
import pytest

import nexus.host.transports.http_sse as http_sse_module
import nexus.host.web as web_module
from nexus.host import protocol as p
from nexus.host.transports.http_sse import HTTPSSEServer


class _Facade:
    def __init__(self):
        self.commands = []
        self.large = False
        self.large_command = False

    async def handle(self, command):
        self.commands.append(command)
        if isinstance(command, p.SessionExport) and self.large_command:
            return p.SessionExportResult(
                session=command.session,
                format=command.format,
                content="x" * (web_module.MAX_WEB_RESPONSE + 1),
            )
        if isinstance(command, p.LogsRead):
            return p.LogsReadResult(
                daemon=p.DaemonLogPage(), session=p.SessionLogPage()
            )
        if isinstance(command, p.WorktreeList):
            return p.WorktreeListResult(worktrees=[{
                "child_id": "child-typed", "lifecycle": "finalized",
                "dirty": False, "review_id": "a" * 32,
                "digest": "b" * 64, "acknowledged": False,
            }])
        if isinstance(command, p.WorktreeReview):
            return p.WorktreeReviewResult(
                child_id=command.child_id, status="finalized",
                record={"child_id": command.child_id, "lifecycle": "finalized"},
                entries=[{"path": "src/example.py", "change": "modified"}],
                diff=[{"path": "src/example.py", "patch": "@@ -1 +1 @@\n-old\n+new"}],
                cursor=command.cursor, has_more=False,
                review_id="a" * 32, digest="b" * 64,
            )
        return p.HealthResult(sessions=1)

    def web_snapshot(self, session_id):
        if self.large:
            return {"schema_version": 1, "session": session_id, "seq": 0, "view": {"payload": "x" * (32 * 1024 * 1024 + 1)}}
        if ".." in session_id or session_id.startswith(("/", "\\")):
            raise ValueError("invalid session identifier")
        return {"schema_version": 1, "session": session_id, "seq": 0, "view": {"turns": []}}


async def _request(port, method, path, *, headers=None, body=b""):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    values = {"Host": f"127.0.0.1:{port}", "Connection": "close"}
    if body:
        values["Content-Length"] = str(len(body))
    values.update(headers or {})
    raw = [f"{method} {path} HTTP/1.1", *(f"{k}: {v}" for k, v in values.items()), "", ""]
    writer.write("\r\n".join(raw).encode("latin-1") + body)
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    response_headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            response_headers[key.lower()] = value.strip()
    length = int(response_headers.get("content-length", "0"))
    payload = await reader.readexactly(length) if length else b""
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return status, response_headers, payload


@pytest.mark.asyncio
async def test_browser_ticket_cookie_csrf_and_snapshot(monkeypatch):
    monkeypatch.setattr(web_module, "MAX_WEB_RESPONSE", 256)
    facade = _Facade()
    server = await HTTPSSEServer(facade, web_workspace="/workspace").start()
    try:
        origin = f"http://127.0.0.1:{server.port}"
        ticket = server._web.issue_ticket()
        status, headers, data = await _request(
            server.port,
            "POST",
            "/v1/web/ticket/redeem",
            headers={"Origin": origin, "Content-Type": "application/json"},
            body=msgspec.json.encode({"ticket": ticket}),
        )
        assert status == 200
        cookie = headers["set-cookie"].split(";", 1)[0]
        assert "HttpOnly" in headers["set-cookie"]
        assert "SameSite=Strict" in headers["set-cookie"]
        bootstrap = msgspec.json.decode(data)
        assert bootstrap["workspace"] == "/workspace"

        status, _, data = await _request(
            server.port,
            "GET",
            "/v1/web/session-view?session=demo",
            headers={"Cookie": cookie},
        )
        assert status == 200
        assert msgspec.json.decode(data) == {
            "schema_version": 1,
            "session": "demo",
            "seq": 0,
            "view": {"turns": []},
        }

        command = p.encode_command(p.Health())
        status, _, _ = await _request(
            server.port,
            "POST",
            "/v1/web/command",
            headers={"Cookie": cookie, "Origin": origin, "Content-Type": "application/json"},
            body=command,
        )
        assert status == 403
        status, _, data = await _request(
            server.port,
            "POST",
            "/v1/web/command",
            headers={"Cookie": cookie, "Origin": origin, "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
            body=command,
        )
        assert status == 200
        assert isinstance(p.decode_result(data), p.HealthResult)

        # Worktree projections include bounded review metadata and exceed this
        # test's deliberately tiny response limit.
        monkeypatch.setattr(web_module, "MAX_WEB_RESPONSE", 8192)
        for worktree_command, expected_type in (
            (p.WorktreeList(), p.WorktreeListResult),
            (p.WorktreeReview(child_id="child-typed", review_id="a" * 32, cursor=0, limit=8), p.WorktreeReviewResult),
        ):
            status, _, data = await _request(
                server.port, "POST", "/v1/web/command",
                headers={"Cookie": cookie, "Origin": origin,
                         "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
                body=p.encode_command(worktree_command),
            )
            assert status == 200
            assert isinstance(p.decode_result(data), expected_type)

        status, _, payload = await _request(
            server.port,
            "POST",
            "/v1/web/command",
            headers={"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "\xe9", "Content-Type": "application/json"},
            body=command,
        )
        assert status == 403
        assert b"forbidden" in payload

        oversized = p.encode_command(p.SessionExport(session="demo"))
        facade.large_command = True
        try:
            status, _, payload = await _request(
                server.port,
                "POST",
                "/v1/web/command",
                headers={"Cookie": cookie, "Origin": origin, "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
                body=oversized,
            )
        finally:
            facade.large_command = False
        assert status == 413 and b"command result too large" in payload

        logs_command = p.encode_command(p.LogsRead(session="demo"))
        for denied_headers, expected in (
            ({"Origin": origin, "Content-Type": "application/json"}, 401),
            ({"Cookie": cookie, "Origin": origin, "Content-Type": "application/json"}, 403),
            ({"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "wrong", "Content-Type": "application/json"}, 403),
            ({"Cookie": cookie, "Origin": "https://evil.example", "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"}, 403),
        ):
            status, _, _ = await _request(
                server.port, "POST", "/v1/web/command",
                headers=denied_headers, body=logs_command,
            )
            assert status == expected
        status, _, data = await _request(
            server.port,
            "POST",
            "/v1/web/command",
            headers={"Cookie": cookie, "Origin": origin, "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
            body=logs_command,
        )
        assert status == 200
        assert isinstance(p.decode_result(data), p.LogsReadResult)

        for bad_headers in (
            {"Cookie": cookie, "Origin": "https://evil.example", "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
            {"Cookie": cookie, "Origin": origin, "X-CSRF-Token": "wrong", "Content-Type": "application/json"},
            {"Cookie": cookie, "Origin": origin, "Content-Type": "application/json"},
            {"Origin": origin, "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
            {"Cookie": "nexus_web=invalid", "Origin": origin, "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
        ):
            status, _, _ = await _request(server.port, "POST", "/v1/web/command", headers=bad_headers, body=command)
            assert status in (401, 403)

        for forbidden in (p.Shutdown(reason="no"), p.WebLaunch()):
            status, _, _ = await _request(server.port, "POST", "/v1/web/command",
                headers={"Cookie": cookie, "Origin": origin, "X-CSRF-Token": bootstrap["csrf"], "Content-Type": "application/json"},
                body=p.encode_command(forbidden))
            assert status == 403
        assert facade.commands == [
            p.Health(),
            p.WorktreeList(),
            p.WorktreeReview(child_id="child-typed", review_id="a" * 32, cursor=0, limit=8),
            p.SessionExport(session="demo"),
            p.LogsRead(session="demo"),
        ]

        status, _, data = await _request(server.port, "GET", "/v1/web/session-view?session=..%2F..%2Fetc%2Fpasswd", headers={"Cookie": cookie})
        assert status in (400, 404)
        assert b"/etc/passwd" not in data
        facade.large = True
        try:
            status, _, payload = await _request(server.port, "GET", "/v1/web/session-view?session=large", headers={"Cookie": cookie})
        finally:
            facade.large = False
        assert status == 413 and b"view too large" in payload

        # A ticket is consumed atomically; replay cannot mint another cookie.
        status, _, _ = await _request(
            server.port,
            "POST",
            "/v1/web/ticket/redeem",
            headers={"Origin": origin, "Content-Type": "application/json"},
            body=msgspec.json.encode({"ticket": ticket}),
        )
        assert status == 401
    finally:
        await server.aclose()






@pytest.mark.asyncio
async def test_static_app_and_deep_link_are_same_origin_hardened():
    server = await HTTPSSEServer(_Facade()).start()
    try:
        status, headers, body = await _request(server.port, "GET", "/s/session-1")
        assert status == 200
        assert b"Nexus" in body
        assert "default-src 'self'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        status, headers, body = await _request(server.port, "GET", "/styles/tokens.css")
        assert status == 200 and b"--" in body
        assert headers["cache-control"] == "no-cache"
        status, _, _ = await _request(server.port, "GET", "/styles/../../etc/passwd")
        assert status == 404
        ticket = server._web.issue_ticket()
        origin = f"http://127.0.0.1:{server.port}"
        status, headers, payload = await _request(
            server.port, "POST", "/v1/web/ticket/redeem",
            headers={"Origin": origin, "Content-Type": "application/json"},
            body=msgspec.json.encode({"ticket": ticket}),
        )
        assert status == 200
        cookie = headers["set-cookie"].split(";", 1)[0]
        csrf = msgspec.json.decode(payload)["csrf"]
        status, logout_headers, _ = await _request(
            server.port, "POST", "/v1/web/logout",
            headers={"Cookie": cookie, "Origin": origin, "X-CSRF-Token": csrf},
        )
        assert status == 204 and "Max-Age=0" in logout_headers["set-cookie"]
        status, _, _ = await _request(server.port, "GET", "/v1/web/bootstrap", headers={"Cookie": cookie})
        assert status == 401
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_peer_http_rejects_oversized_serialized_result(monkeypatch):
    monkeypatch.setattr(web_module, "MAX_WEB_RESPONSE", 256)
    monkeypatch.setattr(http_sse_module, "MAX_WEB_RESPONSE", 256)
    facade = _Facade()
    facade.large_command = True
    server = await HTTPSSEServer(facade).start()
    try:
        status, _, payload = await _request(
            server.port,
            "POST",
            "/v1/command",
            headers={
                "Authorization": f"Bearer {server.token}",
                "Origin": f"http://127.0.0.1:{server.port}",
                "Content-Type": "application/json",
            },
            body=p.encode_command(p.SessionExport(session="demo")),
        )
        assert status == 413 and b"command result too large" in payload
    finally:
        await server.aclose()
