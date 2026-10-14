"""The gateway over raw HTTP: a real listener, the real auth store, a fake upstream host."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass

import httpx
import msgspec
import pytest

from nexus.gateway.app import GatewayApp, Limits, public_hosts
from nexus.gateway.httpd import HTTPServer
from nexus.gateway.store import SCOPES, GatewayError, GatewayStore
from nexus.gateway.upstream import UpstreamError, UpstreamResponse
from nexus.host import protocol as p
from nexus.session.manager import SessionSummary

_DEFAULT = object()


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


# -- fake upstream ---------------------------------------------------------------------


class FakeStream:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def aiter_bytes(self):
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk


class FakeUpstream:
    """Stands in for ``Upstream``: records calls and answers like a workspace host."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.commands: list[tuple[str, object, bool]] = []
        #: command type name -> a Result struct, or ``(status, body bytes)``
        self.results: dict[str, object] = {}
        #: command type name -> exception raised by ``request``
        self.errors: dict[str, Exception] = {}
        self.sessions = [SessionSummary(id="s1", title="First")]
        self.view = (200, json.dumps({"schema_version": 1, "session": "s1", "seq": 0, "view": {"turns": []}}).encode())
        self.voice = (200, b'{"type":"VoiceTranscribeResult","text":"hello"}')
        self.stream_queue: asyncio.Queue = asyncio.Queue()
        self.stream_opens: list[tuple[str, str, dict]] = []
        self.stream_error: Exception | None = None

    @staticmethod
    def _reply(status: int, body: bytes) -> UpstreamResponse:
        return UpstreamResponse(status, httpx.Headers(), body)

    async def request(self, workspace, method, path, *, params=None, content=None,
                      content_type="application/json", idempotent, max_body=0):
        self.calls.append({
            "workspace": workspace, "method": method, "path": path, "params": params,
            "content": content, "content_type": content_type, "idempotent": idempotent,
        })
        if path == "/v1/web/command":
            command = p.decode_command(content)
            name = type(command).__name__
            self.commands.append((workspace, command, idempotent))
            if name in self.errors:
                raise self.errors[name]
            if name in self.results:
                value = self.results[name]
                if isinstance(value, tuple):
                    return self._reply(*value)
                return self._reply(200, p.encode_result(value))
            if isinstance(command, p.SessionOpen):
                return self._reply(200, p.encode_result(p.SessionOpenResult(session=SessionSummary(id=command.session))))
            if isinstance(command, p.SessionList):
                return self._reply(200, p.encode_result(p.SessionListResult(sessions=self.sessions)))
            if isinstance(command, p.LiveConsent):
                return self._reply(200, p.encode_result(p.LiveConsentResult(accepted=bool(command.accept))))
            return self._reply(200, p.encode_result(p.HealthResult()))
        if path == "/v1/web/session-view":
            return self._reply(*self.view)
        if path == "/v1/web/voice":
            return self._reply(*self.voice)
        return self._reply(404, b"{}")

    @contextlib.asynccontextmanager
    async def stream(self, workspace, path, params):
        self.stream_opens.append((workspace, path, dict(params)))
        if self.stream_error is not None:
            raise self.stream_error
        yield FakeStream(self.stream_queue)

    def push(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self.stream_queue.put_nowait(chunk)

    def end(self) -> None:
        self.stream_queue.put_nowait(None)

    def commands_of(self, kind: type) -> list:
        return [command for _, command, _ in self.commands if isinstance(command, kind)]


# -- raw HTTP client ---------------------------------------------------------------------


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    body: bytes = b""

    def json(self):
        return json.loads(self.body)


def parse_response(data: bytes) -> Resp:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return Resp(int(lines[0].split()[1]), headers, body)


def build(method, target, *, host, token=None, content_type=None, body=b"", headers=None) -> bytes:
    lines = [f"{method} {target} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host}")
    lines.append("Connection: close")
    if token:
        lines.append(f"Authorization: Bearer {token}")
    if content_type:
        lines.append(f"Content-Type: {content_type}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
    lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def exchange(port: int, raw: bytes, *, timeout: float = 5.0) -> Resp:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(-1), timeout)
        except ConnectionError:
            data = b""
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return parse_response(data)


class SSEClient:
    def __init__(self, reader, writer, status: int, headers: dict[str, str]) -> None:
        self.reader, self.writer, self.status, self.headers = reader, writer, status, headers

    async def frame(self, timeout: float = 3.0) -> bytes:
        return await asyncio.wait_for(self.reader.readuntil(b"\n\n"), timeout)

    async def rest(self, timeout: float = 3.0) -> bytes:
        return await asyncio.wait_for(self.reader.read(-1), timeout)

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


# -- rig ----------------------------------------------------------------------------------


class Rig:
    def __init__(self, tmp_path, *, scopes=SCOPES, device_hosts: int = 1, host_count: int = 2,
                 limits: Limits | None = None, public_url: str = "", server_kwargs=None) -> None:
        self.tmp_path = tmp_path
        self.scopes = scopes
        self.device_hosts = device_hosts
        self.host_count = host_count
        self.limits = limits or Limits(recheck=0.05, keepalive=30.0)
        self.public_url = public_url
        self.server_kwargs = server_kwargs or {}
        self.clock = Clock()
        self.upstream = FakeUpstream()

    async def __aenter__(self) -> Rig:
        self.store = GatewayStore(self.tmp_path / "gateway", clock=self.clock)
        self.hosts = []
        for index in range(self.host_count):
            workspace = self.tmp_path / f"ws{index}"
            workspace.mkdir()
            self.hosts.append(self.store.allow(workspace))
        self.device, self.token = self.pair()
        self.app = GatewayApp(
            self.store, self.upstream, public_url=self.public_url, probe=lambda path: "running",
            limits=self.limits, clock=self.clock,
        )
        self.server = HTTPServer(self.app, **self.server_kwargs)
        await self.server.start()
        self.app.bind_port(self.server.port)
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.server.aclose()

    def pair(self, scopes=None, hosts=None):
        chosen = [h.host_id for h in self.hosts[: self.device_hosts]] if hosts is None else hosts
        code, _ = self.store.create_pairing(scopes=scopes or self.scopes, host_ids=chosen)
        return self.store.redeem(code, "Test phone")

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def host_id(self) -> str:
        return self.hosts[0].host_id

    def raw(self, method, target, *, token=_DEFAULT, host=_DEFAULT, **kwargs) -> bytes:
        return build(
            method, target,
            host=f"127.0.0.1:{self.port}" if host is _DEFAULT else host,
            token=self.token if token is _DEFAULT else token, **kwargs,
        )

    async def request(self, method, target, **kwargs) -> Resp:
        return await exchange(self.port, self.raw(method, target, **kwargs))

    async def command(self, command, *, host_id=None, **kwargs) -> Resp:
        return await self.request(
            "POST", f"/v1/hosts/{host_id or self.host_id}/command",
            content_type="application/json", body=p.encode_command(command), **kwargs,
        )

    async def sse(self, session="s1", *, query="", token=_DEFAULT, headers=None, host_id=None) -> SSEClient:
        target = f"/v1/hosts/{host_id or self.host_id}/sessions/{session}/events{query}"
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(self.raw("GET", target, token=token, headers=headers))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        parsed = parse_response(head)
        return SSEClient(reader, writer, parsed.status, parsed.headers)


def assert_error(resp: Resp, status: int, code: str) -> None:
    assert resp.status == status, resp.body
    body = resp.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == code


async def eventually(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


# -- health, headers, host, origin ---------------------------------------------------------


async def test_health_needs_no_credential_and_carries_security_headers(tmp_path):
    async with Rig(tmp_path) as rig:
        resp = await rig.request("GET", "/healthz", token=None)
    assert resp.status == 200 and resp.json() == {"ok": True, "version": 1}
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["content-security-policy"].startswith("default-src 'none'")
    assert resp.headers["content-type"] == "application/json"


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1", "127.0.0.1:1", "localhost", "0.0.0.0:8765", "", None])
async def test_an_unexpected_host_header_is_refused_before_anything_else(tmp_path, host):
    async with Rig(tmp_path) as rig:
        for route in ("/healthz", "/v1/me"):
            assert_error(await rig.request("GET", route, host=host), 421, "invalid_host")
        assert_error(await rig.request("POST", "/v1/pair", host=host, token=None, body=b"{}"), 421, "invalid_host")
    assert rig.upstream.calls == []


async def test_loopback_names_with_the_bound_port_are_accepted(tmp_path):
    async with Rig(tmp_path) as rig:
        for host in (f"127.0.0.1:{rig.port}", f"localhost:{rig.port}", f"LOCALHOST:{rig.port}"):
            assert (await rig.request("GET", "/healthz", host=host)).status == 200


async def test_the_public_https_name_is_served_but_other_ports_are_not(tmp_path):
    async with Rig(tmp_path, public_url="https://gw.example.ts.net") as rig:
        for host in ("gw.example.ts.net", "gw.example.ts.net:443", "GW.Example.TS.net"):
            assert (await rig.request("GET", "/v1/me", host=host)).status == 200
        assert_error(await rig.request("GET", "/v1/me", host="gw.example.ts.net:8443"), 421, "invalid_host")
        assert_error(await rig.request("GET", "/v1/me", host="other.ts.net"), 421, "invalid_host")


def test_public_url_is_validated():
    assert public_hosts("") == frozenset()
    assert public_hosts("https://h.ts.net") == {"h.ts.net", "h.ts.net:443"}
    assert public_hosts("https://h.ts.net/") == {"h.ts.net", "h.ts.net:443"}
    assert public_hosts("https://h.ts.net:8443") == {"h.ts.net", "h.ts.net:8443"}
    for bad in (
        "http://h.ts.net", "https://", "https://u:p@h.ts.net", "https://h.ts.net/path",
        "https://h.ts.net?x=1", "https://h.ts.net#f", "ftp://h.ts.net", "h.ts.net", "https://h.ts.net:notaport",
        "https://-bad-.ts.net", "https://a b.ts.net",
    ):
        with pytest.raises(GatewayError):
            public_hosts(bad)


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1", ""])
async def test_any_origin_header_is_refused_because_native_clients_send_none(tmp_path, origin):
    async with Rig(tmp_path) as rig:
        for method, route in (("GET", "/healthz"), ("GET", "/v1/me"), ("POST", "/v1/pair")):
            resp = await rig.request(method, route, headers={"Origin": origin})
            assert_error(resp, 403, "origin_forbidden")


# -- routing ----------------------------------------------------------------------------------


async def test_unknown_routes_and_wrong_methods(tmp_path):
    async with Rig(tmp_path) as rig:
        assert_error(await rig.request("GET", "/"), 404, "not_found")
        assert_error(await rig.request("GET", "/v1"), 404, "not_found")
        assert_error(await rig.request("GET", "/v1/web/session-view?session=s1"), 404, "not_found")
        assert_error(await rig.request("GET", "/v1/hosts/NOTHEX/sessions"), 404, "not_found")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/bad%20id/view"), 404, "not_found")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/shell"), 404, "not_found")
        for method, route, allow in (
            ("POST", "/healthz", "GET"), ("DELETE", "/v1/me", "GET"), ("GET", "/v1/pair", "POST"),
            ("PUT", "/v1/hosts", "GET"), ("GET", f"/v1/hosts/{rig.host_id}/command", "POST"),
            ("GET", f"/v1/hosts/{rig.host_id}/voice", "POST"), ("TRACE", "/healthz", "GET"),
        ):
            resp = await rig.request(method, route)
            assert_error(resp, 405, "method_not_allowed")
            assert resp.headers["allow"] == allow
    assert rig.upstream.calls == []


