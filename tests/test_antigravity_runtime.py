"""Antigravity (``agy``): subagent-only routing, model discovery and durable outcome (no model turn)."""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nexus.agents import AgentManager, ChildSpec, SubagentBudget
from nexus.agents.antigravity_worker import AntigravityWorker
from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, ModelsSection, ProviderSection
from nexus.errors import ConfigError
from nexus.host import facade
from nexus.host_support import model_settings
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.registry import ADAPTER_ANTIGRAVITY, ModelRegistry, normalize_adapter_kind
from nexus.model.request import ModelRequest
from nexus.model.router import ModelRouter
from nexus.runtime import Runtime, _ChildRuntime

MODEL = "gemini-3.8-flash-high"
REF = f"antigravity/{MODEL}"


def section(**fields):
    fields.setdefault("worker_inherits_agy_settings", True)
    return ProviderSection(kind="antigravity", **fields)


def fake_runtime(providers=None, registry=None, tiers=None):
    providers = dict(providers or {})
    return SimpleNamespace(
        _router=ModelRouter(dict(providers)), _providers=providers, _registry=registry, _tiers=tiers,
        _environ={}, _validate_claude_section=Runtime._validate_claude_section,
        _external_tier_route=lambda *a, **k: None,
    )


def spec_for(tmp_path, *, model=REF, config=None, **extra):
    config = config or Config(v2=ConfigV2(providers={"antigravity": section()}))
    return SimpleNamespace(agent="explicit", config=config, workspace=tmp_path, model=model,
                           tools=("read", "edit"), metadata={}, **extra)


# -- schema and kind ---------------------------------------------------------


def test_schema_opt_in_is_false_by_default_and_validated():
    assert ProviderSection(kind="antigravity").worker_inherits_agy_settings is False
    assert ProviderSection(kind="agy", worker_inherits_agy_settings=True).worker_inherits_agy_settings
    assert ProviderSection(worker_inherits_agy_settings=True).worker_inherits_agy_settings
    with pytest.raises(ValueError, match="boolean"):
        ProviderSection(kind="antigravity", worker_inherits_agy_settings="yes")
    with pytest.raises(ValueError, match="only to kind = antigravity"):
        ProviderSection(kind="claude-code", worker_inherits_agy_settings=True)
    assert "worker_inherits_agy_settings=True" in repr(section())


def test_kind_alias_and_adapter_kind():
    assert normalize_adapter_kind("agy") == ADAPTER_ANTIGRAVITY == normalize_adapter_kind("antigravity")
    for name, sec in (("x", ProviderSection(kind="agy")), ("antigravity", None), ("agy", ProviderSection())):
        assert Runtime._adapter_kind(name, sec) == ADAPTER_ANTIGRAVITY


def test_section_validation_refuses_fields_agy_cannot_honour():
    Runtime._validate_antigravity_section("antigravity", section(executable="agy", timeout_seconds=30))
    with pytest.raises(ConfigError, match="worker_tools"):
        Runtime._validate_antigravity_section("antigravity", section(worker_tools=["Read"]))
    with pytest.raises(ConfigError, match="api_key"):
        Runtime._validate_antigravity_section("antigravity", section(api_key="x"))
    with pytest.raises(ConfigError, match="only to kind = antigravity"):
        Runtime._validate_claude_section("claude-agent", SimpleNamespace(
            kind="claude-agent", worker_inherits_agy_settings=True))


# -- root, title and fallback exclusion --------------------------------------


def test_never_a_nexus_provider_and_never_the_main_model(tmp_path):
    config = Config(v2=ConfigV2(providers={"antigravity": section()}))
    runtime = Runtime(tmp_path, config=config, home=tmp_path / "home", environ={})
    assert "antigravity" not in runtime._providers
    for bad in (
        Config(model=REF, v2=config.v2),
        Config(v2=ConfigV2(providers=config.v2.providers, models=ModelsSection(fallback=[REF]))),
        Config(v2=ConfigV2(providers=config.v2.providers, models=ModelsSection(fast=REF))),
    ):
        with pytest.raises(ConfigError, match="explicit subagent route"):
            Runtime(tmp_path, config=bad, home=tmp_path / "home", environ={})


