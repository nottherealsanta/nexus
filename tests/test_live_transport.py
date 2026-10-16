"""Signaling and sideband behaviour of CodexLiveTransport, offline (httpx MockTransport + fake socket)."""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

from nexus.live import protocol as lp
from nexus.live.transport import (
    CALLS_URL,
    CodexLiveTransport,
    _default_connector,
    error_for_status,
    parse_call_id,
    safe_detail,
)

SESSION = lp.LiveSession(instructions="hello", voice="sol")
SECRET = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.signaturesignaturesignature1234567890"
CALL_ID = "rtc_abc-123"


class Auth:
    def __init__(self, fail: Exception | None = None):
        self.fail = fail

    async def headers(self):
        if self.fail:
            raise self.fail
        return {"authorization": f"Bearer {SECRET}", "originator": "nexus", "user-agent": "Nexus/0.1",
                "ChatGPT-Account-Id": "acct"}


class FakeSocket:
    def __init__(self, frames=()):
        self.inbox: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self.inbox.put_nowait(frame)
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def send(self, text):
        self.sent.append(text)

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def signaling_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_handler(captured: list[httpx.Request], location=f"/backend-api/codex/realtime/calls/{CALL_ID}"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, text="v=0\r\nanswer\r\n", headers={"Location": location})
    return handler


async def no_sleep(_):
    return None


