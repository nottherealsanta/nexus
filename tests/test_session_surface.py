"""Phase 3.5 session surface: detached turns, subscriptions, presence, queue.

Covers the plan's §14.2/§14.5/§14.6 session contract:

* ``start_turn`` runs detached and completes with zero subscribers;
* ``subscribe`` replays from the append-only log then follows live with no gaps
  and no duplicates, and closing it never cancels the turn;
* several subscribers observe equivalent ordered streams;
* ``send`` stays the attached, cancel-on-close compatibility wrapper;
* ``enqueue`` is a FIFO queue with ``input.queued``/``consumed``/``dropped``;
* presence is a subscriber count, attendance is derived, and a drop to zero
  applies the unattended policy to a pending approval;
* the single-active-turn lock is always released.

All providers are offline ``ScriptedProvider`` scripts; no network is touched.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.core.loop import ResolvedModel
from nexus.core.turn import TurnLimits
from nexus.errors import SessionBusy
from nexus.events import Event
from nexus.model.message import Document, Image, Text
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.runtime import Runtime
from nexus.session.manager import SessionManager
from nexus.session.session import TERMINAL_EVENTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingAssembler:
    def __init__(self):
        self.seen = []

    def assemble(self, session):
        self.seen.append(list(session.messages))
        return ModelRequest(
            messages=list(session.messages), provider="scripted", model="m"
        )


class Resolver:
    def __init__(self, provider):
        self.provider = provider

    def resolve(self, request):
        return ResolvedModel(self.provider, "m", self.provider.capabilities("m"))


def make_session(tmp_path, provider, name="s"):
    assembler = RecordingAssembler()
    manager = SessionManager(
        tmp_path, assemble=assembler, provider_for=Resolver(provider)
    )
    return manager.open(name), assembler


def make_config(*, mode="ask", unattended="deny"):
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode=mode, on_unattended=unattended),
            tools=ToolsSection(),
        ),
    )


async def wait_for(predicate, timeout=2.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


def turn_events(events, turn_id):
    return [event for event in events if event.turn == turn_id]


def parked_script(release):
    """A turn that parks mid-stream until ``release`` is set."""
    return [
        MessageStart(model="m", provider="scripted"),
        TextDelta(text="a"),
        Wait(event=release),
        MessageStop(stop_reason="end_turn"),
    ]


# ---------------------------------------------------------------------------
# Detached turns and zero subscribers
# ---------------------------------------------------------------------------


async def test_turn_completes_with_zero_subscribers(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session, _ = make_session(tmp_path, provider)

    turn_id = await session.start_turn("hi")
    assert session.active is True
    assert session.viewers == 0
    assert session.active_turn_id == turn_id

    await session.wait_idle()

    assert session.active is False
    assert session.viewers == 0
    assert session.events[-1].type == "turn.completed"
    # No subscriber ever attached, so no presence events were written.
    assert not [e for e in session.events if e.type.startswith("presence.")]


async def test_late_subscriber_replays_full_turn(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session, _ = make_session(tmp_path, provider)
    turn_id = await session.start_turn("hi")
    await session.wait_idle()

    replayed = [
        event
        async for event in session.subscribe(
            0, until=lambda e: e.type in TERMINAL_EVENTS
        )
    ]
    assert turn_events(replayed, turn_id) == turn_events(session.events, turn_id)
    assert replayed[-1].type == "turn.completed"


async def test_single_active_turn_is_concurrency_safe(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    with pytest.raises(SessionBusy):
        await session.start_turn("two")

    session.cancel("stop")
    await session.wait_idle()
    assert session.active is False


# ---------------------------------------------------------------------------
# Subscribe: catch-up then follow, no gaps, no duplicates
# ---------------------------------------------------------------------------


async def test_late_subscriber_replays_then_follows_live(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(
        parked_script(release), text_response("second")
    )
    session, _ = make_session(tmp_path, provider)

    first = await session.start_turn("first")
    await wait_for(lambda: session.active)
    release.set()
    await session.wait_idle()

    received = []
    holder = {}

    async def follow():
        async for event in session.subscribe(0):
            received.append(event)
            if (
                event.type in TERMINAL_EVENTS
                and event.turn == holder.get("turn")
            ):
                break

    task = asyncio.create_task(follow())
    await wait_for(lambda: session.viewers == 1)
    second = holder["turn"] = await session.start_turn("second")
    await session.wait_idle()
    await asyncio.wait_for(task, 2)

    # Replay included the whole first turn...
    assert turn_events(received, first) == turn_events(session.events, first)
    # ...and the live second turn arrived intact, in order, exactly once.
    assert turn_events(received, second) == turn_events(session.events, second)
    seqs = [event.seq for event in received]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


async def test_two_subscribers_observe_equivalent_streams(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session, _ = make_session(tmp_path, provider)
    holder = {}

    def until(event):
        return event.type in TERMINAL_EVENTS and event.turn == holder.get("turn")

    async def collect():
        return [event async for event in session.subscribe(0, until=until)]

    first = asyncio.create_task(collect())
    second = asyncio.create_task(collect())
    await wait_for(lambda: session.viewers == 2)
    turn_id = holder["turn"] = await session.start_turn("hi")
    await session.wait_idle()
    a, b = await asyncio.gather(first, second)

    assert turn_events(a, turn_id) == turn_events(b, turn_id)
    assert turn_events(a, turn_id) == turn_events(session.events, turn_id)


async def test_catchup_live_boundary_has_no_gaps_or_duplicates(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(
        parked_script(release), text_response("second")
    )
    session, _ = make_session(tmp_path, provider)
    holder = {}

    # Attach the follower while a turn is parked mid-stream: part of the turn is
    # already persisted (catch-up) and part arrives live (follow).
    turn_id = holder["turn"] = await session.start_turn("first")
    await wait_for(lambda: session.active)
    stream = session.subscribe(0)
    first = await stream.__anext__()
    assert session.viewers == 1

    seen = [first]
    release.set()
    async for event in stream:
        seen.append(event)
        if event.type in TERMINAL_EVENTS and event.turn == turn_id:
            break
    await stream.aclose()
    await session.wait_idle()

    seqs = [event.seq for event in seen]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    assert turn_events(seen, turn_id) == turn_events(session.events, turn_id)


async def test_closing_subscription_does_not_cancel_turn(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    await wait_for(lambda: session.active)

    stream = session.subscribe(0)
    await stream.__anext__()
    assert session.viewers == 1
    await stream.aclose()

    assert session.viewers == 0
    assert session.active is True  # the detached turn is still running

    release.set()
    await session.wait_idle()
    assert session.active is False
    assert session.events[-1].type == "turn.completed"


# ---------------------------------------------------------------------------
# send compatibility wrapper
# ---------------------------------------------------------------------------


async def test_send_wrapper_matches_persisted_events(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session, _ = make_session(tmp_path, provider)

    events = [event async for event in session.send("hi")]

    assert events == session.events
    assert events[0].type == "turn.started"
    assert events[-1].type == "turn.completed"
    assert session.active is False
    assert [m.role for m in session.messages] == ["user", "assistant"]


async def test_send_early_close_cancels_and_releases_lock(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    stream = session.send("one")
    first = await stream.__anext__()
    assert first.type == "turn.started"

    with pytest.raises(SessionBusy):
        await session.send("two").__anext__()

    await stream.aclose()
    assert session.active is False
    assert any(event.type == "turn.cancelled" for event in session.events)

    events = [event async for event in session.send("two")]
    assert events[-1].type == "turn.completed"


# ---------------------------------------------------------------------------
# Input queue
# ---------------------------------------------------------------------------


async def test_enqueue_is_fifo_and_consumed_at_next_boundary(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(
        parked_script(release),
        text_response("two"),
        text_response("three"),
    )
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    await wait_for(lambda: session.active)

    first = session.enqueue("two")
    second = session.enqueue("three")
    assert session.queue_depth == 2
    assert [e.type for e in session.events].count("input.queued") == 2

    release.set()
    await session.wait_idle()

    assert session.queue_depth == 0
    consumed = [e for e in session.events if e.type == "input.consumed"]
    assert [e.data["queued_id"] for e in consumed] == [first, second]
    user_texts = [
        m.content[0].text
        for m in session.messages
        if m.role == "user" and isinstance(m.content[0], Text)
    ]
    assert user_texts == ["one", "two", "three"]


async def test_cancel_drops_queued_inputs(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("unused"))
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    await wait_for(lambda: session.active)
    session.enqueue("two")
    session.enqueue("three")

    session.cancel("stop")
    await session.wait_idle()

    assert session.queue_depth == 0
    assert session.active is False
    assert [e.type for e in session.events].count("input.dropped") == 2
    # Dropped inputs never ran.
    assert session.messages[-1].content[0].text == "one"


async def test_start_turn_without_input_consumes_the_queue(tmp_path):
    provider = ScriptedProvider(text_response("queued"))
    session, _ = make_session(tmp_path, provider)

    queued_id = session.enqueue("queued input")
    assert session.queue_depth == 1

    await session.start_turn()
    await session.wait_idle()

    assert session.queue_depth == 0
    consumed = [e for e in session.events if e.type == "input.consumed"]
    assert consumed and consumed[0].data["queued_id"] == queued_id


# ---------------------------------------------------------------------------
# Presence and derived attendance
# ---------------------------------------------------------------------------


async def test_presence_count_drives_derived_attendance(tmp_path):
    session = SessionManager(tmp_path).open("presence")
    assert session.viewers == 0
    assert session.attended is False

    a = session.subscribe(0, follow=False)
    await a.__anext__()
    assert session.viewers == 1
    assert session.attended is True

    b = session.subscribe(0, follow=False)
    await b.__anext__()
    assert session.viewers == 2

    await a.aclose()
    assert session.viewers == 1
    assert session.attended is True

    await b.aclose()
    assert session.viewers == 0
    assert session.attended is False

    kinds = [e.type for e in session.events]
    assert kinds.count("presence.joined") == 2
    assert kinds.count("presence.left") == 2


async def test_viewer_drop_applies_unattended_policy_to_pending_approval(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = Runtime(
        tmp_path, config=make_config(mode="ask"), providers={"scripted": provider}
    )
    session = runtime.session("presence-approval")

    # A view attaches, so the session is attended and the Write must be asked.
    stream = session.subscribe(0)
    first = await stream.__anext__()
    assert session.viewers == 1
    assert session.attended is True

    await session.start_turn("go")
    async for event in stream:
        if event.type == "permission.requested":
            break
        assert event.type not in TERMINAL_EVENTS, first
    assert session.pending_permissions  # the approval is genuinely parked

    # The only viewer disconnects: attendance falls to zero, and the session's
    # unattended policy resolves the pending request instead of hanging.
    await stream.aclose()
    assert session.viewers == 0
    await session.wait_idle()

    resolved = [e for e in session.events if e.type == "permission.resolved"]
    assert resolved
    assert resolved[-1].data["decision"] == "deny_once"
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Lock lifecycle
# ---------------------------------------------------------------------------


async def test_lock_released_after_detached_turn(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    await wait_for(lambda: session.active)

    # Closing a subscription must not free the lock (the turn still owns it).
    stream = session.subscribe(0)
    await stream.__anext__()
    await stream.aclose()
    assert session.active is True

    release.set()
    await session.wait_idle()
    assert session.active is False

    # The lock is genuinely free: a fresh attached turn completes.
    events = [event async for event in session.send("two")]
    assert events[-1].type == "turn.completed"


# ---------------------------------------------------------------------------
# Review regressions: queue start semantics, idle waits, limits, pending ids
# ---------------------------------------------------------------------------


async def test_queued_start_while_busy_does_not_lose_the_submission(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    await wait_for(lambda: session.active)
    queued_id = session.enqueue("two")

    # A no-input start while busy must fail *before* consuming the FIFO head.
    with pytest.raises(SessionBusy):
        await session.start_turn()
    assert session.queued_ids == (queued_id,)
    assert session.queue_depth == 1

    release.set()
    await session.wait_idle()
    # The boundary drained it exactly once, with no contradictory drop.
    assert session.queue_depth == 0
    consumed = [
        e.data["queued_id"] for e in session.events if e.type == "input.consumed"
    ]
    assert consumed == [queued_id]
    assert not [e for e in session.events if e.type == "input.dropped"]


async def test_wait_idle_returns_promptly_with_a_pending_queue(tmp_path):
    provider = ScriptedProvider(text_response("later"))
    session, _ = make_session(tmp_path, provider)

    queued_id = session.enqueue("later")
    assert session.queue_depth == 1

    # No running turn: a pending queue is idle, so this returns (no spin).
    await asyncio.wait_for(session.wait_idle(), 0.5)
    assert session.active is False
    assert session.queued_ids == (queued_id,)

    # An explicit start drains it.
    await session.start_turn()
    await session.wait_idle()
    assert session.queue_depth == 0


async def test_auto_start_queued_disabled_leaves_the_queue_pending(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("queued"))
    manager = SessionManager(
        tmp_path,
        assemble=RecordingAssembler(),
        provider_for=Resolver(provider),
        auto_start_queued=False,
    )
    session = manager.open("no-auto")

    await session.start_turn("one")
    await wait_for(lambda: session.active)
    queued_id = session.enqueue("two")

    release.set()
    await session.wait_idle()
    # The boundary did not silently promote the queue.
    assert session.queued_ids == (queued_id,)

    # An explicit start still drains it.
    await session.start_turn()
    await session.wait_idle()
    assert session.queue_depth == 0


async def test_explicit_start_turn_limits_override_the_config_snapshot(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    limits = TurnLimits(max_iterations=3, max_seconds=42.0)
    await session.start_turn("hi", limits=limits)
    await session.wait_idle()

    started = next(e for e in session.events if e.type == "turn.started")
    assert started.data["limits"] == {"max_iterations": 3, "max_seconds": 42.0}


def test_terminal_event_clears_pending_permission_ids(tmp_path):
    session = SessionManager(tmp_path).open("pending-terminal")
    session._observe_event(Event(type="permission.requested", data={"id": "r1"}))
    assert session.pending_permissions == ("r1",)

    session._observe_event(Event(type="turn.completed", data={}))
    assert session.pending_permissions == ()


async def test_cancel_clears_pending_permission_ids(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = Runtime(
        tmp_path, config=make_config(mode="ask"), providers={"scripted": provider}
    )
    session = runtime.session("cancel-pending")

    stream = session.subscribe(0)
    await stream.__anext__()  # a viewer makes the Write prompt
    await session.start_turn("go")
    await wait_for(lambda: session.pending_permissions)
    assert session.pending_permissions

    session.cancel("stop")
    assert session.pending_permissions == ()
    await session.wait_idle()
    assert session.pending_permissions == ()
    await stream.aclose()
    await runtime.aclose()


async def test_subscription_cleanup_survives_presence_publish_failure(
    tmp_path, monkeypatch
):
    session = SessionManager(tmp_path).open("cleanup")
    stream = session.subscribe(0, follow=False)
    await stream.__anext__()
    assert session.viewers == 1
    assert session._bus.subscribers == 1

    real_emit = session._emit

    def failing_emit(event_type, data=None):
        if event_type in ("presence.left", "presence.changed"):
            raise RuntimeError("publish failed")
        return real_emit(event_type, data)

    monkeypatch.setattr(session, "_emit", failing_emit)

    # Cleanup must complete despite the failing presence publishes.
    await stream.aclose()
    assert session.viewers == 0
    assert session._bus.subscribers == 0


async def test_turn_task_cancelled_before_start_releases_the_lease(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    session, _ = make_session(tmp_path, provider)

    await session.start_turn("one")
    task = session._turn_task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert session.active is False

    # The flock is genuinely free: a fresh attached turn completes.
    events = [event async for event in session.send("two")]
    assert events[-1].type == "turn.completed"


async def test_queued_multimodal_bytes_are_json_safe_and_reopen(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    manager = SessionManager(
        tmp_path, assemble=RecordingAssembler(), provider_for=Resolver(provider)
    )
    session = manager.open("queue-mm")
    image = Image(media_type="image/png", data=b"\x89PNG\x00\xff\x01")
    document = Document(
        media_type="application/pdf", data=b"%PDF-1.7\x00binary", title="d"
    )
    queued_id = session.enqueue([image, document])

    # The durable event is JSON-native under any encoder.
    queued_event = next(e for e in session.events if e.type == "input.queued")
    json.dumps(queued_event.data)

    manager.evict("queue-mm")
    reopened = manager.open("queue-mm")
    assert reopened.queue_depth == 1
    assert reopened.queued_ids == (queued_id,)

    await reopened.start_turn()
    await reopened.wait_idle()
    blocks = reopened.messages[0].content
    assert isinstance(blocks[0], Image) and blocks[0].data == image.data
    assert isinstance(blocks[1], Document) and blocks[1].data == document.data
