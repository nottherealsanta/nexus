"""Phase 1 Runtime tests: composition, lifecycle, and provider ownership."""
import asyncio
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentsSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ProviderSection,
)
from nexus.events import Event
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime, _ChildEventSink
from nexus.tools.manager import ToolManager

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


async def test_child_event_sink_propagates_todo_append_failure():
    relayed = []

    class Session:
        def append_event(self, _event):
            raise OSError("child log unavailable")

    async def relay(event_type, data):
        relayed.append((event_type, data))

    sink = _ChildEventSink(Session(), relay)
    event = Event(type="todo.updated", data={"todos": [], "revision": 1})

    try:
        await sink.emit(event)
    except OSError as exc:
        assert str(exc) == "child log unavailable"
    else:
        raise AssertionError("todo.updated append failure was suppressed")

    assert relayed == []


def scripted_config(model="scripted/sm"):
    return Config(model=model, version=2, v2=ConfigV2(model=ModelSection(default=model)))


def anthropic_config(model="anthropic/claude-test"):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            providers={"anthropic": ProviderSection(api_key="test-key")},
        ),
    )


# ---------------------------------------------------------------------------
# Multi-turn conversation
# ---------------------------------------------------------------------------


async def test_two_turn_scripted_conversation_sees_exact_ir(tmp_path):
    provider = ScriptedProvider(text_response("first"), text_response("second"))
    runtime = Runtime(tmp_path, config=scripted_config(), providers={"scripted": provider})
    session = runtime.session("conv")

    first_events = [event async for event in session.send("one")]
    second_events = [event async for event in session.send("two")]

    assert provider.calls == 2
    first, second = provider.requests

    assert [(m.role, m.content[0].text) for m in first.messages] == [("user", "one")]
    assert [(m.role, m.content[0].text) for m in second.messages] == [
        ("user", "one"),
        ("assistant", "first"),
        ("user", "two"),
    ]
    assert first.model == "sm"
    assert first.provider == "scripted"
    assert second.model == "sm"

    assert first_events[0].type == "turn.started"
    assert first_events[-1].type == "turn.completed"
    assert second_events[-1].type == "turn.completed"


async def test_runtime_open_and_default_session_stored_in_state_db(tmp_path):
    """Sessions land in the shared state database, not ``<workspace>/.nexus`` (STATE_PLAN §2)."""
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime.open(tmp_path, config=scripted_config(), providers={"scripted": provider})
    session = runtime.session("main")

    await _drain(session.send("hi"))

    assert runtime.sessions.exists("main")
    assert not (tmp_path / ".nexus").exists()


async def test_iteration_path_guard_protects_explicit_home_state_database(tmp_path):
    from types import SimpleNamespace

    from nexus.config.paths import state_db_path
    from nexus.session.db import StateDatabase
    from nexus.tools.permissions import PathSecurityError

    workspace = tmp_path / "workspace"
    home = tmp_path / "custom-home"
    workspace.mkdir()
    home.mkdir()
    config = Config(
        model="scripted/sm",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/sm"),
            permissions=PermissionsSection(write_roots=[str(tmp_path)]),
        ),
    )
    runtime = Runtime(
        workspace,
        home=home,
        config=config,
        providers={"scripted": ScriptedProvider(text_response("unused"))},
    )

    database = state_db_path(home)
    assert isinstance(runtime._state_db, StateDatabase)
    assert runtime._state_db.path == database
    manager = runtime._build_iteration_manager(
        config, SimpleNamespace(tools={})
    )

    try:
        for protected in (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        ):
            for for_write in (False, True):
                with pytest.raises(PathSecurityError) as exc_info:
                    manager.path_guard.resolve(str(protected), for_write=for_write)
                assert exc_info.value.code == "state_db"
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Provider lifecycle / ownership
# ---------------------------------------------------------------------------


async def test_injected_providers_are_not_closed_by_runtime(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=scripted_config(), providers={"scripted": provider})

    await runtime.aclose()
    await runtime.aclose()  # idempotent

    assert provider.closed is False
    assert runtime.closed is True


