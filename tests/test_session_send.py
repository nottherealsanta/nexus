"""Phase 1 ``Session.send`` integration tests (plan sections 4, 5.1)."""
import asyncio

import pytest

from nexus.config import Config
from nexus.config.schema import AgentSection, ConfigV2, ModelParams, ModelSection
from nexus.context import ContextManager
from nexus.core.loop import ResolvedModel
from nexus.errors import ConfigError, ProviderError, SessionBusy, SessionError
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.request import ModelRequest, ToolSchema
from nexus.model.router import ModelRouter
from nexus.model.stream import MessageStart, MessageStop, TextDelta, Usage
from nexus.session.manager import SessionManager
from nexus.session.records import MessageRecord


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
        return ResolvedModel(
            self.provider, "m", self.provider.capabilities("m")
        )


def make_session(tmp_path, provider, name="s"):
    assembler = RecordingAssembler()
    manager = SessionManager(
        tmp_path, assemble=assembler, provider_for=Resolver(provider)
    )
    return manager.open(name), assembler


async def drain(iterator):
    return [event async for event in iterator]


def scripted_config(model="scripted/first", temperature=None):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(
                default=model, params=ModelParams(temperature=temperature)
            )
        ),
    )


# ---------------------------------------------------------------------------
# Ordering and persistence
# ---------------------------------------------------------------------------


async def test_send_orders_and_persists_events(tmp_path):
    provider = ScriptedProvider(text_response("hello", usage=Usage(input=1, output=2)))
    session, _ = make_session(tmp_path, provider)

    events = await drain(session.send("hi"))
    types = [event.type for event in events]

    assert types[0] == "turn.started"
    assert types[-1] == "turn.completed"
    assert types.index("context.assembled") < types.index("model.started")
    assert types.index("text.delta") < types.index("text")
    assert types.count("turn.completed") == 1

    # Every yielded event was persisted first, in the same order.
    assert [event.type for event in session.events] == types
    seqs = [event.seq for event in session.events]
    assert seqs == sorted(seqs) and all(seq > 0 for seq in seqs)

    assert [m.role for m in session.messages] == ["user", "assistant"]
    assert session.messages[0].content[0].text == "hi"
    assert session.messages[1].content[0].text == "hello"
    assert session.active is False


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


async def test_concurrent_turn_is_rejected(tmp_path):
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), TextDelta(text="x"), Wait()]
    )
    session, _ = make_session(tmp_path, provider)

    stream = session.send("one")
    first = await stream.__anext__()
    assert first.type == "turn.started"

    with pytest.raises(SessionBusy):
        await session.send("two").__anext__()

    await stream.aclose()
    assert session.active is False


async def test_empty_input_is_rejected_before_lock(tmp_path):
    provider = ScriptedProvider(text_response("x"))
    session, _ = make_session(tmp_path, provider)
    with pytest.raises(ValueError):
        await session.send([]).__anext__()
    assert session.active is False


class _FakeGate:
    def __init__(self):
        self.resolved = []

    def resolve(self, request_id, decision):
        self.resolved.append((request_id, decision))
        return True


class _FakeToolBundle:
    def __init__(self, schemas, gate):
        self.schemas = schemas
        self.dispatcher = None
        self.gate = gate


async def test_send_freezes_schemas_and_delegates_resolution(tmp_path):
    provider = ScriptedProvider(text_response("hi"))
    schema = ToolSchema(name="Read", description="d", input_schema={"type": "object"})
    gate = _FakeGate()
    seen = {}

    def factory(*, config, session, turn_id, attended):
        seen["attended"] = attended
        return _FakeToolBundle([schema], gate)

    context = ContextManager(tmp_path, config=scripted_config("scripted/first"))
    router = ModelRouter({"scripted": provider}, default="scripted/first")
    session = SessionManager(
        tmp_path, assemble=context, provider_for=router, tools=factory
    ).open("tools")

    resolved = None
    async for event in session.send("go", attended=True):
        if event.type == "turn.started":
            resolved = session.resolve_permission("x", "allow_once")

    assert resolved is True
    assert gate.resolved == [("x", "allow_once")]
    assert provider.requests[0].tools == [schema]
    assert seen["attended"] is True
    # No active turn: resolution is safely rejected.
    assert session.resolve_permission("x", "allow_once") is False