# -- authentication -----------------------------------------------------------------------------


async def test_every_non_public_route_needs_a_valid_bearer_token(tmp_path):
    async with Rig(tmp_path) as rig:
        host = rig.host_id
        routes = [
            ("GET", "/v1/me"), ("GET", "/v1/hosts"), ("GET", f"/v1/hosts/{host}/sessions"),
            ("GET", f"/v1/hosts/{host}/sessions/s1/view"), ("GET", f"/v1/hosts/{host}/sessions/s1/events"),
            ("POST", f"/v1/hosts/{host}/command"), ("POST", f"/v1/hosts/{host}/voice"),
        ]
        wrong = [None, "", "garbage", rig.token + "x", "x" * 300]
        for method, route in routes:
            for token in wrong:
                resp = await rig.request(method, route, token=token)
                assert_error(resp, 401, "unauthorized")
                assert resp.headers["www-authenticate"].startswith("Bearer")
                if token:
                    assert token not in resp.body.decode()
        # the token is accepted only as a Bearer header: not as another scheme, nor in the URL
        assert_error(await rig.request("GET", "/v1/me", token=None, headers={"Authorization": f"Basic {rig.token}"}), 401, "unauthorized")
        assert_error(await rig.request("GET", "/v1/me", token=None, headers={"Authorization": rig.token}), 401, "unauthorized")
        assert_error(await rig.request("GET", f"/v1/me?token={rig.token}", token=None), 401, "unauthorized")
        assert_error(await rig.request("GET", "/v1/me", token=None, headers={"Cookie": f"token={rig.token}"}), 401, "unauthorized")
    assert rig.upstream.calls == []


