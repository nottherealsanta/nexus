"""Routing: explicit claude-code children use the external worker; all others are unchanged."""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from claude_code_fakes import install

from nexus.agents import ChildSpec, SubagentBudget
from nexus.agents.runner import SubagentOutcome
from nexus.agents import claude_code_worker as cw
from nexus.agents.claude_code_worker import ClaudeCodeWorker
from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelsSection, ProviderSection
from nexus.errors import ConfigError
from nexus.model.providers.claude_agent import ClaudeAgentProvider
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.registry import ADAPTER_CLAUDE_CODE, normalize_adapter_kind
from nexus.model.request import ModelRequest
from nexus.model.router import ModelRouter
from nexus.runtime import Runtime, _ChildRuntime


@pytest.fixture(autouse=True)
def machine_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "nexus_home", lambda: tmp_path / ".nexus")


def test_kind_aliases():
    for name in ("claude-code", "claude_code", "claudecode"):
        assert normalize_adapter_kind(name) == ADAPTER_CLAUDE_CODE


@pytest.mark.parametrize("enabled", [False, True])
def test_main_gate(tmp_path, enabled):
    config = Config(model="claude-code/claude-opus-5.5", v2=ConfigV2(
        providers={"claude-code": ProviderSection(kind="claude-code")},
        models=ModelsSection(claude_code_subagent_backend="sdk", claude_code_main_enabled=enabled),
    ))
    if not enabled:
        with pytest.raises(ConfigError, match="requires claude_code_main_enabled"):
            Runtime(config=config, workspace=tmp_path)
        return
    runtime = Runtime(config=config, workspace=tmp_path)
    assert runtime.claude_code_main_enabled() is enabled
    resolved = runtime._router.resolve(ModelRequest(messages=[], model=config.model))
    assert isinstance(resolved.provider, ClaudeAgentProvider)
    assert resolved.provider.name == "claude-code"


def fake_runtime(parent=None):
    providers = {"claude-code": parent} if parent is not None else {}
    return SimpleNamespace(
        _router=ModelRouter(dict(providers)), _providers=dict(providers), _registry=None, _tiers=None,
        _environ={}, _validate_claude_section=Runtime._validate_claude_section,
    )


def test_explicit_child_plans_external_worker_not_a_router_swap(tmp_path):
    sdk = ClaudeAgentProvider(workspace=tmp_path)
    sdk.name = "claude-code"
    runtime = fake_runtime(sdk)
    spec = SimpleNamespace(agent="explicit", config=Config(), workspace=tmp_path, model="claude-code/claude-opus-5.5",
                           tools=("read", "edit"), metadata={})
    child = _ChildRuntime(runtime, spec, object())
    external = child._external
    assert isinstance(external.worker, ClaudeCodeWorker) and external.model == "claude-opus-5.5"
    assert external.provider == "claude-code" and not external.policy.read_only
    assert external.policy.permission_mode == "dontAsk" and external.policy.restricted
    # The Nexus loop never sees Claude Code: the child router has no claude-code provider.
    with pytest.raises(ConfigError):
        child._router.resolve(ModelRequest(messages=[], model="claude-code/claude-opus-5.5"))
    assert runtime._providers["claude-code"] is sdk


@pytest.mark.asyncio
async def test_sdk_backend_runs_claude_children_in_the_nexus_loop(tmp_path):
    config = Config(v2=ConfigV2(
        providers={"claude-code": ProviderSection(kind="claude-code")},
        models=ModelsSection(claude_code_subagent_backend="sdk"),
    ))
    runtime = Runtime(config=config, workspace=tmp_path)
    # Main selection stays off: the root router still cannot reach Claude Code.
    with pytest.raises(ConfigError):
        runtime._router.resolve(ModelRequest(messages=[], model="claude-code/claude-haiku-4-5"))
    spec = SimpleNamespace(agent="explicit", config=config, workspace=tmp_path,
                           model="claude-code/claude-haiku-4-5", tools=("read", "edit"), metadata={})
    child = _ChildRuntime(runtime, spec, object())
    assert child._external is None  # no interactive worker
    resolved = child._router.resolve(ModelRequest(messages=[], model="claude-code/claude-haiku-4-5"))
    assert isinstance(resolved.provider, ClaudeAgentProvider) and resolved.provider in child._owned
    await child.aclose()
    assert child._owned == [] and resolved.provider._closed
    await runtime.aclose()