async def test_send_without_wiring_raises(tmp_path):
    session = SessionManager(tmp_path).open("s")
    with pytest.raises(SessionError):
        await session.send("hi").__anext__()
    assert session.active is False


# ---------------------------------------------------------------------------
# Cancellation and early close
# ---------------------------------------------------------------------------


async def test_session_cancel_terminates_turn_and_releases_lock(tmp_path):
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), TextDelta(text="partial"), Wait()]
    )
    session, _ = make_session(tmp_path, provider)
    seen = []

    async def consume():
        async for event in session.send("go"):
            seen.append(event)

    task = asyncio.create_task(consume())
    for _ in range(200):
        await asyncio.sleep(0)
        if any(event.type == "text.delta" for event in seen):
            break
    assert any(event.type == "text.delta" for event in seen)

    session.cancel("user stopped")
    await asyncio.wait_for(task, timeout=2)

    assert seen[-1].type == "turn.cancelled"
    assert session.active is False


async def test_early_consumer_close_cancels_producer_and_releases_lock(tmp_path):
    provider = ScriptedProvider(
        [MessageStart(model="m", provider="scripted"), Wait()],
        text_response("ok"),
    )
    session, _ = make_session(tmp_path, provider)

    stream = session.send("one")
    first = await stream.__anext__()
    assert first.type == "turn.started"

    await stream.aclose()
    assert session.active is False

    # The lock is genuinely free: a fresh turn runs to completion.
    events = await drain(session.send("two"))
    assert events[-1].type == "turn.completed"


async def test_provider_failure_is_reported_and_lock_released(tmp_path):
    provider = ScriptedProvider([ProviderError("boom")])
    session, _ = make_session(tmp_path, provider)

    events = await drain(session.send("x"))
    types = [event.type for event in events]

    assert types[-1] == "turn.failed"
    assert "boom" in next(e for e in events if e.type == "turn.failed").data["error"]
    assert session.active is False


# ---------------------------------------------------------------------------
# Per-turn configuration reload
# ---------------------------------------------------------------------------


async def test_config_reloads_between_turns(tmp_path):
    provider = ScriptedProvider(text_response("a"), text_response("b"))
    holder = {"config": scripted_config("scripted/first", temperature=0.1)}
    context = ContextManager(tmp_path, config_loader=lambda: holder["config"])
    router = ModelRouter({"scripted": provider}, default="scripted/first")
    session = SessionManager(
        tmp_path, assemble=context, provider_for=router
    ).open("cfg")

    await drain(session.send("one"))
    holder["config"] = scripted_config("scripted/second", temperature=0.9)
    await drain(session.send("two"))

    assert provider.requests[0].model == "first"
    assert provider.requests[1].model == "second"
    assert provider.requests[0].params.temperature == 0.1
    assert provider.requests[1].params.temperature == 0.9


# ---------------------------------------------------------------------------
# Dangling recovery (append-only durable history)
# ---------------------------------------------------------------------------


async def test_dangling_tool_use_is_recovered_before_assembly(tmp_path):
    provider = ScriptedProvider(text_response("done"))
    session, assembler = make_session(tmp_path, provider, name="d")
    session.append_message(Message(role="user", content=[Text(text="read it")]))
    session.append_message(
        Message(
            role="assistant",
            content=[ToolUse(id="call-1", name="Read", input={"path": "a"})],
        )
    )

    await drain(session.send("next"))

    first_messages = assembler.seen[0]
    # Recovery is an append-only user(ToolResult) message; the new input is
    # appended after it, so stored IR may contain consecutive user messages.
    assert [message.role for message in first_messages] == [
        "user",
        "assistant",
        "user",
        "user",
    ]
    recovered = first_messages[-2].content[0]
    assert isinstance(recovered, ToolResult)
    assert recovered.tool_use_id == "call-1"
    assert recovered.is_error is True
    assert first_messages[-1].content[0].text == "next"

    persisted = session.messages
    assert [message.role for message in persisted] == [
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
    ]


