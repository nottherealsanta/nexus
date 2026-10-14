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


async def test_repeated_drops_end_the_call_with_sideband_lost():
    sockets = iter([FakeSocket([ConnectionResetError("a")]), FakeSocket([ConnectionResetError("b")]),
                    FakeSocket([ConnectionResetError(f"c Bearer {SECRET}")])])

    async def connector(url, headers):
        return next(sockets)

    sink = Sink()
    transport = CodexLiveTransport(Auth(), client=signaling_client(ok_handler([])), connector=connector, sleep=no_sleep)
    await transport.start("sdp", SESSION, on_event=sink.on_event, on_closed=sink.on_closed)
    await settle(lambda: sink.closed)
    assert sink.closed[0][0] == "sideband_lost" and SECRET not in sink.closed[0][1]
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


async def test_ambiguous_drops_share_the_reconnect_budget():
    sockets = [WriteSocket("raise"), WriteSocket("raise"), WriteSocket("raise")]
    transport, sink = await open_transport(sockets)
    for index in range(2):
        with pytest.raises(lp.LiveError) as err:
            await transport.send(msg(f"m{index}"))
        assert err.value.code == "live_send_ambiguous"
        await settle(lambda n=index + 1: sum(isinstance(e, lp.SidebandReconnected) for e in sink.events) == n)
    with pytest.raises(lp.LiveError):
        await transport.send(msg("third"))
    await settle(lambda: sink.closed)  # budget of 2 exhausted: the call ends instead of a 3rd reconnect
    assert sink.closed[0][0] == "sideband_lost"
    with pytest.raises(lp.LiveError) as err:
        await transport.send(msg("after"))
    assert err.value.code == "live_not_connected"
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