def test_ordinary_children_are_unchanged(tmp_path):
    sdk = ClaudeAgentProvider(workspace=tmp_path)
    sdk.name = "claude-code"
    other = ScriptedProvider(text_response("hi"))
    runtime = SimpleNamespace(
        _router=ModelRouter({"claude-code": sdk, "openai": other}),
        _providers={"claude-code": sdk, "openai": other}, _registry=None, _tiers=None, _environ={},
        _validate_claude_section=Runtime._validate_claude_section,
    )
    config = Config(v2=ConfigV2(providers={"claude-code": ProviderSection(kind="claude-code")}))
    spec = SimpleNamespace(agent="general", config=config, workspace=tmp_path, model="openai/gpt-4o",
                           tools=("read",), metadata={})
    child = _ChildRuntime(runtime, spec, object())
    assert child._external is None
    assert child._router is not runtime._router  # claude-code filtered out of the child router
    assert child._router.resolve(ModelRequest(messages=[], model="openai/gpt-4o")).provider is other
    with pytest.raises(ConfigError):
        child._router.resolve(ModelRequest(messages=[], model="claude-code/claude-opus-5.5"))
    # With no Claude Code provider configured at all, the parent router is reused as-is.
    plain = fake_runtime()
    unchanged = _ChildRuntime(plain, SimpleNamespace(config=Config(), workspace=tmp_path, model="standard"), object())
    assert unchanged._external is None and unchanged._router is plain._router


@pytest.mark.asyncio
async def test_child_close_closes_only_its_worker(tmp_path):
    from unittest.mock import AsyncMock

    parent = SimpleNamespace(aclose=AsyncMock())
    spec = SimpleNamespace(config=Config(), workspace=tmp_path, model="claude-code/sonnet", tools=(), metadata={})
    child = _ChildRuntime(fake_runtime(parent), spec, object())
    child._external.worker.aclose = AsyncMock()
    await child.aclose()
    child._external.worker.aclose.assert_awaited_once()
    parent.aclose.assert_not_awaited()


def test_default_model_budget_guard_and_alias_section(tmp_path):
    spec = SimpleNamespace(config=Config(), workspace=tmp_path, model="standard", provider="claude-code",
                           tools=("write",), metadata={})
    # No silent default model: an unnamed (tier/alias) model is refused, an exact bare ID is kept.
    with pytest.raises(ConfigError, match="exact model"):
        _ChildRuntime(fake_runtime(), spec, object())
    spec.model = "claude-haiku-4-5"
    assert _ChildRuntime(fake_runtime(), spec, object())._external.model == "claude-haiku-4-5"
    spec.budget = SimpleNamespace(token_budget=100, cost_budget=None)
    with pytest.raises(ConfigError, match="cannot enforce"):
        _ChildRuntime(fake_runtime(), spec, object())
    spec.budget = SimpleNamespace(token_budget=None, cost_budget=1.5)
    with pytest.raises(ConfigError, match="cannot enforce"):
        _ChildRuntime(fake_runtime(), spec, object())
    aliased = SimpleNamespace(
        config=Config(v2=ConfigV2(providers={"cli": ProviderSection(kind="claude-code")})),
        workspace=tmp_path, model="cli/claude-sonnet-4-6", tools=(), metadata={})
    child = _ChildRuntime(fake_runtime(), aliased, object())
    assert child._external.provider == "cli" and child._external.model == "claude-sonnet-4-6"


