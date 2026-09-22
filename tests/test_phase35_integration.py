"""Phase 3.5 integration gaps: shared handles, unattended policy, cancellable
waits, and input-queue crash recovery.

Four seams are closed here:

* **One live handle per session id.** ``SessionManager`` caches a single
  :class:`~nexus.session.session.Session`, so every ``Runtime.session(id)``
  caller shares one bus, presence count, input queue, and active turn. The
  eviction seam frees a handle without disturbing fork/replay.
* **``permissions.on_unattended`` wired to derived presence.** The runtime feeds
  the config policy to the session, which applies ``deny``/``allow``/``fail_turn``
  when the last viewer leaves an approval pending (or a request arrives with zero
  viewers). First resolver wins.
* **Cancellable waits.** ``wait_turn``/``wait_idle`` are ``asyncio.wait_for``
  compatible and shield the detached turn from waiter cancellation.
* **Queue rehydration.** Pending ``input.queued`` submissions not yet consumed or
  dropped survive a crash/restart; malformed payloads fail safe.

All providers are offline ``ScriptedProvider`` scripts; no network is touched.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.errors import SessionBusy
from nexus.events import Event
from nexus.model.message import Document, Image
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.stream import (
    MessageStart,
    MessageStop,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)
from nexus.runtime import Runtime
from nexus.session.manager import SessionManager
from nexus.tools.permissions import Decision

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, mode="ask", unattended="deny") -> Config:
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


def make_runtime(tmp_path, provider, **config_kwargs) -> Runtime:
    return Runtime(
        tmp_path,
        config=make_config(**config_kwargs),
        providers={"scripted": provider},
    )


async def wait_for(predicate, timeout=3.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


async def wait_until_idle(session, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)


async def attach_viewer(session):
    stream = session.subscribe(0)
    await stream.__anext__()
    return stream


def parked_script(release):
    return [
        MessageStart(model="m", provider="scripted"),
        TextDelta(text="parked"),
        Wait(event=release),
        MessageStop(stop_reason="end_turn"),
    ]


def parked_write_script(release, path="b.txt", content="B"):
    return [
        MessageStart(model="m", provider="scripted"),
        TextDelta(text="working"),
        Wait(event=release),
        ToolCallStart(id="c1", name="Write"),
        ToolCallEnd(id="c1", input={"path": path, "content": content}),
        MessageStop(stop_reason="tool_use"),
    ]


def permission_resolutions(session):
    return [e for e in session.events if e.type == "permission.resolved"]


# ---------------------------------------------------------------------------
# One live handle per session id
# ---------------------------------------------------------------------------


async def test_runtime_session_returns_one_live_handle_per_id(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("hi")))
    first = runtime.session("shared")
    second = runtime.session("shared")

    assert first is second
    assert runtime.sessions.open("shared") is first
    assert runtime.sessions.live_sessions == ("shared",)

    evicted = runtime.sessions.evict("shared")
    assert evicted is first
    fresh = runtime.session("shared")
    assert fresh is not first
    assert runtime.sessions.live_sessions == ("shared",)
    await runtime.aclose()


async def test_cross_handle_presence_and_subscription_are_shared(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("hi")))
    a = runtime.session("shared-presence")
    b = runtime.session("shared-presence")
    assert a is b

    stream = await attach_viewer(a)
    assert b.viewers == 1
    assert b.attended is True

    await stream.aclose()
    assert b.viewers == 0
    assert a.attended is False

    kinds = [e.type for e in a.events]
    assert kinds.count("presence.joined") == 1
    assert kinds.count("presence.left") == 1
    await runtime.aclose()


async def test_cross_handle_active_turn_is_shared_and_exclusive(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("shared-turn")
    b = runtime.session("shared-turn")

    turn_id = await a.start_turn("one")
    assert b.active is True
    assert b.active_turn_id == turn_id
    with pytest.raises(SessionBusy):
        await b.start_turn("two")

    release.set()
    await b.wait_idle()
    assert a.active is False
    assert b.events[-1].type == "turn.completed"
    await runtime.aclose()


async def test_manager_cache_preserves_handle_bind_updates(tmp_path):
    manager = SessionManager(tmp_path)
    first = manager.open("bind")
    first.bind(attended=True)

    assert manager.open("bind") is first
    assert manager.open("bind").attended is True

    assert manager.close("bind") is True
    assert manager.evict("bind") is None  # already evicted
    replaced = manager.open("bind")
    assert replaced is not first
    assert manager.live_sessions == ("bind",)
    assert manager.close_all() == [replaced]
    assert manager.live_sessions == ()


def test_recover_option_still_applies_to_a_cached_handle(tmp_path):
    from nexus.model.message import Message, ToolUse

    manager = SessionManager(tmp_path)
    handle = manager.open("recover-cached", recover=False)
    handle.append_message(
        Message(role="assistant", content=[ToolUse(id="c1", name="Read", input={})])
    )
    assert handle.recovered == ()

    # Opening with recover=True returns the same live handle and applies recovery.
    reopened = manager.open("recover-cached", recover=True)
    assert reopened is handle
    assert reopened.messages[-1].content[0].tool_use_id == "c1"


async def test_eviction_does_not_disturb_fork_or_replay(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("evict-src")
    session.append_event(Event(type="turn.started", data={"i": 1}))
    manager.evict("evict-src")

    child = manager.fork("evict-src")
    replayed = [event async for event in manager.replay("evict-src")]
    assert [event.type for event in replayed] == ["turn.started"]
    assert child.events[0].data == {"i": 1}


# ---------------------------------------------------------------------------
# permissions.on_unattended -> derived presence fallback
# ---------------------------------------------------------------------------


async def test_config_dictated_allow_applied_on_viewer_drop(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, unattended="allow")
    session = runtime.session("cfg-allow")
    stream = await attach_viewer(session)

    await session.start_turn("go")
    await wait_for(lambda: session.pending_permissions)

    await stream.aclose()
    await wait_until_idle(session)

    resolved = permission_resolutions(session)
    assert resolved and resolved[-1].data["decision"] == "allow_once"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"
    await runtime.aclose()


async def test_config_dictated_deny_applied_on_viewer_drop(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, unattended="deny")
    session = runtime.session("cfg-deny")
    stream = await attach_viewer(session)

    await session.start_turn("go")
    await wait_for(lambda: session.pending_permissions)

    await stream.aclose()
    await wait_until_idle(session)

    resolved = permission_resolutions(session)
    assert resolved and resolved[-1].data["decision"] == "deny_once"
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


async def test_config_dictated_fail_turn_fails_on_viewer_drop(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, unattended="fail_turn")
    session = runtime.session("cfg-fail")
    stream = await attach_viewer(session)

    await session.start_turn("go")
    await wait_for(lambda: session.pending_permissions)

    await stream.aclose()
    await wait_until_idle(session)

    assert session.events[-1].type == "turn.failed"
    assert not any(e.type == "turn.completed" for e in session.events)
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


async def test_config_allow_with_zero_viewers_auto_allows_without_prompt(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, unattended="allow")
    session = runtime.session("zero-allow")
    assert session.viewers == 0

    await session.start_turn("go")
    await wait_until_idle(session)

    assert session.events[-1].type == "turn.completed"
    assert not any(e.type == "permission.requested" for e in session.events)
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B"
    await runtime.aclose()


async def test_config_fail_turn_with_zero_viewers_fails_without_prompt(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, unattended="fail_turn")
    session = runtime.session("zero-fail")
    assert session.viewers == 0

    await session.start_turn("go")
    await wait_until_idle(session)

    assert session.events[-1].type == "turn.failed"
    assert not any(e.type == "permission.requested" for e in session.events)
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


async def test_first_resolver_wins_over_unattended_fallback(tmp_path):
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "b.txt", "content": "B"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, unattended="allow")
    session = runtime.session("first-wins-fallback")
    stream = await attach_viewer(session)

    await session.start_turn("go")
    await wait_for(lambda: session.pending_permissions)
    request_id = session.pending_permissions[0]

    # The viewer answers first; the later presence fallback must lose.
    assert session.resolve_permission(request_id, Decision.DENY_ONCE) is True
    await stream.aclose()
    await wait_until_idle(session)

    resolved = permission_resolutions(session)
    assert len(resolved) == 1
    assert resolved[0].data["decision"] == "deny_once"
    assert not (tmp_path / "b.txt").exists()
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Cancellable, wait_for-compatible waits that shield the turn
# ---------------------------------------------------------------------------


async def test_wait_turn_reports_timeout_and_keeps_turn_running(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("wait-turn-timeout")
    turn_id = await session.start_turn("one")
    await wait_for(lambda: session.active)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(session.wait_turn(turn_id), 0.05)
    assert session.active is True  # the timeout did not cancel the turn

    release.set()
    await asyncio.wait_for(session.wait_idle(), 3)
    assert session.events[-1].type == "turn.completed"
    await runtime.aclose()


async def test_wait_idle_reports_timeout_and_keeps_turn_running(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("wait-idle-timeout")
    await session.start_turn("one")
    await wait_for(lambda: session.active)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(session.wait_idle(), 0.05)
    assert session.active is True

    release.set()
    await asyncio.wait_for(session.wait_idle(), 3)
    assert session.events[-1].type == "turn.completed"
    await runtime.aclose()


async def test_cancelling_wait_turn_waiter_does_not_cancel_the_turn(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("waiter-cancel")
    turn_id = await session.start_turn("one")
    await wait_for(lambda: session.active)

    waiter = asyncio.create_task(session.wait_turn(turn_id))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert session.active is True  # shielded from waiter cancellation

    release.set()
    await session.wait_idle()
    assert session.events[-1].type == "turn.completed"
    await runtime.aclose()


async def test_wait_turn_on_a_cancelled_turn_returns_quietly(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("cancelled-wait")
    turn_id = await session.start_turn("one")
    await wait_for(lambda: session.active)

    waiter = asyncio.create_task(session.wait_turn(turn_id))
    await asyncio.sleep(0)
    session.cancel("stop")
    await waiter  # a turn that ended cancelled must not raise into the waiter
    assert session.events[-1].type == "turn.cancelled"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Input-queue rehydration across a crash/restart
# ---------------------------------------------------------------------------


async def test_pending_queue_rehydrates_and_drains_fifo(tmp_path):
    provider = ScriptedProvider(
        text_response("one"), text_response("two"), text_response("three")
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("queue-restart")
    first = session.enqueue("q1")
    second = session.enqueue("q2")
    assert session.queued_ids == (first, second)

    # Simulate a restart: drop the live handle and reopen from the log.
    runtime.sessions.evict("queue-restart")
    reopened = runtime.session("queue-restart")
    assert reopened is not session
    assert reopened.active is False
    assert reopened.queue_depth == 2
    assert reopened.queued_ids == (first, second)

    # No event is re-emitted by rehydration.
    queued = [e for e in reopened.events if e.type == "input.queued"]
    assert [e.data["queued_id"] for e in queued] == [first, second]

    # A single no-input turn drains the whole FIFO at successive boundaries.
    await reopened.start_turn()
    await wait_until_idle(reopened)

    consumed = [e.data["queued_id"] for e in reopened.events if e.type == "input.consumed"]
    assert consumed == [first, second]
    user_texts = [
        message.content[0].text
        for message in reopened.messages
        if message.role == "user" and message.content
    ]
    assert user_texts == ["q1", "q2"]
    await runtime.aclose()


async def test_rehydration_drops_consumed_and_dropped_ids(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("queue-transitions")
    session.append_event(
        Event(
            type="input.queued",
            data={"queued_id": "a", "content": [{"type": "text", "text": "a"}]},
        )
    )
    session.append_event(Event(type="input.consumed", data={"queued_id": "a"}))
    session.append_event(
        Event(
            type="input.queued",
            data={"queued_id": "b", "content": [{"type": "text", "text": "b"}]},
        )
    )
    session.append_event(
        Event(type="input.dropped", data={"queued_id": "b", "reason": "gone"})
    )
    session.append_event(
        Event(
            type="input.queued",
            data={"queued_id": "c", "content": [{"type": "text", "text": "c"}]},
        )
    )

    manager.evict("queue-transitions")
    reopened = manager.open("queue-transitions")
    assert reopened.queued_ids == ("c",)


def test_malformed_input_events_fail_safe_on_open(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("queue-malformed")
    session.append_event(
        Event(
            type="input.queued",
            data={"queued_id": "ok", "content": [{"type": "text", "text": "ok"}]},
        )
    )
    # Missing id.
    session.append_event(
        Event(type="input.queued", data={"content": [{"type": "text", "text": "x"}]})
    )
    # Non-string id.
    session.append_event(
        Event(
            type="input.queued",
            data={"queued_id": 5, "content": [{"type": "text", "text": "x"}]},
        )
    )
    # Content is not a list of blocks.
    session.append_event(
        Event(type="input.queued", data={"queued_id": "not-a-list", "content": "nope"})
    )
    # Unknown block tag.
    session.append_event(
        Event(
            type="input.queued",
            data={"queued_id": "bad-tag", "content": [{"type": "bogus"}]},
        )
    )
    # Empty content.
    session.append_event(
        Event(type="input.queued", data={"queued_id": "empty", "content": []})
    )

    manager.evict("queue-malformed")
    reopened = manager.open("queue-malformed")  # must not raise
    assert reopened.queued_ids == ("ok",)
    assert reopened.queue_depth == 1


async def test_queued_multimodal_bytes_rehydrate_losslessly(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("queue-multimodal")
    image = Image(media_type="image/png", data=b"\x89PNG\x00\xff\x01")
    document = Document(
        media_type="application/pdf", data=b"%PDF-1.7\x00binary", title="d"
    )
    queued_id = session.enqueue([image, document])

    # Simulate a restart: the durable payload must reopen with exact bytes.
    runtime.sessions.evict("queue-multimodal")
    reopened = runtime.session("queue-multimodal")
    assert reopened.queued_ids == (queued_id,)

    await reopened.start_turn()
    await wait_until_idle(reopened)
    blocks = reopened.messages[0].content
    assert isinstance(blocks[0], Image) and blocks[0].data == image.data
    assert isinstance(blocks[1], Document) and blocks[1].data == document.data
    await runtime.aclose()


def test_tagged_base64_queued_payload_rehydrates(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("queue-tagged")
    encoded = base64.b64encode(b"\x00\x01\xff").decode("ascii")
    session.append_event(
        Event(
            type="input.queued",
            data={
                "queued_id": "tagged",
                "content": [
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data": {"__nexus_bytes__": encoded},
                        "url": None,
                    }
                ],
            },
        )
    )

    manager.evict("queue-tagged")
    reopened = manager.open("queue-tagged")
    assert reopened.queue_depth == 1
    (item,) = list(reopened._queue)
    block = item.content[0]
    assert isinstance(block, Image)
    assert block.data == b"\x00\x01\xff"


async def test_evict_refuses_active_and_viewed_handles(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("live-evict")

    await session.start_turn("one")
    await wait_for(lambda: session.active)
    with pytest.raises(SessionBusy):
        runtime.sessions.evict("live-evict")
    assert runtime.sessions.open("live-evict") is session

    release.set()
    await wait_until_idle(session)

    stream = await attach_viewer(session)
    assert session.viewers == 1
    with pytest.raises(SessionBusy):
        runtime.sessions.evict("live-evict")
    await stream.aclose()
    assert runtime.sessions.evict("live-evict") is session
    await runtime.aclose()