def test_claude_worker_main_guard_is_independent_of_main_toggle():
    providers = {"claude-code": ProviderSection(kind="claude-code")}
    worker_main_on = Config(v2=ConfigV2(
        model=ModelSection(default="claude-code/claude-haiku-5-5"),
        providers=providers,
        models=ModelsSection(claude_code_main_enabled=True),
    ))
    with pytest.raises(ConfigError, match="subagent-only"):
        Runtime._refuse_subagent_only_selection(worker_main_on, providers)

    # Worker-backed Claude Code stays subagent-only even with its main toggle on.
    worker_backend_main_on = Config(v2=ConfigV2(
        model=ModelSection(default="claude-code/claude-haiku-5-5"),
        providers=providers,
        models=ModelsSection(
            claude_code_subagent_backend="worker", claude_code_main_enabled=True,
        ),
    ))
    with pytest.raises(ConfigError, match="subagent-only"):
        Runtime._refuse_subagent_only_selection(worker_backend_main_on, providers)

    sdk_main_off = Config(v2=ConfigV2(
        model=ModelSection(default="claude-code/claude-haiku-5-5"),
        providers=providers,
        models=ModelsSection(claude_code_subagent_backend="sdk"),
    ))
    with pytest.raises(ConfigError, match="requires claude_code_main_enabled"):
        Runtime._refuse_subagent_only_selection(sdk_main_off, providers)

    sdk_main_on = Config(v2=ConfigV2(
        model=ModelSection(default="claude-code/claude-haiku-5-5"),
        providers=providers,
        models=ModelsSection(
            claude_code_subagent_backend="sdk", claude_code_main_enabled=True,
        ),
    ))
    Runtime._refuse_subagent_only_selection(sdk_main_on, providers)

    # Exercise the canonical [model] fields independently of their mirrored
    # [models] values: every worker route must be rejected as a main default.
    for field, value in (
        ("default", "claude-code/claude-haiku-5-5"),
        ("fast", "claude-code/claude-haiku-5-5"),
        ("plan", "claude-code/claude-haiku-5-5"),
        ("fallback", ["claude-code/claude-haiku-5-5"]),
    ):
        config_with_legacy_model = Config(v2=ConfigV2(
            model=ModelSection(**{field: value}), providers=providers,
            models=ModelsSection(claude_code_main_enabled=True),
        ))
        with pytest.raises(ConfigError, match="subagent-only"):
            Runtime._refuse_subagent_only_selection(config_with_legacy_model, providers)


def test_ordinary_children_never_see_antigravity(tmp_path):
    other = ScriptedProvider(text_response("hi"))
    leaked = ScriptedProvider(text_response("no"))
    runtime = fake_runtime({"antigravity": leaked, "openai": other})
    spec = spec_for(tmp_path, model="openai/gpt-4o")
    child = _ChildRuntime(runtime, spec, object())
    assert child._external is None
    assert set(child._router._providers) == {"openai"}


# -- explicit routing --------------------------------------------------------


def test_explicit_child_plans_the_antigravity_worker(tmp_path):
    child = _ChildRuntime(fake_runtime(), spec_for(tmp_path), object())
    external = child._external
    assert isinstance(external.worker, AntigravityWorker) and external.model == MODEL
    assert external.provider == "antigravity" and external.kind == ADAPTER_ANTIGRAVITY
    assert external.inherits_settings and external.provenance
    assert not external.policy.read_only and external.policy.permission_mode == "auto"


def test_alias_section_and_bare_model_with_explicit_provider(tmp_path):
    config = Config(v2=ConfigV2(providers={"agy": ProviderSection(kind="agy", worker_inherits_agy_settings=True)}))
    child = _ChildRuntime(fake_runtime(), spec_for(tmp_path, model=f"agy/{MODEL}", config=config), object())
    assert child._external.provider == "agy" and child._external.model == MODEL
    bare = spec_for(tmp_path, model=MODEL, provider="antigravity")
    assert _ChildRuntime(fake_runtime(), bare, object())._external.model == MODEL
    tier = spec_for(tmp_path, model="medium", provider="antigravity")
    tiers = SimpleNamespace(order=("low", "medium", "high"))
    with pytest.raises(ConfigError, match="exact model"):
        _ChildRuntime(fake_runtime(tiers=tiers), tier, object())


def test_no_implicit_parent_inheritance(tmp_path):
    spec = spec_for(tmp_path, parent_model=REF, requested_model=None)
    with pytest.raises(ConfigError, match="inheriting the parent's model"):
        _ChildRuntime(fake_runtime(), spec, object())
    named = spec_for(tmp_path, parent_model=REF, requested_model=REF)
    assert _ChildRuntime(fake_runtime(), named, object())._external is not None