def test_read_only_child_is_never_restricted_or_refused(tmp_path):
    # Read-only is prompt guidance only (user decision 2026-10-17).
    section = ProviderSection(kind="claude-code", worker_permission_mode="acceptEdits")
    config = Config(v2=ConfigV2(providers={"claude-code": section}))
    read_only = SimpleNamespace(config=config, workspace=tmp_path, model="claude-code/claude-haiku-4-5",
                                tools=("read", "grep", "glob"), metadata={})
    policy = _ChildRuntime(fake_runtime(), read_only, object())._external.policy
    assert not policy.read_only and policy.tools is None and policy.permission_mode == "acceptEdits"
    flagged = SimpleNamespace(config=config, workspace=tmp_path, model="claude-code/claude-haiku-4-5",
                              tools=("write",), metadata={"selection": {"read_only": True}})
    assert not _ChildRuntime(fake_runtime(), flagged, object())._external.policy.read_only
    wide = Config(v2=ConfigV2(providers={"claude-code": ProviderSection(
        kind="claude-code", worker_tools=["Read", "Bash"])}))
    wide_spec = replace_spec(read_only, config=wide)
    assert _ChildRuntime(fake_runtime(), wide_spec, object())._external.policy.tools == ("Read", "Bash")


def replace_spec(spec, **changes):
    return SimpleNamespace(**{**vars(spec), **changes})


def test_worker_fields_only_belong_to_claude_code():
    Runtime._validate_claude_section("claude-code", ProviderSection(kind="claude-code", worker_tools=["Read"]))
    with pytest.raises(ConfigError, match="only to kind = claude-code"):
        Runtime._validate_claude_section("claude-agent", ProviderSection(kind="claude-agent", worker_tools=["Read"]))
    with pytest.raises(ConfigError, match="subscription login"):
        Runtime._validate_claude_section("claude-code", ProviderSection(kind="claude-code", api_key="x"))


def test_registry_alias_kind():
    from nexus.model.registry import ModelRegistry

    registry = ModelRegistry(env={}, providers={"cli": ProviderSection(kind="claude-code")})
    registry.install_raw(b'{"cli":{"id":"cli","name":"CLI","models":{}}}')
    assert registry.provider_status("cli").adapter == ADAPTER_CLAUDE_CODE


@pytest.mark.asyncio
async def test_auth_uses_existing_mock_path():
    from unittest.mock import AsyncMock, Mock

    from nexus.host_support.provider_auth import connected

    auth = SimpleNamespace(status=AsyncMock(return_value=True))
    runtime = SimpleNamespace(_config=Config(), _environ={}, _claude_auth_factory=Mock(return_value=auth))
    assert await connected(runtime, "claude-code")
    auth.status.assert_awaited_once()


# -- end to end through the real Runtime -------------------------------------


def child_spec(tmp_path, runtime, config, *, emit, tools=("read", "edit", "bash")):
    return ChildSpec(
        task_id="task-1", agent="claude", description="delegate", system_prompt="", prompt="fix the bug",
        session_id="root/sub/1", parent_session="root", parent_agent_id="root", parent_call_id="call",
        root_turn_id="turn-1", depth=1, tools=tools, dropped_tools=("grep",), model="claude-code/claude-haiku-4-5",
        requested_model=None, parent_model=None, provider=None, reasoning_effort=None, tier="medium",
        requested_tier="medium", clamped=False, max_iterations=7, context_tokens=None, worktree=None,
        permissions=None, grants=(), budget=SubagentBudget(), cancel=None, emit=emit, hooks=None, metadata={},
        agent_id="root/sub/1", config=config, workspace=tmp_path, worktree_scope=False,
    )


async def external_run(tmp_path, mode, **fake_config):
    from nexus.agents import AgentManager

    exe = install(tmp_path / "bin", mode=mode, **fake_config)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "pre.txt").write_text("existing")
    section = ProviderSection(kind="claude-code", executable=exe, timeout_seconds=2,
                              worker_allowed_tools=["Edit"], worker_permission_mode="dontAsk")
    config = Config(model="standard", v2=ConfigV2(providers={"claude-code": section}))
    runtime = Runtime(workspace, config=config, home=tmp_path / "home", environ={"PATH": os.environ["PATH"]},
                      agents=AgentManager.for_workspace(workspace, seed=False))
    relayed = []

    async def emit(kind, data=None):
        relayed.append((kind, dict(data or {})))

    spec = child_spec(workspace, runtime, config, emit=emit)
    child = _ChildRuntime(runtime, spec, object())
    try:
        outcome = await child.run()
    finally:
        await child.aclose()
    return runtime, spec, outcome, relayed, workspace


