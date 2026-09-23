"""Phase 8a2 supervisor: global cap, fair scheduling, cancel, no-view survival.

The supervisor is the daemon's fork-bomb guard (PLAN §14.5). These tests drive it
with deterministic fake sessions so the scheduling order is observable, then with
a real offline :class:`~nexus.runtime.Runtime` to prove a turn submitted with no
subscriber runs to completion and is fully recoverable from the session log.
"""
from __future__ import annotations

import asyncio

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import Supervisor
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


async def drive(sup, *sessions, timeout=3.0):
    """Release each active fake turn until the supervisor has no work left."""

    async def _drive():
        while sup.running or sup.queued:
            for session in sessions:
                if session.active:
                    session.finish()
            await asyncio.sleep(0)

    await asyncio.wait_for(_drive(), timeout)


class _Clock:
    """Records live sessions so a test can assert the cap was never exceeded."""

    def __init__(self):
        self.live: set[str] = set()
        self.max_live = 0
        self.order: list[str] = []

    def enter(self, name):
        self.live.add(name)
        self.max_live = max(self.max_live, len(self.live))
        self.order.append(f"+{name}")

    def leave(self, name):
        self.live.discard(name)
        self.order.append(f"-{name}")


class _FakeSession:
    """A session whose turn parks until the test releases it."""

    def __init__(self, name, clock):
        self.name = name
        self.clock = clock
        self.active = False
        self.started: list[tuple[str, object]] = []
        self.cancelled = 0
        self.failed: list[tuple[str, str]] = []
        self._release = asyncio.Event()

    async def start_turn(self, content=None, *, turn_id=None):
        if self.active:
            raise RuntimeError("session already active")
        self.active = True
        self._release = asyncio.Event()
        self.started.append((turn_id, content))
        self.clock.enter(self.name)
        return turn_id

    def fail_turn(self, turn_id, error, *, reason=""):
        self.failed.append((turn_id, error))

    async def wait_turn(self, turn_id=None):
        await self._release.wait()
        self.active = False
        self.clock.leave(self.name)

    def cancel(self, reason=None, drop_queue=True):
        self.cancelled += 1
        self.active = False
        self._release.set()

    def finish(self):
        self._release.set()


class _FailingStartSession(_FakeSession):
    """A session whose ``start_turn`` raises before any producer runs."""

    async def start_turn(self, content=None, *, turn_id=None):
        raise RuntimeError("provider unavailable")


async def test_global_cap_serializes_concurrent_turns():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)
    b = _FakeSession("b", clock)

    await sup.submit("a", a, "first")
    await sup.submit("b", b, "second")
    await asyncio.sleep(0)

    assert sup.running == 1
    assert clock.max_live == 1
    assert len(a.started) == 1
    assert b.started == []  # parked behind the global cap
    assert sup.queued_for("b") == 1

    await drive(sup, a, b)
    assert clock.max_live == 1
    assert len(b.started) == 1
    assert clock.order == ["+a", "-a", "+b", "-b"]


async def test_per_session_queue_preserves_order():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)

    await sup.submit("a", a, "one")
    await sup.submit("a", a, "two")
    await sup.submit("a", a, "three")
    await asyncio.sleep(0)
    assert sup.running_for("a") == 1
    assert sup.queued_for("a") == 2

    await drive(sup, a)
    assert [content for _turn, content in a.started] == ["one", "two", "three"]


async def test_round_robin_scheduling_is_fair_across_sessions():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)
    b = _FakeSession("b", clock)

    for content in ("a1", "a2"):
        await sup.submit("a", a, content)
    for content in ("b1", "b2"):
        await sup.submit("b", b, content)

    await drive(sup, a, b)
    assert clock.order == ["+a", "-a", "+b", "-b", "+a", "-a", "+b", "-b"]


async def test_cancel_stops_active_turn_and_drops_queue():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)

    await sup.submit("a", a, "one")
    await sup.submit("a", a, "two")
    await asyncio.sleep(0)
    assert sup.running_for("a") == 1 and sup.queued_for("a") == 1

    cancelled, dropped = await sup.cancel("a", reason="stop")
    assert cancelled is True
    assert dropped == 1
    assert a.cancelled == 1

    await wait_for(lambda: sup.running == 0 and sup.queued == 0, timeout=3.0)
    assert len(a.started) == 1  # the parked submission never started


async def test_wait_idle_returns_when_nothing_is_scheduled():
    sup = Supervisor(max_concurrent=2)
    await asyncio.wait_for(sup.wait_idle(), timeout=1.0)
    assert sup.running == 0 and sup.queued == 0