async def test_me_reports_the_device(tmp_path):
    async with Rig(tmp_path, scopes=("read", "voice")) as rig:
        resp = await rig.request("GET", "/v1/me")
    body = resp.json()
    assert set(body) == {"device_id", "name", "scopes", "expires_at"}
    assert body["device_id"] == rig.device.device_id
    assert body["name"] == "Test phone"
    assert body["scopes"] == ["read", "voice"]
    assert body["expires_at"] == int(rig.device.expires_at) and isinstance(body["expires_at"], int)
    assert rig.token not in resp.body.decode()


async def test_hosts_lists_only_this_devices_workspaces(tmp_path):
    async with Rig(tmp_path, device_hosts=1, host_count=2) as rig:
        resp = await rig.request("GET", "/v1/hosts")
        assert resp.status == 200
        rows = resp.json()
        assert isinstance(rows, list) and len(rows) == 1
        assert set(rows[0]) == {"host_id", "workspace", "label", "state"}
        assert rows[0]["host_id"] == rig.hosts[0].host_id and rows[0]["state"] == "running"
        assert rows[0]["workspace"] == rig.hosts[0].workspace
        other = await rig.request("GET", f"/v1/hosts/{rig.hosts[1].host_id}/sessions")
        assert_error(other, 404, "unknown_host")  # allowed on the gateway, but not for this device
        assert_error(await rig.request("GET", f"/v1/hosts/{'0' * 16}/sessions"), 404, "unknown_host")

        wide = rig.pair(hosts=[h.host_id for h in rig.hosts])[1]
        assert len((await rig.request("GET", "/v1/hosts", token=wide)).json()) == 2
    assert rig.upstream.calls == []


# -- pairing --------------------------------------------------------------------------------------


def pair_body(code, name="iPhone") -> bytes:
    return msgspec.json.encode({"code": code, "device_name": name})


async def test_pairing_redeems_a_single_use_code_into_a_token(tmp_path):
    async with Rig(tmp_path) as rig:
        code, _ = rig.store.create_pairing(scopes="read,prompt", name="label")
        resp = await rig.request("POST", "/v1/pair", token=None, content_type="application/json", body=pair_body(code))
        assert resp.status == 200
        body = resp.json()
        assert set(body) == {"device_id", "token", "scopes", "expires_at", "gateway_id"}
        assert body["scopes"] == ["read", "prompt"]
        assert isinstance(body["expires_at"], int) and body["expires_at"] > rig.clock.now
        assert body["gateway_id"] == rig.store.gateway_id
        assert resp.headers["cache-control"] == "no-store"

        me = await rig.request("GET", "/v1/me", token=body["token"])
        assert me.status == 200 and me.json()["name"] == "iPhone" and me.json()["device_id"] == body["device_id"]

        again = await rig.request("POST", "/v1/pair", token=None, body=pair_body(code))
        assert_error(again, 403, "invalid_code")
        assert body["token"] not in again.body.decode()
        assert body["token"] not in (rig.store.root / "state.json").read_text()


async def test_pairing_accepts_a_typed_code_without_content_type(tmp_path):
    async with Rig(tmp_path) as rig:
        code, _ = rig.store.create_pairing()
        resp = await rig.request("POST", "/v1/pair", token=None, body=pair_body(code.lower().replace("-", " "), ""))
        assert resp.status == 200