def test_refusals_are_explicit(tmp_path):
    def build(**changes):
        return _ChildRuntime(fake_runtime(), replace_spec(spec_for(tmp_path), **changes), object())

    with pytest.raises(ConfigError, match="token or cost budgets"):
        build(budget=SimpleNamespace(token_budget=10, cost_budget=None))
    with pytest.raises(ConfigError, match="token or cost budgets"):
        build(budget=SimpleNamespace(token_budget=None, cost_budget=1.0))
    with pytest.raises(ConfigError, match="worktree"):
        build(worktree_scope=True)
    with pytest.raises(ConfigError, match="read-only"):
        build(tools=("read", "grep", "glob"))
    with pytest.raises(ConfigError, match="read-only"):
        build(tools=("write",), metadata={"selection": {"read_only": True}})
    with pytest.raises(ConfigError, match="not a valid agy model"):
        build(model="antigravity/bad model;rm")
    with pytest.raises(ConfigError, match="exact model"):
        build(model="antigravity/")
    unsupported = Config(v2=ConfigV2(providers={"antigravity": section(worker_mcp_config="{}")}))
    with pytest.raises(ConfigError, match="worker_mcp_config"):
        build(config=unsupported)
    # The worker's own explicit-consent rule is not bypassed.
    optout = Config(v2=ConfigV2(providers={"antigravity": ProviderSection(kind="antigravity")}))
    with pytest.raises(ConfigError, match="worker_inherits_agy_settings"):
        build(config=optout)


def replace_spec(spec, **changes):
    return SimpleNamespace(**{**vars(spec), **changes})


def test_claude_code_children_are_unchanged(tmp_path):
    from nexus.agents.claude_code_worker import ClaudeCodeWorker

    config = Config(v2=ConfigV2(providers={"claude-code": ProviderSection(kind="claude-code")}))
    spec = SimpleNamespace(agent="e", config=config, workspace=tmp_path, model="claude-code/claude-haiku-4-5",
                           tools=("read", "edit"), metadata={})
    external = _ChildRuntime(fake_runtime(), spec, object())._external
    assert isinstance(external.worker, ClaudeCodeWorker) and external.label == "Claude Code"


# -- tier routing ------------------------------------------------------------


def test_pinned_tier_routes_to_antigravity_for_children_only(tmp_path):
    config = Config(v2=ConfigV2(
        providers={"antigravity": section()}, models=ModelsSection(tiers={"medium": [REF]})))
    runtime = Runtime(tmp_path, config=config, home=tmp_path / "home", environ={})
    assert runtime._external_tier_route("medium") == REF
    assert runtime._external_tier_route("medium", claude=False) == REF
    assert runtime._tier_runnable("medium")
    with pytest.raises(ConfigError):
        runtime._router.resolve(ModelRequest(messages=[], model=REF))
    spec = SimpleNamespace(agent="e", config=config, workspace=tmp_path, model="medium",
                           tools=("read", "edit"), metadata={})
    assert _ChildRuntime(runtime, spec, object())._external.model == MODEL


# -- registry discovery ------------------------------------------------------


class FakeCli:
    def __init__(self, models=None, error=None, hang=False, sync=False):
        self.models_result, self.error, self.hang, self.sync, self.made = models, error, hang, sync, []

    def __call__(self, executable=None, environ=None):
        self.made.append((executable, environ))
        outer = self

        class Cli:
            def models(self):
                if outer.sync:
                    return outer.models_result
                return outer.run()

        return Cli()

    async def run(self):
        if self.hang:
            await asyncio.sleep(3600)
        if self.error:
            raise self.error
        return self.models_result


def registry_for(cli, **fields):
    return ModelRegistry(
        providers={"antigravity": section(**fields)}, env={"X": "1"}, offline=True, use_snapshot=False,
        antigravity_cli=cli, antigravity_timeout_s=0.2)


