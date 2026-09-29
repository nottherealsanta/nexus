"""Selected-agent metadata on the host wire."""
from __future__ import annotations

from typing import ClassVar

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    AgentsSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.capabilities import Capabilities
from nexus.model.providers.openai import API_CHAT, API_RESPONSES, OpenAIProvider
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.reasoning_effort import ReasoningEffortSelection
from nexus.model.registry import ModelRegistry
from nexus.runtime import Runtime


def _config(*, agent: str, model: str | None) -> Config:
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            agent=AgentSection(name=agent),
            model=ModelSection(default=model),
            permissions=PermissionsSection(mode="allow"),
        ),
    )


def _write_agent(root, name: str, *, extra: str = "") -> None:
    directory = root / ".agents" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: metadata test agent\n"
        "contexts: [root, subagent]\n"
        f"{extra}"
        "---\nagent prompt\n",
        encoding="utf-8",
    )


async def test_current_agent_metadata_tracks_configured_session_override_and_unset(tmp_path):
    _write_agent(
        tmp_path,
        "custom",
        extra=(
            "provider: openai\n"
            "model: openai/agent-model\n"
            "reasoning_effort: high\n"
            "color: #12ab34\n"
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=_config(agent="custom", model="scripted/configured"),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    facade = HostFacade(runtime)
    try:
        configured = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(configured, p.AgentCurrentResult)
        assert (configured.name, configured.source) == ("custom", "config")
        assert configured.color == "#12AB34"
        assert (configured.provider, configured.model) == ("scripted", "configured")
        assert configured.reasoning_effort is None
        assert configured.reasoning_effort_source is None

        selected_agent = await facade.handle(
            p.AgentSelect(session="s", name="custom")
        )
        assert isinstance(selected_agent, p.AgentSelectResult)
        selected_metadata = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(selected_metadata, p.AgentCurrentResult)
        assert (selected_metadata.name, selected_metadata.source) == (
            "custom",
            "session",
        )
        assert selected_metadata.color == configured.color

        selected = await facade.handle(
            p.ModelSelect(session="s", ref="scripted/session-choice")
        )
        assert isinstance(selected, p.ModelSelectResult)
        overridden = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(overridden, p.AgentCurrentResult)
        assert (overridden.provider, overridden.model) == ("scripted", "session-choice")
        assert overridden.color == configured.color

        fresh_session = await facade.handle(p.AgentCurrent(session="fresh-session"))
        assert isinstance(fresh_session, p.AgentCurrentResult)
        assert (fresh_session.provider, fresh_session.model) == ("scripted", "configured")
    finally:
        await runtime.aclose()


async def test_current_agent_metadata_uses_shared_next_turn_codex_route(tmp_path):
    _write_agent(
        tmp_path,
        "custom",
        extra="reasoning_effort: high\ncolor: #12ab34\n",
    )
    runtime = Runtime(
        tmp_path,
        config=_config(agent="general", model="codex/gpt-5.6-luna"),
        providers={
            "codex": OpenAIProvider(
                api=API_RESPONSES,
                model="fallback-model",
                capabilities=Capabilities(thinking=True),
            ),
            "scripted": ScriptedProvider(text_response("ok")),
            "openai": OpenAIProvider(
                api=API_CHAT,
                model="fallback-model",
                capabilities=Capabilities(thinking=True),
            ),
        },
    )
    facade = HostFacade(runtime)
    try:
        await facade.handle(p.AgentSelect(session="s", name="custom"))
        selected_agent = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(selected_agent, p.AgentCurrentResult)
        assert (selected_agent.provider, selected_agent.model) == (
            "codex",
            "gpt-5.6-luna",
        )
        assert selected_agent.reasoning_effort is None
        assert selected_agent.reasoning_effort_source is None

        await facade.handle(p.ModelSelect(session="s", ref="openai/override"))
        override = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(override, p.AgentCurrentResult)
        assert (override.provider, override.model) == ("openai", "override")
        assert override.reasoning_effort is None
        assert override.reasoning_effort_source is None

        handle = runtime.session("s")
        handle.select_reasoning_effort(ReasoningEffortSelection(effort="high"))
        dormant = await facade.handle(p.AgentCurrent(session="s"))
        assert isinstance(dormant, p.AgentCurrentResult)
        assert dormant.stored_override == "high"
        assert dormant.reasoning_effort is None
        assert dormant.reasoning_effort_source is None
    finally:
        await runtime.aclose()


async def test_agents_list_contains_sanitized_metadata_and_source(tmp_path):
    _write_agent(
        tmp_path,
        "listed",
        extra=(
            "provider: vendor\n"
            "model: vendor/model-id\n"
            "reasoning_effort: medium\n"
            "color: #aabbcc\n"
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=_config(agent="general", model="scripted/m"),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    try:
        result = await HostFacade(runtime).handle(p.AgentsList())
        assert isinstance(result, p.AgentsListResult)
        listed = next(agent for agent in result.agents if agent["name"] == "listed")
        assert listed["source"] == "workspace"
        assert listed["provider"] == "vendor"
        assert listed["model"] == "vendor/model-id"
        assert listed["reasoning_effort"] == "medium"
        assert listed["color"] == "#AABBCC"
        assert listed["contexts"] == ["root", "subagent"]
    finally:
        await runtime.aclose()


async def test_agents_list_preserves_agent_defined_max_effort(tmp_path):
    _write_agent(tmp_path, "maximal", extra="reasoning_effort: max\n")
    runtime = Runtime(
        tmp_path,
        config=_config(agent="general", model="scripted/m"),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    try:
        result = await HostFacade(runtime).handle(p.AgentsList())
        assert isinstance(result, p.AgentsListResult)
        listed = next(agent for agent in result.agents if agent["name"] == "maximal")
        assert listed["reasoning_effort"] == "max"
    finally:
        await runtime.aclose()


async def test_effort_is_reported_only_when_openai_applies_it(tmp_path):
    _write_agent(
        tmp_path,
        "reasoning",
        extra=(
            "model: openai/agent-model\n"
            "reasoning_effort: high\n"
            "color: #123456\n"
        ),
    )
    for api, thinking, expected in (
        (API_RESPONSES, True, "high"),
        (API_CHAT, True, None),
        (API_RESPONSES, False, None),
    ):
        runtime = Runtime(
            tmp_path,
            config=_config(agent="reasoning", model="openai/configured-model"),
            providers={
                "openai": OpenAIProvider(
                    api=api,
                    model="test-model",
                    capabilities=Capabilities(thinking=thinking),
                )
            },
        )
        try:
            result = await HostFacade(runtime).handle(p.AgentCurrent(session="s"))
            assert isinstance(result, p.AgentCurrentResult)
            assert (result.provider, result.model) == ("openai", "agent-model")
            assert result.reasoning_effort is None
        finally:
            await runtime.aclose()


def test_agent_current_result_roundtrip_and_legacy_decode():
    result = p.AgentCurrentResult(
        session="s",
        name="custom",
        source="session",
        color="#12AB34",
        provider="openai",
        model="gpt-5",
        reasoning_effort="high",
    )
    assert p.decode_result(p.encode_result(result)) == result

    legacy = p.decode_result(
        b'{"type":"AgentCurrentResult","session":"s","name":"general",'
        b'"source":"default"}'
    )
    assert legacy == p.AgentCurrentResult(session="s", name="general")
    assert p.AgentCurrentResult(session="s").name == "build"


async def test_agent_metadata_omits_unavailable_values(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=Config(
            model=None,
            version=2,
            v2=ConfigV2(
                agent=AgentSection(name="general"),
                agents=AgentsSection(enabled=False),
                model=ModelSection(default=None),
                permissions=PermissionsSection(mode="allow"),
            ),
        ),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
    )
    try:
        result = await HostFacade(runtime).handle(p.AgentCurrent(session="s"))
        assert isinstance(result, p.AgentCurrentResult)
        assert result.color is None
        assert result.provider is None
        assert result.model is None
        assert result.reasoning_effort is None
    finally:
        await runtime.aclose()


async def test_reasoning_effort_host_select_clear_and_replay(tmp_path):
    def make_runtime():
        registry = ModelRegistry(
            providers={"openai": {"kind": "openai"}},
            env={},
            snapshot_path=tmp_path / "missing-models.json",
            use_snapshot=False,
            offline=True,
        )
        registry.install_raw(
            '{"openai":{"models":{"model-a":{"reasoning":true,'
            '"reasoning_efforts":["high"],"modalities":{"output":["text"]}}}}}'
        )
        return Runtime(
            tmp_path,
            config=_config(agent="general", model="openai/model-a"),
            providers={
                "openai": OpenAIProvider(
                    api=API_RESPONSES,
                    model="fallback-model",
                    capabilities=Capabilities(thinking=True),
                )
            },
            registry=registry,
        )

    runtime = make_runtime()
    try:
        facade = HostFacade(runtime)
        current = await facade.handle(p.AgentCurrent(session="effort"))
        assert isinstance(current, p.AgentCurrentResult)
        assert current.supported_levels == ["high"]
        assert (current.provider, current.model) == ("openai", "model-a")
        assert current.reasoning_effort is None

        accepted = await facade.handle(
            p.ReasoningEffortSelect(session="effort", effort="high")
        )
        assert isinstance(accepted, p.ReasoningEffortSelectResult)
        assert accepted.accepted is True
        assert (accepted.stored_override, accepted.effective_effort, accepted.source) == (
            "high",
            "high",
            "session",
        )

        handle = runtime.session("effort")
        events = [event for event in handle.events if event.type == "reasoning_effort.selected"]
        assert len(events) == 1
        assert events[0].data == {"effort": "high", "version": 1}

        selected = await facade.handle(p.AgentCurrent(session="effort"))
        assert isinstance(selected, p.AgentCurrentResult)
        assert selected.supported_levels == ["high"]
        assert selected.reasoning_effort == "high"
        assert selected.reasoning_effort_source == "session"
        assert selected.stored_override == "high"

        snapshot = runtime._assembler.for_turn(session=handle)
        request = await snapshot.assemble(handle)
        assert (request.provider, request.model) == ("openai", "model-a")
        assert request.params.reasoning_effort == "high"

        await runtime.aclose()
        runtime = make_runtime()
        facade = HostFacade(runtime)
        replayed = runtime.session("effort", create=False)
        assert replayed.reasoning_effort_selection == ReasoningEffortSelection(effort="high")
        replayed_current = await facade.handle(p.AgentCurrent(session="effort"))
        assert isinstance(replayed_current, p.AgentCurrentResult)
        assert replayed_current.reasoning_effort == "high"
        assert replayed_current.reasoning_effort_source == "session"
        assert replayed_current.stored_override == "high"

        cleared = await facade.handle(
            p.ReasoningEffortSelect(session="effort", effort=None)
        )
        assert isinstance(cleared, p.ReasoningEffortSelectResult)
        assert cleared.accepted is True
        assert cleared.stored_override is None
        assert cleared.effective_effort is None
        assert cleared.source is None
        events = [
            event
            for event in replayed.events
            if event.type == "reasoning_effort.selected"
        ]
        assert [event.data for event in events] == [
            {"effort": "high", "version": 1},
            {"effort": None, "version": 1},
        ]

        await runtime.aclose()
        runtime = make_runtime()
        try:
            reopened = runtime.session("effort", create=False)
            assert reopened.reasoning_effort_selection == ReasoningEffortSelection(
                effort=None
            )
            after_clear = await HostFacade(runtime).handle(
                p.AgentCurrent(session="effort")
            )
            assert isinstance(after_clear, p.AgentCurrentResult)
            assert after_clear.reasoning_effort is None
            assert after_clear.reasoning_effort_source is None
            assert after_clear.stored_override is None
        finally:
            await runtime.aclose()
    except BaseException:
        if not runtime._closed:
            await runtime.aclose()
        raise


async def test_max_only_effort_routes_through_host_request_and_responses_body(tmp_path):
    import json

    import httpx

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=(
                'event: response.created\ndata: '
                '{"type":"response.created","response":{"id":"r1",'
                '"model":"model-a","status":"in_progress"}}\n\n'
                'event: response.completed\ndata: '
                '{"type":"response.completed","response":{"id":"r1",'
                '"status":"completed","usage":{}}}\n\n'
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
        '{"openai":{"models":{"model-a":{"reasoning":true,'
        '"reasoning_efforts":["max"],"modalities":{"output":["text"]}}}}}'
    )
    provider = OpenAIProvider(
        api_key="test",
        api=API_RESPONSES,
        model="fallback-model",
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(handler),
    )
    runtime = Runtime(
        tmp_path,
        config=_config(agent="general", model="openai/model-a"),
        providers={"openai": provider},
        registry=registry,
    )
    try:
        facade = HostFacade(runtime)
        current = await facade.handle(p.AgentCurrent(session="max-effort"))
        assert isinstance(current, p.AgentCurrentResult)
        assert (current.provider, current.model) == ("openai", "model-a")
        assert current.supported_levels == ["max"]

        selected = await facade.handle(
            p.ReasoningEffortSelect(session="max-effort", effort="max")
        )
        assert isinstance(selected, p.ReasoningEffortSelectResult)
        assert (selected.accepted, selected.effective_effort, selected.source) == (
            True,
            "max",
            "session",
        )

        handle = runtime.session("max-effort")
        snapshot = runtime._assembler.for_turn(session=handle)
        request = await snapshot.assemble(handle)
        assert (request.provider, request.model) == ("openai", "model-a")
        assert request.params.reasoning_effort == "max"
        _events = [event async for event in provider.stream(request)]
        assert captured["body"]["reasoning"] == {"effort": "max"}  # type: ignore[index]
    finally:
        await runtime.aclose()


async def test_reasoning_effort_rejection_has_no_event_and_dormant_override(tmp_path):
    class Handle:
        id = "s"

        def __init__(self):
            self.events = []
            self.reasoning_effort_selection = None

        def bind(self, **_kwargs):
            pass

        def select_reasoning_effort(self, selection):
            self.events.append(selection.effort)
            self.reasoning_effort_selection = selection

        def clear_reasoning_effort(self):
            self.select_reasoning_effort(ReasoningEffortSelection(effort=None))

    class RuntimeStub:
        workspace = str(tmp_path)
        sessions = type("Sessions", (), {"list": lambda _self: []})()
        extensions = None
        agents = None
        registry = None
        tiers = None
        providers: ClassVar[dict] = {}

        def __init__(self):
            self.handle = Handle()
            self.levels = ("low", "high")

        def session(self, _session, **_kwargs):
            return self.handle

        def root_reasoning_effort_metadata(self, handle):
            stored = getattr(handle.reasoning_effort_selection, "effort", None)
            return {
                "supported_levels": self.levels,
                "stored_override": stored,
                "effective_effort": stored if stored in self.levels else None,
                "source": "session" if stored in self.levels else None,
            }

        def effective_session_agent(self, _handle):
            return "general", "default"

    runtime = RuntimeStub()
    facade = HostFacade(runtime)
    child_handle = Handle()

    runtime.levels = ("low", "high")
    current = await facade.handle(p.AgentCurrent(session="s"))
    assert isinstance(current, p.AgentCurrentResult)
    assert current.supported_levels == ["low", "high"]
    assert current.reasoning_effort is None

    invalid = await facade.handle(p.ReasoningEffortSelect(session="s", effort="turbo"))
    unsupported = await facade.handle(p.ReasoningEffortSelect(session="s", effort="medium"))
    unsupported_xhigh = await facade.handle(
        p.ReasoningEffortSelect(session="s", effort="xhigh")
    )
    assert isinstance(invalid, p.ErrorResult) and "unknown" in invalid.message
    assert isinstance(unsupported, p.ErrorResult) and "not supported" in unsupported.message
    assert isinstance(unsupported_xhigh, p.ErrorResult)
    assert "not supported" in unsupported_xhigh.message
    assert runtime.handle.events == []

    selected = await facade.handle(p.ReasoningEffortSelect(session="s", effort="high"))
    assert isinstance(selected, p.ReasoningEffortSelectResult)
    assert child_handle.events == []  # selection remains scoped to the root session
    runtime.levels = ("low",)
    dormant = await facade.handle(p.AgentCurrent(session="s"))
    assert isinstance(dormant, p.AgentCurrentResult)
    assert dormant.stored_override == "high"
    assert dormant.reasoning_effort is None
    assert dormant.reasoning_effort_source is None


async def test_reasoning_effort_metadata_never_exposes_config_or_secrets(tmp_path):
    secret = "sk-live-supersecret123456"
    _write_agent(
        tmp_path,
        "safe",
        extra=(f"description: api_key={secret}\nreasoning_effort: high\n"),
    )
    runtime = Runtime(
        tmp_path,
        config=_config(agent="safe", model="scripted/model"),
        providers={"scripted": ScriptedProvider(capabilities=Capabilities(thinking=True))},
    )
    try:
        result = await HostFacade(runtime).handle(p.AgentCurrent(session="safe"))
        wire = p.encode_result(result).decode()
        assert isinstance(result, p.AgentCurrentResult)
        assert secret not in wire
        assert "api_key" not in wire
        assert "reasoning_effort" in wire
        assert result.model == "model"
    finally:
        await runtime.aclose()