async def test_owned_anthropic_provider_is_closed(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=anthropic_config(),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")),
    )
    provider = runtime.providers["anthropic"]
    client = provider.transport.client
    assert client.is_closed is False

    await runtime.aclose()

    assert client.is_closed is True
    assert runtime.closed is True


async def test_context_manager_closes_owned_providers(tmp_path):
    async with Runtime(
        tmp_path,
        config=anthropic_config(),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")),
    ) as runtime:
        client = runtime.providers["anthropic"].transport.client
        assert client.is_closed is False
    assert client.is_closed is True


async def test_runtime_snapshots_config_once_at_construction(tmp_path):
    calls = {"n": 0}
    config = anthropic_config()

    def loader():
        calls["n"] += 1
        return config

    runtime = Runtime(
        tmp_path,
        config_loader=loader,
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )

    # Providers and router share one construction-time snapshot.
    assert calls["n"] == 1
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Normal Anthropic construction without network
# ---------------------------------------------------------------------------


async def test_anthropic_runtime_with_mock_transport(tmp_path):
    fixture = (FIXTURES / "text_stream.sse").read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=fixture)

    runtime = Runtime(
        tmp_path,
        config=anthropic_config(),
        http_transport=httpx.MockTransport(handler),
    )
    session = runtime.session("live")

    events = [event async for event in session.send("hello")]

    assert requests[0].headers["x-api-key"] == "test-key"
    assert any(event.type == "text" and event.data["text"] == "Hello" for event in events)
    assert events[-1].type == "turn.completed"

    await runtime.aclose()


# ---------------------------------------------------------------------------
# Native tool infrastructure
# ---------------------------------------------------------------------------


async def test_runtime_builds_native_tools_and_closes_owned_registry(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    config = scripted_config()
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})

    assert runtime.job_registry is not None
    assert runtime.job_registry.closed is False

    session = runtime.session("t")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="turn-1", attended=False
    )
    assert turn is not None
    assert len(turn.schemas) == 13
    assert turn.gate is not None

    await runtime.aclose()
    assert runtime.job_registry.closed is True


async def test_runtime_injected_tool_manager_is_used(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    config = scripted_config()
    manager = ToolManager(config, workspace=tmp_path, profile="research")
    runtime = Runtime(
        tmp_path, config=config, providers={"scripted": provider}, tools=manager
    )
    session = runtime.session("t")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="turn-1", attended=False
    )
    assert turn.manager.profile == manager.profile
    assert turn.manager.names == ("read", "glob", "grep", "todowrite", "question", "skill")
    assert [schema.name for schema in turn.schemas] == ["read", "glob", "grep", "todowrite", "question", "skill"]
    await runtime.aclose()


async def test_injected_todo_store_survives_agent_definition_rebuild_and_reopen(tmp_path):
    from nexus.tools.builtin.todo import TodoStore

    config = Config(
        model="scripted/sm",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/sm"),
            agents=AgentsSection(enabled=True),
        ),
    )
    role_dir = tmp_path / ".agents" / "agents"
    role_dir.mkdir(parents=True)
    (role_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review\ncontexts: [root]\n"
        "profile: research\n---\nReview carefully.\n",
        encoding="utf-8",
    )

    original_manager = ToolManager(
        config, workspace=tmp_path, profile="research", todo_store=TodoStore()
    )
    first_runtime = Runtime(
        tmp_path,
        config=config,
        providers={"scripted": ScriptedProvider(text_response("saved"))},
        tools=original_manager,
    )
    first_session = first_runtime.session("injected-todo-reopen")
    first_session.append_event(
        Event(
            type="todo.updated",
            session=first_session.id,
            data={
                "session": first_session.id,
                "agent_id": "root",
                "revision": 4,
                "todos": [
                    {"id": "persisted", "content": "reopened", "status": "pending"}
                ],
            },
        )
    )
    await first_runtime.aclose()
    assert original_manager.closed is False

    reopened_manager = ToolManager(
        config, workspace=tmp_path, profile="research", todo_store=TodoStore()
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"scripted": ScriptedProvider(text_response("reopened"))},
        tools=reopened_manager,
    )
    session = runtime.session("injected-todo-reopen", create=False)
    assert reopened_manager.todo_store.revision(session.id, "root") == 4

    session._turn_agent_definition = runtime.agents.resolve("reviewer", context="root")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="agent-rebuild", attended=False
    )

    assert turn.manager is not reopened_manager
    assert turn.manager.todo_store is reopened_manager.todo_store
    assert turn.manager.todo_store.get(session.id, "root")[0].content == "reopened"
    await runtime.aclose()
    assert reopened_manager.closed is False