async def test_recovery_and_turn_grow_the_log_append_only(tmp_path):
    provider = ScriptedProvider(text_response("done"))
    session, _ = make_session(tmp_path, provider, name="append")
    session.append_message(Message(role="user", content=[Text(text="read it")]))
    session.append_message(
        Message(
            role="assistant",
            content=[ToolUse(id="call-1", name="Read", input={"path": "a"})],
        )
    )
    before = tuple(session.records)

    await drain(session.send("next"))

    after = tuple(session.records)
    assert after[: len(before)] == before  # durable records are only appended
    # Exactly recovery(ToolResult) + user + assistant were appended as messages.
    appended_messages = [
        record for record in after[len(before) :] if isinstance(record, MessageRecord)
    ]
    assert len(appended_messages) == 3
    assert len(session.messages) == 5


async def test_recovered_history_is_valid_anthropic_wire(tmp_path):
    from nexus.model.providers.anthropic import build_request_body

    provider = ScriptedProvider(text_response("done"))
    session, _ = make_session(tmp_path, provider, name="wire")
    session.append_message(
        Message(
            role="assistant",
            content=[ToolUse(id="call-1", name="Read", input={"path": "a"})],
        )
    )

    await drain(session.send("next"))

    request = provider.requests[0]
    # Durable IR is [assistant, user, user]; the adapter coalesces same-role.
    assert [message.role for message in request.messages] == [
        "assistant",
        "user",
        "user",
    ]
    body = build_request_body(request, model="m")
    assert [message["role"] for message in body["messages"]] == ["assistant", "user"]
    user_content = body["messages"][-1]["content"]
    assert user_content[0]["type"] == "tool_result"
    assert user_content[0]["tool_use_id"] == "call-1"
    assert user_content[-1] == {"type": "text", "text": "next"}


# ---------------------------------------------------------------------------
# Per-turn frozen configuration (one snapshot / one reload per turn)
# ---------------------------------------------------------------------------


async def test_active_turn_ignores_config_changes_between_iterations(tmp_path):
    (tmp_path / "SOUL.md").write_text("SOUL-A", encoding="utf-8")
    holder = {"config": scripted_config("scripted/first", temperature=0.1)}

    def mutate_midturn(req):
        # Runs after iteration 1's assembly, before iteration 2's assembly.
        holder["config"] = scripted_config("other/second", temperature=0.9)
        (tmp_path / "SOUL.md").write_text("SOUL-B", encoding="utf-8")
        return tool_response(("c1", "Read", {"path": "a"}))

    provider = ScriptedProvider([mutate_midturn], text_response("done"))
    other = ScriptedProvider(text_response("unused"), name="other")
    context = ContextManager(tmp_path, config_loader=lambda: holder["config"])
    router = ModelRouter(
        {"scripted": provider, "other": other}, default="scripted/first"
    )
    session = SessionManager(
        tmp_path, assemble=context, provider_for=router
    ).open("frozen")

    await drain(session.send("go"))

    assert len(provider.requests) == 2
    assert other.calls == 0
    for request in provider.requests:
        assert request.model == "first"
        assert request.provider == "scripted"
        assert request.params.temperature == 0.1
        assert "SOUL-A" in (request.system or "")
        assert "SOUL-B" not in (request.system or "")


async def test_one_config_reload_per_turn_across_iterations(tmp_path):
    calls = {"n": 0}
    holder = {"config": scripted_config("scripted/first", temperature=0.1)}

    def loader():
        calls["n"] += 1
        return holder["config"]

    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a"})),
        text_response("done"),
        text_response("again"),
    )
    context = ContextManager(tmp_path, config_loader=loader)
    router = ModelRouter({"scripted": provider}, default="scripted/first")
    session = SessionManager(tmp_path, assemble=context, provider_for=router).open("once")

    await drain(session.send("go"))
    assert provider.calls == 2  # two loop iterations
    assert calls["n"] == 1  # ...but exactly one config reload for the turn

    await drain(session.send("again"))
    assert calls["n"] == 2  # reloaded for the next turn