async def test_start_failure_persists_a_terminal_turn_failed_and_unwedges():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    failing = _FailingStartSession("a", clock)

    turn_id = await sup.submit("a", failing, "one")
    await wait_for(lambda: sup.running == 0 and sup.queued == 0)
    # The pre-assigned turn id is the one persisted as failed, so a follower
    # that subscribed for this turn sees a terminal event.
    assert failing.failed == [(turn_id, "RuntimeError: provider unavailable")]

    # The failure must not wedge the pump: unrelated work still runs.
    ok = _FakeSession("b", clock)
    await sup.submit("b", ok, "two")
    await drive(sup, ok)
    assert len(ok.started) == 1


def test_max_concurrent_must_be_positive():
    with pytest.raises(ValueError):
        Supervisor(max_concurrent=0)
    with pytest.raises(TypeError):
        Supervisor(max_concurrent=True)


async def test_finished_idle_turn_is_forgotten_and_resubmission_works():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)

    await sup.submit("a", a, "one")
    await drive(sup, a)

    # The handle is not retained once the turn is done and nothing is queued.
    assert "a" not in sup._sessions
    # A returning submission re-registers and runs normally.
    await sup.submit("a", a, "two")
    await drive(sup, a)
    assert [content for _turn, content in a.started] == ["one", "two"]


async def test_forget_refuses_while_active_or_queued():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)
    b = _FakeSession("b", clock)

    await sup.submit("a", a, "one")
    await sup.submit("b", b, "two")
    await asyncio.sleep(0)

    assert sup.running_for("a") == 1
    assert sup.forget("a") is False  # an active turn still needs the handle
    assert sup.queued_for("b") == 1
    assert sup.forget("b") is False  # a parked submission still needs it

    await drive(sup, a, b)
    assert "a" not in sup._sessions and "b" not in sup._sessions


async def test_cancel_forgets_an_idle_queue_dropping_session():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)

    await sup.submit("a", a, "one")
    await drive(sup, a)
    await sup.submit("a", a, "two")
    await sup.submit("a", a, "three")
    await asyncio.sleep(0)
    assert sup.running_for("a") == 1 and sup.queued_for("a") == 1

    cancelled, dropped = await sup.cancel("a", reason="stop")
    assert cancelled is True and dropped == 1
    await wait_for(lambda: sup.running == 0 and sup.queued == 0)
    assert "a" not in sup._sessions