async def test_iteration_tool_managers_are_not_retained(tmp_path):
    from types import SimpleNamespace

    runtime = Runtime(
        tmp_path,
        config=scripted_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )

    for _ in range(20):
        manager = runtime._build_iteration_manager(
            scripted_config(), SimpleNamespace(tools={})
        )
        assert manager._owns_job_registry is False
        assert manager._owns_todo_store is False
        assert len(runtime._owned_tools) == 0
        assert len(runtime._tracked_tools) <= 1

    await runtime.aclose()


def test_tool_dispatcher_context_has_stable_root_and_child_agent_ids(tmp_path):
    from types import SimpleNamespace

    from nexus.runtime import _ToolDispatcherAdapter
    from nexus.tools.spec import ToolCall

    config = scripted_config()
    outbound_http = object()
    manager = ToolManager(config, workspace=tmp_path, profile="research")
    first_turn = _ToolDispatcherAdapter(
        manager,
        workspace=tmp_path,
        session_id="session-1",
        turn_id="turn-1",
        config=config,
        agent_id="root",
        outbound_http=outbound_http,
    )
    next_turn = _ToolDispatcherAdapter(
        manager,
        workspace=tmp_path,
        session_id="session-1",
        turn_id="turn-2",
        config=config,
        agent_id="root",
        outbound_http=outbound_http,
    )
    child = _ToolDispatcherAdapter(
        manager,
        workspace=tmp_path,
        session_id="child-session",
        turn_id="child-turn",
        config=config,
        agent_id="session-1/sub/1",
        outbound_http=outbound_http,
    )
    grandchild = _ToolDispatcherAdapter(
        manager,
        workspace=tmp_path,
        session_id="grandchild-session",
        turn_id="grandchild-turn",
        config=config,
        agent_id="session-1/sub/1/sub/1",
        outbound_http=outbound_http,
    )
    call = ToolCall(id="call", name="todowrite")
    spec = SimpleNamespace(agent_id="session-1/sub/1")

    assert first_turn._ctx_factory(call, spec).agent_id == "root"
    assert next_turn._ctx_factory(call, spec).agent_id == "root"
    assert child._ctx_factory(call, spec).agent_id == "session-1/sub/1"
    contexts = (
        first_turn._ctx_factory(call, spec),
        next_turn._ctx_factory(call, spec),
        child._ctx_factory(call, spec),
        grandchild._ctx_factory(call, spec),
    )
    assert all(context.outbound_http is outbound_http for context in contexts)


@pytest.mark.parametrize("owned", [False, True])
async def test_runtime_outbound_http_service_ownership_and_context_sharing(
    tmp_path, owned
):
    class OutboundService:
        def __init__(self):
            self.close_count = 0

        async def get(self, *_args, **_kwargs):
            raise AssertionError("the lifecycle test must not make HTTP requests")

        async def aclose(self):
            self.close_count += 1

    from nexus.runtime import _ToolDispatcherAdapter
    from nexus.tools.spec import ToolCall

    service = OutboundService()
    config = scripted_config()
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"scripted": ScriptedProvider(text_response("ok"))},
        outbound_http_service=service,
        owns_outbound_http_service=owned,
    )
    session = runtime.session("outbound-service")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="turn-1", attended=False
    )
    call = ToolCall(id="read-call", name="read")
    root_context = turn.dispatcher._ctx_factory(call, object())
    child_dispatcher = _ToolDispatcherAdapter(
        turn.manager,
        workspace=tmp_path,
        session_id="child-session",
        turn_id="child-turn",
        config=config,
        agent_id="root/sub/1",
        outbound_http=runtime._outbound_http_service,
    )
    grandchild_dispatcher = _ToolDispatcherAdapter(
        turn.manager,
        workspace=tmp_path,
        session_id="grandchild-session",
        turn_id="grandchild-turn",
        config=config,
        agent_id="root/sub/1/sub/1",
        outbound_http=runtime._outbound_http_service,
    )

    assert root_context.outbound_http is service
    assert child_dispatcher._ctx_factory(call, object()).outbound_http is service
    assert grandchild_dispatcher._ctx_factory(call, object()).outbound_http is service
    # One service object carries the outbound rate/concurrency limits across
    # every agent context, with no network request required.
    assert runtime._outbound_http_service is service

    await runtime.aclose()
    await runtime.aclose()
    assert service.close_count == int(owned)