async def test_discovers_models_from_agy_and_keeps_them_subagent_only():
    cli = FakeCli([(MODEL, "Gemini Flash High"), ("claude-sonnet-4-6", "Sonnet"), (MODEL, "dup"), ("bad id", "x")])
    registry = registry_for(cli, executable="/opt/agy")
    await registry.load()
    assert cli.made == [("/opt/agy", {"X": "1"})]
    rows = registry.list(provider="antigravity")
    assert [row.id for row in rows] == sorted([MODEL, "claude-sonnet-4-6"])
    row = next(row for row in rows if row.id == MODEL)
    assert row.name == "Gemini Flash High" and row.ref == REF and registry.get(REF) is row
    status = registry.provider_status("antigravity")
    assert status.adapter == status.kind == ADAPTER_ANTIGRAVITY and status.configured and not status.selectable
    assert registry.list(selectable_only=True) == rows
    with pytest.raises(ConfigError, match="not selectable"):
        registry.resolve(REF)
    # A tier-ranked listing (what price-ordered tiers walk) never contains the subagent-only models.
    assert all(registry.list(tier=tier) == [] for tier in ("low", "medium", "high"))


async def test_discovery_failure_leaves_no_unverified_models():
    for cli in (FakeCli(error=OSError("agy: not found")), FakeCli(hang=True), FakeCli([]), FakeCli("garbage")):
        registry = registry_for(cli)
        await registry.load()
        assert registry.list(provider="antigravity") == [] and registry.get(REF) is None
        assert registry.provider_status("antigravity").reachable is False
    failing = registry_for(FakeCli(error=RuntimeError("boom")))
    await failing.load()
    assert "boom" in failing.external_errors()["antigravity"]


async def test_refresh_replaces_and_clears_models_without_refetching_the_catalogue():
    cli = FakeCli([(MODEL, "A")], sync=True)
    registry = registry_for(cli)
    await registry.load()
    assert registry.get(REF) is not None
    cli.models_result = [("other-model", "B")]
    status = await registry.refresh_external()
    assert registry.get(REF) is None and registry.get("antigravity/other-model") is not None
    assert status.source == "empty"
    cli.error = None
    cli.models_result = []
    await registry.refresh_external()
    assert registry.list(provider="antigravity") == []


async def test_unconfigured_registry_discovers_without_execution_consent():
    cli = FakeCli([(MODEL, "A")])
    registry = ModelRegistry(providers={}, offline=True, use_snapshot=False, antigravity_cli=cli)
    await registry.load()
    assert cli.made == [(None, None)]
    assert registry.get(REF) is not None
    assert not registry.provider_status("antigravity").selectable
    with pytest.raises(ConfigError, match="worker_inherits_agy_settings"):
        from nexus.agents.antigravity_worker import WorkerConfig, resolve_policy
        resolve_policy(WorkerConfig.from_section(None), read_only=False)


async def test_runtime_rejects_a_model_agy_did_not_report(tmp_path):
    registry = registry_for(FakeCli([(MODEL, "A")]))
    await registry.load()
    Runtime._check_antigravity_model(SimpleNamespace(_registry=registry), "antigravity", MODEL)
    with pytest.raises(ConfigError, match="refresh the model list"):
        Runtime._check_antigravity_model(SimpleNamespace(_registry=registry), "antigravity", "stale-model")
    empty = registry_for(FakeCli(error=OSError("x")))
    await empty.load()
    with pytest.raises(ConfigError, match="installed and signed in"):
        Runtime._check_antigravity_model(SimpleNamespace(_registry=empty), "antigravity", MODEL)


# -- settings / facade -------------------------------------------------------


def settings_runtime(registry=None):
    config = Config(v2=ConfigV2(providers={"antigravity": section()}))
    return SimpleNamespace(_load_config=lambda: config, router=SimpleNamespace(providers={}), registry=registry)


def test_settings_rows_mark_antigravity_subagent_only():
    runtime = settings_runtime()
    assert model_settings.antigravity_providers(runtime) == {"antigravity", "agy"}
    rows = model_settings.candidate_rows(runtime, [REF, "openai/gpt-4o"])
    assert rows[0]["scope"] == "subagents" and rows[0]["connected"]
    assert rows[1]["scope"] == "" and not rows[1]["connected"]


async def test_default_model_cannot_be_antigravity_and_unknown_ids_get_a_hint():
    runtime = settings_runtime()
    with pytest.raises(ConfigError, match="subagents only"):
        await model_settings.default_models_set(runtime, [REF])
    registry = registry_for(FakeCli([(MODEL, "A")]))
    await registry.load()
    runtime = replace_spec(settings_runtime(registry), tiers=SimpleNamespace(order=["low", "medium", "high"]))
    with pytest.raises(ConfigError, match="`agy models`"):
        model_settings._check_refs_known(runtime, ["antigravity/stale"])
    model_settings._check_refs_known(runtime, [REF])