async def test_cancel_idle_parked_drops_durable_queue_and_rehydrates_empty(tmp_path):
    """An idle parked cancel must drop the session's own persisted queue.

    The supervisor's queue and the session's durable ``input.queued`` FIFO are
    separate: dropping only the former would let a reload rehydrate and execute
    a submission a cancel explicitly dropped. The drop must go through
    ``session.cancel(drop_queue=True)`` so it emits ``input.dropped``.
    """
    provider = ScriptedProvider(text_response("held"), text_response("parked"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("s")
    session.bind(auto_start_queued=False)
    sup = Supervisor(max_concurrent=1)

    # Occupy the only slot with an unrelated session so "s" stays parked.
    clock = _Clock()
    holder = _FakeSession("holder", clock)
    await sup.submit("holder", holder, "busy")
    assert sup.running_for("holder") == 1

    queued_id = session.enqueue("parked")
    turn_id = await sup.submit("s", session, None, queued_id=queued_id)
    await asyncio.sleep(0)
    assert sup.queued_for("s") == 1
    assert session.active is False
    assert session.queue_depth == 1

    cancelled, dropped = await sup.cancel("s", reason="drop parked")
    assert cancelled is False  # no turn was in flight
    assert dropped == 1
    assert session.queue_depth == 0
    kinds = [event.type for event in session.events]
    assert kinds.count("input.queued") == 1
    assert kinds.count("input.dropped") == 1

    # A reload/rehydrate must not resurrect the dropped submission, and the
    # pre-assigned turn id was never started.
    runtime.sessions.evict("s")
    reloaded = runtime.session("s", recover=False)
    assert reloaded.queue_depth == 0
    assert all(event.turn != turn_id for event in reloaded.events)

    await sup.aclose()
    await runtime.aclose()


async def test_aclose_cancels_everything():
    clock = _Clock()
    sup = Supervisor(max_concurrent=1)
    a = _FakeSession("a", clock)
    await sup.submit("a", a, "one")
    await sup.submit("a", a, "two")
    await asyncio.sleep(0)

    await sup.aclose()
    assert sup.running == 0 and sup.queued == 0
    assert a.cancelled == 1


# ---------------------------------------------------------------------------
# Real runtime: a turn with no viewer runs to completion
# ---------------------------------------------------------------------------


def _config() -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


async def test_turn_survives_zero_views_and_late_subscriber_recovers(tmp_path):
    provider = ScriptedProvider(text_response("background"))
    runtime = Runtime(tmp_path, config=_config(), providers={"scripted": provider})
    session = runtime.session("bg")
    sup = Supervisor(max_concurrent=2)
    session.bind(auto_start_queued=False)

    await sup.submit("bg", session, "work")
    assert session.viewers == 0  # nothing is watching
    await sup.wait_idle(timeout=5.0)

    assert session.active is False
    kinds = [event.type for event in session.events]
    assert "turn.completed" in kinds

    # A late subscriber replays the whole turn from the authoritative log.
    before = list(session.events)
    replayed = [event async for event in session.subscribe(0, follow=False)]
    assert replayed
    assert replayed[: len(before)] == before
    assert any(event.type == "turn.completed" for event in replayed)
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Cancel racing a start, before the session has a lease
# ---------------------------------------------------------------------------


def _gated_runtime(tmp_path):
    """A runtime whose session start parks on an ``ensure_ready`` barrier."""
    runtime = Runtime(
        tmp_path, config=_config(), providers={"scripted": ScriptedProvider(text_response("x"))}
    )
    session = runtime.session("s")
    session.bind(auto_start_queued=False)
    entered = asyncio.Event()
    barrier = asyncio.Event()

    async def ensure_ready() -> None:
        entered.set()
        await barrier.wait()

    session.bind(ensure_ready=ensure_ready)
    return runtime, session, entered, barrier


async def test_cancel_during_ensure_ready_before_lease_is_not_lost(tmp_path):
    """A cancel that lands before ``session.active`` exists must still win.

    ``start_turn`` yields in ``ensure_ready`` before it installs a lease, so a
    cancel racing that window used to find ``session.active is False`` and be
    dropped, letting the turn run to completion. The supervisor now records the
    request and cancels the moment ownership exists.
    """
    runtime, session, entered, barrier = _gated_runtime(tmp_path)
    sup = Supervisor(max_concurrent=1)

    submit_task = asyncio.create_task(sup.submit("s", session, "go"))
    await asyncio.wait_for(entered.wait(), timeout=3.0)
    # Deterministic barrier: the start is in flight and no lease exists yet.
    assert session.active is False
    assert sup.running_for("s") == 1

    cancelled, dropped = await sup.cancel("s", reason="stop")
    assert cancelled is True
    assert dropped == 0

    barrier.set()
    await asyncio.wait_for(submit_task, timeout=3.0)
    await sup.wait_idle(timeout=3.0)

    assert session.active is False
    kinds = [event.type for event in session.events]
    assert "turn.cancelled" in kinds
    assert "turn.completed" not in kinds
    await runtime.aclose()


async def test_cancel_during_ensure_ready_keeps_queued_input_consistent(tmp_path):
    """A cancelled in-flight queue consume is consumed+cancelled, not stranded.

    The submission must not be dropped (it is about to run) nor run to
    completion; it becomes a cancelled turn. A reload must not rehydrate it.
    """
    runtime, session, entered, barrier = _gated_runtime(tmp_path)
    sup = Supervisor(max_concurrent=1)

    queued_id = session.enqueue("first")
    submit_task = asyncio.create_task(
        sup.submit("s", session, None, queued_id=queued_id)
    )
    await asyncio.wait_for(entered.wait(), timeout=3.0)
    assert session.active is False
    assert session.queue_depth == 1

    cancelled, dropped = await sup.cancel("s", reason="stop")
    assert cancelled is True
    # The head becomes a cancelled turn; nothing is counted as dropped.
    assert dropped == 0

    barrier.set()
    await asyncio.wait_for(submit_task, timeout=3.0)
    await sup.wait_idle(timeout=3.0)

    kinds = [event.type for event in session.events]
    assert kinds.count("input.queued") == 1
    assert kinds.count("input.consumed") == 1
    assert kinds.count("input.dropped") == 0
    assert "turn.cancelled" in kinds
    assert session.queue_depth == 0

    runtime.sessions.evict("s")
    reloaded = runtime.session("s", recover=False)
    assert reloaded.queue_depth == 0
    await sup.aclose()
    await runtime.aclose()


async def test_cancel_reports_durable_queue_even_without_a_supervisor_pending(
    tmp_path,
):
    """``dropped`` counts a session-only durable queue, not just parked work."""
    runtime = Runtime(
        tmp_path, config=_config(), providers={"scripted": ScriptedProvider(text_response("x"))}
    )
    session = runtime.session("s")
    session.bind(auto_start_queued=False)
    sup = Supervisor(max_concurrent=1)

    # Occupy the only slot so "s" stays parked.
    clock = _Clock()
    holder = _FakeSession("holder", clock)
    await sup.submit("holder", holder, "busy")

    queued_id = session.enqueue("one")
    await sup.submit("s", session, None, queued_id=queued_id)
    session.enqueue("two")  # durable-only: no supervisor pending mirrors it
    await asyncio.sleep(0)
    assert sup.queued_for("s") == 1
    assert session.queue_depth == 2

    cancelled, dropped = await sup.cancel("s", reason="stop")
    assert cancelled is False
    assert dropped == 2  # both durable submissions, counted once each
    assert session.queue_depth == 0
    await sup.aclose()
    await runtime.aclose()


def _gated_start_runtime(tmp_path, *, fail_start: bool = False):
    """A runtime whose start parks, then fails or succeeds at the barrier."""
    runtime = Runtime(
        tmp_path, config=_config(), providers={"scripted": ScriptedProvider(text_response("x"))}
    )
    session = runtime.session("s")
    session.bind(auto_start_queued=False)
    entered = asyncio.Event()
    barrier = asyncio.Event()

    async def ensure_ready() -> None:
        entered.set()
        await barrier.wait()
        if fail_start:
            raise RuntimeError("simulated start failure")

    session.bind(ensure_ready=ensure_ready)
    return runtime, session, entered, barrier


async def test_cancel_during_ensure_ready_reconciles_dropped_when_start_fails(
    tmp_path,
):
    """The authoritative cancel count matches the durable log when a start fails.

    The synchronous ``cancel`` cannot know whether the in-flight queue consume
    will take its head; if the start then fails, the head is dropped rather than
    consumed. ``_start`` reconciles the count once the start resolves, so the
    reported ``dropped`` is never one short.
    """
    runtime, session, entered, barrier = _gated_start_runtime(
        tmp_path, fail_start=True
    )
    events: list[tuple[str, dict]] = []
    sup = Supervisor(max_concurrent=1, emit=lambda t, data: events.append((t, data)))

    queued_id = session.enqueue("first")
    submit_task = asyncio.create_task(
        sup.submit("s", session, None, queued_id=queued_id)
    )
    await asyncio.wait_for(entered.wait(), timeout=3.0)

    cancelled, dropped = await sup.cancel("s", reason="stop")
    assert cancelled is True
    # The pre-honor estimate assumed the consume would take the head.
    assert dropped == 0

    barrier.set()
    await asyncio.wait_for(submit_task, timeout=3.0)
    await sup.wait_idle(timeout=3.0)

    kinds = [event.type for event in session.events]
    assert kinds.count("input.queued") == 1
    assert kinds.count("input.dropped") == 1
    assert kinds.count("input.consumed") == 0
    assert session.queue_depth == 0

    # The reconciling report agrees with the log: one dropped submission.
    reports = [data for kind, data in events if kind == "daemon.session_cancelled"]
    assert reports and reports[-1]["dropped"] == 1
    await sup.aclose()
    await runtime.aclose()


async def test_cancel_during_ensure_ready_reports_zero_when_consume_succeeds(
    tmp_path,
):
    """A queue-consuming start that succeeds consumes its head; no drop."""
    runtime, session, entered, barrier = _gated_start_runtime(tmp_path)
    events: list[tuple[str, dict]] = []
    sup = Supervisor(max_concurrent=1, emit=lambda t, data: events.append((t, data)))

    queued_id = session.enqueue("first")
    submit_task = asyncio.create_task(
        sup.submit("s", session, None, queued_id=queued_id)
    )
    await asyncio.wait_for(entered.wait(), timeout=3.0)

    cancelled, dropped = await sup.cancel("s", reason="stop")
    assert cancelled is True and dropped == 0

    barrier.set()
    await asyncio.wait_for(submit_task, timeout=3.0)
    await sup.wait_idle(timeout=3.0)

    kinds = [event.type for event in session.events]
    assert kinds.count("input.consumed") == 1
    assert kinds.count("input.dropped") == 0
    reports = [data for kind, data in events if kind == "daemon.session_cancelled"]
    assert reports and reports[-1]["dropped"] == 0
    await sup.aclose()
    await runtime.aclose()
