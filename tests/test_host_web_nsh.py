"""The NSH web client route: /app/ static serving with its route-scoped CSP."""
from __future__ import annotations

import base64
import hashlib

import pytest

import nexus.host.nsh_static as static_module
import nexus.host.web as web_module
from nexus.host.transports.http_sse import HTTPSSEServer
from tests.test_web_transport import _Facade, _request

INLINE_A = "const fetches = new Map();"
INLINE_B = "\n  import('./nsh.abc.js');\n"
INDEX = (
    "<!doctype html><html><head>"
    f"<script>{INLINE_A}</script>"
    "<script src=\"ext.js\"></script>"
    "<style>body{margin:0}</style></head><body>"
    f"<script type=\"module\">{INLINE_B}</script></body></html>"
).encode()


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "nsh"
    root.mkdir()
    (root / "index.html").write_bytes(INDEX)
    (root / "nsh.abc.wasm").write_bytes(b"\0asm\1\0\0\0")
    (root / "nsh.abc.js").write_bytes(b"export default 1;")
    (root / "manifest.webmanifest").write_text("{}")
    (tmp_path / "secret.txt").write_text("secret")
    monkeypatch.setattr(static_module, "NSH_ROOT", root)
    return root


@pytest.mark.asyncio
async def test_missing_bundle_is_404(tmp_path, monkeypatch):
    monkeypatch.setattr(static_module, "NSH_ROOT", tmp_path / "absent")
    server = await HTTPSSEServer(_Facade()).start()
    try:
        status, _, body = await _request(server.port, "GET", "/app/")
        assert status == 404
        assert body == b'{"error":"nsh app not built"}'
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_index_csp_hashes_inline_scripts(bundle):
    server = await HTTPSSEServer(_Facade()).start()
    try:
        status, headers, body = await _request(server.port, "GET", "/app/")
        assert status == 200 and body == INDEX
        csp = headers["content-security-policy"]
        assert "'wasm-unsafe-eval'" in csp
        assert _hash(INLINE_A) in csp and _hash(INLINE_B) in csp
        assert csp.count("'sha256-") == 2  # the src= script is not hashed
        assert "style-src 'self' 'unsafe-inline'" in csp
        assert "frame-ancestors 'none'" in csp
        assert headers["cache-control"] == "no-cache"
        assert headers["x-content-type-options"] == "nosniff"
        status, _, _ = await _request(server.port, "GET", "/app")
        assert status == 301
        status, headers, _ = await _request(server.port, "GET", "/app/manifest.webmanifest")
        assert headers["content-type"] == "application/manifest+json"
        assert headers["cache-control"] == "no-cache"
        # Old browser-client URLs redirect to the app; API responses keep the strict CSP.
        status, headers, _ = await _request(server.port, "GET", "/")
        assert status == 303 and headers["location"] == "/app/"
        _, headers, _ = await _request(server.port, "GET", "/v1/web/bootstrap")
        assert "wasm-unsafe-eval" not in headers["content-security-policy"]
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_wasm_mime_and_immutable_cache(bundle):
    server = await HTTPSSEServer(_Facade()).start()
    try:
        status, headers, body = await _request(server.port, "GET", "/app/nsh.abc.wasm")
        assert status == 200 and body.startswith(b"\0asm")
        assert headers["content-type"] == "application/wasm"
        assert headers["cache-control"] == "public, max-age=31536000, immutable"
        status, headers, _ = await _request(server.port, "GET", "/app/nsh.abc.js")
        assert headers["content-type"] == "text/javascript"
        status, _, _ = await _request(server.port, "GET", "/app/missing.js")
        assert status == 404
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_traversal_and_wrong_host_rejected(bundle):
    server = await HTTPSSEServer(_Facade()).start()
    try:
        status, _, _ = await _request(server.port, "GET", "/app/../secret.txt")
        assert status == 404
        status, _, _ = await _request(server.port, "GET", "/app/..%2Fsecret.txt")
        assert status == 404
        status, _, _ = await _request(server.port, "GET", "/app/", headers={"Host": "evil.example"})
        assert status == 421
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_launch_ticket_in_query_sets_cookie_and_redirects(bundle):
    server = await HTTPSSEServer(_Facade()).start()
    try:
        web = server._web
        ticket = web.issue_ticket()
        status, headers, _ = await _request(server.port, "GET", f"/app/?ticket={ticket}")
        assert status == 303
        assert headers["location"].endswith("/app/")
        assert headers["set-cookie"].startswith(f"{web.cookie_name}=")
        # One use only: a replay redirects without a cookie.
        status, headers, _ = await _request(server.port, "GET", f"/app/?ticket={ticket}")
        assert status == 303 and "set-cookie" not in headers
    finally:
        await server.aclose()


# --- GET /v1/web/session-poll (generic long-poll transport) -------------------

import asyncio  # noqa: E402

import msgspec  # noqa: E402

import nexus.host.web_poll as poll_module  # noqa: E402


class _PollFacade(_Facade):
    def __init__(self, frames=(), *, hang=False, fail=False, live=None):
        super().__init__()
        self.frames = list(frames)
        self.hang = hang
        self.fail = fail
        self.live = live  # optional asyncio.Queue of frames yielded after `frames`
        self.calls = []
        self.closed = 0

    async def subscribe_web(self, session, from_seq=0):
        self.calls.append((session, from_seq))
        try:
            if self.fail:
                raise RuntimeError("boom")
            for frame in self.frames:
                yield frame
            while self.live is not None:
                yield await self.live.get()
            if self.hang:
                await asyncio.sleep(60)
        finally:
            self.closed += 1