async def test_pairing_rejects_bad_requests_without_counting_them_as_guesses(tmp_path):
    async with Rig(tmp_path) as rig:
        for body in (b"", b"not json", b"[]", b"{}", b'{"code": 5}', b'{"code": "x", "device_name": 5}',
                     msgspec.json.encode({"code": "A" * 65})):
            assert_error(await rig.request("POST", "/v1/pair", token=None, body=body), 400, "bad_request")
        for _ in range(12):  # none of the above counted toward the backoff
            assert (await rig.request("POST", "/v1/pair", token=None, body=b"nope")).status == 400
        assert_error(
            await rig.request("POST", "/v1/pair", token=None, content_type="text/plain", body=b"{}"),
            415, "unsupported_media_type",
        )
        big = await rig.request("POST", "/v1/pair", token=None, body=b"x" * 4097)
        assert_error(big, 413, "body_too_large")


async def test_failed_pairing_attempts_back_off_then_recover(tmp_path):
    async with Rig(tmp_path) as rig:
        code, _ = rig.store.create_pairing()
        for _ in range(rig.limits.pair_failures):
            assert_error(await rig.request("POST", "/v1/pair", token=None, body=pair_body("WRONG-WRONG")), 403, "invalid_code")
        # blocked: even the real code is refused, and it is not consumed
        blocked = await rig.request("POST", "/v1/pair", token=None, body=pair_body(code))
        assert_error(blocked, 429, "rate_limited")
        assert int(blocked.headers["retry-after"]) >= 1
        assert rig.store.pending_count() == 1
        rig.clock.now += rig.limits.pair_window + 1
        ok = await rig.request("POST", "/v1/pair", token=None, body=pair_body(code))
        assert ok.status == 200


async def test_expired_code_is_refused(tmp_path):
    async with Rig(tmp_path) as rig:
        code, _ = rig.store.create_pairing()
        rig.clock.now += 121
        assert_error(await rig.request("POST", "/v1/pair", token=None, body=pair_body(code)), 403, "invalid_code")


# -- revocation and scopes ---------------------------------------------------------------------------


