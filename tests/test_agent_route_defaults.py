"""Markdown agent routing defaults at the runtime request boundary."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    AgentsSection,
    ConfigV2,
    ModelSection,
    ModelsSection,
    PermissionsSection,
    ProviderSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.capabilities import Capabilities
from nexus.model.providers.openai import API_RESPONSES, OpenAIProvider
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.model.reasoning_effort import ReasoningEffortSelection
from nexus.model.registry import ModelRegistry
from nexus.model.selection import ModelSelection
from nexus.model.tiers import TierTable
from nexus.runtime import Runtime
from nexus.session.agent_selection import AgentSelection
from nexus.view import fold


async def test_agent_model_provider_effort_and_body_reach_responses_request(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            content=(
                'event: response.created\ndata: '
                '{"type":"response.created","response":{"id":"r1",'
                '"model":"chosen-model","status":"in_progress"}}\n\n'
                'event: response.completed\ndata: '
                '{"type":"response.completed","response":{"id":"r1",'
                '"status":"completed","usage":{}}}\n\n'
            ),
        )

    (tmp_path / ".nexus" / "agents").mkdir(parents=True)
    (tmp_path / ".nexus" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review\ncontexts: [root]\n"
        "provider: openai\nmodel: chosen-model\nreasoning_effort: high\n---\n"
        "Reviewer system body.\n",
        encoding="utf-8",
    )
    workspace_provider = ScriptedProvider(text_response("unused"), name="workspace")
    agent_provider = OpenAIProvider(
        api_key="test",
        model="fallback-model",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(handler),
    )
    config = Config(
        model="workspace/workspace-model",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            model=ModelSection(default="workspace/workspace-model"),
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    registry = ModelRegistry(
        providers={"openai": {"kind": "openai"}},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
    )
    registry.install_raw(
        '{"openai":{"models":{"chosen-model":{"reasoning":true,'
        '"reasoning_efforts":["high"],"modalities":{"output":["text"]}}}}}'
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"workspace": workspace_provider, "openai": agent_provider},
        registry=registry,
    )
    # Preserve the installed in-memory fixture through the normal turn-boundary
    # acquisition hook; its metadata is the source of the asserted exact level.
    runtime._registry_loaded = True
    try:
        session = runtime.session("route-default")
        session.select_agent(AgentSelection(name="reviewer"))
        assert runtime.root_route_metadata(session) == {
            "provider": "openai",
            "model": "chosen-model",
        }
        assert captured == {}
        [event async for event in session.send("review")]
        assert captured["model"] == "chosen-model"
        assert captured["reasoning"] == {"effort": "high", "summary": "auto"}
        assert "Reviewer system body." in captured["instructions"]
        assert workspace_provider.calls == 0
    finally:
        await runtime.aclose()


async def test_completed_turn_freezes_selected_agent_and_models_dev_effort_through_replay(
    tmp_path,
):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=(
                'event: response.created\ndata: '
                '{"type":"response.created","response":{"id":"r1",'
                '"model":"gpt-5.6-luna","status":"in_progress"}}\n\n'
                'event: response.output_text.delta\ndata: '
                '{"type":"response.output_text.delta","delta":"done"}\n\n'
                'event: response.completed\ndata: '
                '{"type":"response.completed","response":{"id":"r1",'
                '"status":"completed","usage":{}}}\n\n'
            ),
        )

    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review\ncontexts: [root]\n"
        "model: inherit\nreasoning_effort: high\n---\nReviewer instructions.\n",
        encoding="utf-8",
    )
    provider_section = ProviderSection(auth="chatgpt_oauth", profile="default", api="responses")
    registry = ModelRegistry(
        providers={"codex": provider_section},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
        provider_aliases={"codex": "openai"},
    )
    registry.install_raw(
        (Path(__file__).parent / "fixtures" / "models.dev" / "openai-gpt-5.6.json").read_bytes()
    )
    config = Config(
        model="codex/gpt-5.6-luna",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            model=ModelSection(default="codex/gpt-5.6-luna"),
            models=ModelsSection(default="codex/gpt-5.6-luna", offline=True),
            providers={"codex": provider_section},
            permissions=PermissionsSection(mode="allow"),
        ),
    )

    class OAuthManager:
        def __init__(self, *, profile):
            assert profile == "default"

        async def headers(self):
            return {"authorization": "Bearer test-token"}

    runtime = Runtime(
        tmp_path,
        config=config,
        registry=registry,
        codex_auth_factory=OAuthManager,
        http_transport=httpx.MockTransport(handler),
    )
    runtime._registry_loaded = True
    try:
        session = runtime.session("frozen-agent-effort")
        session.select_agent(AgentSelection(name="reviewer"))
        session.select_model(
            ModelSelection(
                reference="codex/gpt-5.6-luna",
                provider="codex",
                model="gpt-5.6-luna",
            )
        )
        session.select_reasoning_effort(ReasoningEffortSelection(effort="high"))
        turn_id = await session.start_turn("review")
        # start_turn has frozen the actual agent/model/effort but has not yielded
        # to its producer yet. Change all three durable next-turn selections
        # before execution proceeds; this turn and its replay must keep the
        # original effective values.
        session.select_agent(AgentSelection(name="general"))
        session.select_model(
            ModelSelection(
                reference="openai/gpt-5.6",
                provider="codex",
                model="gpt-5.6",
            )
        )
        session.select_reasoning_effort(ReasoningEffortSelection(effort="low"))
        await session.wait_turn(turn_id)

        assert captured["body"]["reasoning"] == {"effort": "high", "summary": "auto"}  # type: ignore[index]
        model_started = next(
            event for event in session.events
            if event.type == "model.started" and event.turn == turn_id
        )
        turn_started = next(
            event for event in session.events
            if event.type == "turn.started" and event.turn == turn_id
        )
        assert model_started.data["reasoning_effort"] == "high"
        assert turn_started.data["agent"]["name"] == "reviewer"
        assert turn_started.data["agent"]["source"] == "session"

        # Session-level changes while the turn was pending execution apply only
        # to the next turn, while replay keeps the actual request metadata.
        view = fold(session.events)
        frozen_turn = next(turn for turn in view.turns if turn.id == turn_id)
        assert set(frozen_turn.agent) == {
            "name", "source", "fingerprint", "read_only", "profile", "tools"
        }
        assert frozen_turn.agent["name"] == "reviewer"
        assert frozen_turn.reasoning_effort == "high"

        runtime.sessions.evict(session.id)
        reopened = runtime.session(session.id, create=False)
        replayed = fold(reopened.events)
        replayed_turn = next(turn for turn in replayed.turns if turn.id == turn_id)
        assert replayed_turn.agent == frozen_turn.agent
        assert replayed_turn.reasoning_effort == frozen_turn.reasoning_effort
        assert reopened.agent_selection.name == "general"
        assert reopened.model_selection.model == "gpt-5.6"
        assert reopened.reasoning_effort_selection.effort == "low"
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("agent_enabled", [True, False])
async def test_effort_is_unknown_when_catalogue_does_not_support_it_and_agent_can_be_absent(
    tmp_path, agent_enabled
):
    provider = ScriptedProvider(
        text_response("done"),
        name="openai",
        model="plain-model",
        capabilities=Capabilities(thinking=True),
    )
    registry = ModelRegistry(
        providers={"openai": {"kind": "openai"}},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
    )
    registry.install_raw(
        '{"openai":{"models":{"plain-model":{"reasoning":true,'
        '"modalities":{"output":["text"]}}}}}'
    )
    config = Config(
        model="openai/plain-model",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            agents=AgentsSection(enabled=agent_enabled),
            model=ModelSection(default="openai/plain-model"),
            models=ModelsSection(default="openai/plain-model", offline=True),
            providers={"openai": ProviderSection(api_key="test", api="responses")},
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"openai": provider}, registry=registry)
    runtime._registry_loaded = True
    try:
        session = runtime.session(f"unsupported-effort-{agent_enabled}")
        session.select_reasoning_effort(ReasoningEffortSelection(effort="high"))
        turn_id = await session.start_turn("review")
        await session.wait_turn(turn_id)

        assert registry.resolve("openai/plain-model").reasoning_efforts == ()
        assert provider.requests[0].params.reasoning_effort is None
        turn_started = next(
            event for event in session.events
            if event.type == "turn.started" and event.turn == turn_id
        )
        model_started = next(
            event for event in session.events
            if event.type == "model.started" and event.turn == turn_id
        )
        assert model_started.data["reasoning_effort"] is None
        if agent_enabled:
            assert turn_started.data["agent"]["name"] == "build"
            assert turn_started.data["agent"]["source"] == "config"
        else:
            assert "agent" not in turn_started.data

        view = fold(session.events)
        turn = next(turn for turn in view.turns if turn.id == turn_id)
        assert turn.reasoning_effort is None
        assert (turn.agent is not None) is agent_enabled
    finally:
        await runtime.aclose()


async def test_configured_tier_does_not_mask_selected_concrete_agent_route(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review\ncontexts: [root]\n"
        "provider: scripted\nmodel: agent-model\nreasoning_effort: high\n---\n"
        "Reviewer system body.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        text_response("done"),
        name="scripted",
        capabilities=Capabilities(thinking=True),
    )
    config = Config(
        model="medium",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            model=ModelSection(default="medium"),
            models=ModelsSection(offline=True),
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"scripted": provider},
        tiers=TierTable(),
    )

    class EffortRegistry:
        def get(self, reference):
            if reference == "scripted/agent-model":
                return type("Model", (), {"reasoning_efforts": ("high",)})()
            return None

    runtime._assembler._registry = EffortRegistry()
    try:
        session = runtime.session("agent-tier-mismatch")
        session.select_agent(AgentSelection(name="reviewer"))
        current = await HostFacade(runtime).handle(p.AgentCurrent(session=session.id))
        assert isinstance(current, p.AgentCurrentResult)
        assert (current.provider, current.model) == ("scripted", "agent-model")
        assert current.supported_levels == ["high"]
        assert current.reasoning_effort == "high"

        [event async for event in session.send("review")]
        request = provider.requests[0]
        assert (request.provider, request.model) == (current.provider, current.model)
        assert request.params.reasoning_effort == current.reasoning_effort
    finally:
        await runtime.aclose()


async def test_agent_tier_is_resolved_from_agent_model(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review\ncontexts: [root]\n"
        "model: low\n---\nReviewer system body.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("done"), name="openai")
    tiers = TierTable(
        overrides={
            "low": ["openai/agent-tier-model"],
            "medium": ["openai/workspace-model"],
        }
    )
    registry = ModelRegistry(
        providers={"openai": {"kind": "openai"}},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
        tier_table=tiers,
    )
    registry.install_raw(
        '{"openai":{"models":{"agent-tier-model":{"reasoning":true,'
        '"modalities":{"output":["text"]}},"workspace-model":{}}}}'
    )
    config = Config(
        model="medium",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="general"),
            model=ModelSection(default="medium"),
            models=ModelsSection(offline=True),
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"openai": provider},
        tiers=tiers,
        registry=registry,
    )
    runtime._registry_loaded = True
    try:
        session = runtime.session("agent-tier")
        session.select_agent(AgentSelection(name="reviewer"))
        assert runtime.root_route_metadata(session) == {
            "provider": "openai",
            "model": "agent-tier-model",
        }
        [event async for event in session.send("review")]
        assert (provider.requests[0].provider, provider.requests[0].model) == (
            "openai",
            "agent-tier-model",
        )
    finally:
        await runtime.aclose()


async def test_explicit_session_model_beats_root_agent_default_on_next_turn(tmp_path):
    (tmp_path / ".nexus" / "agents").mkdir(parents=True)
    (tmp_path / ".nexus" / "agents" / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review\ncontexts: [root]\n"
        "provider: scripted\nmodel: agent-model\nreasoning_effort: high\n---\n"
        "Reviewer system body.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("done"), name="scripted")
    config = Config(
        model="scripted/workspace-model",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="reviewer"),
            model=ModelSection(default="scripted/workspace-model"),
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})
    try:
        session = runtime.session("explicit-model")
        session.select_model(
            ModelSelection(
                reference="scripted/session-model",
                provider="scripted",
                model="session-model",
                tier="low",
            )
        )
        assert runtime.root_route_metadata(session) == {
            "provider": "scripted",
            "model": "session-model",
        }
        [event async for event in session.send("review")]

        request = provider.requests[0]
        assert request.provider == "scripted"
        assert request.model == "session-model"
        assert request.params.reasoning_effort is None
    finally:
        await runtime.aclose()


async def test_child_task_model_beats_declared_role_model_and_provider(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "root.md").write_text(
        "---\nname: root\ndescription: root agent\ncontexts: [root]\n"
        "provider: rootprov\nmodel: root-model\n---\nRoot prompt.\n",
        encoding="utf-8",
    )
    (agents / "worker.md").write_text(
        "---\nname: worker\ndescription: worker\ncontexts: [subagent]\n"
        "provider: roleprov\n---\nWorker prompt.\n",
        encoding="utf-8",
    )
    root_provider = ScriptedProvider(
        tool_response(
            (
                "task-call",
                "Task",
                {
                    "prompt": "work",
                    "subagent_type": "worker",
                    "model": "taskprov/task-model",
                },
            )
        ),
        text_response("root done"),
        name="rootprov",
    )
    task_provider = ScriptedProvider(text_response("child done"), name="taskprov")
    role_provider = ScriptedProvider(text_response("unused"), name="roleprov")
    config = Config(
        model="rootprov/workspace-model",
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name="root"),
            model=ModelSection(default="rootprov/workspace-model"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={
            "rootprov": root_provider,
            "roleprov": role_provider,
            "taskprov": task_provider,
        },
    )
    try:
        [event async for event in runtime.session("child-override").send("delegate")]

        assert root_provider.requests[0].model == "root-model"
        assert task_provider.requests[0].provider == "taskprov"
        assert task_provider.requests[0].model == "task-model"
        assert role_provider.calls == 0
    finally:
        await runtime.aclose()


async def test_child_role_provider_model_beats_inherited_root_model(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "root.md").write_text(
        "---\nname: root\ndescription: root agent\ncontexts: [root]\n"
        "provider: rootprov\nmodel: root-model\n---\nRoot prompt.\n",
        encoding="utf-8",
    )
    (agents / "worker.md").write_text(
        "---\nname: worker\ndescription: worker\ncontexts: [subagent]\n"
        "provider: roleprov\nmodel: role-model\n---\nWorker prompt.\n",
        encoding="utf-8",
    )
    root_provider = ScriptedProvider(
        tool_response(
            ("task-call", "Task", {"prompt": "work", "subagent_type": "worker"})
        ),
        text_response("root done"),
        name="rootprov",
    )
    role_provider = ScriptedProvider(text_response("child done"), name="roleprov")
    runtime = Runtime(
        tmp_path,
        config=Config(
            model="rootprov/workspace-model",
            version=2,
            v2=ConfigV2(
                agent=AgentSection(name="root"),
                model=ModelSection(default="rootprov/workspace-model"),
                permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            ),
        ),
        providers={
            "rootprov": root_provider,
            "roleprov": role_provider,
        },
    )
    try:
        [event async for event in runtime.session("child-role-default").send("delegate")]

        assert root_provider.requests[0].model == "root-model"
        assert role_provider.requests[0].provider == "roleprov"
        assert role_provider.requests[0].model == "role-model"
    finally:
        await runtime.aclose()


async def test_child_without_model_default_inherits_root_agent_model(tmp_path):
    agents = tmp_path / ".nexus" / "agents"
    agents.mkdir(parents=True)
    (agents / "root.md").write_text(
        "---\nname: root\ndescription: root agent\ncontexts: [root]\n"
        "provider: rootprov\nmodel: root-model\n---\nRoot prompt.\n",
        encoding="utf-8",
    )
    root_provider = ScriptedProvider(
        tool_response(
            ("task-call", "Task", {"prompt": "work", "subagent_type": "general"})
        ),
        text_response("child done"),
        text_response("root done"),
        name="rootprov",
    )
    runtime = Runtime(
        tmp_path,
        config=Config(
            model="rootprov/workspace-model",
            version=2,
            v2=ConfigV2(
                agent=AgentSection(name="root"),
                model=ModelSection(default="rootprov/workspace-model"),
                permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            ),
        ),
        providers={"rootprov": root_provider},
    )
    try:
        [event async for event in runtime.session("child-inherits-root").send("delegate")]

        assert [request.model for request in root_provider.requests] == [
            "root-model",
            "root-model",
            "root-model",
        ]
    finally:
        await runtime.aclose()
