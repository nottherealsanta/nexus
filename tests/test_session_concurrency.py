"""Phase 3.5 H4: session concurrency, detached turns, and stream integrity.

This is the stress/race companion to ``test_session_surface``. It repeats the
catch-up/live boundary many times, forces the bounded bus to drop events under a
slow subscriber, races concurrent ``start_turn`` calls, repeatedly cancels turns
to prove the per-session lock is always released, and pins the input queue's
FIFO ordering. All providers are offline ``ScriptedProvider`` scripts.

Everything here is deterministic: no wall-clock sleeps beyond ``sleep(0)`` and
bounded polling, so the suite cannot hang on a regression.
"""
from __future__ import annotations

import asyncio

from nexus.core.loop import ResolvedModel
from nexus.errors import SessionBusy
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.session.manager import SessionManager
from nexus.session.session import TERMINAL_EVENTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingAssembler:
    def __init__(self):
        self.seen: list[list] = []

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


def make_session(tmp_path, provider, name="s", **kwargs):
    manager = SessionManager(
        tmp_path,
        assemble=RecordingAssembler(),
        provider_for=Resolver(provider),
        **kwargs,
    )
    return manager.open(name)


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


async def wait_until_idle(session, timeout=3.0):
    """Bounded, cancellable idle wait (``wait_idle`` itself is shielded)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)


def turn_events(events, turn_id):
    return [event for event in events if event.turn == turn_id]


def any_event(session, event_type):
    return any(event.type == event_type for event in session.events)


def parked_script(release):
    """A turn that persists some state, then parks mid-stream until released."""
    return [
        MessageStart(model="m", provider="scripted"),
        TextDelta(text="a"),
        Wait(event=release),
        MessageStop(stop_reason="end_turn"),
    ]


def delta_step(text):
    """A single streamed delta that yields to the loop first."""

    async def step(_request):
        await asyncio.sleep(0)
        return TextDelta(text=text)

    return step


def assert_contiguous(events):
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs), "events are not in sequence order"
    assert len(seqs) == len(set(seqs)), "an event was yielded twice"


# ---------------------------------------------------------------------------
# Detached turns and late subscribers
# ---------------------------------------------------------------------------


async def test_detached_turn_completes_with_zero_subscribers(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session = make_session(tmp_path, provider, name="detached")

    turn_id = await session.start_turn("hi")
    assert session.viewers == 0
    assert session.active_turn_id == turn_id

    await wait_until_idle(session)
    assert session.active is False
    assert session.events[-1].type == "turn.completed"
    assert not any(e.type.startswith("presence.") for e in session.events)


async def test_late_subscriber_replays_from_seq_zero(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session = make_session(tmp_path, provider, name="late")
    turn_id = await session.start_turn("hi")
    await wait_until_idle(session)

    replayed = [
        event
        async for event in session.subscribe(
            0, until=lambda e: e.type in TERMINAL_EVENTS
        )
    ]
    assert turn_events(replayed, turn_id) == turn_events(session.events, turn_id)
    assert replayed[-1].type == "turn.completed"


async def test_two_live_subscribers_get_equivalent_ordered_streams(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    session = make_session(tmp_path, provider, name="two-live")
    holder: dict = {}

    def until(event):
        return event.type in TERMINAL_EVENTS and event.turn == holder.get("turn")

    async def collect():
        return [event async for event in session.subscribe(0, until=until)]

    a_task = asyncio.create_task(collect())
    b_task = asyncio.create_task(collect())
    await wait_for(lambda: session.viewers == 2)
    holder["turn"] = await session.start_turn("hi")
    await wait_until_idle(session)
    a, b = await asyncio.gather(a_task, b_task)

    turn_id = holder["turn"]
    assert turn_events(a, turn_id) == turn_events(b, turn_id)
    assert turn_events(a, turn_id) == turn_events(session.events, turn_id)
    assert_contiguous(a)
    assert_contiguous(b)


async def test_repeated_catchup_live_race_never_gaps_or_duplicates(tmp_path):
    for i in range(15):
        release = asyncio.Event()
        provider = ScriptedProvider(
            parked_script(release), text_response("second")
        )
        session = make_session(tmp_path, provider, name=f"race-{i}")

        turn_id = await session.start_turn("first")
        # Wait until part of the turn is durable, so catch-up is non-empty and
        # the rest arrives live: the exact catch-up/follow boundary race.
        await wait_for(lambda s=session: any_event(s, "text.delta"))

        stream = session.subscribe(0)
        seen = [await stream.__anext__()]
        release.set()
        async for event in stream:
            seen.append(event)
            if event.type in TERMINAL_EVENTS and event.turn == turn_id:
                break
        await stream.aclose()
        await wait_until_idle(session)

        assert_contiguous(seen)
        assert turn_events(seen, turn_id) == turn_events(session.events, turn_id)


async def test_slow_subscriber_recovers_dropped_events_from_the_log(tmp_path):
    # A one-slot bus buffer guarantees drops while the subscriber stalls.
    script = [MessageStart(model="m", provider="scripted")]
    script += [delta_step(str(i)) for i in range(40)]
    script.append(MessageStop(stop_reason="end_turn"))
    provider = ScriptedProvider(script)
    session = make_session(tmp_path, provider, name="slow", event_buffer=1)

    holder: dict = {}
    seen: list = []
    stalled = {"done": False}

    async def follow():
        async for event in session.subscribe(0):
            seen.append(event)
            if not stalled["done"] and event.type == "text.delta":
                stalled["done"] = True
                await asyncio.sleep(0.02)
            if event.type in TERMINAL_EVENTS and event.turn == holder.get("turn"):
                break

    follow_task = asyncio.create_task(follow())
    await wait_for(lambda: session.viewers == 1)
    holder["turn"] = await session.start_turn("go")
    await wait_until_idle(session)
    await asyncio.wait_for(follow_task, 3)

    assert_contiguous(seen)
    turn_id = holder["turn"]
    assert turn_events(seen, turn_id) == turn_events(session.events, turn_id)
    assert sum(1 for e in seen if e.type == "text.delta") == 40


async def test_closing_every_view_does_not_cancel_the_turn(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    session = make_session(tmp_path, provider, name="close-views")

    await session.start_turn("one")
    await wait_for(lambda: session.active)

    streams = [session.subscribe(0) for _ in range(3)]
    for stream in streams:
        await stream.__anext__()
    assert session.viewers == 3

    for stream in streams:
        await stream.aclose()
    assert session.viewers == 0
    assert session.active is True

    release.set()
    await wait_until_idle(session)
    assert session.active is False
    assert session.events[-1].type == "turn.completed"


# ---------------------------------------------------------------------------
# start_turn races and repeated cancellation
# ---------------------------------------------------------------------------


async def test_repeated_concurrent_start_turn_has_exactly_one_winner(tmp_path):
    for i in range(10):
        release = asyncio.Event()
        provider = ScriptedProvider(parked_script(release), text_response("ok"))
        session = make_session(tmp_path, provider, name=f"win-{i}")

        async def attempt(text, sess=session):
            try:
                await sess.start_turn(text)
                return True
            except SessionBusy:
                return False

        results = await asyncio.gather(attempt("one"), attempt("two"))
        assert sum(results) == 1, "both or neither concurrent start_turn won"

        session.cancel("stop")
        await wait_until_idle(session)
        assert session.active is False


async def test_repeated_cancellation_always_releases_the_lock(tmp_path):
    scripts = [parked_script(asyncio.Event()) for _ in range(8)]
    scripts.append(text_response("done"))
    provider = ScriptedProvider(*scripts)
    session = make_session(tmp_path, provider, name="cancel-loop")

    for _ in range(8):
        turn_id = await session.start_turn("work")
        # Park the turn inside the provider script before cancelling, so the
        # script is genuinely in flight (not merely scheduled).
        await wait_for(
            lambda tid=turn_id, sess=session: any(
                e.type == "text.delta" and e.turn == tid
                for e in sess.events
            ),
            timeout=3.0,
        )
        session.cancel("stop")
        await wait_until_idle(session)
        assert session.active is False
        assert session.events[-1].type == "turn.cancelled"

    events = [event async for event in session.send("final")]
    assert events[-1].type == "turn.completed"


# ---------------------------------------------------------------------------
# Input queue ordering
# ---------------------------------------------------------------------------


async def test_queue_is_fifo_and_events_are_ordered(tmp_path):
    release = asyncio.Event()
    scripts = [parked_script(release)]
    scripts += [text_response(f"reply-{i}") for i in range(5)]
    provider = ScriptedProvider(*scripts)
    session = make_session(tmp_path, provider, name="queue")

    await session.start_turn("start")
    await wait_for(lambda: session.active)

    queued_ids = [session.enqueue(f"q{i}") for i in range(5)]
    assert session.queue_depth == 5

    release.set()
    await wait_until_idle(session)

    queued = [e for e in session.events if e.type == "input.queued"]
    consumed = [e for e in session.events if e.type == "input.consumed"]
    assert [e.data["queued_id"] for e in queued] == queued_ids
    assert [e.data["queued_id"] for e in consumed] == queued_ids
    assert_contiguous(session.events)
