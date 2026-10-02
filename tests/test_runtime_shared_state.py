"""Phase 3.5 H4: one ``Runtime`` serving concurrent sessions without bleed.

The runtime owns one provider map, one router, one shell ``JobRegistry``, one
``TodoStore``, and one ``SessionManager`` shared by every session. These tests
exercise that sharing through the *public* runtime API only: two sessions run
overlapping turns through the shared provider and tool stack with no cross-talk
in their logs or files; the session lock stays per id (so a turn on one id never
blocks another, while a second handle to a locked id is refused); the shared job
registry stays partitioned; and ``send`` remains the attached compatibility
wrapper it was before Phase 3.5.

All providers are offline scripts; shell jobs use harmless ``sleep`` processes
that are always reaped.
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
from nexus.errors import SessionBusy
from nexus.model.message import Text
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.runtime import Runtime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(*, mode="allow", unattended="deny") -> Config:
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
    """Bounded, cancellable idle wait so a hang is a failure, not a timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.active or session.queue_depth:
        if loop.time() > deadline:
            raise AssertionError("session did not settle within the timeout")
        await asyncio.sleep(0.001)


def parked_script(release):
    return [
        MessageStart(model="m", provider="scripted"),
        TextDelta(text="parked"),
        Wait(event=release),
        MessageStop(stop_reason="end_turn"),
    ]


def user_texts(session):
    texts = []
    for message in session.messages:
        if message.role != "user" or not message.content:
            continue
        block = message.content[0]
        if isinstance(block, Text):
            texts.append(block.text)
    return texts


def turn_ids(events):
    return {event.turn for event in events if event.turn is not None}


# ---------------------------------------------------------------------------
# Concurrent sessions over shared state
# ---------------------------------------------------------------------------


async def test_two_sessions_run_overlapping_turns_without_crosstalk(tmp_path):
    barrier = asyncio.Event()
    arrived = {"count": 0}

    async def respond(request):
        last = request.messages[-1]
        text = None
        if (
            last.role == "user"
            and last.content
            and isinstance(last.content[0], Text)
        ):
            text = last.content[0].text
        if text is not None and text.startswith("go:"):
            tag = text.split(":", 1)[1]
            arrived["count"] += 1
            if arrived["count"] >= 2:
                barrier.set()
            else:
                await barrier.wait()
            return tool_response(
                ("c1", "Write", {"path": f"{tag}.txt", "content": tag})
            )
        return text_response("done")

    provider = ScriptedProvider([respond], [respond], [respond], [respond])
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("shared-a")
    b = runtime.session("shared-b")

    await asyncio.gather(a.start_turn("go:a"), b.start_turn("go:b"))
    # The barrier proves overlap: the first turn only proceeds once the second
    # one has arrived.
    await asyncio.wait_for(barrier.wait(), 3)
    await asyncio.gather(wait_until_idle(a), wait_until_idle(b))

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b"
    assert provider.calls == 4

    assert user_texts(a) == ["go:a"]
    assert user_texts(b) == ["go:b"]
    assert turn_ids(a.events).isdisjoint(turn_ids(b.events))
    await runtime.aclose()


async def test_concurrent_sends_do_not_bleed_across_sessions(tmp_path):
    def respond(request):
        last = request.messages[-1]
        text = last.content[0].text if last.content else ""
        return text_response(f"reply:{text}")

    provider = ScriptedProvider([respond], [respond])
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("iso-a")
    b = runtime.session("iso-b")

    async def drain(iterator):
        return [event async for event in iterator]

    a_events, b_events = await asyncio.gather(
        drain(a.send("to-a")), drain(b.send("to-b"))
    )

    assert user_texts(a) == ["to-a"]
    assert user_texts(b) == ["to-b"]
    assert any(
        event.type == "text" and event.data.get("text") == "reply:to-a"
        for event in a_events
    )
    assert not any("to-b" in str(event.data) for event in a_events)
    assert not any("to-a" in str(event.data) for event in b_events)
    await runtime.aclose()