async def test_end_to_end_success_records_and_reports_external_authority(tmp_path):
    runtime, spec, outcome, relayed, workspace = await external_run(tmp_path, "success", touch="fixed.txt")
    try:
        assert outcome.status == "completed" and outcome.ok and outcome.text == "All done."
        assert outcome.usage.total_tokens == 0 and outcome.usage_unknown
        assert outcome.metrics["usage"] == "unknown" and outcome.metrics["provider"] == "claude-code"
        assert outcome.metrics["model"] == "claude-haiku-4-5" and outcome.metrics["transcript_path"].endswith(".jsonl")
        assert outcome.dropped_tools == ("grep",)
        # File changes are verified from the workspace itself.
        assert outcome.files_changed == ("fixed.txt",)
        rendered = outcome.render()
        assert "[usage: unknown — external Claude Code worker]" in rendered
        assert "[authority: Claude Code ran its own tools (mode dontAsk, tools default set, allowed Edit" in rendered
        assert f"in {workspace.resolve()}; Nexus permissions/PathGuard did not mediate them]" in rendered
        assert "workspace-wide" in rendered and "- fixed.txt" in rendered
        as_dict = outcome.to_dict()
        assert as_dict["usage_unknown"] is True and as_dict["external"]["authority"].startswith("Claude Code ran")
        # Events relay upward (turn lifecycle stays in the child log) and never look like Nexus tools.
        kinds = [kind for kind, _ in relayed]
        assert kinds[0] == "external.started" and "external.tool" in kinds and "external.finished" in kinds
        assert "turn.started" not in kinds and "tool.requested" not in kinds
        # The child log is durable: user prompt, assistant answer and external events.
        from nexus.runtime import _child_session_id

        session = runtime._ensure_child_sessions().manager.open(_child_session_id("root/sub/1"), recover=False)
        types = [event.type for event in session.read().events()]
        assert types[0] == "turn.started" and types[-1] == "turn.completed"
        assert {"external.started", "external.tool", "external.finished", "text"} <= set(types)
        # The context snapshot never claims "no tools" for Claude's default set.
        assembled = next(e.data for e in session.read().events() if e.type == "context.assembled")
        assert assembled["tools"] == 1
        assert assembled["request"]["tools"][0]["name"] == "Claude Code default set"
        assert "AskUserQuestion" in assembled["request"]["tools"][0]["description"]
        roles = [(m.role, "".join(b.text for b in m.content if hasattr(b, "text"))) for m in session.messages]
        assert roles == [("user", "fix the bug"), ("assistant", "All done.")]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(("mode", "status", "handoff"), [
    ("exit", "failed", True), ("trust", "needs_attention", False),
])
async def test_end_to_end_non_success_is_visible_and_handed_off(tmp_path, mode, status, handoff):
    runtime, _, outcome, relayed, _ = await external_run(tmp_path, mode)
    try:
        assert outcome.status == status and outcome.is_error and not outcome.ok
        assert bool(outcome.handoff) is handoff
        assert outcome.usage_unknown and "[usage: unknown" in outcome.render()
        finished = [data for kind, data in relayed if kind == "external.finished"]
        assert finished and finished[-1]["status"] == status
    finally:
        await runtime.aclose()


async def test_end_to_end_refusal_is_reported_as_failed_start(tmp_path):
    runtime, spec, _, _, workspace = await external_run(tmp_path, "success")
    try:
        spec = replace(spec, prompt="x" * (70 * 1024), session_id="root/sub/2", agent_id="root/sub/2")
        child = _ChildRuntime(runtime, spec, object())
        outcome = await child.run()
        assert outcome.status == "failed" and "64 KiB" in outcome.text and outcome.handoff
    finally:
        await runtime.aclose()


# -- review fixes: stub worker, no PTY ---------------------------------------


class StubWorker:
    def __init__(self, *, result=None, error=None, hang=False):
        self.result, self.error, self.hang, self.calls = result, error, hang, []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.hang:
            await asyncio.sleep(3600)
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self):
        pass