def test_runtime_default_outbound_service_shares_one_rate_limiter(tmp_path):
    from nexus.net import SafeOutboundHTTPService

    runtime = Runtime(
        tmp_path,
        config=scripted_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )

    service = runtime._outbound_http_service
    assert isinstance(service, SafeOutboundHTTPService)
    assert service._bucket is runtime._outbound_http_service._bucket


async def test_child_runtime_passes_spec_agent_id_to_tool_context(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import nexus.runtime as runtime_module
    from nexus.runtime import _ChildRuntime
    from nexus.tools.builtin.todo import TodoItem, TodoStore
    from nexus.tools.spec import ToolCall

    config = scripted_config()
    store = TodoStore()
    root_todo = TodoItem("root", "root work", "pending")
    sibling_todo = TodoItem("sibling", "sibling work", "pending")
    store.replace("parent-session", [root_todo])
    store.replace("sibling-session", [sibling_todo], agent_id="sibling-agent")
    manager = ToolManager(
        config, workspace=tmp_path, profile="research", todo_store=store
    )
    outbound_http = object()
    captured = {}
    child_session_id = runtime_module._child_session_id("root/sub/1")

    class Lease:
        def release(self):
            pass

    class Session:
        id = child_session_id
        events: ClassVar[list] = [
            Event(
                type="todo.updated",
                session=child_session_id,
                data={
                    "session": child_session_id,
                    "agent_id": "root/sub/1",
                    "revision": 4,
                    "todos": [
                        {
                            "id": "reopened",
                            "content": "child persisted work",
                            "status": "in_progress",
                        }
                    ],
                },
            )
        ]

        def begin_turn(self, **_kwargs):
            return Lease()

    session = Session()

    facade = SimpleNamespace(
        manager=SimpleNamespace(
            open=lambda *_args, **_kwargs: session,
        )
    )
    runtime = SimpleNamespace(
        workspace=tmp_path,
        _ensure_child_sessions=lambda: facade,
        _restore_todos=lambda opened: store.replay(opened),
        _effective_todo_store=lambda: store,
        _outbound_http_service=outbound_http,
        _child_config=lambda _spec: config,
        _build_child_assembler=lambda *_args: object(),
        _build_child_tool_manager=lambda *_args: manager,
        _child_permission_engine=lambda *_args: object(),
        _router=object(),
        _child_cost=lambda *_args: None,
        _child_outcome=lambda _spec, _session, outcome, **_kwargs: outcome,
    )
    spec = SimpleNamespace(
        session_id="root/sub/1",
        agent_id="root/sub/1",
        grants=(),
        max_iterations=1,
        prompt="child task",
        emit=None,
        hooks=None,
    )

    async def fake_run_turn(*, tools, **_kwargs):
        assert store.get(child_session_id, "root/sub/1") == (
            TodoItem("reopened", "child persisted work", "in_progress"),
        )
        assert store.revision(child_session_id, "root/sub/1") == 4
        captured["ctx"] = tools._ctx_factory(
            ToolCall(id="todo-call", name="todowrite"),
            SimpleNamespace(agent_id=spec.agent_id),
        )
        return "child-outcome"

    monkeypatch.setattr(runtime_module, "run_turn", fake_run_turn)

    outcome = await _ChildRuntime(runtime, spec, runner=object()).run()

    assert outcome == "child-outcome"
    assert store.get(child_session_id, "root/sub/1") == ()
    assert store.revision(child_session_id, "root/sub/1") == 0
    assert store.get("parent-session") == (root_todo,)
    assert store.get("sibling-session", "sibling-agent") == (sibling_todo,)
    assert len(session.events) == 1
    store.replay(session)
    assert store.get(child_session_id, "root/sub/1") == (
        TodoItem("reopened", "child persisted work", "in_progress"),
    )
    assert captured["ctx"].session_id != spec.session_id
    assert captured["ctx"].agent_id == spec.agent_id
    assert captured["ctx"].outbound_http is outbound_http


def test_child_workspace_config_fails_closed_without_scoped_permissions(tmp_path):
    from nexus.tools.permissions import PathGuard

    runtime = Runtime(
        tmp_path,
        config=Config(model="scripted/sm"),
        providers={"scripted": ScriptedProvider(text_response("unused"))},
    )
    child = tmp_path / "worktree"
    child.mkdir()
    guard = PathGuard(tmp_path).for_worktree(child)

    with pytest.raises(RuntimeError, match="no permissions section"):
        runtime._child_workspace_config(Config(model="scripted/sm"), guard)


async def test_child_runtime_nested_runner_inherits_parent_model(tmp_path):
    from dataclasses import replace

    from nexus.agents import AgentManager, ChildSpec, SubagentBudget
    from nexus.tools.permissions import PathGuard

    config = Config(
        model="scripted/sm",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/sm"),
            agents=AgentsSection(enabled=True),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"scripted": ScriptedProvider(text_response("unused"))},
        agents=AgentManager.for_workspace(tmp_path, seed=False),
    )
    async def emit(*_args, **_kwargs):
        return None

    spec = ChildSpec(
        task_id="task-1",
        agent="general",
        description="nested model",
        system_prompt="",
        prompt="nested",
        session_id="root/sub/1",
        parent_session="root",
        parent_agent_id="root",
        parent_call_id="task-call",
        root_turn_id="turn-1",
        depth=1,
        tools=("read",),
        dropped_tools=(),
        model="scripted/sm",
        requested_model=None,
        parent_model=None,
        provider=None,
        reasoning_effort=None,
        tier="medium",
        requested_tier="medium",
        clamped=False,
        max_iterations=1,
        context_tokens=None,
        worktree=None,
        permissions=None,
        grants=(),
        budget=SubagentBudget(),
        cancel=None,
        emit=emit,
        hooks=None,
        metadata={},
        agent_id="root/sub/1",
        config=config,
        workspace=tmp_path,
        worktree_scope=False,
    )

    engine = object()
    from nexus.runtime import _ChildAuthority

    spec = replace(
        spec,
        permissions=_ChildAuthority(engine=engine, path_guard=PathGuard(tmp_path)),
    )
    child_runtime = runtime._build_child_runtime(spec)

    assert child_runtime._runner._parent_model == "scripted/sm"
    assert child_runtime._runner._worktree_service is runtime._worktree_service
    assert child_runtime._runner._worktree_root == runtime.workspace.parent / (
        f".nexus-worktrees-{runtime.workspace.name}"
    )
    assert child_runtime._runner._worktree_root == runtime._worktree_root
    root_for = child_runtime._runner._worktree_root_for
    assert root_for.__self__ is runtime
    assert root_for.__func__ is Runtime._owned_worktree_root_for
    assert runtime._worktree_service is not None
    nested_workspace = runtime.workspace / "nested-checkout"
    assert nested_workspace not in runtime._worktree_roots
    owned_child = runtime._worktree_root / "worktrees" / "child"
    with pytest.raises(ValueError, match="outside Runtime-owned worktrees"):
        runtime._owned_worktree_root_for(nested_workspace)
    assert runtime._owned_worktree_root_for(owned_child) == runtime._worktree_root_for(
        owned_child
    )
    assert runtime._owned_worktree_root_for(owned_child) != runtime._worktree_root
    await runtime.aclose()


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_child_runtime_clears_todos_after_failure_or_cancellation(
    tmp_path, monkeypatch, error_type
):
    from types import SimpleNamespace

    import nexus.runtime as runtime_module
    from nexus.runtime import _ChildRuntime
    from nexus.tools.builtin.todo import TodoItem, TodoStore

    config = scripted_config()
    store = TodoStore()
    child_session_id = runtime_module._child_session_id("root/sub/failed")
    root_todo = TodoItem("root", "root work", "pending")
    child_todo = TodoItem("child", "child work", "in_progress")
    store.replace("parent-session", [root_todo])
    store.replace(child_session_id, [child_todo], agent_id="root/sub/failed")

    class Lease:
        def release(self):
            pass

    class Session:
        id = child_session_id
        events: ClassVar[list] = []

        def begin_turn(self, **_kwargs):
            return Lease()

    session = Session()
    manager = ToolManager(config, workspace=tmp_path, profile="research", todo_store=store)
    runtime = SimpleNamespace(
        workspace=tmp_path,
        _ensure_child_sessions=lambda: SimpleNamespace(
            manager=SimpleNamespace(open=lambda *_args, **_kwargs: session)
        ),
        _restore_todos=lambda _opened: None,
        _effective_todo_store=lambda: store,
        _child_config=lambda _spec: config,
        _build_child_assembler=lambda *_args: object(),
        _build_child_tool_manager=lambda *_args: manager,
        _child_permission_engine=lambda *_args: object(),
        _router=object(),
        _child_cost=lambda *_args: None,
        _child_outcome=lambda *_args, **_kwargs: None,
    )
    spec = SimpleNamespace(
        session_id="root/sub/failed",
        agent_id="root/sub/failed",
        grants=(),
        max_iterations=1,
        prompt="child task",
        emit=None,
        hooks=None,
    )

    async def fail_run_turn(**_kwargs):
        raise error_type("child stopped")

    monkeypatch.setattr(runtime_module, "run_turn", fail_run_turn)

    with pytest.raises(error_type, match="child stopped"):
        await _ChildRuntime(runtime, spec, runner=object()).run()

    assert store.get(child_session_id, spec.agent_id) == ()
    assert store.revision(child_session_id, spec.agent_id) == 0
    assert store.get("parent-session") == (root_todo,)