async def test_shared_registries_are_runtime_owned_and_partitioned(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("x")))
    registry = runtime.job_registry
    assert registry is not None
    assert runtime.job_registry is registry  # one shared registry
    try:
        job_a = await registry.spawn("sleep 30", session_id="a", cwd=tmp_path)
        job_b = await registry.spawn("sleep 30", session_id="b", cwd=tmp_path)
        assert job_a.done is False and job_b.done is False

        assert await runtime.close_session_jobs("a") is True
        assert job_a.done is True
        assert job_b.done is False
        assert registry.job(job_a.job_id, session_id="a") is None
        assert registry.job(job_b.job_id, session_id="b") is job_b
        assert await runtime.close_session_jobs("missing") is False
    finally:
        await runtime.aclose()
    assert job_b.done is True


# ---------------------------------------------------------------------------
# Session lock scope
# ---------------------------------------------------------------------------


async def test_session_lock_is_per_id_not_global(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(
        parked_script(release),
        text_response("other"),
        text_response("again"),
    )
    runtime = make_runtime(tmp_path, provider)
    a = runtime.session("lock-a")
    b = runtime.session("lock-b")
    a_again = runtime.session("lock-a")  # a second handle to the same session

    await a.start_turn("one")
    # Wait until ``a`` genuinely holds the lock and is parked in the provider.
    await wait_for(lambda: any(e.type == "text.delta" for e in a.events))

    # A different id is not blocked by a's active turn.
    await b.start_turn("other")
    await wait_until_idle(b)
    assert b.active is False

    # A second handle to the same id is refused while the turn runs.
    with pytest.raises(SessionBusy):
        await a_again.start_turn("two")

    release.set()
    await wait_until_idle(a)
    assert a.active is False

    # The lock is genuinely free for the other handle afterwards.
    await a_again.start_turn("again")
    await wait_until_idle(a_again)
    assert a_again.active is False
    await runtime.aclose()


# ---------------------------------------------------------------------------
# send compatibility
# ---------------------------------------------------------------------------


async def test_send_matches_the_persisted_log(tmp_path):
    provider = ScriptedProvider(text_response("hello"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("send-compat")

    events = [event async for event in session.send("hi")]

    assert events == session.events
    assert events[0].type == "turn.started"
    assert events[-1].type == "turn.completed"
    assert session.active is False
    assert [message.role for message in session.messages] == ["user", "assistant"]
    await runtime.aclose()


async def test_send_early_close_cancels_and_frees_the_lock(tmp_path):
    release = asyncio.Event()
    provider = ScriptedProvider(parked_script(release), text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("send-cancel")

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
    await runtime.aclose()


async def test_two_runtimes_execute_bash_with_isolated_environments(tmp_path, monkeypatch):
    """SHARED_DAEMON_PLAN B1: actual tool turns must use each owner's snapshot."""
    monkeypatch.setenv("NEXUS_TEST_VALUE", "daemon")
    command = 'printf "%s" "$NEXUS_TEST_VALUE" > value.txt'
    runtimes = []
    try:
        for name in ("a", "b"):
            workspace = tmp_path / name
            workspace.mkdir()
            environment = {"PATH": "/usr/bin:/bin", "NEXUS_TEST_VALUE": name}
            provider = ScriptedProvider(
                tool_response(("shell", "bash", {"command": command})),
                text_response("done"),
            )
            runtime = Runtime(
                workspace, config=make_config(), environ=environment,
                providers={"scripted": provider},
            )
            runtimes.append(runtime)
            # Changing the caller's mapping cannot change shell job inheritance.
            environment["NEXUS_TEST_VALUE"] = "mutated"

        async def run(runtime):
            return [event async for event in runtime.session("same-id").send("run bash")]

        await asyncio.gather(*(run(runtime) for runtime in runtimes))
        for name in ("a", "b"):
            assert (tmp_path / name / "value.txt").read_text() == name

        await runtimes[0].aclose()
        assert not runtimes[1].job_registry.closed
        job = await runtimes[1].job_registry.spawn(
            command, cwd=tmp_path / "b", session_id="after-close"
        )
        await asyncio.wait_for(job.wait(), 3)
        assert (tmp_path / "b" / "value.txt").read_text() == "b"
    finally:
        await asyncio.gather(*(runtime.aclose() for runtime in runtimes))