def stub_child(tmp_path, worker, *, max_turn_seconds=0, **overrides):
    from nexus.agents import AgentManager

    workspace = tmp_path / "ws"
    workspace.mkdir()
    section = ProviderSection(kind="claude-code", worker_allowed_tools=["Edit"])
    from nexus.config.schema import AgentSection
    config = Config(model="standard", v2=ConfigV2(providers={"claude-code": section}, agent=AgentSection(max_turn_seconds=max_turn_seconds)))
    runtime = Runtime(workspace, config=config, home=tmp_path / "home", environ={"PATH": os.environ["PATH"]},
                      agents=AgentManager.for_workspace(workspace, seed=False))
    relayed = []

    async def emit(kind, data=None):
        relayed.append((kind, dict(data or {})))

    spec = replace(child_spec(workspace, runtime, config, emit=emit), **overrides)
    child = _ChildRuntime(runtime, spec, object())
    if worker.result is None and worker.error is None and not worker.hang:
        worker.result = cw.WorkerResult(status="completed", text="ok", is_error=False, claude_session="s1",
                                        policy=child._external.policy)
    child._external = replace(child._external, worker=worker)
    return runtime, spec, child, relayed, workspace


async def test_role_instructions_reach_claude_and_the_context_is_recorded(tmp_path):
    worker = StubWorker()
    runtime, spec, child, _, _ = stub_child(tmp_path, worker, system_prompt="Be terse.")
    try:
        outcome = await child.run()
        assert outcome.status == "completed"
        sent = worker.calls[0]["prompt"]
        assert "Be terse." in sent and sent.endswith("Task:\nfix the bug")
        snapshot = runtime.agent_request("root/sub/1")
        assert snapshot["provider"] == "claude-code" and snapshot["prompt"] == "fix the bug"
        assert "Be terse." in snapshot["system"] and "did not mediate them" in snapshot["system"]
    finally:
        await child.aclose()
        await runtime.aclose()


async def test_no_role_instructions_leave_the_prompt_unchanged_and_say_so(tmp_path):
    worker = StubWorker()
    runtime, _, child, _, _ = stub_child(tmp_path, worker)
    try:
        await child.run()
        assert worker.calls[0]["prompt"] == "fix the bug"
        assert "No role instructions were sent" in runtime.agent_request("root/sub/1")["system"]
    finally:
        await child.aclose()
        await runtime.aclose()


async def test_external_child_gets_no_wall_clock_even_when_parent_has_one(tmp_path):
    worker = StubWorker()
    runtime, spec, child, relayed, _ = stub_child(tmp_path, worker, max_turn_seconds=5)
    try:
        await child.run()
        assert "max_seconds" not in worker.calls[0]
        from nexus.runtime import _child_session_id

        session = runtime._ensure_child_sessions().manager.open(_child_session_id(spec.session_id), recover=False)
        started = next(e.data for e in session.read().events() if e.type == "turn.started")
        assert started["limits"]["max_seconds"] == 0
    finally:
        await child.aclose()
        await runtime.aclose()


async def test_unexpected_failure_is_redacted_and_still_discloses_authority(tmp_path):
    secret = "sk-ant-abcdefghijklmnop"
    worker = StubWorker(error=RuntimeError(f"boom {secret}"))
    runtime, _, child, relayed, _ = stub_child(tmp_path, worker)
    try:
        outcome = await child.run()
        assert outcome.status == "failed" and secret not in outcome.text and secret not in (outcome.error or "")
        assert secret not in outcome.handoff and not any(secret in repr(data) for _, data in relayed)
        assert outcome.usage_unknown and "did not mediate" in outcome.metrics["authority"]
    finally:
        await child.aclose()
        await runtime.aclose()


async def test_cancellation_keeps_authority_unknown_usage_and_file_changes(tmp_path):
    from nexus.agents.runner import _external_partial

    worker = StubWorker(hang=True)
    runtime, spec, child, _, workspace = stub_child(tmp_path, worker)
    try:
        task = asyncio.ensure_future(child.run())
        while not worker.calls:
            await asyncio.sleep(0.01)
        (workspace / "new.txt").write_text("written before cancel")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        partial = _external_partial(spec)
        assert partial["files_changed"] == ("new.txt",)
        assert partial["metrics"]["usage"] == "unknown" and "did not mediate" in partial["metrics"]["authority"]
        cancelled = SubagentOutcome(agent="claude", session_id="root/sub/1", status="cancelled", is_error=True,
                                    **partial)
        rendered = cancelled.render()
        assert "[usage: unknown" in rendered and "[authority:" in rendered and "- new.txt" in rendered
    finally:
        await child.aclose()
        await runtime.aclose()