async def test_snapshot_failure_releases_without_mutating_log(tmp_path):
    provider = ScriptedProvider(text_response("x"))

    def loader():
        raise ConfigError("boom")

    context = ContextManager(tmp_path, config_loader=loader)
    router = ModelRouter({"scripted": provider}, default="scripted/first")
    session = SessionManager(tmp_path, assemble=context, provider_for=router).open("snap")

    stream = session.send("hi")
    with pytest.raises(ConfigError):
        await stream.__anext__()

    assert session.active is False
    assert session.messages == []
    assert session.events == []
    assert session.records == []
    assert provider.calls == 0


async def test_turn_limits_come_from_the_config_snapshot(tmp_path):
    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(max_iterations=1),
        ),
    )
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a"})), text_response("unreached")
    )
    context = ContextManager(tmp_path, config=config)
    router = ModelRouter({"scripted": provider}, default="scripted/m")
    session = SessionManager(tmp_path, assemble=context, provider_for=router).open("lim")

    events = await drain(session.send("go"))

    assert provider.calls == 1  # bounded to one iteration by the snapshot config
    assert events[-1].type == "turn.completed"
    assert events[-1].data["stop_reason"] == "max_iterations"


# ---------------------------------------------------------------------------
# Bounded, lossless fan-out
# ---------------------------------------------------------------------------


async def test_slow_consumer_yields_every_persisted_event(tmp_path):
    deltas = [TextDelta(text=f"t{i}") for i in range(40)]
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            *deltas,
            MessageStop(stop_reason="end_turn"),
        ]
    )
    manager = SessionManager(
        tmp_path,
        assemble=RecordingAssembler(),
        provider_for=Resolver(provider),
        event_buffer=2,
    )
    session = manager.open("slow")
    yielded = []

    async for event in session.send("go"):
        yielded.append(event)
        await asyncio.sleep(0.001)  # lag the producer to force backpressure

    assert [event.type for event in yielded] == [
        event.type for event in session.events
    ]
    assert yielded == session.events
    assert yielded[-1].type == "turn.completed"
    assert session.active is False


async def test_completed_turn_persists_snapshot_and_keeps_full_history(tmp_path):
    provider = ScriptedProvider(text_response("hello"), text_response("hello"))
    manager = SessionManager(
        tmp_path,
        assemble=RecordingAssembler(),
        provider_for=Resolver(provider),
        snapshot_every=1,
    )
    session = manager.open("snap")

    events = await drain(session.send("hi"))
    assert events[-1].type == "turn.completed"

    loaded = manager.store.load_snapshot("snap", session.read(force=True))
    assert loaded is not None
    # Snapshot-aware messages equal the authoritative full-log history.
    assert [m.content[0].text for m in loaded.messages] == ["hi", "hello"]
    assert [m.content[0].text for m in session.messages] == ["hi", "hello"]
    # A second turn continues from the snapshot boundary.
    events = await drain(session.send("again"))
    assert events[-1].type == "turn.completed"
    assert [m.content[0].text for m in session.messages] == [
        "hi",
        "hello",
        "again",
        "hello",
    ]


async def test_early_close_under_backpressure_releases_and_persists_terminal(tmp_path):
    deltas = [TextDelta(text=f"t{i}") for i in range(500)]
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            *deltas,
            MessageStop(stop_reason="end_turn"),
        ],
        text_response("done"),
    )
    manager = SessionManager(
        tmp_path,
        assemble=RecordingAssembler(),
        provider_for=Resolver(provider),
        event_buffer=1,
    )
    session = manager.open("stress")

    stream = session.send("go")
    first = await stream.__anext__()
    assert first.type == "turn.started"

    await asyncio.wait_for(stream.aclose(), timeout=2)

    assert session.active is False
    # Cancellation persisted the terminal event even though nothing consumed it.
    assert any(event.type == "turn.cancelled" for event in session.events)

    # The lock is free and a fresh turn completes.
    events = await drain(session.send("again"))
    assert events[-1].type == "turn.completed"
