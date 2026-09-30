"""Reasoning-effort request contract and OpenAI dialect serialization."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import msgspec
import pytest

from nexus.config import Config
from nexus.config.schema import (
    ConfigV2,
    ModelSection,
    ModelsSection,
    PermissionsSection,
    ProviderSection,
)
from nexus.context.manager import ContextManager
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.capabilities import Capabilities
from nexus.model.message import Message, Text
from nexus.model.providers.openai import (
    API_CHAT,
    API_RESPONSES,
    OpenAIProvider,
    build_request_body,
    reasoning_effort_applied,
)
from nexus.model.providers.scripted import ScriptedProvider
from nexus.model.reasoning_effort import ReasoningEffortSelection
from nexus.model.registry import ModelRegistry
from nexus.model.request import ModelRequest, SamplingParams
from nexus.runtime import Runtime


def _request(*, effort: str | None = None, thinking_budget: int | None = None):
    return ModelRequest(
        messages=[Message("user", [Text("Think through this carefully.")])],
        params=SamplingParams(
            reasoning_effort=effort,
            thinking_budget=thinking_budget,
        ),
    )


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max"])
def test_responses_serializes_reasoning_effort_from_normalized_request(effort: str):
    request = _request(effort=effort, thinking_budget=2048)

    body = build_request_body(request, model="gpt-5", api=API_RESPONSES)

    assert body["reasoning"] == {"effort": effort, "summary": "auto"}
    assert "thinking_budget" not in body
    assert reasoning_effort_applied(request, api=API_RESPONSES)


def test_non_reasoning_model_does_not_report_effort_as_applied():
    request = _request(effort="high")

    assert not reasoning_effort_applied(
        request,
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=False),
    )


def test_effort_is_absent_when_not_configured():
    request = _request(thinking_budget=2048)

    body = build_request_body(request, model="gpt-5", api=API_RESPONSES)

    assert "reasoning" not in body
    assert not reasoning_effort_applied(request, api=API_RESPONSES)


def test_chat_completions_does_not_serialize_reasoning_effort():
    request = _request(effort="high")

    body = build_request_body(request, model="gpt-5", api=API_CHAT)

    assert "reasoning" not in body
    assert not reasoning_effort_applied(request, api=API_CHAT)


async def test_responses_provider_sends_effort_from_normalized_request():
    captured: dict[str, object] = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            content=(
                'event: response.created\ndata: '
                '{"type":"response.created","response":{"id":"r1",'
                '"model":"gpt-5","status":"in_progress"}}\n\n'
                'event: response.completed\ndata: '
                '{"type":"response.completed","response":{"id":"r1",'
                '"status":"completed","usage":{}}}\n\n'
            ),
        )

    provider = OpenAIProvider(
        api_key="test",
        model="gpt-5",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(handler),
    )
    try:
        request = _request(effort="high")
        [event async for event in provider.stream(request)]
    finally:
        await provider.aclose()

    assert captured["body"]["reasoning"] == {"effort": "high", "summary": "auto"}  # type: ignore[index]


async def test_responses_provider_omits_effort_for_non_reasoning_model():
    provider = OpenAIProvider(
        api_key="test",
        model="plain-model",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=False),
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(
            200,
            content=(
                'event: response.created\\ndata: '
                '{"type":"response.created","response":{"id":"r1",'
                '"model":"plain-model","status":"in_progress"}}\\n\\n'
                'event: response.completed\\ndata: '
                '{"type":"response.completed","response":{"id":"r1",'
                '"status":"completed","usage":{}}}\\n\\n'
            ),
        )),
    )
    try:
        events = [
            event async for event in provider.stream(_request(effort="medium"))
        ]
    finally:
        await provider.aclose()
    assert events


def test_sampling_params_effort_roundtrips_through_msgpack():
    params = SamplingParams(reasoning_effort="max")

    decoded = msgspec.msgpack.Decoder(SamplingParams).decode(
        msgspec.msgpack.encode(params)
    )

    assert decoded == params


def test_max_effort_is_accepted_by_sampling_and_durable_selection():
    assert SamplingParams(reasoning_effort="max").reasoning_effort == "max"
    selection = ReasoningEffortSelection(effort="max")
    assert ReasoningEffortSelection.from_dict(selection.to_dict()) == selection


def test_context_request_carries_selected_agent_reasoning_effort(tmp_path):
    manager = ContextManager(tmp_path, config=Config(v2=ConfigV2()))
    manager._capabilities = Capabilities(thinking=True)
    manager.agent_definition = type("Agent", (), {"reasoning_effort": "high"})()

    request = manager.assemble(type("Session", (), {"messages": []})())

    assert request.params.reasoning_effort == "high"


def test_for_turn_accepts_and_forwards_runtime_reasoning_effort(tmp_path):
    manager = ContextManager(tmp_path, config=Config(v2=ConfigV2()))
    snapshot = manager.for_turn(reasoning_effort="high")
    snapshot._capabilities = Capabilities(thinking=True)
    snapshot._env = manager._build_env(Config(v2=ConfigV2()))
    request = snapshot.assemble(type("Session", (), {"messages": []})())
    assert request.params.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("thinking", "expected_effort"),
    [(True, "high"), (False, None)],
)
async def test_runtime_for_turn_passes_supported_agent_effort_to_real_context(
    tmp_path, thinking, expected_effort
):
    provider = ScriptedProvider(
        capabilities=Capabilities(thinking=thinking)
    )
    config = Config(
        model="scripted/sm",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/sm"),
            models=ModelsSection(
                default="scripted/sm",
                offline=True,
                reasoning_efforts={"scripted/sm": ["high"]},
            ),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})
    # This test harness explicitly declares the scripted model's supported level.
    runtime._registry = SimpleNamespace(
        get=lambda ref: SimpleNamespace(reasoning_efforts=("high",))
        if ref == "scripted/sm" else None
    )
    runtime._assembler._registry = runtime._registry
    session = runtime.session("agent-effort")
    agent = SimpleNamespace(
        reasoning_effort="high",
        provider=None,
        model=None,
        profile=None,
        load_body=lambda: "agent instructions",
    )
    try:
        # Exercise the real coordinator -> ContextManager.for_turn boundary.
        snapshot = runtime._assembler.for_turn(
            session=session, agent_definition=agent
        )
        request = await snapshot.assemble(session)

        assert request.params.reasoning_effort == expected_effort
        assert snapshot._agent_effort_supported is (expected_effort is not None)
    finally:
        await runtime.aclose()


async def test_root_effort_override_is_frozen_until_next_turn(tmp_path):
    provider = ScriptedProvider(capabilities=Capabilities(thinking=True))
    config = Config(
        model="scripted/sm",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/sm"),
            models=ModelsSection(
                default="scripted/sm",
                offline=True,
                reasoning_efforts={"scripted/sm": ["high", "medium", "low"]},
            ),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})
    # These deterministic levels come only from explicit test metadata.
    runtime._registry = SimpleNamespace(
        get=lambda ref: SimpleNamespace(
            reasoning_efforts=("low", "medium", "high")
        ) if ref == "scripted/sm" else None
    )
    runtime._assembler._registry = runtime._registry
    session = runtime.session("frozen-effort")
    session.select_reasoning_effort(ReasoningEffortSelection(effort="high"))
    agent = SimpleNamespace(
        reasoning_effort="medium", provider=None, model=None,
        profile=None, load_body=lambda: "agent instructions",
    )
    try:
        turn_snapshot = runtime._assembler.for_turn(
            session=session, agent_definition=agent
        )
        session.select_reasoning_effort(ReasoningEffortSelection(effort="low"))
        assert turn_snapshot._reasoning_effort == "high"

        next_snapshot = runtime._assembler.for_turn(
            session=session, agent_definition=agent
        )
        assert next_snapshot._reasoning_effort == "low"
    finally:
        await runtime.aclose()


async def test_runtime_wires_configured_reasoning_effort_overrides(tmp_path):
    config = Config(
        model="scripted/sm",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/sm"),
            models=ModelsSection(
                default="scripted/sm",
                offline=True,
                reasoning_efforts={"scripted/sm": ["high", "low"]},
            ),
        ),
    )
    runtime = Runtime(tmp_path, config=config, providers={"scripted": ScriptedProvider()})
    try:
        assert runtime.registry is not None
        assert runtime.registry._reasoning_effort_overrides == {
            "scripted/sm": ("low", "high")
        }
    finally:
        await runtime.aclose()


def test_root_effort_metadata_uses_explicit_route_levels_and_dormant_override():
    from nexus.runtime import _ContextCoordinator

    class Provider:
        name = "codex"
        _api = "responses"

    class Registry:
        def get(self, ref):
            efforts = ("medium", "xhigh") if ref == "codex/gpt-next" else ("low", "high")
            return SimpleNamespace(reasoning_efforts=efforts)

    class Resolver:
        def resolve(self, _request):
            return SimpleNamespace(
                provider=Provider(), model="gpt-test",
                capabilities=Capabilities(thinking=True),
            )

    config = Config(model="codex/gpt-test", version=2, v2=ConfigV2())
    context = SimpleNamespace(
        effective_config=lambda: config,
        model_reference=lambda _config: ("codex", "gpt-test"),
    )
    coordinator = _ContextCoordinator(context, Resolver())
    coordinator._registry = Registry()
    agent = SimpleNamespace(reasoning_effort="high")
    runtime = SimpleNamespace(_assembler=coordinator)
    runtime._context = context
    runtime._resolver = coordinator._resolver
    runtime._registry = coordinator._registry
    runtime._tiers = SimpleNamespace(order=())

    session = SimpleNamespace(
        model_selection=None,
        agent_selection=None,
        _turn_agent_definition=agent,
        reasoning_effort_selection=ReasoningEffortSelection(effort="high"),
    )
    metadata = Runtime.root_reasoning_effort_metadata(runtime, session)
    assert metadata == {
        "supported_levels": ("low", "high"),
        "stored_override": "high",
        "effective_effort": "high",
        "source": "session",
    }

    session.reasoning_effort_selection = ReasoningEffortSelection(effort="medium")
    metadata = Runtime.root_reasoning_effort_metadata(runtime, session)
    assert metadata == {
        "supported_levels": ("low", "high"),
        "stored_override": "medium",
        "effective_effort": None,
        "source": None,
    }

    session.model_selection = SimpleNamespace(provider="codex", model="gpt-next")

    class SwitchedRegistry:
        def get(self, ref):
            return SimpleNamespace(reasoning_efforts=("medium", "xhigh"))

    runtime._registry = SwitchedRegistry()
    coordinator._registry = runtime._registry
    metadata = Runtime.root_reasoning_effort_metadata(runtime, session)
    assert metadata == {
        "supported_levels": ("medium", "xhigh"),
        "stored_override": "medium",
        "effective_effort": "medium",
        "source": "session",
    }


def test_unknown_metadata_has_no_choices_and_supported_agent_default_applies():
    from nexus.runtime import _ContextCoordinator

    class Provider:
        name = "openai"
        _api = "responses"

    class Resolver:
        def resolve(self, _request):
            return SimpleNamespace(
                provider=Provider(), model="gpt-test",
                capabilities=Capabilities(thinking=True),
            )

    config = Config(model="openai/gpt-test", version=2, v2=ConfigV2())
    context = SimpleNamespace(
        effective_config=lambda: config,
        model_reference=lambda _config: ("openai", "gpt-test"),
    )
    coordinator = _ContextCoordinator(context, Resolver())
    coordinator._registry = SimpleNamespace(get=lambda _ref: SimpleNamespace(reasoning_efforts=()))
    session = SimpleNamespace(
        model_selection=None, agent_selection=None,
        _turn_agent_definition=SimpleNamespace(reasoning_effort="high"),
        reasoning_effort_selection=None,
    )
    runtime = SimpleNamespace(_assembler=coordinator)
    runtime._context = context
    runtime._resolver = coordinator._resolver
    runtime._registry = coordinator._registry
    runtime._tiers = SimpleNamespace(order=())
    assert Runtime.root_reasoning_effort_metadata(runtime, session) == {
        "supported_levels": (), "stored_override": None,
        "effective_effort": None, "source": None,
    }

    coordinator._registry.get = lambda _ref: SimpleNamespace(reasoning_efforts=("high",))
    assert Runtime.root_reasoning_effort_metadata(runtime, session) == {
        "supported_levels": ("high",), "stored_override": None,
        "effective_effort": "high", "source": "agent",
    }


async def test_plain_config_openai_responses_route_without_registry_has_no_efforts(tmp_path):
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    config = Config(
        model="openai/gpt-test",
        version=2,
        v2=ConfigV2(model=ModelSection(default="openai/gpt-test")),
    )
    runtime = Runtime(tmp_path, config=config, providers={"openai": provider})
    try:
        assert runtime.registry is None
        session = runtime.session("no-registry")
        metadata = runtime.root_reasoning_effort_metadata(session)
        assert metadata["supported_levels"] == ()
        assert metadata["effective_effort"] is None
        snapshot = runtime._assembler.for_turn(session=session)
        request = await snapshot.assemble(session)
        assert request.params.reasoning_effort is None
    finally:
        await runtime.aclose()


async def test_registry_known_openai_responses_model_without_effort_metadata_is_unavailable(
    tmp_path,
):
    from nexus.model.registry import ModelRegistry

    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    registry = ModelRegistry(
        providers={"openai": {"kind": "openai"}},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
    )
    registry.install_raw(
        '{"openai":{"models":{"gpt-test":{"reasoning":true,'
        '"modalities":{"output":["text"]}}}}}'
    )
    config = Config(
        model="openai/gpt-test",
        version=2,
        v2=ConfigV2(model=ModelSection(default="openai/gpt-test")),
    )
    runtime = Runtime(
        tmp_path, config=config, providers={"openai": provider}, registry=registry
    )
    try:
        session = runtime.session("missing-levels")
        metadata = runtime.root_reasoning_effort_metadata(session)
        assert registry.resolve("openai/gpt-test").reasoning_efforts == ()
        assert metadata["supported_levels"] == ()
        assert metadata["effective_effort"] is None
        snapshot = runtime._assembler.for_turn(
            session=session, agent_definition=runtime.agents.resolve("general", context="root")
        )
        request = await snapshot.assemble(session)
        assert request.params.reasoning_effort is None
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    ("route_provider", "catalogue_provider", "api", "expected"),
    [
        ("openai", "openai", API_RESPONSES, ["high"]),
        ("openai", "openai", API_CHAT, []),
        # Codex is a runtime alias of the OpenAI catalogue provider, but its
        # actual configured provider object still determines the route dialect.
        ("codex", "openai", API_RESPONSES, ["high"]),
    ],
)
async def test_selectable_model_rows_report_runtime_supported_efforts(
    tmp_path, route_provider, catalogue_provider, api, expected
):
    registry = ModelRegistry(
        providers={route_provider: ProviderSection(kind="openai")},
        provider_aliases={"codex": "openai"} if route_provider == "codex" else None,
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
    )
    registry.install_raw(
        json.dumps(
            {
                catalogue_provider: {
                    "models": {
                        "gpt-test": {
                            "reasoning": True,
                            "reasoning_efforts": ["high"],
                            "modalities": {"output": ["text"]},
                        }
                    }
                }
            }
        )
    )
    reference = f"{route_provider}/gpt-test"
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        api=api,
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    provider.name = route_provider
    config = Config(
        model=reference,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=reference),
            models=ModelsSection(default=reference, offline=True),
            providers={route_provider: ProviderSection(api_key="test", api=api)},
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={route_provider: provider},
        registry=registry,
    )
    runtime._registry_loaded = True
    try:
        facade = HostFacade(runtime)
        session = runtime.session("candidate-list")
        session.select_model(runtime._validate_model_selection(reference))
        if expected:
            session.select_reasoning_effort(ReasoningEffortSelection(effort="high"))
        before_events = list(session.events)
        before_model = session.model_selection
        before_effort = session.reasoning_effort_selection

        listed = await facade.handle(p.ModelsList(selectable_only=True))
        assert isinstance(listed, p.ModelsListResult)
        assert listed.count == 1
        row = listed.models[0]
        assert tuple(row["reasoning_efforts"]) == ("high",)
        assert row["supported_efforts"] == expected
        assert row["provider"] == route_provider

        # The compatibility catalogue view stays unchanged when not used as a
        # picker, and a read-only listing never creates a session selection.
        ordinary = await facade.handle(p.ModelsList())
        assert isinstance(ordinary, p.ModelsListResult)
        assert "supported_efforts" not in ordinary.models[0]
        assert list(session.events) == before_events
        assert session.model_selection == before_model
        assert session.reasoning_effort_selection == before_effort
        wire = msgspec.json.decode(p.encode_result(listed))
        assert wire["models"][0]["supported_efforts"] == expected
        if route_provider == "codex":
            # Catalogue alias rows remain descriptive; a configured router
            # alias that resolves elsewhere is not the same selectable target.
            assert runtime.candidate_supported_efforts("openai", "gpt-test") == ()
    finally:
        await runtime.aclose()


async def test_scripted_candidate_efforts_require_explicit_model_metadata(tmp_path):
    registry = ModelRegistry(
        providers={"scripted": ProviderSection(kind="openai")},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
    )
    registry.install_raw(
        '{"scripted":{"models":{"fixture":{"reasoning":true,'
        '"reasoning_efforts":["medium","high"],'
        '"modalities":{"output":["text"]}}}}}'
    )
    config = Config(
        model="scripted/fixture",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/fixture"),
            models=ModelsSection(default="scripted/fixture", offline=True),
            providers={"scripted": ProviderSection(kind="openai")},
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={
            "scripted": ScriptedProvider(
                name="scripted", capabilities=Capabilities(thinking=True)
            )
        },
        registry=registry,
    )
    runtime._registry_loaded = True
    try:
        row = (await HostFacade(runtime).handle(p.ModelsList(selectable_only=True))).models[0]
        assert row["supported_efforts"] == ["medium", "high"]
    finally:
        await runtime.aclose()


async def test_candidate_efforts_are_empty_when_registry_disables_thinking(tmp_path):
    registry = ModelRegistry(
        providers={"openai": ProviderSection(kind="openai")},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
    )
    registry.install_raw(
        '{"openai":{"models":{"gpt-test":{"reasoning":false,'
        '"reasoning_efforts":["high"],'
        '"modalities":{"output":["text"]}}}}}'
    )
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=False),
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    config = Config(
        model="openai/gpt-test",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="openai/gpt-test"),
            models=ModelsSection(default="openai/gpt-test", offline=True),
            providers={"openai": ProviderSection(api_key="test", api=API_RESPONSES)},
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"openai": provider},
        registry=registry,
    )
    runtime._registry_loaded = True
    try:
        row = (await HostFacade(runtime).handle(p.ModelsList(selectable_only=True))).models[0]
        assert tuple(row["reasoning_efforts"]) == ("high",)
        assert row["supported_efforts"] == []
    finally:
        await runtime.aclose()


async def test_candidate_efforts_are_empty_when_custom_tier_shadows_candidate(tmp_path):
    from nexus.model.tiers import TierTable

    registry = ModelRegistry(
        providers={"openai": ProviderSection(kind="openai")},
        env={},
        snapshot_path=tmp_path / "missing-models.json",
        use_snapshot=False,
        offline=True,
        tier_table=TierTable(overrides={"openai/gpt-test": ["openai/other"]}),
    )
    registry.install_raw(
        '{"openai":{"models":{"gpt-test":{"reasoning":true,'
        '"reasoning_efforts":["high"],"modalities":{"output":["text"]}},'
        '"other":{"reasoning":true,"reasoning_efforts":["low"],'
        '"modalities":{"output":["text"]}}}}}'
    )
    provider = OpenAIProvider(
        api_key="test",
        model="gpt-test",
        api=API_RESPONSES,
        capabilities=Capabilities(thinking=True),
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    config = Config(
        model="openai/gpt-test",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="openai/gpt-test"),
            models=ModelsSection(
                default="openai/gpt-test",
                tiers={"openai/gpt-test": ["openai/other"]},
                offline=True,
            ),
            providers={"openai": ProviderSection(api_key="test", api=API_RESPONSES)},
            permissions=PermissionsSection(mode="allow"),
        ),
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        providers={"openai": provider},
        registry=registry,
    )
    runtime._registry_loaded = True
    try:
        assert runtime.candidate_supported_efforts("openai", "gpt-test") == ()
    finally:
        await runtime.aclose()

def test_context_iteration_forwards_selected_agent_effort_to_model_request(tmp_path):
    manager = ContextManager(tmp_path, config=Config(v2=ConfigV2()))
    manager._capabilities = Capabilities(thinking=True)
    iteration = manager.for_iteration(config=Config(v2=ConfigV2()), reasoning_effort="high")
    iteration._capabilities = Capabilities(thinking=True)
    iteration._env = manager._build_env(Config(v2=ConfigV2()))
    request = iteration.assemble(type("Session", (), {"messages": []})())

    assert request.params.reasoning_effort == "high"


def test_runtime_child_assembler_uses_context_reasoning_effort_keyword():
    captured: dict[str, object] = {}

    class Assembler:
        _context = None

        def _resolve(self, _provider, _model):
            return SimpleNamespace(
                capabilities=Capabilities(thinking=True),
                provider=SimpleNamespace(name="scripted"), model="m",
            )

        @staticmethod
        def _supported_efforts(_resolved, _registry, *, provider_name=None):
            assert provider_name == "scripted"
            return ("high",)

        @staticmethod
        def for_iteration(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

    runtime = Runtime.__new__(Runtime)
    runtime._assembler = Assembler()
    runtime._registry = None
    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(model=ModelSection(default="scripted/m")),
    )
    spec = SimpleNamespace(
        reasoning_effort="high",
        parent_model="scripted/m",
        system_prompt="child prompt",
    )

    Runtime._build_child_assembler(runtime, spec, config)

    assert captured["reasoning_effort"] == "high"
    assert "selected_agent_effort" not in captured


def test_root_effort_is_not_inherited_by_child_assembler():
    captured: dict[str, object] = {}

    class Assembler:
        _context = None

        def for_iteration(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

    runtime = Runtime.__new__(Runtime)
    runtime._assembler = Assembler()
    spec = SimpleNamespace(reasoning_effort=None, system_prompt="child prompt")

    Runtime._build_child_assembler(runtime, spec, Config(v2=ConfigV2()))

    assert captured["reasoning_effort"] is None


def test_manifest_iteration_uses_context_reasoning_effort_keyword(tmp_path):
    manager = ContextManager(tmp_path, config=Config(v2=ConfigV2()))
    snapshot = manager.for_iteration(
        config=Config(v2=ConfigV2()), reasoning_effort="high"
    )
    snapshot._capabilities = Capabilities(thinking=True)
    snapshot._env = manager._build_env(Config(v2=ConfigV2()))
    request = snapshot.assemble(type("Session", (), {"messages": []})())
    assert request.params.reasoning_effort == "high"


def test_manifest_coordinator_forwards_reasoning_effort_to_context_iteration(tmp_path):
    from nexus.runtime import _ManifestEnvironmentFactory

    passed = {}

    class ContextAssembler:
        def for_iteration(self, **kwargs):
            passed.update(kwargs)
            effort = kwargs.get("selected_agent_effort", kwargs.get("reasoning_effort"))
            return SimpleNamespace(_reasoning_effort=effort, freeze_tools=lambda _schemas: None)

    class RuntimeStub:
        _hooks = None
        workspace = tmp_path
        _path_guard = None
        _skills = _extensions = _activations = None
        _selected_manifest = staticmethod(lambda manifest, _session: manifest)
        _selected_skills = staticmethod(lambda _session: None)
        _agents = SimpleNamespace(select_tools=lambda *_args, **_kwargs: SimpleNamespace(selected=()))
        _assembler = ContextAssembler()
        _pre_compact_gate = staticmethod(lambda *_args: None)
        _outbound_http_service = None

        @staticmethod
        def _filter_web_catalog(_config, catalog):
            return catalog

        def _build_iteration_manager(self, *_args, **_kwargs):
            return SimpleNamespace(names=(), specs=(), schemas=lambda: ())

    coordinator = _ManifestEnvironmentFactory.__new__(_ManifestEnvironmentFactory)
    coordinator._runtime = RuntimeStub()
    coordinator._session = SimpleNamespace(id="s", _turn_agent_selection_source="default")
    coordinator._turn_id = "turn"
    coordinator._model_selection = None
    coordinator._agent_definition = SimpleNamespace(reasoning_effort="high", profile=None)
    coordinator._reasoning_effort = "high"
    coordinator._path_guard = None
    coordinator._turn_permissions = None
    coordinator._budget = None
    coordinator._gate = None
    coordinator._activation_for = lambda _session: None
    coordinator._skill_tools_for = lambda *_args: ()
    coordinator._restrict_for = lambda *_args: None
    coordinator._subagent_runner = lambda *_args, **_kwargs: None
    manifest = SimpleNamespace(config=Config(v2=ConfigV2()), skills={}, mcp={}, tools={}, hooks=None)
    coordinator._session._turn_reasoning_effort = "low"
    coordinator.for_iteration(SimpleNamespace(id="s"), SimpleNamespace(manifest=manifest), 0)
    assert passed.get("selected_agent_effort", passed.get("reasoning_effort")) == "high"


@pytest.mark.parametrize("effort", ["", "default", "MAX", "ultra"])
def test_unknown_or_empty_reasoning_effort_is_rejected(effort: str):
    with pytest.raises(ValueError, match="reasoning_effort"):
        SamplingParams(reasoning_effort=effort)


def test_thinking_capability_requests_summary_with_default_effort():
    body = build_request_body(
        _request(), model="gpt-5", api=API_RESPONSES,
        capabilities=Capabilities(thinking=True),
    )
    assert body["reasoning"] == {"summary": "auto"}
    body = build_request_body(
        _request(), model="plain", api=API_RESPONSES,
        capabilities=Capabilities(thinking=False),
    )
    assert "reasoning" not in body