async def test_child_escalations_do_not_leak_after_close(tmp_path):
    runtime, _, child, _, _ = stub_child(tmp_path, StubWorker())
    try:
        runtime._child_escalations["root/sub/1"] = ["needs a human"]
        runtime._child_escalations["root/sub/other"] = ["not mine"]
        await child.aclose()
        assert list(runtime._child_escalations) == ["root/sub/other"]
    finally:
        await runtime.aclose()


def test_worktree_children_are_refused(tmp_path):
    base = dict(config=Config(), workspace=tmp_path, model="claude-code/claude-haiku-4-5", tools=("read",), metadata={})
    for extra in ({"worktree_scope": True}, {"worktree": {"path": str(tmp_path)}}):
        with pytest.raises(ConfigError, match="worktree"):
            _ChildRuntime(fake_runtime(), SimpleNamespace(**base, **extra), object())
    assert _ChildRuntime(fake_runtime(), SimpleNamespace(**base, worktree_scope=False), object())._external


def test_inheriting_the_parents_claude_code_model_is_refused_unless_named(tmp_path):
    parent = "claude-code/claude-sonnet-4-6"
    base = dict(agent="general", config=Config(), workspace=tmp_path, model=parent, parent_model=parent,
                tools=("read",), metadata={})
    with pytest.raises(ConfigError, match="inheriting"):
        _ChildRuntime(fake_runtime(), SimpleNamespace(**base, requested_model=None), object())
    with pytest.raises(ConfigError, match="inheriting"):
        _ChildRuntime(fake_runtime(), SimpleNamespace(**base, requested_model="inherit"), object())
    # Named on the Task call, or pinned by the role, it is explicit.
    assert _ChildRuntime(fake_runtime(), SimpleNamespace(**base, requested_model=parent), object())._external
    roles = SimpleNamespace(resolve=lambda name, context=None: SimpleNamespace(model="", provider="claude-code"))
    runtime = fake_runtime()
    runtime._agents = roles
    assert _ChildRuntime(runtime, SimpleNamespace(**base, requested_model=None), object())._external


def test_outcome_renders_attention_for_the_root_agent():
    from nexus.agents.runner import SubagentOutcome

    blocked = SubagentOutcome(agent="task", session_id="s", status="needs_attention", is_error=True,
                              error="workspace trust requires manual setup", text="stopped")
    text = blocked.render()
    assert "needs attention: workspace trust requires manual setup" in text
    assert "escalation point" in text and "ask the user" in text
    assert blocked.to_dict()["attention"] == "workspace trust requires manual setup"

    done = SubagentOutcome(agent="task", session_id="s", text="done",
                           metrics={"attention": "Claude's auto-mode classifier denied: Bash: Git Destructive"})
    assert done.ok and "Git Destructive" in done.render()
    assert SubagentOutcome(agent="task", session_id="s", text="ok").attention is None


def test_tier_entry_for_claude_code_routes_children_to_the_worker(tmp_path):
    """A tier whose first usable entry is claude-code runs on the worker; later entries are fallbacks."""
    import types

    from nexus.runtime import Runtime

    section = types.SimpleNamespace(kind="claude-code")
    config = types.SimpleNamespace(v2=types.SimpleNamespace(providers={"claude-code": section}))
    router = types.SimpleNamespace(
        tier_candidates=lambda tier: {"low": ("claude-code/claude-haiku-5-5", "codex/m"),
                                      "medium": ("codex/m", "claude-code/claude-sonnet-5-5")}[tier],
        _registry_lookup=lambda ref: types.SimpleNamespace(provider=ref.split("/")[0]),
        _providers={"codex": object()}, tier_runnable=lambda tier: True)
    runtime = types.SimpleNamespace(_config=config, _router=router)
    route = lambda tier: Runtime._claude_code_tier_route(runtime, tier)  # noqa: E731
    assert route("low") == "claude-code/claude-haiku-5-5"
    assert route("medium") is None  # a runnable Nexus provider comes first
