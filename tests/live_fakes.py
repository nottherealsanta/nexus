"""Shared fakes for the GPT Live tests: a scripted transport and an in-memory host."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from nexus.events import Event
from nexus.live import protocol as lp
from nexus.live.manager import SessionContext
from nexus.live.transport import CallAnswer


async def settle(predicate, timeout: float = 2.0) -> None:
    async def wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout)


class FakeLiveTransport:
    """Implements LiveTransport; tests push sideband events with ``push``."""

    def __init__(self, *, fail_start: Exception | None = None, hang_start: bool = False,
                 fail_send: bool = False, open_on_start: bool = True) -> None:
        self.fail_start, self.hang_start, self.fail_send = fail_start, hang_start, fail_send
        self.open_on_start = open_on_start
        self.sent: list[dict[str, Any]] = []
        self.fail_at: dict[int, Exception] = {}
        self.send_gate: asyncio.Event | None = None
        self.attempts = self.in_flight = self.max_in_flight = 0
        self.close_reasons: list[str] = []
        self.offer: str | None = None
        self.session: lp.LiveSession | None = None
        self._on_event = None
        self._on_closed = None

    async def start(self, offer_sdp, session, *, on_event, on_closed):
        self.offer, self.session = offer_sdp, session
        self._on_event, self._on_closed = on_event, on_closed
        if self.hang_start:
            await asyncio.sleep(3600)
        if self.fail_start:
            raise self.fail_start
        if self.open_on_start:
            on_event(lp.SidebandOpen())
        return CallAnswer(call_id="rtc_test", answer_sdp="v=0 answer")

    def push(self, event) -> None:
        self._on_event(event)

    def drop(self, reason="sideband_lost", error="gone") -> None:
        self._on_closed(reason, error)

    async def send(self, message) -> None:
        index = self.attempts
        self.attempts += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.send_gate is not None:
                await self.send_gate.wait()
            if self.fail_send:
                raise lp.LiveError("live_send_failed", "send failed")
            if index in self.fail_at:  # attempt index -> LiveError (a failed chunk is never replayed)
                raise self.fail_at[index]
            self.sent.append(dict(message))
        finally:
            self.in_flight -= 1

    async def close(self, reason="done") -> None:
        self.close_reasons.append(reason)

    def texts(self, kind: str | None = None) -> list[str]:
        return [m["content"][0]["text"] for m in self.sent
                if "content" in m and (kind is None or m["type"] == kind)]


class FakeTap:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)


class FakeHost:
    """An in-memory LiveHost recording emitted events."""

    def __init__(self, sessions=("s1",), turns=None, context=None) -> None:
        self.sessions = set(sessions)
        self.context = context  # a SessionContext, or an exception to raise
        self.context_calls = 0
        self.events: list[tuple[str, str, dict]] = []
        self.viewer_count = 1
        self.running = False
        self.enqueued: list[tuple[str, str, str, dict]] = []
        self.turns = list(turns or [])
        self.tap = FakeTap()
        self.agent = "build"
        self.fail_emit = False
        self._seq = 0

    # LiveHost
    def emit(self, session_id, event_type, data):
        if self.fail_emit:
            raise RuntimeError("session retired")
        self._seq += 1
        self.events.append((session_id, event_type, dict(data)))

    def has_session(self, session_id): return session_id in self.sessions
    def viewers(self, session_id): return self.viewer_count
    def agent_name(self, session_id): return self.agent
    def transcript_turns(self, session_id): return list(self.turns)
    def turn_running(self, session_id): return self.running

    def session_context(self, session_id):
        self.context_calls += 1
        if isinstance(self.context, Exception):
            raise self.context
        return self.context if self.context is not None else SessionContext()

    async def enqueue(self, session_id, text, mode, origin):
        queued_id = f"q{len(self.enqueued) + 1}"
        self.enqueued.append((session_id, text, mode, dict(origin)))
        return queued_id

    def observe(self, session_id): return self.tap

    # helpers
    def of(self, event_type: str) -> list[dict]:
        return [data for _s, kind, data in self.events if kind == event_type]

    def kinds(self) -> list[str]:
        return [kind for _s, kind, _d in self.events]

    def as_events(self) -> list[Event]:
        return [Event(type=kind, data=data, seq=i + 1, session=sid) for i, (sid, kind, data) in enumerate(self.events)]


def live_config(**overrides) -> SimpleNamespace:
    values = dict(enabled=True, voice="default", max_minutes=30, carry_turns=12, progress=True,
                  instructions="", log_raw=False)
    values.update(overrides)
    return SimpleNamespace(**values)