# ---------------------------------------------------------------------------
# Gemini registration (registry catalogue id ``google``)
# ---------------------------------------------------------------------------


def gemini_config(*, section="google", model="google/gemini-3-pro"):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            providers={section: ProviderSection(api_key="test-key")},
        ),
    )


async def test_gemini_is_registered_under_google_and_gemini(tmp_path):
    from nexus.model.request import ModelRequest

    runtime = Runtime(
        tmp_path,
        config=gemini_config(),
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )
    try:
        google = runtime.providers["google"]
        assert google is runtime.providers["gemini"]
        # The adapter identity stays ``gemini`` even though the registry key is
        # the catalogue id ``google``.
        assert google.name == "gemini"
        resolved = runtime.router.resolve(
            ModelRequest(messages=[], model="google/gemini-3-pro")
        )
        assert resolved.provider is google
        assert resolved.model == "gemini-3-pro"
        resolved_alias = runtime.router.resolve(
            ModelRequest(messages=[], model="gemini/gemini-3-pro")
        )
        assert resolved_alias.provider is google
    finally:
        await runtime.aclose()


async def test_gemini_config_section_may_be_named_gemini(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=gemini_config(section="gemini", model="gemini/gemini-3-pro"),
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )
    try:
        assert runtime.providers["google"].name == "gemini"
    finally:
        await runtime.aclose()


async def test_owned_gemini_provider_closes_once(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=gemini_config(),
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )
    client = runtime.providers["google"].transport.client
    assert client.is_closed is False
    await runtime.aclose()
    assert client.is_closed is True
    # Registered under two keys but owned once; a second close must be a no-op.
    await runtime.aclose()
    assert client.is_closed is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain(iterator):
    return [event async for event in iterator]
