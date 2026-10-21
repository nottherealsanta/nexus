"""Usage-aware sub-agent routing: spawn-time only, sub-agents only, fail open.

Plan: plans/done/SUBAGENT_USAGE_ROUTING_PLAN.md sections 4.3-4.5. A real
``Runtime`` supplies the gate (``route_subagent``); the runner records the chosen
route and an ``agent.routed`` event; the reducer replays it into ``AgentView.route``.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.agents import TaskRequest
from nexus.config import Config
from nexus.events import Event
from nexus.model.providers.scripted import ScriptedProvider
from nexus.model.request import ModelRequest
from nexus.model.router import ModelRouter
from nexus.model.usage_gate import GatedFallbacks, UsageSnapshot, UsageVerdict
from nexus.runtime import Runtime, _ChildRuntime
from nexus.view import fold
from tests.test_subagent_runner import Factory, Recorder, make_runner

FIVE_HOURS = 18_000


def snapshot(**used: float | None) -> UsageSnapshot:
    """``codex=92`` is a Codex 5-hour window 92% used; ``None`` is an unparsed window."""
    rows = []
    for provider, percent in used.items():
        windows = [{"label": "5-hour", "kind": "five_hour", "used_percent": percent,
                    "resets_at": int(time.time()) + 3600, "period_seconds": FIVE_HOURS}]
        rows.append({"id": provider.replace("_", "-"), "windows": windows})
    return UsageSnapshot.from_result({"providers": rows, "fetched_at": time.time()})


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "anthropic/claude-opus-5"\noffline = true\n'
        '[providers.anthropic]\nkind = "anthropic"\n[providers.openai]\nkind = "openai"\n', encoding="utf-8")
    runtime = Runtime(workspace, home=tmp_path / "home",
                      environ={"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k"})
    await runtime.ensure_models()
    anthropic = [f"anthropic/{i.id}" for i in runtime.registry.list(provider="anthropic")][0]
    openai = [f"openai/{i.id}" for i in runtime.registry.list(provider="openai")][0]
    # Pin ``low`` to a known order (anthropic first, openai second) and apply it live.
    (workspace / "nexus.toml").write_text(
        (workspace / "nexus.toml").read_text(encoding="utf-8")
        + f'[models.tiers]\nlow = ["{anthropic}", "{openai}"]\n', encoding="utf-8")
    assert await runtime.reload_tier_config()
    runtime.refs = (anthropic, openai)
    try:
        yield runtime
    finally:
        await runtime.aclose()


async def spawn(tmp_path, runtime, recorder=None):
    factory, recorder = Factory(), recorder or Recorder()
    runner = make_runner(
        tmp_path, factory, event_sink=recorder, parent_model="anthropic/claude-opus-5",
        tiers=runtime._tiers, tier_candidates=runtime._router.tier_candidates,
        route_gate=runtime.route_subagent)
    outcome = await runner.spawn(TaskRequest(prompt="x", subagent_type="quick"))
    return outcome, factory.specs[-1], recorder


async def test_blocked_first_candidate_reroutes_and_replays(tmp_path, runtime):
    first, second = runtime.refs
    runtime.set_usage_snapshot(snapshot(anthropic=92.0))
    outcome, spec, recorder = await spawn(tmp_path, runtime)
    assert spec.model == second and spec.tier == "low"
    routed = recorder.of("agent.routed")
    assert len(routed) == 1
    assert routed[0]["first"] == first and routed[0]["chosen"] == second and routed[0]["all_blocked"] is False
    assert routed[0]["skipped"][0]["kind"] == "five_hour" and routed[0]["skipped"][0]["remaining_percent"] == pytest.approx(8.0)
    assert recorder.types().index("agent.routed") == recorder.types().index("agent.spawned") + 1

    events = [Event(type=t, data=d, seq=i + 1, ts=float(i), session="root", turn=None, id=f"e{i:03d}")
              for i, (t, d) in enumerate(recorder.events) if t in ("agent.spawned", "agent.routed")]
    view = fold(events)
    agent = next(iter(view.agents.values()))
    assert agent.route["chosen"] == second and agent.route["skipped"][0]["reference"] == first


async def test_all_blocked_uses_first_and_marks_it(tmp_path, runtime):
    first, _ = runtime.refs
    runtime.set_usage_snapshot(snapshot(anthropic=95.0, openai=99.0))
    _, spec, recorder = await spawn(tmp_path, runtime)
    assert spec.model == "low"  # unchanged: the tier still resolves to its first runnable model
    routed = recorder.of("agent.routed")[0]
    assert routed["all_blocked"] is True and routed["chosen"] == first and len(routed["skipped"]) == 2


@pytest.mark.parametrize("make", [
    lambda: None,                                                   # no snapshot at all
    lambda: snapshot(anthropic=None),                               # unparsed window
    lambda: UsageSnapshot.from_result({"providers": [{"id": "anthropic", "error": "boom", "windows": [
        {"kind": "five_hour", "used_percent": 99.0}]}], "fetched_at": time.time()}),
    lambda: UsageSnapshot.from_result({"providers": [{"id": "anthropic", "windows": [
        {"kind": "five_hour", "used_percent": 99.0}]}], "fetched_at": time.time() - 10_000}),  # stale
])
async def test_unknown_or_stale_usage_fails_open(tmp_path, runtime, make):
    runtime.set_usage_snapshot(make())
    _, spec, recorder = await spawn(tmp_path, runtime)
    assert spec.model == "low" and recorder.of("agent.routed") == []


async def test_snapshot_change_reroutes_next_spawn_and_reset_restores(tmp_path, runtime):
    _, second = runtime.refs
    runtime.set_usage_snapshot(snapshot(anthropic=10.0))
    assert (await spawn(tmp_path, runtime))[1].model == "low"
    runtime.set_usage_snapshot(snapshot(anthropic=91.0))
    assert (await spawn(tmp_path, runtime))[1].model == second
    runtime.set_usage_snapshot(snapshot(anthropic=0.0))
    assert (await spawn(tmp_path, runtime))[1].model == "low"


async def test_disabled_gate_never_reroutes(tmp_path, runtime):
    from nexus.model.usage_gate import UsagePolicy

    runtime.set_usage_snapshot(snapshot(anthropic=99.0))
    runtime.usage_policy = lambda: UsagePolicy(enabled=False)
    _, spec, recorder = await spawn(tmp_path, runtime)
    assert spec.model == "low" and recorder.of("agent.routed") == []


async def test_root_requests_are_never_rerouted(runtime):
    first, _ = runtime.refs
    runtime.set_usage_snapshot(snapshot(anthropic=99.0))
    for model in (first, "low"):
        resolved = runtime._router.resolve(ModelRequest(messages=[], model=model))
        assert resolved.provider.name == "anthropic"
    selection = runtime._validate_model_selection(first)
    assert selection.provider == "anthropic"


async def test_headroom_maps_references_and_providers_to_quota(runtime):
    runtime.set_usage_snapshot(snapshot(anthropic=99.0))
    assert runtime.usage_headroom("anthropic/claude-opus-5").state == "blocked"
    assert runtime.usage_headroom("anthropic").state == "blocked"
    assert runtime.usage_headroom("openai/gpt-6").state == "unknown"


def test_fallbacks_skip_blocked_providers():
    providers = {name: ScriptedProvider([], name=name) for name in ("a", "b", "c")}
    router = ModelRouter(providers, fallback=["b/m", "c/m"])
    request = ModelRequest(messages=[], model="a/m")
    assert [r.provider.name for r in router.fallbacks(request)] == ["b", "c"]
    gated = GatedFallbacks(router, lambda p: UsageVerdict(p, "blocked" if p == "b" else "ok"))
    assert [r.provider.name for r in gated.fallbacks(request)] == ["c"]
    assert gated.resolve(request).provider.name == "a"
    # Everything blocked: keep the chain rather than strip it.
    every = GatedFallbacks(router, lambda p: UsageVerdict(p, "blocked"))
    assert [r.provider.name for r in every.fallbacks(request)] == ["b", "c"]


def test_child_runtime_wraps_its_router_with_gated_fallbacks(tmp_path):
    router = ModelRouter({})
    runtime = SimpleNamespace(
        _router=router, _providers={}, _registry=None, _tiers=None, _environ={},
        usage_headroom=lambda ref: UsageVerdict(ref, "ok"))
    spec = SimpleNamespace(agent="x", config=Config(), workspace=tmp_path, model="a/m", tools=(), metadata={})
    child = _ChildRuntime(runtime, spec, object())
    assert isinstance(child._router, GatedFallbacks)
