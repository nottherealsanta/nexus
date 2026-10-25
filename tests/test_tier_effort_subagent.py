"""A tier-routed subagent gets the configured ``models.tier_efforts`` effort.

Regression: the child assembler never stamped ``metadata["tier"]``, so once the
context manager assembled the concrete model the router could not recover the
tier and ``ModelRouter.tier_effort`` returned the (null) request effort.
"""
from __future__ import annotations

from pathlib import Path

from test_phase6_integration import drain, tool_result_for

from nexus.model.capabilities import Capabilities
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from types import SimpleNamespace

from nexus.runtime import Runtime

CAPS = Capabilities(tools=True, streaming=True, thinking=True, max_context_tokens=200_000, max_output_tokens=8192)


async def test_medium_tier_subagent_runs_with_configured_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "codex/gpt-6.1-sol"\noffline = true\n'
        '[providers.codex]\nkind = "codex"\n'
        '[permissions]\nmode = "allow"\non_unattended = "allow"\n'
        '[models.tiers]\nmedium = ["codex/gpt-6.1-sol"]\n'
        '[models.tier_efforts]\nmedium = {"codex/gpt-6.1-sol" = "low"}\n',
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("t1", "subagent", {"prompt": "survey", "model": "medium"})),
        text_response("child report"),
        text_response("root done"),
        name="codex",
        capabilities=CAPS,
    )
    runtime = Runtime(ws, home=tmp_path / "home", providers={"codex": provider}, environ={})
    try:
        await runtime.ensure_models()
        session = runtime.session("root")
        await drain(session)
        assert not tool_result_for(session, "t1").is_error
        efforts = [r.params.reasoning_effort for r in provider.requests]
        assert efforts[1] == "low"  # the child; the root runs a concrete model
    finally:
        await runtime.aclose()


async def test_main_session_on_a_tier_gets_the_configured_effort(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "codex/gpt-6.1-sol"\noffline = true\n'
        '[agent]\nname = "tiered"\n'
        '[providers.codex]\nkind = "codex"\n'
        '[models.tiers]\nmedium = ["codex/gpt-6.1-sol"]\n'
        '[models.tier_efforts]\nmedium = {"codex/gpt-6.1-sol" = "high"}\n',
        encoding="utf-8",
    )
    agents = ws / ".agents" / "agents"
    agents.mkdir(parents=True)
    (agents / "tiered.md").write_text(
        "---\nname: tiered\ndescription: tier-routed root\nmodel: medium\ncontexts: [root]\n---\nWork.\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("hi"), name="codex", capabilities=CAPS)
    runtime = Runtime(ws, home=tmp_path / "home", providers={"codex": provider}, environ={})
    try:
        await runtime.ensure_models()
        await drain(runtime.session("root"))
        assert [r.params.reasoning_effort for r in provider.requests] == ["high"]
    finally:
        await runtime.aclose()


async def test_subagent_only_routes_report_the_efforts_a_child_run_honors(tmp_path, monkeypatch):
    """Worker-backed Claude Code takes ``claude --effort``; Antigravity has no effort control."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "codex/gpt-6.1-sol"\noffline = true\n'
        '[providers.codex]\nkind = "codex"\n'
        '[providers.claude-code]\nkind = "claude-code"\n'
        '[models.reasoning_efforts]\n"claude-code/claude-sonnet-5-5" = ["low", "high", "none"]\n'
        '[providers.antigravity]\nkind = "antigravity"\n',
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("x"), name="codex", capabilities=CAPS)
    runtime = Runtime(ws, home=tmp_path / "home", providers={"codex": provider}, environ={})
    try:
        await runtime.ensure_models()
        claude = runtime.candidate_supported_efforts("claude-code", "claude-sonnet-5-5", subagent=True)
        assert claude == ("low", "high")  # "none" is not a `claude --effort` level
        # The main-session answer is unchanged: a worker-backed route is not a main model.
        assert runtime.candidate_supported_efforts("claude-code", "claude-sonnet-5-5") == ()
        assert runtime.candidate_supported_efforts("antigravity", "gemini-3.8-flash-high", subagent=True) == ()
        # Ordinary providers answer identically for either question.
        assert runtime.candidate_supported_efforts("codex", "gpt-6.1-sol", subagent=True) == (
            runtime.candidate_supported_efforts("codex", "gpt-6.1-sol")
        )
    finally:
        await runtime.aclose()


def test_external_child_effort_comes_from_the_role_or_the_tier_entry():
    from nexus.runtime import _ChildRuntime

    def child(effort, tier_efforts, kind="claude-code"):
        spec = SimpleNamespace(
            reasoning_effort=effort, tier="high",
            config=SimpleNamespace(v2=SimpleNamespace(models=SimpleNamespace(tier_efforts=tier_efforts))),
        )
        obj = object.__new__(_ChildRuntime)
        obj._spec = spec
        obj._external = SimpleNamespace(kind=kind, provider="claude-code", model="claude-opus-5-5")
        return obj._external_effort()

    table = {"high": {"claude-code/claude-opus-5-5": "max"}}
    assert child(None, table) == "max"
    assert child("low", table) == "low"
    assert child(None, {}) is None
    assert child("none", {}) is None  # not a claude --effort level
    assert child(None, table, kind="antigravity") is None