def test_provider_row_is_external_cli_login_and_idempotent():
    rows = facade._with_antigravity_row([{"id": "codex"}])
    row = rows[-1]
    assert row["id"] == "antigravity" and row["methods"] == ["cli"] and row["can_logout"] is False
    assert row["capabilities"] == ["subagent"] and "run `agy`" in row["help"]
    assert facade._with_antigravity_row(rows) is rows


# -- durable outcome ---------------------------------------------------------


def child_spec(workspace, config, *, emit):
    return ChildSpec(
        task_id="task-1", agent="agy", description="delegate", system_prompt="", prompt="fix the bug",
        session_id="root/sub/1", parent_session="root", parent_agent_id="root", parent_call_id="call",
        root_turn_id="turn-1", depth=1, tools=("read", "edit", "bash"), dropped_tools=(), model=REF,
        requested_model=None, parent_model=None, provider=None, reasoning_effort=None, tier="medium",
        requested_tier="medium", clamped=False, max_iterations=7, context_tokens=None, worktree=None,
        permissions=None, grants=(), budget=SubagentBudget(), cancel=None, emit=emit, hooks=None, metadata={},
        agent_id="root/sub/1", config=config, workspace=workspace, worktree_scope=False,
    )


class StubWorker:
    def __init__(self, **fields):
        self.fields, self.calls = fields, []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(**{
            "status": "completed", "text": "done", "is_error": False, "claude_session": "run-1", "error": None,
            "transcript_path": None, "tool_calls": 2, "clip_notes": (), "elapsed_seconds": 0.5,
            "attention": None, **self.fields})

    async def aclose(self):
        pass


async def run_stub(tmp_path, **fields):
    from nexus.runtime import _child_session_id

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    config = Config(model="standard", v2=ConfigV2(providers={"antigravity": section()}))
    runtime = Runtime(workspace, config=config, home=tmp_path / "home", environ={"PATH": os.environ["PATH"]},
                      agents=AgentManager.for_workspace(workspace, seed=False))

    async def emit(kind, data=None):
        pass

    child = _ChildRuntime(runtime, child_spec(workspace, config, emit=emit), object())
    worker = StubWorker(**fields)
    child._external = replace(child._external, worker=worker)
    try:
        outcome = await child.run()
        session = runtime._ensure_child_sessions().manager.open(_child_session_id("root/sub/1"), recover=False)
        return outcome, worker, [(event.type, dict(event.data)) for event in session.read().events()]
    finally:
        await child.aclose()
        await runtime.aclose()


async def test_outcome_records_generic_provenance_and_unknown_usage(tmp_path):
    outcome, worker, events = await run_stub(tmp_path)
    assert outcome.status == "completed" and outcome.ok and worker.calls[0]["model"] == MODEL
    metrics = outcome.metrics
    assert metrics["provider"] == "antigravity" and metrics["model"] == MODEL and metrics["usage"] == "unknown"
    assert metrics["external_kind"] == "antigravity" and metrics["external_label"] == "Antigravity (agy)"
    assert metrics["inherits_agy_settings"] is True and metrics["provenance"]
    assert metrics["external_session"] == "run-1" and "claude_session" not in metrics
    assert outcome.usage_unknown and outcome.usage.total_tokens == 0
    assert "[usage: unknown — external Antigravity (agy) worker]" in outcome.render()
    external = outcome.to_dict()["external"]
    assert external["external_kind"] == "antigravity" and external["external_session"] == "run-1"
    assert any(kind == "turn.completed" and data.get("usage_unknown") for kind, data in events)


async def test_reported_usage_is_kept_and_nothing_is_fabricated(tmp_path):
    outcome, _, events = await run_stub(tmp_path, usage={"input_tokens": 10, "output_tokens": 5})
    assert outcome.metrics["usage"] == "reported" and not outcome.usage_unknown
    assert outcome.usage.total_tokens == 15
    assert not any(data.get("usage_unknown") for kind, data in events if kind == "turn.completed")
    empty, _, _ = await run_stub(tmp_path / "again", usage={"input_tokens": 0})
    assert empty.usage_unknown