class Clock:
    """Fake clock: ``sleep`` advances it (and records the delay), so recovery bounds run instantly."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self):
        return self.t

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.t += delay
        await asyncio.sleep(0)

    def transport(self, connector, auth=None, posts=None):
        return CodexLiveTransport(auth or Auth(), client=signaling_client(ok_handler(posts if posts is not None else [])),
                                  connector=connector, sleep=self.sleep, clock=self.now)


class Sink:
    def __init__(self):
        self.events, self.closed = [], []

    async def on_event(self, event):
        self.events.append(event)

    async def on_closed(self, reason, error):
        self.closed.append((reason, error))


async def settle(predicate, timeout=2.0):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout)


async def test_signaling_request_shape_and_location_parsing():
    captured: list[httpx.Request] = []
    socket = FakeSocket()
    urls: list[tuple[str, dict]] = []

    async def connector(url, headers):
        urls.append((url, dict(headers)))
        return socket

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler(captured)), connector=connector)
    answer = await transport.start("v=0\r\noffer", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    assert answer.call_id == CALL_ID and answer.answer_sdp.startswith("v=0")

    request = captured[0]
    assert str(request.url) == CALLS_URL and request.method == "POST"
    assert json.loads(request.content) == {"sdp": "v=0\r\noffer", "session": SESSION.to_payload()}
    assert request.headers["content-type"] == "application/json"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert request.headers["openai-alpha"] == "quicksilver=v2"
    assert request.headers["originator"] == "nexus"
    assert request.headers["chatgpt-account-id"] == "acct"
    ids = {request.headers[h] for h in ("session-id", "thread-id", "x-session-id")}
    assert len(ids) == 3  # three distinct random UUIDs, not the Nexus session id
    assert "x-oai-attestation" not in request.headers

    await settle(lambda: urls)
    url, headers = urls[0]
    assert url == f"wss://api.openai.com/v1/live/{CALL_ID}"
    assert headers["authorization"] == f"Bearer {SECRET}" and headers["openai-alpha"] == "quicksilver=v2"
    assert "content-type" not in headers
    await settle(lambda: sink.events)
    assert isinstance(sink.events[0], lp.SidebandOpen)
    await transport.close()


@pytest.mark.parametrize(("location", "fallback", "expected"), [
    ("/backend-api/codex/realtime/calls/rtc_x1?foo=bar", None, "rtc_x1"),
    ("https://chatgpt.com/x/123e4567-e89b-12d3-a456-426614174000", None, "123e4567-e89b-12d3-a456-426614174000"),
    ("rtc_rel", None, "rtc_rel"),
    (None, "rtc_from_header", "rtc_from_header"),
    ("/calls/not-an-id", "rtc_fb", "rtc_fb"),
])
def test_parse_call_id(location, fallback, expected):
    assert parse_call_id(location, CALLS_URL, fallback) == expected


def test_parse_call_id_missing():
    with pytest.raises(lp.LiveError) as err:
        parse_call_id("/calls/nothing", CALLS_URL, "bad")
    assert err.value.code == "live_no_call_id"


@pytest.mark.parametrize(("status", "code", "needle"), [
    (401, "live_auth", "nexus auth codex login"),
    (403, "live_forbidden", "plan"),
    (429, "live_rate_limited", "try again later"),
    (400, "live_signaling", "(400)"),
])
async def test_status_mapping_is_actionable(status, code, needle):
    transport = CodexLiveTransport(
        Auth(), client=signaling_client(lambda r: httpx.Response(status, text=f"bad Bearer {SECRET}")),
        connector=lambda *a: None)
    with pytest.raises(lp.LiveError) as err:
        await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert err.value.code == code and needle in err.value.message
    assert SECRET not in err.value.message and "Bearer" not in err.value.message.replace("Bearer ***", "")


async def test_5xx_is_retried_once_then_reported():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="overloaded")

    transport = CodexLiveTransport(Auth(), client=signaling_client(handler), sleep=no_sleep)
    with pytest.raises(lp.LiveError) as err:
        await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert len(calls) == 2 and "503" in err.value.message and "overloaded" in err.value.message


async def test_5xx_then_success():
    responses = iter([httpx.Response(500, text="x"),
                      httpx.Response(200, text="v=0 answer", headers={"Location": f"/c/{CALL_ID}"})])
    transport = CodexLiveTransport(
        Auth(), client=signaling_client(lambda r: next(responses)), sleep=no_sleep,
        connector=lambda *a: asyncio.sleep(3600))
    answer = await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert answer.call_id == CALL_ID
    await transport.close()


@pytest.mark.parametrize("body", ["", "   \n"])
async def test_empty_answer_is_an_error(body):
    transport = CodexLiveTransport(Auth(), client=signaling_client(
        lambda r: httpx.Response(200, text=body, headers={"Location": f"/c/{CALL_ID}"})))
    with pytest.raises(lp.LiveError) as err:
        await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert "empty" in err.value.message


async def test_missing_call_id_is_an_error():
    transport = CodexLiveTransport(Auth(), client=signaling_client(lambda r: httpx.Response(200, text="v=0")))
    with pytest.raises(lp.LiveError) as err:
        await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert err.value.code == "live_no_call_id"


async def test_network_errors_are_redacted_and_typed():
    def handler(request):
        raise httpx.ConnectError(f"boom Bearer {SECRET}")

    transport = CodexLiveTransport(Auth(), client=signaling_client(handler))
    with pytest.raises(lp.LiveError) as err:
        await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert err.value.code == "live_signaling" and SECRET not in err.value.message


async def test_missing_credential_maps_to_auth_error_without_text_leak():
    transport = CodexLiveTransport(Auth(fail=RuntimeError(f"codex login required Bearer {SECRET}")))
    with pytest.raises(lp.LiveError) as err:
        await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    assert err.value.code == "live_auth" and SECRET not in err.value.message


async def test_sideband_events_are_parsed_and_sends_are_ordered():
    socket = FakeSocket([json.dumps({"type": "session.started", "session": {"id": "s"}}),
                         json.dumps({"type": "mystery"})])
    sink = Sink()

    async def connector(url, headers):
        return socket

    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: len(sink.events) == 3)
    assert [type(e) for e in sink.events] == [lp.SidebandOpen, lp.SessionStarted, lp.Unknown]
    for index in range(5):
        await transport.send(lp.append_message(f"m{index}", channel="speakable"))
    assert [json.loads(s)["content"][0]["text"] for s in socket.sent] == [f"m{i}" for i in range(5)]
    await transport.close("bye")
    assert json.loads(socket.sent[-1]) == {"type": "session.close"}
    assert socket.closed == (1000, "bye")
    await transport.close()  # idempotent
    with pytest.raises(lp.LiveError):
        await transport.send({"type": "x"})
    assert sink.closed == []  # a deliberate close is not reported as a drop


async def test_connect_retries_with_backoff_then_fails_closed():
    attempts, sleeps = [], []

    async def connector(url, headers):
        attempts.append(1)
        raise OSError("refused")

    async def sleep(seconds):
        sleeps.append(seconds)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    assert len(attempts) == 5 and sleeps == [0.2, 0.4, 0.8, 1.6]
    assert sink.closed[0][0] == "startup_failed"
    await transport.close()


async def test_sideband_auth_failure_is_not_retried():
    attempts = []

    async def connector(url, headers):
        attempts.append(1)
        raise lp.LiveError("live_auth", "ChatGPT rejected the sign-in")

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    assert len(attempts) == 1 and "sign-in" in sink.closed[0][1]
    await transport.close()


@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("hang", [False, True])
async def test_empty_open_timeout_is_diagnostic_and_bounded(monkeypatch, reconnect, hang):
    monkeypatch.setattr("nexus.live.transport.CONNECT_TIMEOUT_S", 0.01)
    monkeypatch.setattr("nexus.live.transport.RECONNECT_TIMEOUT_S", 0.01)
    socket = FakeSocket()
    attempts = 0

    async def connector(url, headers):
        nonlocal attempts
        attempts += 1
        if reconnect and attempts == 1:
            return socket
        if hang:
            await asyncio.Event().wait()
        raise TimeoutError()

    sink = Sink()
    clock = Clock()
    transport = clock.transport(connector)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        if reconnect:
            await settle(lambda: sink.events)
            socket.inbox.put_nowait(ConnectionResetError("dropped"))
        await settle(lambda: sink.closed)
        reason, error = sink.closed[0]
        assert reason == ("sideband_lost" if reconnect else "startup_failed")
        assert "TimeoutError" in error and "within 0.01s" in error
        if reconnect:
            assert attempts > 5 and "gave up after 300s" in error  # bounded by the total cap, not a count
        else:
            assert attempts == 5
        assert socket.sent == []  # neither recovery nor failure replays commentary
    finally:
        await transport.close()


async def test_reconnect_open_has_full_budget_and_retries_without_replay(monkeypatch):
    # Scaled timings: open takes longer than the credential-refresh deadline.
    monkeypatch.setattr("nexus.live.transport.RECONNECT_TIMEOUT_S", 0.001)
    monkeypatch.setattr("nexus.live.transport.CONNECT_TIMEOUT_S", 0.2)
    sockets = [FakeSocket(), FakeSocket()]
    attempts = 0
    delays = []

    async def sleep(delay):
        delays.append(delay)

    async def connector(url, headers):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return sockets[0]
        await asyncio.sleep(0.01)
        if attempts == 2:
            raise TimeoutError()
        return sockets[1]

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=sleep)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        await settle(lambda: sink.events)
        sockets[0].inbox.put_nowait(ConnectionResetError("dropped"))
        await settle(lambda: any(isinstance(e, lp.SidebandReconnected) for e in sink.events))
        assert attempts == 3 and delays == [0.2, 0.4]
        assert not sink.closed and all(not socket.sent for socket in sockets)
    finally:
        await transport.close()


async def test_open_failure_detail_is_typed_redacted_and_bounded():
    async def connector(url, headers):
        raise OSError(f"failed {SECRET} acct Bearer short-secret " + "x" * 2000)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        await settle(lambda: sink.closed)
        error = sink.closed[0][1]
        assert "OSError: failed" in error
        assert all(secret not in error for secret in (SECRET, "acct", "short-secret"))
        assert len(error) <= 400
    finally:
        await transport.close()


async def test_one_reconnect_after_a_drop_then_continues():
    first = FakeSocket([ConnectionResetError("dropped")])
    second = FakeSocket([json.dumps({"type": "session.updated", "session": {"id": "s"}})])
    sockets = iter([first, second])

    async def connector(url, headers):
        return next(sockets)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: any(isinstance(e, lp.SessionUpdated) for e in sink.events))
    assert sink.closed == []
    await transport.close()


async def test_repeated_drops_keep_recovering_until_the_total_bound_then_sideband_lost():
    sockets = iter([FakeSocket([ConnectionResetError("a")]), FakeSocket([ConnectionResetError("b")]),
                    FakeSocket([ConnectionResetError(f"c Bearer {SECRET}")])])
    opens = 0

    async def connector(url, headers):
        nonlocal opens
        opens += 1
        try:
            return next(sockets)
        except StopIteration:
            raise OSError(f"refused Bearer {SECRET}") from None

    sink, clock = Sink(), Clock()
    transport = clock.transport(connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    reason, detail = sink.closed[0]
    assert reason == "sideband_lost" and SECRET not in detail and "gave up after 300s" in detail
    assert opens > 3 and clock.t >= 300  # more than the old budget of 2, ended only by the bound
    await transport.close()


async def test_drop_diagnostics_include_close_metadata_redacted_and_bounded():
    class Frame:
        code = 1011
        reason = f"keepalive Bearer {SECRET} " + "x" * 500

    class Closed(Exception):
        rcvd = None
        sent = Frame()

    first = FakeSocket([Closed("no close frame received or sent")])

    async def connector(url, headers):
        if first.closed is None and not first.sent:
            first.sent.append("opened")
            return first
        raise error_for_status(404, sideband=True)

    sink = Sink()
    transport = Clock().transport(connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    detail = sink.closed[0][1]
    assert "Closed" in detail and "rcvd=none" in detail and "sent=1011" in detail and "used 1" in detail and "gap" in detail
    assert SECRET not in detail and len(detail) < 800
    await transport.close()


async def test_binary_frame_is_a_protocol_error():
    sink = Sink()

    async def connector(url, headers):
        return FakeSocket([b"\x00\x01"])

    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    assert sink.closed[0][0] == "protocol_error"
    await transport.close()


async def test_missing_websocket_client_hint_says_reinstall_not_extra_or_cargo(monkeypatch):
    monkeypatch.setitem(sys.modules, "websockets", None)  # a None entry makes the lazy import raise ImportError
    with pytest.raises(lp.LiveError) as err:
        await _default_connector("wss://api.openai.com/v1/live/rtc_x", {})
    assert err.value.code == "live_unsupported"
    hint = err.value.message
    assert "uv tool install --upgrade --force nexus-harness" in hint and "restart the daemon" in hint
    assert "[live]" not in hint and "cargo" not in hint.lower()


async def test_default_connector_sends_no_client_pings(monkeypatch):
    seen = {}

    async def connect(url, **kwargs):
        seen.update(kwargs)
        return FakeSocket()

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    await _default_connector("wss://api.openai.com/v1/live/rtc_x", {})
    assert seen["ping_interval"] is None  # Codex-like: server pings are answered by the library, ours are off
    assert "ping_timeout" not in seen


class WriteSocket(FakeSocket):
    """A socket whose writes can be gated, failed or hung; ``close`` frees a hung write."""

    def __init__(self, mode="ok", error=None):
        super().__init__()
        self.mode, self.error = mode, error
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def send(self, text):
        self.started.set()
        if self.mode == "gated":
            await self.gate.wait()
            raise self.error or OSError("write failed")
        if self.mode == "hang":
            await self.gate.wait()
            raise OSError("closed while writing")
        if self.mode == "raise":
            raise self.error or OSError("write failed")
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        self.gate.set()
        await super().close(code, reason)


async def open_transport(sockets, gate=None):
    """Transport whose connector hands out ``sockets`` in order; the 2nd+ connect waits on ``gate``."""
    pool = iter(sockets)
    sink = Sink()
    connects = []

    async def connector(url, headers):
        connects.append(url)
        if len(connects) > 1 and gate is not None:
            await gate.wait()
        return next(pool)

    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    return transport, sink


def msg(text):
    return lp.append_message(text, channel="speakable")


async def test_waiter_never_writes_to_a_socket_replaced_while_it_waited_for_the_lock():
    old, new, gate = WriteSocket("gated"), WriteSocket("ok"), asyncio.Event()
    transport, sink = await open_transport([old, new], gate)
    first = asyncio.create_task(transport.send(msg("one")))
    await old.started.wait()
    second = asyncio.create_task(transport.send(msg("two")))  # snapshots nothing; waits on the send lock
    await asyncio.sleep(0)
    old.gate.set()  # first write fails -> ambiguous, old socket invalidated, reconnect gated
    with pytest.raises(lp.LiveError) as err:
        await first
    assert err.value.code == "live_send_ambiguous"
    assert not second.done()
    gate.set()
    await asyncio.wait_for(second, 2)
    assert old.sent == [] and [json.loads(t)["content"][0]["text"] for t in new.sent] == ["two"]  # one never replayed
    assert old.closed is not None
    assert [type(e) for e in sink.events] == [lp.SidebandOpen, lp.SidebandReconnected]
    assert sink.events[1].gap_s >= 0 and sink.closed == []
    await transport.close()


async def test_send_during_reconnect_waits_and_succeeds_on_the_new_socket():
    old, new, gate = FakeSocket(), WriteSocket("ok"), asyncio.Event()
    transport, sink = await open_transport([old, new], gate)
    old.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: not transport._ready.is_set())
    pending = asyncio.create_task(transport.send(msg("hello")))
    await asyncio.sleep(0.01)
    assert not pending.done() and old.closed is not None  # waiting, replaced socket closed
    gate.set()
    await asyncio.wait_for(pending, 2)
    assert len(new.sent) == 1 and old.sent == []
    assert sum(isinstance(e, lp.SidebandReconnected) for e in sink.events) == 1
    await transport.close()


async def test_send_wait_for_reconnect_is_bounded_and_means_not_sent(monkeypatch):
    monkeypatch.setattr("nexus.live.transport.SEND_WAIT_TIMEOUT_S", 0.05)
    old = FakeSocket()
    transport, _ = await open_transport([old, FakeSocket()], asyncio.Event())  # reconnect never completes
    old.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: not transport._ready.is_set())
    with pytest.raises(lp.LiveError) as err:
        await asyncio.wait_for(transport.send(msg("x")), 2)
    assert err.value.code == "live_not_connected" and "not sent" in err.value.message
    await transport.close()


async def test_send_before_initial_connect_is_bounded_not_sent(monkeypatch):
    monkeypatch.setattr("nexus.live.transport.SEND_WAIT_TIMEOUT_S", 0.05)
    gate = asyncio.Event()

    async def connector(url, headers):
        await gate.wait()
        return FakeSocket()

    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector)
    await transport.start("sdp", SESSION, on_event=Sink().on_event, on_closed=Sink().on_closed)
    with pytest.raises(lp.LiveError) as err:
        await asyncio.wait_for(transport.send(msg("x")), 2)
    assert err.value.code == "live_not_connected"
    await transport.close()


@pytest.mark.parametrize("mode", ["raise", "hang"])
async def test_failed_or_timed_out_write_is_ambiguous_never_replayed_and_recovers(mode, monkeypatch):
    monkeypatch.setattr("nexus.live.transport.SEND_TIMEOUT_S", 0.05)
    bad = WriteSocket(mode, OSError(f"boom Bearer {SECRET} acct"))
    good = WriteSocket("ok")
    transport, sink = await open_transport([bad, good])
    with pytest.raises(lp.LiveError) as err:
        await transport.send(msg("once"))
    error = err.value
    assert error.code == "live_send_ambiguous"
    assert "not retried" in error.message and "may or may not" in error.message
    assert SECRET not in error.message and "acct" not in error.message and "Bearer" not in error.message
    await settle(lambda: any(isinstance(e, lp.SidebandReconnected) for e in sink.events))
    assert bad.sent == [] and good.sent == []  # nothing replayed on the replacement
    assert bad.closed is not None  # damaged socket closed
    await transport.send(msg("next"))  # recovered: new traffic flows
    assert len(good.sent) == 1 and sink.closed == []
    await transport.close()


async def test_repeated_ambiguous_drops_keep_reconnecting_and_never_replay():
    sockets = [WriteSocket("raise"), WriteSocket("raise"), WriteSocket("raise"), WriteSocket("ok")]
    transport, sink = await open_transport(sockets)
    for index in range(3):
        with pytest.raises(lp.LiveError) as err:
            await transport.send(msg(f"m{index}"))
        assert err.value.code == "live_send_ambiguous"
        await settle(lambda n=index + 1: sum(isinstance(e, lp.SidebandReconnected) for e in sink.events) == n)
    await transport.send(msg("fourth"))  # no per-call attempt limit: the 3rd reconnect also succeeded
    assert len(sockets[3].sent) == 1 and not any(s.sent for s in sockets[:3]) and sink.closed == []
    await transport.close()


async def test_close_wakes_senders_waiting_for_a_reconnect_and_for_the_lock():
    old, gate = WriteSocket("hang"), asyncio.Event()
    transport, _ = await open_transport([old, FakeSocket()], gate)
    stuck = asyncio.create_task(transport.send(msg("stuck")))
    await old.started.wait()
    queued = asyncio.create_task(transport.send(msg("queued")))
    await asyncio.sleep(0)
    await asyncio.wait_for(transport.close(), 5)
    for task in (stuck, queued):
        with pytest.raises(lp.LiveError):
            await asyncio.wait_for(task, 2)
    assert queued.exception().code == "live_not_connected"
    assert old.closed is not None


async def test_close_wakes_a_sender_waiting_for_reconnect():
    old, gate = FakeSocket(), asyncio.Event()
    transport, _ = await open_transport([old, FakeSocket()], gate)
    old.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: not transport._ready.is_set())
    waiting = asyncio.create_task(transport.send(msg("w")))
    await asyncio.sleep(0.01)
    await asyncio.wait_for(transport.close(), 5)
    with pytest.raises(lp.LiveError) as err:
        await asyncio.wait_for(waiting, 2)
    assert err.value.code == "live_not_connected"


class ConnectionClosedOK(Exception):
    code = 1000


async def test_clean_server_close_seen_by_a_send_ends_the_call_without_reconnect():
    bad = WriteSocket("raise", ConnectionClosedOK("sent 1000 (OK)"))
    transport, sink = await open_transport([bad, WriteSocket("ok")])
    connects_before = len(sink.events)
    with pytest.raises(lp.LiveError) as err:
        await transport.send(msg("x"))
    assert err.value.code == "live_send_ambiguous"
    await settle(lambda: sink.closed)
    assert sink.closed == [("remote_closed", None)]
    assert len(sink.events) == connects_before and bad.sent == []
    await transport.close()


async def test_reconnect_uses_fresh_credentials_and_redacts_them():
    new_secret = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ5In0.rotatedrotatedrotated1234567890"
    seen: list[str] = []

    class Rotating(Auth):
        calls = 0

        async def headers(self):
            Rotating.calls += 1
            token = SECRET if Rotating.calls == 1 else new_secret
            return {"authorization": f"Bearer {token}", "originator": "nexus"}

    old, new = FakeSocket(), FakeSocket()
    pool = iter([old, new])

    async def connector(url, headers):
        seen.append(headers["authorization"])
        return next(pool)

    sink = Sink()
    transport = CodexLiveTransport(Rotating(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    old.inbox.put_nowait(ConnectionResetError(f"drop Bearer {new_secret}"))
    await settle(lambda: len(seen) == 2)
    assert seen == [f"Bearer {SECRET}", f"Bearer {new_secret}"]
    assert new_secret in transport._secret_values
    assert new_secret not in transport._clean(f"x {new_secret} y")
    await transport.close()


async def test_reconnect_auth_failure_fails_fast():
    class Flaky(Auth):
        calls = 0

        async def headers(self):
            Flaky.calls += 1
            if Flaky.calls > 1:
                raise RuntimeError(f"expired Bearer {SECRET}")
            return await super().headers()

    old = FakeSocket()
    connects = []

    async def connector(url, headers):
        connects.append(url)
        return old

    sink = Sink()
    transport = CodexLiveTransport(Flaky(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    old.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: sink.closed)
    assert sink.closed[0][0] == "sideband_lost" and SECRET not in (sink.closed[0][1] or "")
    assert len(connects) == 1
    await transport.close()


async def test_lone_surrogate_is_escaped_and_does_not_damage_the_connection():
    class StrictSocket(WriteSocket):
        async def send(self, text):
            text.encode("utf-8")  # what the real client does
            await super().send(text)

    sock = StrictSocket("ok")
    transport, sink = await open_transport([sock, StrictSocket("ok")])
    await transport.send(msg("bad \ud800 text"))
    assert json.loads(sock.sent[0])["content"][0]["text"] == "bad \ud800 text"
    await transport.send(msg("next"))
    assert len(sock.sent) == 2 and len(sink.events) == 1 and sink.closed == []
    await transport.close()


async def test_cancel_mid_send_then_close_leaves_no_reconnect_or_close_write():
    hung = WriteSocket("hang")
    connects = []
    pool = iter([hung, FakeSocket()])

    async def connector(url, headers):
        connects.append(url)
        return next(pool)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    reader = transport._reader
    sender = asyncio.create_task(transport.send(msg("stuck")))
    await hung.started.wait()
    sender.cancel()
    closing = asyncio.ensure_future(transport.close())  # same turn, like the manager's _finish
    with pytest.raises(asyncio.CancelledError):
        await sender
    await asyncio.wait_for(closing, 5)
    assert len(connects) == 1 and reader.done()
    assert hung.sent == [] and hung.closed is not None  # no session.close on the damaged socket
    assert [type(e) for e in sink.events] == [lp.SidebandOpen] and sink.closed == []
    assert transport._ws is None and transport._reader is None and transport._retired == []


def test_safe_detail_redacts_and_bounds():
    assert SECRET not in safe_detail(f"x Bearer {SECRET} y")
    assert len(safe_detail("a " * 1000)) <= 300
    assert error_for_status(500, "").message == "call creation failed (500)"


@pytest.mark.parametrize("status,retryable", [
    (400, False), (401, False), (403, False), (404, False), (405, False),
    (408, True), (410, False), (422, False), (429, False),
    (500, True), (502, True), (503, True), (504, True),
])
def test_http_refusal_retry_classification(status, retryable):
    error = error_for_status(status, sideband=True)
    assert error.status == status and error.retryable is retryable
    if status in (404, 410):
        assert "call unavailable/expired" in error.message
        assert "not retrying" in error.message
        assert "call creation" not in error.message


@pytest.mark.parametrize("reconnect", [False, True])
@pytest.mark.parametrize("status", [400, 404, 410, 422])
async def test_permanent_upgrade_refusal_stops_without_spending_remaining_budget(reconnect, status):
    socket = FakeSocket()
    attempts = []
    posts = []

    async def connector(url, headers):
        attempts.append(url)
        if reconnect and len(attempts) == 1:
            return socket
        raise error_for_status(status, f"missing {SECRET} acct Bearer short-secret", sideband=True)

    sink = Sink()
    clock = Clock()
    transport = clock.transport(connector, posts=posts)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        if reconnect:
            await settle(lambda: sink.events)
            socket.inbox.put_nowait(ConnectionResetError("dropped"))
        await settle(lambda: sink.closed)
        reason, error = sink.closed[0]
        assert reason == ("sideband_lost" if reconnect else "startup_failed")
        # Initial open: only a 404 gets the short early-join retries (3 more attempts); recovery never retries.
        assert len(attempts) == (2 if reconnect else 4 if status == 404 else 1)
        assert len(posts) == 1  # never silently replace the conversation/call
        assert f"({status})" in error and "sideband WebSocket upgrade rejected" in error
        assert all(secret not in error for secret in (SECRET, "acct", "short-secret"))
        assert not socket.sent
        if not reconnect and status == 404:
            assert clock.sleeps == [0.1, 0.3, 0.8]
        if reconnect:
            assert "attempts used 1," in error
    finally:
        await transport.close()


async def test_reconnect_404_reuses_same_call_url_once_and_never_posts_again():
    socket = FakeSocket()
    urls = []
    posts = []

    async def connector(url, headers):
        urls.append(url)
        if len(urls) == 1:
            return socket
        raise error_for_status(404, f"gone Bearer {SECRET}", sideband=True)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler(posts)),
                                   connector=connector, sleep=no_sleep)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        await settle(lambda: sink.events)
        socket.inbox.put_nowait(ConnectionResetError("dropped"))
        await settle(lambda: sink.closed)
        reason, error = sink.closed[0]
        assert reason == "sideband_lost"
        assert len(urls) == 2 and urls[0] == urls[1]  # no other URL exists in the protocol
        assert len(posts) == 1  # no replacement call
        assert "recovery attempts used 1," in error and "id was accepted earlier" in error
        assert "no replacement call created" in error and SECRET not in error
    finally:
        await transport.close()


async def test_upgrade_refusal_body_is_omitted_and_status_retained(monkeypatch):
    import websockets
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    async def connect(*args, **kwargs):
        raise InvalidStatus(Response(404, "Not Found", headers=websockets.Headers(),
                                     body=f"unknown call Bearer {SECRET} ".encode() + b"x" * 4000))

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    with pytest.raises(lp.LiveError) as err:
        await _default_connector("wss://api.openai.com/v1/live/rtc_x", {})
    assert err.value.status == 404 and not err.value.retryable
    assert "unknown call" not in err.value.message and SECRET not in err.value.message
    assert "call unavailable/expired" in err.value.message
    assert len(err.value.message) < 500


@pytest.mark.parametrize("boundary", [300, 2048])
@pytest.mark.parametrize("credential", ["opaque-auth-secret-1234567890", "account-id-1234567890"])
async def test_upgrade_refusal_boundary_credentials_never_reach_display(monkeypatch, boundary, credential):
    import websockets
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    class BoundaryAuth:
        async def headers(self):
            return {
                "authorization": f"Bearer {credential if credential.startswith('opaque-') else SECRET}",
                "ChatGPT-Account-Id": credential if credential.startswith("account-") else "other-account",
            }

    attempts = []
    fragment = credential[:10]
    body = ("x" * (boundary - len(fragment)) + credential + " provider detail").encode()

    async def connect(*args, **kwargs):
        attempts.append(args)
        raise InvalidStatus(Response(404, "Not Found", headers=websockets.Headers(), body=body))

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)
    sink = Sink()
    transport = CodexLiveTransport(BoundaryAuth(), client=signaling_client(ok_handler([])), sleep=no_sleep)
    # the initial open retries an early 404 (3 more times), then fails
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        await settle(lambda: sink.closed)
        reason, error = sink.closed[0]
        assert reason == "startup_failed" and len(attempts) == 4
        assert "(404)" in error and "not retrying" in error
        assert credential not in error and fragment not in error
        assert len(error) <= 300
    finally:
        await transport.close()


@pytest.mark.parametrize("boundary", [300, 2048])
@pytest.mark.parametrize("credential", ["opaque-auth-secret-1234567890", "account-id-1234567890"])
async def test_signaling_refusal_scrubs_credentials_before_truncation(boundary, credential):
    class BoundaryAuth:
        async def headers(self):
            return {
                "authorization": f"Bearer {credential if credential.startswith('opaque-') else SECRET}",
                "ChatGPT-Account-Id": credential if credential.startswith("account-") else "other-account",
            }

    fragment = credential[:10]
    # Whitespace puts the credential at the raw 2048 boundary while keeping it
    # visible after whitespace normalization. At 300, exercise display truncation.
    padding = " " * (boundary - len(fragment)) if boundary == 2048 else "x" * (boundary - len(fragment))
    body = padding + credential + " useful provider diagnostic"
    transport = CodexLiveTransport(
        BoundaryAuth(), client=signaling_client(lambda _: httpx.Response(404, text=body)), sleep=no_sleep,
    )
    sink = Sink()
    try:
        with pytest.raises(lp.LiveError) as err:
            await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        assert err.value.status == 404 and not err.value.retryable
        assert credential not in err.value.message and fragment not in err.value.message
        assert "***" in err.value.message and len(err.value.message) < 500
        if boundary == 2048:
            assert "useful provider diagnostic" in err.value.message
    finally:
        await transport.close()


@pytest.mark.parametrize("open_budget,open_duration,recovers", [
    (5.0, 6.0, False),  # reproduce the reported historical diagnostic exactly
    (15.0, 6.0, True),  # DNS/TLS/upgrade may legitimately take more than 5s
    (15.0, 16.0, False),  # both opens must be cancelled at the 15s deadline
])
async def test_reconnect_deadlines_and_historical_5s_diagnostic(
    monkeypatch, open_budget, open_duration, recovers,
):
    # Keep production deadline values and diagnostics, but compress elapsed time.
    # Capture every wait_for so a 5s outer wrapper around open is detected too.
    from nexus.live import transport as live_transport

    real_wait_for = asyncio.wait_for
    deadlines = []

    async def accelerated_wait_for(awaitable, timeout):
        deadlines.append(timeout)
        return await real_wait_for(awaitable, timeout * 0.01 if timeout in (5.0, 15.0) else timeout)

    monkeypatch.setattr(live_transport, "CONNECT_TIMEOUT_S", open_budget)
    monkeypatch.setattr(live_transport, "RECOVERY_GIVE_UP_S", 10.0)  # few attempts; real time is scaled
    monkeypatch.setattr(asyncio, "wait_for", accelerated_wait_for)
    sockets = [FakeSocket(), FakeSocket()]
    attempts = 0
    cancelled = 0
    delays = []

    async def connector(url, headers):
        nonlocal attempts, cancelled
        attempts += 1
        if attempts == 1:
            return sockets[0]
        try:
            await asyncio.sleep(open_duration * 0.01)
        except asyncio.CancelledError:
            cancelled += 1
            raise
        return sockets[1]

    clock = Clock()

    async def backoff(delay):
        delays.append(delay)
        await clock.sleep(delay)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])),
                                   connector=connector, sleep=backoff, clock=clock.now)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        await settle(lambda: sink.events)
        deadlines.clear()
        sockets[0].inbox.put_nowait(ConnectionResetError("dropped"))
        await settle(lambda: sink.closed or any(isinstance(e, lp.SidebandReconnected) for e in sink.events))
        if recovers:
            assert not sink.closed and attempts == 2 and cancelled == 0 and delays == [0.2]
            assert deadlines.count(5.0) == 1  # credential refresh only
            assert deadlines.count(15.0) == 1  # open, no nested 5s wait
        else:
            assert attempts >= 3 and cancelled == attempts - 1 and delays[:2] == [0.2, 0.4]
            reason, error = sink.closed[0]
            assert reason == "sideband_lost"
            assert f"Could not open the sideband connection: TimeoutError: WebSocket open did not complete within {open_budget:g}s" in error
            assert "gave up after 10s" in error
            if open_budget == 15:
                assert deadlines.count(5.0) == attempts - 1  # one refresh per attempt
                assert deadlines.count(15.0) == attempts - 1
        assert all(not socket.sent for socket in sockets)
    finally:
        await transport.close()


async def test_reconnect_credential_timeout_does_not_open_or_retry(monkeypatch):
    monkeypatch.setattr("nexus.live.transport.RECONNECT_TIMEOUT_S", 0.01)

    class ExpiringAuth(Auth):
        expired = False

        async def headers(self):
            if self.expired:
                await asyncio.Event().wait()
            return await super().headers()

    socket = FakeSocket()
    opens = []

    async def connector(url, headers):
        opens.append(url)
        return socket

    sink = Sink()
    auth = ExpiringAuth()
    transport = CodexLiveTransport(auth, client=signaling_client(ok_handler([])),
                                   connector=connector, sleep=no_sleep)
    try:
        await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
        await settle(lambda: sink.events)
        auth.expired = True
        socket.inbox.put_nowait(ConnectionResetError("dropped"))
        await settle(lambda: sink.closed)
        reason, error = sink.closed[0]
        assert reason == "sideband_lost" and "Timed out refreshing credentials" in error
        assert "WebSocket open did not complete" not in error
        assert len(opens) == 1 and socket.sent == []
    finally:
        await transport.close()


# -- Codex-like join and recovery ---------------------------------------------


async def test_initial_open_retries_an_early_404_then_succeeds():
    sock, urls = FakeSocket(), []

    async def connector(url, headers):
        urls.append(url)
        if len(urls) <= 2:
            raise error_for_status(404, sideband=True)
        return sock

    sink, clock, posts = Sink(), Clock(), []
    transport = clock.transport(connector, posts=posts)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    assert len(urls) == 3 and clock.sleeps == [0.1, 0.3] and sink.closed == [] and len(posts) == 1
    await transport.close()


async def test_recovery_backoff_doubles_from_200ms_and_caps_at_5s_then_410_ends_it():
    first, opens = FakeSocket(), 0

    async def connector(url, headers):
        nonlocal opens
        opens += 1
        if opens == 1:
            return first
        if opens == 10:
            raise error_for_status(410, sideband=True)
        raise OSError("network down")

    sink, clock = Sink(), Clock()
    transport = clock.transport(connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    first.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: sink.closed)
    assert clock.sleeps == [0.2, 0.4, 0.8, 1.6, 3.2, 5.0, 5.0, 5.0, 5.0]
    reason, error = sink.closed[0]
    assert reason == "sideband_lost" and "(410)" in error and "no replacement call created" in error
    await transport.close()


async def test_attempt_counter_resets_only_after_a_stable_connection():
    clock = Clock()
    sockets = [FakeSocket(), FakeSocket(), FakeSocket(), FakeSocket()]
    pool = iter(sockets)

    async def connector(url, headers):
        return next(pool)

    sink = Sink()
    transport = clock.transport(connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)

    def reconnected(n):
        return sum(isinstance(e, lp.SidebandReconnected) for e in sink.events) == n

    sockets[0].inbox.put_nowait(ConnectionResetError("d1"))  # short-lived: n = 1
    await settle(lambda: reconnected(1))
    sockets[1].inbox.put_nowait(ConnectionResetError("d2"))  # short-lived again: n = 2
    await settle(lambda: reconnected(2))
    clock.t += 31  # sockets[2] stays up long enough to count as stable
    sockets[2].inbox.put_nowait(ConnectionResetError("d3"))  # counter reset: n = 1
    await settle(lambda: reconnected(3))
    assert clock.sleeps == [0.2, 0.4, 0.2] and sink.closed == []
    await transport.close()


async def test_recovery_total_bound_is_measured_from_the_start_of_unstable_failure():
    first, opens = FakeSocket(), 0

    async def connector(url, headers):
        nonlocal opens
        opens += 1
        if opens == 1:
            return first
        raise OSError("down")

    sink, clock = Sink(), Clock()
    transport = clock.transport(connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    first.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: sink.closed)
    assert 300 <= clock.t < 300 + 5.0 + 0.001  # stops at the first check past the bound
    assert "gave up after 300s of continuous failure" in sink.closed[0][1]
    await transport.close()


async def test_clean_server_close_seen_by_the_reader_is_terminal_without_reconnect():
    sock, opens = FakeSocket([ConnectionClosedOK("received 1000 (OK)")]), []

    async def connector(url, headers):
        opens.append(url)
        return sock

    sink, clock = Sink(), Clock()
    transport = clock.transport(connector)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    assert sink.closed == [("remote_closed", None)] and len(opens) == 1 and clock.sleeps == []
    await transport.close()


async def test_stop_during_recovery_backoff_ends_without_reporting_a_drop():
    first, gate = FakeSocket(), asyncio.Event()

    async def connector(url, headers):
        if not first.sent:
            first.sent.append("opened")
            return first
        raise OSError("down")

    async def sleep(_):  # a real backoff wait that only stop can end
        await gate.wait()

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.events)
    first.inbox.put_nowait(ConnectionResetError("dropped"))
    await settle(lambda: not transport._ready.is_set())
    await asyncio.wait_for(transport.close(), 5)
    assert sink.closed == []