async def _login(server):
    origin = f"http://127.0.0.1:{server.port}"
    status, headers, _ = await _request(
        server.port, "POST", "/v1/web/ticket/redeem",
        headers={"Origin": origin, "Content-Type": "application/json"},
        body=msgspec.json.encode({"ticket": server._web.issue_ticket()}),
    )
    assert status == 200
    return {"Cookie": headers["set-cookie"].split(";", 1)[0]}


def _frame(seq, ops=None):
    return {"schema_version": 1, "session": "demo", "seq": seq, "ops": ops or [{"op": "add", "path": "/x", "value": seq}]}


async def _poll(server, auth, query):
    status, headers, body = await _request(server.port, "GET", f"/v1/web/session-poll?{query}", headers=auth)
    return status, headers, (msgspec.json.decode(body) if body else None)


@pytest.mark.asyncio
async def test_poll_requires_cookie_and_valid_params():
    server = await HTTPSSEServer(_PollFacade()).start()
    try:
        status, _, _ = await _request(server.port, "GET", "/v1/web/session-poll?session=demo")
        assert status == 401
        auth = await _login(server)
        for query in ("session=..%2Fx", "session=demo&from_seq=-1", "session=demo&from_seq=x", "session=demo&wait=abc", "session=demo&wait=nan"):
            status, _, _ = await _poll(server, auth, query)
            assert status == 400, query
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_poll_returns_ready_frames_acks_by_from_seq_and_reuses_one_subscription():
    facade = _PollFacade([_frame(3), _frame(4)], hang=True)
    server = await HTTPSSEServer(facade).start()
    try:
        auth = await _login(server)
        status, headers, data = await _poll(server, auth, "session=demo&from_seq=2&wait=5")
        assert status == 200 and headers["content-type"] == "application/json"
        assert data["schema_version"] == 1 and data["session"] == "demo"
        assert [f["seq"] for f in data["frames"]] == [3, 4]
        # A lost response: the same cursor gets the same frames again.
        _, _, again = await _poll(server, auth, "session=demo&from_seq=2&wait=5")
        assert [f["seq"] for f in again["frames"]] == [3, 4]
        # Acknowledging 4 empties the buffer; the poll waits and times out.
        _, _, empty = await _poll(server, auth, "session=demo&from_seq=4&wait=0.1")
        assert empty == {"schema_version": 1, "session": "demo", "frames": []}
        # One subscription served all three polls (no per-poll presence churn).
        assert facade.calls == [("demo", 2)]
        assert facade.closed == 0
    finally:
        await server.aclose()
    assert facade.closed == 1


@pytest.mark.asyncio
async def test_poll_wakes_on_new_frame_and_behind_cursor_replays():
    live = asyncio.Queue()
    facade = _PollFacade(live=live)
    server = await HTTPSSEServer(facade).start()
    try:
        auth = await _login(server)
        task = asyncio.create_task(_poll(server, auth, "session=demo&from_seq=5&wait=5"))
        await asyncio.sleep(0.1)
        live.put_nowait(_frame(6))
        _, _, data = await asyncio.wait_for(task, 2)
        assert [f["seq"] for f in data["frames"]] == [6]
        _, _, data = await _poll(server, auth, "session=demo&from_seq=6&wait=0")
        assert data["frames"] == []
        # Behind the retained buffer (new viewer cursor): a fresh feed from that cursor.
        _, _, data = await _poll(server, auth, "session=demo&from_seq=1&wait=0")
        assert facade.calls == [("demo", 5), ("demo", 1)]
        assert facade.closed == 1
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_poll_wait_is_clamped(monkeypatch):
    monkeypatch.setattr(web_module, "POLL_MAX_WAIT", 0.1)
    server = await HTTPSSEServer(_PollFacade(hang=True)).start()
    try:
        auth = await _login(server)
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, _, data = await _poll(server, auth, "session=demo&wait=9999")
        assert status == 200 and data["frames"] == []
        assert loop.time() - started < 1.5
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_poll_caps_frame_count_and_replaces_oversized_frame():
    big = _frame(1)
    big["ops"] = [{"op": "append", "path": "/t", "value": "x" * (300 * 1024)}]
    frames = [big] + [_frame(i) for i in range(2, 120)]
    server = await HTTPSSEServer(_PollFacade(frames, hang=True)).start()
    try:
        auth = await _login(server)
        _, _, data = await _poll(server, auth, "session=demo&wait=1")
        out = data["frames"]
        assert len(out) == poll_module.POLL_MAX_FRAMES
        assert out[0] == {"schema_version": 1, "session": "demo", "seq": 1, "resync": True}
        assert [f["seq"] for f in out[1:4]] == [2, 3, 4]
    finally:
        await server.aclose()


@pytest.mark.asyncio
async def test_poll_facade_error_yields_resync_and_ended_feed_delivers_then_resyncs():
    server = await HTTPSSEServer(_PollFacade(fail=True)).start()
    try:
        auth = await _login(server)
        _, _, data = await _poll(server, auth, "session=demo&from_seq=7")
        assert data["frames"] == [{"schema_version": 1, "session": "demo", "seq": 7, "resync": True}]
    finally:
        await server.aclose()
    server = await HTTPSSEServer(_PollFacade([_frame(3)])).start()  # iterator ends after one frame
    try:
        auth = await _login(server)
        _, _, data = await _poll(server, auth, "session=demo&from_seq=2&wait=1")
        assert [f["seq"] for f in data["frames"]] == [3, 3]
        assert data["frames"][-1]["resync"] is True
    finally:
        await server.aclose()