async def test_non_success_is_a_visible_failure_with_handoff(tmp_path):
    outcome, _, events = await run_stub(
        tmp_path, status="failed", is_error=True, text="partial", error="no terminal event")
    assert outcome.status == "failed" and outcome.is_error and outcome.handoff and outcome.text == "partial"
    assert outcome.metrics["external_kind"] == "antigravity"
    assert [data["error"] for kind, data in events if kind == "turn.failed"] == ["no terminal event"]


async def test_fresh_host_contract_exposes_subagents_and_refuses_root():
    from nexus.host.facade import HostFacade
    cli = FakeCli([(MODEL, "Gemini Flash High")])
    registry = ModelRegistry(providers={}, offline=True, use_snapshot=False, antigravity_cli=cli)
    await registry.load()
    config = SimpleNamespace(v2=SimpleNamespace(providers={}))
    runtime = SimpleNamespace(_registry=registry, registry=registry, _load_config=lambda: config)
    host = HostFacade.__new__(HostFacade)
    host.runtime = runtime
    listed = [host._model_row(row, selectable_only=True) for row in host.list_models(selectable_only=True)]
    assert f"{listed[0]['provider']}/{listed[0]['id']}" == REF
    assert listed[0]["scope"] == "subagents"
    assert "Subagents only" in listed[0]["name"]
    runtime._adapter_kind = Runtime._adapter_kind
    with pytest.raises(ConfigError, match="Subagents only"):
        Runtime._validate_model_selection(runtime, REF)


def test_claude_code_model_scopes_follow_sdk_and_worker_backend():
    from nexus.host.facade import HostFacade

    row = SimpleNamespace(provider="claude-code", id="claude-sonnet-4-5")

    class Registry:
        def list(self, **kwargs):
            return [row]

        def provider_status(self, provider):
            return SimpleNamespace(kind="claude-code")

    for backend, expected_subagent in (("sdk", True), ("worker", True)):
        config = SimpleNamespace(v2=SimpleNamespace(
            models=ModelsSection(claude_code_subagent_backend=backend, claude_code_main_enabled=True),
            providers={"claude-code": ProviderSection(kind="claude-code")},
        ))
        runtime = SimpleNamespace(
            registry=Registry(), _load_config=lambda config=config: config,
            claude_code_main_enabled=lambda: True,
            _adapter_kind=Runtime._adapter_kind,
        )
        host = HostFacade.__new__(HostFacade)
        host.runtime = runtime
        assert [r.provider for r in host.list_models(selectable_only=True, scope="subagents")] == (
            ["claude-code"] if expected_subagent else []
        )
        main = host.list_models(selectable_only=True, scope="main")
        assert bool(main) is (backend == "sdk")


async def test_custom_alias_discovery_keeps_builtin_antigravity_visible():
    cli = FakeCli([(MODEL, "Flash")])
    registry = ModelRegistry(providers={"studio": {"kind": "agy", "executable": "/opt/studio-agy"}},
                             offline=True, use_snapshot=False, antigravity_cli=cli)
    await registry.load()
    assert cli.made == [("/opt/studio-agy", None), (None, None)]
    assert {row.ref for row in registry.list(selectable_only=True)} == {f"studio/{MODEL}", REF}
    assert registry.provider_status("antigravity").kind == ADAPTER_ANTIGRAVITY
    assert registry.provider_status("studio").kind == ADAPTER_ANTIGRAVITY


async def test_fresh_missing_cli_has_no_models():
    cli = FakeCli([], error=OSError("agy not found"))
    registry = ModelRegistry(providers={}, offline=True, use_snapshot=False, antigravity_cli=cli)
    await registry.load()
    assert registry.list(selectable_only=True) == []
    assert not registry.provider_status("antigravity").reachable


def test_low_tier_antigravity_refusal_discloses_selected_route(tmp_path):
    runtime = fake_runtime()
    runtime._tiers = SimpleNamespace(order=("low", "medium", "high"))
    runtime._external_tier_route = lambda tier, **kwargs: REF if tier == "low" else None
    spec = replace_spec(spec_for(tmp_path, model="low"), tools=("glob", "read"))
    with pytest.raises(ConfigError, match="read-only") as refused:
        _ChildRuntime(runtime, spec, object())
    assert refused.value.selected_model == REF
    assert "Auto mode does not enforce" in str(refused.value)