async def test_revoking_a_device_takes_effect_on_the_next_request(tmp_path):
    async with Rig(tmp_path) as rig:
        assert (await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions")).status == 200
        rig.store.revoke(rig.device.device_id)
        for method, route in (("GET", "/v1/me"), ("GET", "/v1/hosts"), ("GET", f"/v1/hosts/{rig.host_id}/sessions")):
            assert_error(await rig.request(method, route), 401, "unauthorized")
        assert_error(await rig.command(p.Health()), 401, "unauthorized")
    assert len(rig.upstream.calls) == 1


async def test_an_expired_device_is_refused(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.clock.now = rig.device.expires_at + 1
        assert_error(await rig.request("GET", "/v1/me"), 401, "unauthorized")


async def test_disallowing_a_workspace_cuts_access_to_it(tmp_path):
    async with Rig(tmp_path) as rig:
        assert (await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions")).status == 200
        rig.store.disallow(rig.host_id)
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions"), 404, "unknown_host")
        assert_error(await rig.command(p.Health()), 404, "unknown_host")
        assert (await rig.request("GET", "/v1/hosts")).json() == []


async def test_scopes_gate_each_route_before_the_body_is_read(tmp_path):
    async with Rig(tmp_path, scopes=("prompt",)) as rig:
        host = rig.host_id
        for method, route in (
            ("GET", "/v1/hosts"), ("GET", f"/v1/hosts/{host}/sessions"),
            ("GET", f"/v1/hosts/{host}/sessions/s1/view"), ("GET", f"/v1/hosts/{host}/sessions/s1/events"),
        ):
            assert_error(await rig.request(method, route), 403, "scope_required")
        voice = await rig.request(
            "POST", f"/v1/hosts/{host}/voice?request_id={uuid.uuid4()}", content_type="audio/wav", body=b"RIFF",
        )
        assert_error(voice, 403, "scope_required")
        assert (await rig.request("GET", "/v1/me")).status == 200
    assert rig.upstream.calls == []


# -- sessions and view ----------------------------------------------------------------------------------


async def test_sessions_lists_through_the_host_session_list_command(tmp_path):
    async with Rig(tmp_path) as rig:
        resp = await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions")
    assert resp.status == 200
    body = resp.json()
    assert body["type"] == "SessionListResult"
    assert [s["id"] for s in body["sessions"]] == ["s1"] and body["truncated"] is False
    workspace, command, idempotent = rig.upstream.commands[0]
    assert workspace == rig.hosts[0].workspace and isinstance(command, p.SessionList) and idempotent is True
    assert rig.upstream.calls[0]["path"] == "/v1/web/command" and rig.upstream.calls[0]["method"] == "POST"


async def test_sessions_are_bounded(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.sessions = [SessionSummary(id=f"s{i}") for i in range(501)]
        body = (await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions")).json()
    assert len(body["sessions"]) == 500 and body["truncated"] is True


async def test_sessions_surface_host_failures_as_a_generic_error(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.results["SessionList"] = p.ErrorResult(message="boom /secret/path")
        resp = await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions")
        assert_error(resp, 502, "host_unavailable")
        assert "secret" not in resp.body.decode()
        rig.upstream.results["SessionList"] = (500, b"traceback...")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions"), 502, "host_unavailable")
        rig.upstream.errors["SessionList"] = UpstreamError("host_unavailable", 502)
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions"), 502, "host_unavailable")


async def test_view_opens_an_existing_session_then_returns_the_host_snapshot_unchanged(tmp_path):
    async with Rig(tmp_path) as rig:
        resp = await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/view")
    assert resp.status == 200 and resp.headers["content-type"] == "application/json"
    assert resp.body == rig.upstream.view[1]
    opened = rig.upstream.commands_of(p.SessionOpen)
    assert len(opened) == 1 and opened[0].session == "s1" and opened[0].create is False
    view_call = rig.upstream.calls[1]
    assert (view_call["method"], view_call["path"], view_call["params"]) == ("GET", "/v1/web/session-view", {"session": "s1"})
    assert view_call["idempotent"] is True


async def test_view_of_an_unknown_session_never_creates_it(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.results["SessionOpen"] = p.ErrorResult(message="no such session")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/ghost/view"), 404, "session_unavailable")
        rig.upstream.results.pop("SessionOpen")
        rig.upstream.view = (404, b"{}")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/view"), 404, "session_unavailable")
        rig.upstream.view = (500, b"{}")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/view"), 502, "host_unavailable")
    assert all(c.create is False for c in rig.upstream.commands_of(p.SessionOpen))


# -- commands -----------------------------------------------------------------------------------------------


async def test_a_command_is_forwarded_and_the_host_result_returned(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.results["SessionStart"] = p.SessionStartResult(session="s1", turn_id="t9")
        resp = await rig.command(p.SessionStart(session="s1", content="hello"))
    assert resp.status == 200 and resp.headers["content-type"] == "application/json"
    result = p.decode_result(resp.body)
    assert isinstance(result, p.SessionStartResult) and result.turn_id == "t9"
    workspace, command, idempotent = rig.upstream.commands[0]
    assert workspace == rig.hosts[0].workspace and command.content == "hello"
    assert idempotent is False  # a prompt is never retried


async def test_reads_are_marked_retryable_and_writes_are_not(tmp_path):
    async with Rig(tmp_path) as rig:
        await rig.command(p.Health())
        await rig.command(p.LiveConsent())
        await rig.command(p.LiveConsent(accept=True))
        await rig.command(p.SessionCancel(session="s1"))
    assert [idempotent for _, _, idempotent in rig.upstream.commands] == [True, True, False, False]


async def test_commands_outside_the_allow_list_never_reach_the_host(tmp_path):
    async with Rig(tmp_path) as rig:
        for command in (
            p.ProjectSessionsList(), p.ProjectSessionOpen(workspace="/tmp", session="s"),
            p.SessionShell(session="s1", command="id"), p.SettingsInventory(scope="global"),
            p.ProviderKeySet(provider="openai", key="sk-TOPSECRETKEY"), p.WebLaunch(), p.Shutdown(),
            p.SessionDelete(session="s1"), p.LogsRead(),
        ):
            resp = await rig.command(command)
            assert_error(resp, 403, "command_denied")
            assert "TOPSECRETKEY" not in resp.body.decode()
    assert rig.upstream.calls == []


async def test_command_scopes_are_enforced_per_command(tmp_path):
    async with Rig(tmp_path, scopes=("read",)) as rig:
        assert (await rig.command(p.SessionList())).status == 200
        assert (await rig.command(p.LiveConsent())).status == 200
        for command in (
            p.SessionStart(session="s1", content="x"),
            p.PermissionResolve(session="s1", request_id="r1", decision="allow"),
            p.LiveConsent(accept=True),
            p.LiveStart(session="s1", offer_sdp="v=0"),
            p.VoiceCancel(request_id="abc"),
        ):
            assert_error(await rig.command(command), 403, "scope_required")
    assert len(rig.upstream.commands) == 2


async def test_live_commands_need_the_live_scope(tmp_path):
    async with Rig(tmp_path, scopes=("read", "live")) as rig:
        for command in (
            p.LiveStart(session="s1", offer_sdp="v=0"), p.LiveMute(session="s1", muted=True),
            p.LiveStop(session="s1"), p.LiveHelperEvent(session="s1", kind="ready"),
            p.LiveConsent(accept=True), p.LiveConsent(accept=False), p.LiveConsent(), p.LiveStatus(),
        ):
            assert (await rig.command(command)).status == 200, command
    assert len(rig.upstream.commands) == 8


async def test_permission_lease_is_attributed_to_the_paired_device(tmp_path):
    async with Rig(tmp_path) as rig:
        resp = await rig.command(p.PermissionResolve(session="s1", request_id="r1", decision="allow", client_id="spoofed"))
    assert resp.status == 200
    forwarded = rig.upstream.commands_of(p.PermissionResolve)[0]
    assert forwarded.client_id == f"gateway:{rig.device.device_id}"


async def test_malformed_and_out_of_bounds_commands_are_rejected_without_echo(tmp_path):
    async with Rig(tmp_path) as rig:
        host = rig.host_id
        for body in (b"", b"not json", b"[]", b'{"type":"NoSuchCommand"}', b'{"type":"SessionStart"}'):
            resp = await rig.request("POST", f"/v1/hosts/{host}/command", content_type="application/json", body=body)
            assert_error(resp, 400, "malformed_command")
        marker = "TOP-SECRET-MARKER"
        resp = await rig.command(p.SessionStart(session="../" + marker, content="x"))
        assert_error(resp, 400, "invalid_command")
        assert marker not in resp.body.decode()
        resp = await rig.command(p.SessionStart(session="s1", content="x" * (256 * 1024 + 1)))
        assert_error(resp, 400, "invalid_command")
        resp = await rig.command(p.SessionStart(session="s1", content="x", attachments=[marker]))
        assert_error(resp, 400, "invalid_command")
        assert marker not in resp.body.decode()
    assert rig.upstream.calls == []


async def test_command_content_type_and_size_are_enforced_before_the_body_is_read(tmp_path):
    async with Rig(tmp_path) as rig:
        host = rig.host_id
        body = p.encode_command(p.Health())
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded"):
            resp = await rig.request("POST", f"/v1/hosts/{host}/command", content_type=content_type, body=body)
            assert_error(resp, 415, "unsupported_media_type")
        assert (await rig.request("POST", f"/v1/hosts/{host}/command", content_type="Application/JSON; charset=utf-8", body=body)).status == 200
        # a declared oversize body is refused from the headers alone
        head = rig.raw("POST", f"/v1/hosts/{host}/command", content_type="application/json").replace(
            b"\r\n\r\n", b"\r\nContent-Length: 1048577\r\n\r\n", 1,
        )
        assert_error(await exchange(rig.port, head), 413, "body_too_large")
    assert len(rig.upstream.calls) == 1


async def test_host_outcomes_map_to_stable_gateway_errors(tmp_path):
    async with Rig(tmp_path) as rig:
        command = p.SessionStart(session="s1", content="x")
        rig.upstream.results["SessionStart"] = (400, b"{}")
        assert_error(await rig.command(command), 400, "host_rejected")
        rig.upstream.results["SessionStart"] = (403, b"{}")
        assert_error(await rig.command(command), 400, "host_rejected")
        rig.upstream.results["SessionStart"] = (413, b"{}")
        assert_error(await rig.command(command), 502, "response_too_large")
        rig.upstream.results["SessionStart"] = (500, b"Traceback (most recent call last)")
        resp = await rig.command(command)
        assert_error(resp, 502, "host_unavailable")
        assert "Traceback" not in resp.body.decode()
        rig.upstream.results.pop("SessionStart")
        rig.upstream.errors["SessionStart"] = UpstreamError("upstream_auth", 503)
        assert_error(await rig.command(command), 503, "upstream_auth")


async def test_gateway_never_retries_a_command_whose_outcome_is_unknown(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.errors["SessionStart"] = UpstreamError("host_unavailable", 502)
        resp = await rig.command(p.SessionStart(session="s1", content="send once"))
        assert_error(resp, 502, "host_unavailable")
    assert len(rig.upstream.commands_of(p.SessionStart)) == 1


async def test_host_error_results_are_redacted_and_bounded(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.results["Health"] = p.ErrorResult(
            kind="bad kind!\n", message="see https://user:hunter2@example.com/x " + "y" * 1000,
        )
        resp = await rig.command(p.Health())
    result = p.decode_result(resp.body)
    assert isinstance(result, p.ErrorResult)
    assert result.kind == "badkind"
    assert "hunter2" not in result.message and len(result.message) <= 400


# -- voice ----------------------------------------------------------------------------------------------------


async def test_voice_is_forwarded_once_with_the_wav_body(tmp_path):
    request_id = str(uuid.uuid4())
    audio = b"RIFF" + b"\x00" * 64
    async with Rig(tmp_path) as rig:
        resp = await rig.request(
            "POST", f"/v1/hosts/{rig.host_id}/voice?request_id={request_id}",
            content_type="audio/wav", body=audio,
        )
    assert resp.status == 200 and resp.json()["text"] == "hello"
    call = rig.upstream.calls[0]
    assert (call["method"], call["path"], call["params"]) == ("POST", "/v1/web/voice", {"request_id": request_id})
    assert call["content"] == audio and call["content_type"] == "audio/wav"
    assert call["idempotent"] is False


async def test_voice_validates_request_id_content_type_and_body(tmp_path):
    good = str(uuid.uuid4())
    async with Rig(tmp_path) as rig:
        route = f"/v1/hosts/{rig.host_id}/voice"
        for query in ("", "?request_id=", "?request_id=abc", f"?request_id={good}&request_id={good}",
                      f"?request_id={good.upper()}x", "?request_id=" + "a" * 40):
            resp = await rig.request("POST", route + query, content_type="audio/wav", body=b"RIFF")
            assert_error(resp, 400, "bad_request")
        assert_error(
            await rig.request("POST", f"{route}?request_id={good}", content_type="audio/wav"), 400, "bad_request",
        )
        for content_type in (None, "application/json", "audio/mpeg"):
            resp = await rig.request("POST", f"{route}?request_id={good}", content_type=content_type, body=b"RIFF")
            assert_error(resp, 415, "unsupported_media_type")
        head = rig.raw("POST", f"{route}?request_id={good}", content_type="audio/wav").replace(
            b"\r\n\r\n", b"\r\nContent-Length: 8388609\r\n\r\n", 1,
        )
        assert_error(await exchange(rig.port, head), 413, "body_too_large")
    assert rig.upstream.calls == []


async def test_voice_host_outcomes(tmp_path):
    request_id = str(uuid.uuid4())
    async with Rig(tmp_path) as rig:
        async def send():
            return await rig.request(
                "POST", f"/v1/hosts/{rig.host_id}/voice?request_id={request_id}",
                content_type="audio/wav", body=b"RIFF",
            )

        rig.upstream.voice = (400, b"{}")
        assert_error(await send(), 400, "host_rejected")
        rig.upstream.voice = (415, b"{}")
        assert_error(await send(), 400, "host_rejected")
        rig.upstream.voice = (413, b"{}")
        assert_error(await send(), 413, "body_too_large")
        rig.upstream.voice = (500, b"{}")
        assert_error(await send(), 502, "host_unavailable")
    assert len(rig.upstream.calls) == 4  # never retried


# -- event stream ---------------------------------------------------------------------------------------------------

FRAME1 = b'id: 1\nevent: view\ndata: {"seq":1}\n\n'
FRAME2 = b'id: 2\nevent: view\ndata: {"seq":2}\n\n'


async def test_events_relay_host_frames_unchanged_and_replace_comments_and_retry(tmp_path):
    async with Rig(tmp_path) as rig:
        sse = await rig.sse(query="?from_seq=5")
        try:
            assert sse.status == 200
            assert sse.headers["content-type"] == "text/event-stream; charset=utf-8"
            assert sse.headers["cache-control"] == "no-store"
            assert sse.headers["x-accel-buffering"] == "no"
            assert await sse.frame() == b"retry: 3000\n\n"
            rig.upstream.push(FRAME1)
            assert await sse.frame() == FRAME1
            rig.upstream.push(b": keepalive\n\nretry: 1000\n\n" + FRAME2[:10])
            rig.upstream.push(FRAME2[10:])
            assert await sse.frame() == FRAME2
            assert rig.app.stream_count == 1
        finally:
            await sse.close()
        await eventually(lambda: rig.app.stream_count == 0)
    opened = rig.upstream.commands_of(p.SessionOpen)
    assert len(opened) == 1 and opened[0].create is False
    assert rig.upstream.stream_opens == [(rig.hosts[0].workspace, "/v1/web/session-events", {"session": "s1", "from_seq": "5"})]


async def test_events_resume_from_last_event_id_and_validate_the_cursor(tmp_path):
    async with Rig(tmp_path) as rig:
        sse = await rig.sse(headers={"Last-Event-ID": "7"})
        await sse.frame()
        await sse.close()
        assert rig.upstream.stream_opens[0][2]["from_seq"] == "7"
        default = await rig.sse()
        await default.frame()
        await default.close()
        assert rig.upstream.stream_opens[1][2]["from_seq"] == "0"
        for query in ("?from_seq=abc", "?from_seq=-1", "?from_seq=1.5", "?from_seq=" + "9" * 17):
            resp = await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/events{query}")
            assert_error(resp, 400, "bad_request")
        await eventually(lambda: rig.app.stream_count == 0)


async def test_events_end_with_a_resync_when_the_host_stream_closes(tmp_path):
    async with Rig(tmp_path) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            rig.upstream.push(FRAME1)
            rig.upstream.end()
            assert await sse.frame() == FRAME1
            assert await sse.frame() == b'event: resync\ndata: {"reason":"upstream_closed"}\n\n'
            assert await sse.rest() == b""
        finally:
            await sse.close()


async def test_a_host_error_event_is_relayed_without_a_second_resync(tmp_path):
    async with Rig(tmp_path) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            rig.upstream.push(b'event: error\ndata: {"resync":true}\n\n')
            rig.upstream.end()
            assert await sse.frame() == b'event: error\ndata: {"resync":true}\n\n'
            assert await sse.rest() == b""
        finally:
            await sse.close()


async def test_events_keep_an_idle_connection_alive(tmp_path):
    limits = Limits(recheck=0.05, keepalive=0.2)
    async with Rig(tmp_path, limits=limits) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            assert await sse.frame() == b": keepalive\n\n"
        finally:
            await sse.close()


async def test_a_slow_consumer_is_dropped_with_a_resync_instead_of_buffering(tmp_path):
    limits = Limits(recheck=0.05, keepalive=30.0, sse_queue_events=2)
    async with Rig(tmp_path, limits=limits) as rig:
        rig.upstream.push(b"".join(b"id: %d\nevent: view\ndata: {}\n\n" % i for i in range(10)))
        sse = await rig.sse()
        try:
            assert await sse.frame() == b"retry: 3000\n\n"
            data = await sse.rest()
        finally:
            await sse.close()
    assert data.count(b"event: view") == 2
    assert data.endswith(b'event: resync\ndata: {"reason":"slow_consumer"}\n\n')


async def test_an_oversize_host_frame_ends_the_stream(tmp_path):
    limits = Limits(recheck=0.05, keepalive=30.0, max_frame_bytes=64)
    async with Rig(tmp_path, limits=limits) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            rig.upstream.push(b"data: " + b"x" * 200 + b"\n\n")
            assert await sse.frame() == b'event: resync\ndata: {"reason":"frame_too_large"}\n\n'
            assert await sse.rest() == b""
        finally:
            await sse.close()


async def test_stream_limits_per_device_and_the_slot_is_freed_on_disconnect(tmp_path):
    limits = Limits(recheck=0.05, keepalive=30.0, max_streams_per_device=1)
    async with Rig(tmp_path, limits=limits) as rig:
        first = await rig.sse()
        await first.frame()
        refused = await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/events")
        assert_error(refused, 429, "too_many_streams")
        other = rig.pair()[1]  # another device has its own allowance
        second = await rig.sse(token=other)
        assert second.status == 200
        await second.close()
        await first.close()
        await eventually(lambda: rig.app.stream_count == 0)
        again = await rig.sse()
        assert again.status == 200
        await again.close()


async def test_global_stream_limit(tmp_path):
    limits = Limits(recheck=0.05, keepalive=30.0, max_streams=1, max_streams_per_device=4)
    async with Rig(tmp_path, limits=limits) as rig:
        first = await rig.sse()
        await first.frame()
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/events"), 429, "too_many_streams")
        await first.close()
        await eventually(lambda: rig.app.stream_count == 0)


async def test_revoking_a_device_ends_its_open_stream(tmp_path):
    limits = Limits(recheck=0.2, keepalive=30.0)
    async with Rig(tmp_path, limits=limits) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            rig.upstream.push(FRAME1)
            assert await sse.frame() == FRAME1
            rig.store.revoke(rig.device.device_id)
            data = await sse.rest(timeout=5)
            assert b'event: error\ndata: {"error":"access_revoked"}' in data
        finally:
            await sse.close()
        await eventually(lambda: rig.app.stream_count == 0)


async def test_disallowing_the_workspace_ends_its_open_stream(tmp_path):
    limits = Limits(recheck=0.2, keepalive=30.0)
    async with Rig(tmp_path, limits=limits) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            rig.store.disallow(rig.host_id)
            data = await sse.rest(timeout=5)
            assert b"access_revoked" in data
        finally:
            await sse.close()


async def test_events_report_a_host_failure_before_the_stream_starts(tmp_path):
    async with Rig(tmp_path) as rig:
        rig.upstream.stream_error = UpstreamError("host_unavailable", 502)
        resp = await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/events")
        assert_error(resp, 502, "host_unavailable")
        rig.upstream.stream_error = None
        rig.upstream.results["SessionOpen"] = p.ErrorResult(message="gone")
        assert_error(await rig.request("GET", f"/v1/hosts/{rig.host_id}/sessions/s1/events"), 404, "session_unavailable")
        await eventually(lambda: rig.app.stream_count == 0)


async def test_shutdown_tells_open_streams_to_resync(tmp_path):
    async with Rig(tmp_path) as rig:
        sse = await rig.sse()
        try:
            await sse.frame()
            await asyncio.wait_for(rig.server.aclose(), 10)
            data = await sse.rest(timeout=5)
            assert b'"reason":"shutdown"' in data
        finally:
            await sse.close()
        assert rig.app.stream_count == 0


# -- HTTP server bounds -----------------------------------------------------------------------------------------------


async def test_malformed_requests_are_refused_with_a_generic_400(tmp_path):
    async with Rig(tmp_path) as rig:
        host = f"Host: 127.0.0.1:{rig.port}\r\n"
        cases = {
            "two-part request line": b"GET /healthz\r\n" + host.encode() + b"\r\n",
            "absolute-form target": b"GET http://127.0.0.1/healthz HTTP/1.1\r\n" + host.encode() + b"\r\n",
            "http/2 version": b"GET /healthz HTTP/2.0\r\n" + host.encode() + b"\r\n",
            "non-ascii target": "GET /hé HTTP/1.1\r\n".encode("utf-8") + host.encode() + b"\r\n",
            "fragment": b"GET /healthz#x HTTP/1.1\r\n" + host.encode() + b"\r\n",
            "chunked": b"POST /v1/pair HTTP/1.1\r\n" + host.encode() + b"Transfer-Encoding: chunked\r\n\r\n",
            "expect": b"POST /v1/pair HTTP/1.1\r\n" + host.encode() + b"Expect: 100-continue\r\n\r\n",
            "upgrade": b"GET /healthz HTTP/1.1\r\n" + host.encode() + b"Upgrade: websocket\r\n\r\n",
            "duplicate header": b"GET /healthz HTTP/1.1\r\n" + host.encode() + host.encode() + b"\r\n",
            "folded header": b"GET /healthz HTTP/1.1\r\n" + host.encode() + b"X-A: 1\r\n  folded\r\n\r\n",
            "header without colon": b"GET /healthz HTTP/1.1\r\n" + host.encode() + b"nocolon\r\n\r\n",
            "bad content-length": b"POST /v1/pair HTTP/1.1\r\n" + host.encode() + b"Content-Length: abc\r\n\r\n",
            "negative content-length": b"POST /v1/pair HTTP/1.1\r\n" + host.encode() + b"Content-Length: -1\r\n\r\n",
            "control byte in header": b"GET /healthz HTTP/1.1\r\n" + host.encode() + b"X-A: a\x01b\r\n\r\n",
        }
        for name, raw in cases.items():
            resp = await exchange(rig.port, raw)
            assert resp.status == 400, name
            assert resp.json()["error"] == "bad_request", name
    assert rig.upstream.calls == []


async def test_oversize_and_excessive_headers_are_refused(tmp_path):
    async with Rig(tmp_path) as rig:
        big = rig.raw("GET", "/healthz", headers={"X-Pad": "a" * 20000})
        assert_error(await exchange(rig.port, big), 431, "headers_too_large")
        many = rig.raw("GET", "/healthz", headers={f"X-H{i}": "1" for i in range(70)})
        assert_error(await exchange(rig.port, many), 431, "headers_too_large")
        long_target = rig.raw("GET", "/healthz?" + "a" * 5000)
        assert (await exchange(rig.port, long_target)).status == 400


async def test_slow_heads_and_bodies_time_out(tmp_path):
    async with Rig(tmp_path, server_kwargs={"head_timeout": 0.2, "body_timeout": 0.2}) as rig:
        partial = await exchange(rig.port, b"GET /heal")
        assert_error(partial, 408, "request_timeout")
        head = rig.raw("POST", "/v1/pair", token=None).replace(b"\r\n\r\n", b"\r\nContent-Length: 50\r\n\r\n", 1)
        slow = await exchange(rig.port, head + b"12345")
        assert_error(slow, 408, "request_timeout")


async def test_connection_limit_answers_busy(tmp_path):
    async with Rig(tmp_path, server_kwargs={"max_connections": 1}) as rig:
        _, idle_writer = await asyncio.open_connection("127.0.0.1", rig.port)
        try:
            await eventually(lambda: rig.server.connection_count == 1)
            assert_error(await rig.request("GET", "/healthz"), 503, "busy")
        finally:
            idle_writer.close()
        await eventually(lambda: rig.server.connection_count == 0)
        assert (await rig.request("GET", "/healthz")).status == 200


async def test_keep_alive_serves_several_requests_then_closes_at_the_cap(tmp_path):
    async with Rig(tmp_path, server_kwargs={"max_requests": 2}) as rig:
        reader, writer = await asyncio.open_connection("127.0.0.1", rig.port)
        try:
            request = f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{rig.port}\r\n\r\n".encode()
            writer.write(request + request + request)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(-1), 5)
        finally:
            writer.close()
    assert data.count(b"HTTP/1.1 200") == 2


def test_the_listener_refuses_non_loopback_addresses():
    for host in ("0.0.0.0", "::", "192.168.1.5", "10.0.0.1", "example.com", "localhost", ""):
        with pytest.raises(ValueError):
            HTTPServer(object(), host=host)  # type: ignore[arg-type]
    HTTPServer(object(), host="127.0.0.1")  # type: ignore[arg-type]
    HTTPServer(object(), host="::1")  # type: ignore[arg-type]
