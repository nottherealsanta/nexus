"""Phase 6 P6I: the subagent and lifecycle-hook integration.

This is the end-to-end packet that wires the P6A/P6B/P6C components into the
runtime, the extension manifest, the loop, and configuration:

* ``agents/*.md`` and ``hooks.toml``/``hooks/*.py`` are discovered by the same
  serialized manifest rebuild as tools, so one generation carries the whole
  world and a reload advances them together;
* the runtime owns the ``AgentManager``/``HookManager``/``SubagentRunner`` and
  seeds the three roles once;
* the loop runs the nine lifecycle hooks using the pinned generation's specs;
* ``PreToolUse`` runs over the whole batch before dispatch, a block becomes an
  exact sanitized ``ToolResult``, and a modify is revalidated and re-gated;
* a ``Task`` child can never exceed its parent (tools, permissions, tier,
  depth, fan-out, budget), and its events replay with agent metadata.

Providers are scripts; agents and hooks are real hot-loaded files.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    AgentsSection,
    ConfigV2,
    ContextSection,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.model.message import Message, Text, ToolResult
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
    tool_response,
)
from nexus.model.registry import Cost
from nexus.model.stream import Usage
from nexus.runtime import Runtime
from nexus.view import fold

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    profile: str = "coding",
    mode: str = "allow",
    allow: list[str] | None = None,
    write_roots: list[str] | None = None,
    max_tier: str = "medium",
    max_depth: int = 3,
    max_concurrent: int = 4,
    max_fanout: int | None = None,
    token_budget: int | None = None,
    cost_budget: float | None = None,
    seed_roles: bool = True,
    max_tokens: int = 180000,
    compact_at_fraction: float = 0.85,
    compaction: str = "hybrid",
) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile=profile),
            permissions=PermissionsSection(
                mode=mode,
                allow=list(allow or []),
                on_unattended="allow",
                write_roots=list(write_roots) if write_roots else ["./"],
            ),
            tools=ToolsSection(),
            agents=AgentsSection(
                max_tier=max_tier,
                max_depth=max_depth,
                max_concurrent=max_concurrent,
                max_fanout=max_fanout,
                token_budget=token_budget,
                cost_budget=cost_budget,
                seed_roles=seed_roles,
            ),
            context=ContextSection(
                max_tokens=max_tokens,
                compact_at_fraction=compact_at_fraction,
                compaction=compaction,
            ),
        ),
    )


def make_runtime(tmp_path: Path, provider, config: Config | None = None) -> Runtime:
    return Runtime(
        tmp_path,
        config=config or make_config(),
        providers={"scripted": provider},
    )


async def drain(session):
    return [event async for event in session.send("go")]


async def wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.005)


def tool_results(session) -> list:
    results = []
    for message in session.messages:
        if message.role != "user":
            continue
        for block in message.content:
            if type(block).__name__ == "ToolResult":
                results.append(block)
    return results


def tool_result_for(session, call_id):
    for block in tool_results(session):
        if block.tool_use_id == call_id:
            return block
    return None


def write_hook_module(tmp_path: Path, name: str, body: str) -> None:
    directory = tmp_path / ".nexus" / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(body, encoding="utf-8")


def write_agent(tmp_path: Path, name: str, *, description: str, body: str = "body") -> None:
    directory = tmp_path / ".nexus" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def clean_git_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Nexus Worktree Test")
    git(path, "config", "user.email", "nexus-worktree@example.invalid")
    (path / ".gitignore").write_text(".nexus/\n", encoding="utf-8")
    (path / "tracked.txt").write_text("parent base\n", encoding="utf-8")
    git(path, "add", ".gitignore", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    return path


# ---------------------------------------------------------------------------
# Manifest discovery / hot reload
# ---------------------------------------------------------------------------


async def test_manifest_discovers_seeded_agents_and_hooks(tmp_path):
    write_hook_module(
        tmp_path,
        "observer",
        "from nexus.hooks.model import HookDecision\n"
        "def _warn(inv, ctx):\n"
        "    return HookDecision.warn('seen')\n"
        "HOOKS = [{'event': 'SessionStart', 'name': 'observer', 'run': _warn}]\n",
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    await runtime.ensure_started()

    manifest = runtime.manifest
    assert {"general", "build", "explore", "plan"} <= set(manifest.agents)
    assert "SessionStart" in manifest.hooks
    assert [spec.name for spec in manifest.hooks["SessionStart"]] == ["observer"]
    # The roles are real, editable workspace files.
    agents_dir = tmp_path / ".nexus" / "agents"
    assert (agents_dir / "general.md").is_file()
    await runtime.aclose()


async def test_agent_and_hook_reload_advances_the_same_generation(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    await runtime.ensure_started()
    before = runtime.manifest.generation

    write_agent(tmp_path, "auditor", description="audits")
    write_hook_module(
        tmp_path,
        "gate",
        "from nexus.hooks.model import HookDecision\n"
        "def _allow(inv, ctx):\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'TurnEnd', 'name': 'gate', 'run': _allow}]\n",
    )
    report = await runtime.extensions.reload(trigger="test")

    assert report.changed
    assert runtime.manifest.generation > before
    assert "auditor" in runtime.manifest.agents
    assert "TurnEnd" in runtime.manifest.hooks
    await runtime.aclose()


async def test_unchanged_agents_and_hooks_do_not_churn_the_generation(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    await runtime.ensure_started()
    generation = runtime.manifest.generation
    first_agents = runtime.manifest.agents
    first_hooks = runtime.manifest.hooks

    report = await runtime.extensions.reload(trigger="test")

    assert report.changed is False
    assert runtime.manifest.generation == generation
    # Object reuse: an equal rebuild keeps the same definition objects.
    assert runtime.manifest.agents["general"] is first_agents["general"]
    assert runtime.manifest.hooks == first_hooks
    await runtime.aclose()


async def test_deleted_seeded_role_is_not_resurrected(tmp_path):
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    await runtime.ensure_started()
    (tmp_path / ".nexus" / "agents" / "general.md").unlink()

    await runtime.extensions.reload(trigger="test")

    # The marker prevents re-seeding; the deleted workspace file is never
    # recreated, and the role still resolves from a lower tier.
    assert not (tmp_path / ".nexus" / "agents" / "general.md").exists()
    assert (tmp_path / ".nexus" / "agents" / ".seeded").exists()
    assert "general" in runtime.manifest.agents
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Task: named and ad-hoc subagents, nested events
# ---------------------------------------------------------------------------


async def test_named_task_runs_a_child_and_replays_nested_events(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "survey", "subagent_type": "general"})),
        text_response("child findings"),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    assert events[-1].type == "turn.completed"
    result = tool_result_for(session, "t1")
    assert result is not None and result.is_error is False
    assert "child findings" in result.content[0].text

    # The child's own turn is replayed in the parent log with agent metadata.
    spawned = [e for e in events if e.type == "agent.spawned"]
    completed = [e for e in events if e.type == "agent.completed"]
    assert len(spawned) == 1 and len(completed) == 1
    meta = spawned[0].data["agent"]
    assert meta["parent"] == "root"
    assert meta["session"] == "root/sub/1"
    assert meta["parent_call_id"] == "t1"
    assert meta["parent_session"] == "root"
    assert meta["parent_agent_id"] is None
    assert meta["root_turn_id"]
    child_text = [
        e for e in events if e.type == "text" and isinstance(e.data.get("agent"), dict)
    ]
    assert child_text and child_text[0].data["agent"]["id"] == "root/sub/1"
    transcript = HostFacade(runtime).agent_transcript("root", meta["id"])
    assert transcript["found"] is True
    assert transcript["status"] == "completed"
    assert transcript["view"]["body"]["messages"][-1]["blocks"][-1]["text"] == "child findings"

    # A real, replayable child session was written under the agents directory.
    assert (tmp_path / ".nexus" / "sessions" / "agents").is_dir()
    await runtime.aclose()


async def test_normal_child_retains_parent_bash_authority(tmp_path):
    target = tmp_path / "normal-child-shell.txt"
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "run the requested shell"})),
        tool_response(
            ("shell", "bash", {"command": f"printf normal > {target}"}),
        ),
        text_response("child done"),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider)

    events = await asyncio.wait_for(drain(runtime.session("normal-child-shell")), timeout=20)

    spawn = next(event.data for event in events if event.type == "agent.spawned")
    assert "bash" in spawn["tools"]
    assert target.read_text(encoding="utf-8") == "normal"
    await runtime.aclose()


async def test_worktree_child_runtime_is_workspace_scoped_and_logs_in_parent(
    tmp_path,
):
    parent = clean_git_repo(tmp_path / "repository")
    provider = ScriptedProvider(
        tool_response(
            (
                "spawn",
                "subagent",
                {"prompt": "work in this isolated checkout", "worktree": True},
            )
        ),
        tool_response(
            (
                "nested",
                "subagent",
                {"prompt": "inspect without changing anything", "subagent_type": "explore"},
            )
        ),
        text_response("nested read-only findings"),
        tool_response(
            (
                "parent-bash-escape",
                "bash",
                {"command": f"echo escaped > {parent / 'parent-bash-write.txt'}"},
            ),
            (
                "write",
                "write",
                {"path": "child-output.txt", "content": "from child"},
            ),
            (
                "parent-escape",
                "write",
                {"path": str(parent / "parent-write.txt"), "content": "escape"},
            ),
        ),
        text_response("child finished"),
        text_response("parent finished"),
    )
    runtime = Runtime(
        parent,
        config=make_config(mode="allow", allow=["Task(*)"]),
        providers={"scripted": provider},
    )
    session = runtime.session("worktree-root").mark_attended(True)
    await session.start_turn("go")
    await wait_for(
        lambda: any(event.type == "permission.requested" for event in session.events)
    )
    permission = next(
        event.data for event in session.events if event.type == "permission.requested"
    )
    assert permission["key"].endswith(":worktree")
    assert permission["default_rule"] != "subagent"
    session.resolve_permission(permission["id"], "allow_once")
    await session.wait_idle()
    events = list(session.events)

    spawned = next(event.data for event in events if event.type == "agent.spawned")
    worktree = spawned["worktree"]
    checkout = Path(worktree["path"])
    assert not (parent / "parent-bash-write.txt").exists()
    assert any(
        event.type == "tool.failed"
        and event.data.get("call_id") == "parent-bash-escape"
        and event.data.get("executed") is False
        for event in events
    )
    agent_spawns = [event.data for event in events if event.type == "agent.spawned"]
    assert len(agent_spawns) == 2
    nested_spawn = next(data for data in agent_spawns if data["depth"] == 2)
    assert nested_spawn["worktree"] is None
    assert not (set(nested_spawn["tools"]) & {"write", "edit", "multiedit", "bash"})
    assert (checkout / "child-output.txt").read_text(encoding="utf-8") == "from child"
    assert not (parent / "parent-write.txt").exists()
    assert (parent / "tracked.txt").read_text(encoding="utf-8") == "parent base\n"
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == ""
    child_session_log = next(
        (parent / ".nexus" / "sessions" / "agents").glob("*.jsonl")
    ).read_text(encoding="utf-8")
    assert str(checkout) in child_session_log
    assert (parent / ".nexus" / "sessions" / "agents").is_dir()
    assert not (checkout / ".nexus" / "sessions" / "agents").exists()
    from nexus.agents.worktrees import WorktreeService

    daemon = parent.parent / f".nexus-worktrees-{parent.name}"
    record = WorktreeService().inspect("worktree-root/sub/1", root=daemon)
    assert record.path == checkout
    await runtime.aclose()


async def test_worktree_child_catalog_cannot_run_shell_or_custom_writer(tmp_path):
    parent = clean_git_repo(tmp_path / "repository")
    extension_dir = parent / ".nexus" / "tools"
    extension_dir.mkdir(parents=True)
    (extension_dir / "custom_writer.py").write_text(
        "from pathlib import Path\n"
        "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
        "SPEC = ToolSpec(name='CustomWriter', description='custom', "
        "input_schema={'type':'object','properties':{},'additionalProperties':False}, "
        "bundle='fs')\n"
        "async def run(args, ctx):\n"
        f"    Path({str(parent / 'custom-parent-write.txt')!r}).write_text('x')\n"
        "    return ToolExecutionResult.text('ran')\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(
            ("spawn", "subagent", {"prompt": "try unreviewed tools", "worktree": True})
        ),
        tool_response(
            (
                "bash-escape",
                "bash",
                {"command": f"echo escaped > {parent / 'parent-shell-write.txt'}"},
            ),
            ("custom-escape", "CustomWriter", {}),
            ("nested-spawn", "subagent", {"prompt": "inherit safety ceiling"}),
        ),
        tool_response(
            (
                "grandchild-bash",
                "bash",
                {"command": f"echo escaped > {parent / 'grandchild-shell-write.txt'}"},
            ),
            ("grandchild-custom", "CustomWriter", {}),
        ),
        text_response("child done"),
        text_response("child done"),
        text_response("parent done"),
    )
    runtime = Runtime(
        parent,
        config=make_config(mode="allow", allow=["Task(*)"]),
        providers={"scripted": provider},
    )
    session = runtime.session("catalog-worktree").mark_attended(True)

    await session.start_turn("go")
    await wait_for(
        lambda: any(event.type == "permission.requested" for event in session.events)
    )
    permission = next(
        event.data for event in session.events if event.type == "permission.requested"
    )
    assert permission["key"].endswith(":worktree")
    session.resolve_permission(permission["id"], "allow_once")
    await session.wait_idle()
    events = list(session.events)

    spawns = [event.data for event in events if event.type == "agent.spawned"]
    spawn = next(data for data in spawns if data["depth"] == 1)
    advertised = set(spawn["tools"])
    assert "bash" not in advertised
    assert "bash_output" not in advertised
    assert "kill_shell" not in advertised
    assert "CustomWriter" not in advertised
    nested = next(data for data in spawns if data["depth"] == 2)
    assert nested["worktree"] is None
    assert "bash" not in nested["tools"]
    assert "CustomWriter" not in nested["tools"]
    assert not (parent / "parent-shell-write.txt").exists()
    assert not (parent / "grandchild-shell-write.txt").exists()
    assert not (parent / "custom-parent-write.txt").exists()
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == ""
    failed_calls = {
        event.data.get("call_id")
        for event in events
        if event.type == "tool.failed" and event.data.get("executed") is False
    }
    assert {
        "bash-escape", "custom-escape", "grandchild-bash", "grandchild-custom"
    } <= failed_calls
    await runtime.aclose()


async def test_worktree_child_clips_parent_relative_write_root(tmp_path):
    from nexus.agents.worktrees import WorktreeService

    parent = clean_git_repo(tmp_path / "repository")
    outside = parent.parent / "outside.txt"
    config = make_config(write_roots=["../"])
    runtime = Runtime(
        parent,
        config=config,
        providers={"scripted": ScriptedProvider(text_response("unused"))},
    )
    child = WorktreeService().create(
        parent,
        "guard-check",
        root=parent.parent / f".nexus-worktrees-{parent.name}",
    )
    from nexus.tools.builtin.write import run as run_write
    from nexus.tools.permissions import PathGuard
    from nexus.tools.spec import ToolContext

    child_guard = PathGuard(parent, write_roots=("../",)).for_worktree(
        child.path
    )
    child_config = runtime._child_workspace_config(config, child_guard)
    result = await run_write(
        {"path": str(outside), "content": "no"},
        ToolContext(
            workspace=child.path,
            session_id="guard-check",
            turn_id="guard-check",
            config=child_config,
        ),
    )
    assert result.is_error
    assert not outside.exists()
    await runtime.aclose()


async def test_worktree_child_cancellation_retains_checkout_changes(tmp_path):
    parent = clean_git_repo(tmp_path / "repository")
    provider = ScriptedProvider(
        tool_response(
            (
                "spawn",
                "subagent",
                {"prompt": "write then wait", "worktree": True},
            )
        ),
        tool_response(
            (
                "write-child",
                "write",
                {"path": "retained.txt", "content": "retain on cancel"},
            )
        ),
        [Wait()],
    )
    runtime = Runtime(parent, config=make_config(), providers={"scripted": provider})
    session = runtime.session("worktree-cancel").mark_attended(True)
    await session.start_turn("go")
    await wait_for(lambda: bool(session.pending_permissions))
    permission_id = session.pending_permissions[0]
    assert session.resolve_permission(permission_id, "allow_once") is True

    async def wait_for_retained_write():
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            spawned = next(
                (event.data for event in session.events if event.type == "agent.spawned"),
                None,
            )
            if spawned is not None:
                checkout = Path(spawned["worktree"]["path"])
                if (checkout / "retained.txt").exists():
                    return checkout
            await asyncio.sleep(0.01)
        raise TimeoutError("child did not write into its worktree")

    checkout = await asyncio.wait_for(wait_for_retained_write(), timeout=12)
    session.cancel("stop child")
    await asyncio.wait_for(session.wait_idle(), timeout=12)

    assert (checkout / "retained.txt").read_text(encoding="utf-8") == "retain on cancel"
    assert git(checkout, "status", "--porcelain", "--untracked-files=all")
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == ""
    await runtime.aclose()


@pytest.mark.parametrize("repository_kind", ["dirty", "non_git"])
async def test_runtime_worktree_spawn_refuses_dirty_or_non_git_parent(
    tmp_path, repository_kind
):
    if repository_kind == "dirty":
        parent = clean_git_repo(tmp_path / "repository")
        (parent / "tracked.txt").write_text("preserve dirty data\n", encoding="utf-8")
        before = git(parent, "status", "--porcelain", "--untracked-files=all")
    else:
        parent = tmp_path / "not-a-repository"
        parent.mkdir()

    provider = ScriptedProvider(
        tool_response(
            (
                "spawn",
                "subagent",
                {"prompt": "must be refused", "worktree": True},
            )
        ),
        text_response("parent finished"),
    )
    runtime = Runtime(parent, config=make_config(), providers={"scripted": provider})
    events = await asyncio.wait_for(drain(runtime.session("refusal")), timeout=15)

    spawned = [event for event in events if event.type == "agent.spawned"]
    assert spawned == []
    if repository_kind == "dirty":
        assert git(parent, "status", "--porcelain", "--untracked-files=all") == before
        assert (parent / "tracked.txt").read_text(encoding="utf-8") == "preserve dirty data\n"
    else:
        assert not tuple(parent.glob(".nexus-worktrees-*"))
    await runtime.aclose()


async def test_parallel_tasks_both_run(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            ("t1", "Task", {"prompt": "a", "subagent_type": "general"}),
            ("t2", "Task", {"prompt": "b", "subagent_type": "general"}),
        ),
        text_response("report-a"),
        text_response("report-b"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    assert events[-1].type == "turn.completed"
    first = tool_result_for(session, "t1")
    second = tool_result_for(session, "t2")
    assert first is not None and second is not None
    assert {first.content[0].text, second.content[0].text} == {
        "report-a",
        "report-b",
    }
    spawned = [e for e in events if e.type == "agent.spawned"]
    assert len(spawned) == 2
    assert {e.data["agent"]["session"] for e in spawned} == {
        "root/sub/1",
        "root/sub/2",
    }
    assert {e.data["agent"]["parent_call_id"] for e in spawned} == {"t1", "t2"}
    assert {e.data["agent"]["id"]: e.data["agent"]["parent_call_id"] for e in spawned} == {
        "root/sub/1": "t1",
        "root/sub/2": "t2",
    }
    await runtime.aclose()


async def test_ad_hoc_tools_narrow_and_drops_are_reported(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            (
                "t1",
                "Task",
                {
                    "prompt": "look",
                    "tools": ["Read", "NoSuchTool"],
                    "subagent_type": "general",
                },
            )
        ),
        text_response("child"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    result = tool_result_for(session, "t1")
    assert result is not None
    assert "NoSuchTool" in result.content[0].text  # dropped tools are surfaced
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Tier clamp / depth / fan-out limits
# ---------------------------------------------------------------------------


async def test_requested_high_tier_is_clamped_and_reported(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            (
                "t1",
                "Task",
                {"prompt": "plan", "subagent_type": "planner", "model": "high"},
            )
        ),
        text_response("plan report"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider, config=make_config(max_tier="medium"))
    session = runtime.session("root")
    events = await drain(session)

    assert events[-1].type == "turn.completed"
    assert "agent.clamped" in [e.type for e in events]
    result = tool_result_for(session, "t1")
    assert result is not None and result.is_error is False
    await runtime.aclose()


async def test_depth_limit_refuses_a_grandchild(tmp_path):
    # Parent -> general child -> Task again; max_depth=1 refuses the grandchild.
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "outer", "subagent_type": "general"})),
        tool_response(("t2", "Task", {"prompt": "inner", "subagent_type": "general"})),
        text_response("outer report"),
        text_response("done"),
    )
    runtime = make_runtime(
        tmp_path, provider, config=make_config(max_depth=1)
    )
    session = runtime.session("root")
    events = await drain(session)

    assert events[-1].type == "turn.completed"
    # The grandchild's Task call is refused inside the child turn; its
    # tool.completed is relayed into the parent log with is_error set.
    grandchild = [
        e
        for e in events
        if e.type == "tool.completed" and e.data.get("call_id") == "t2"
    ]
    assert grandchild and grandchild[0].data.get("is_error") is True
    await runtime.aclose()


async def test_nested_agents_keep_immediate_parent_and_root_turn(tmp_path):
    provider = ScriptedProvider(
        tool_response(("outer-call", "Task", {"prompt": "outer", "subagent_type": "general"})),
        tool_response(("inner-call", "Task", {"prompt": "inner", "subagent_type": "general"})),
        text_response("inner report"),
        text_response("outer report"),
        text_response("root report"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    live = await drain(session)
    state = fold(list(session.events))
    assert state.to_dict() == fold(live).to_dict()
    top = state.agents["root/sub/1"]
    assert set(top.body.agents) == {"root/sub/1/sub/1"}
    nested = top.body.agents["root/sub/1/sub/1"]
    assert nested.parent_agent_id == top.id
    assert nested.parent_call_id == "inner-call"
    assert nested.root_turn_id == top.root_turn_id
    transcript = HostFacade(runtime).agent_transcript("root", nested.id)
    assert transcript["found"] is True
    assert any(
        "inner report" in "".join(block["text"] for block in message["blocks"] if block["kind"] == "text")
        for message in transcript["view"]["body"]["messages"]
    )
    await runtime.aclose()


async def test_parallel_agents_complete_out_of_order_and_stay_correlated(tmp_path):
    slow = asyncio.Event()
    fast = asyncio.Event()
    provider = ScriptedProvider(
        tool_response(
            ("slow-call", "Task", {"prompt": "slow", "subagent_type": "general"}),
            ("fast-call", "Task", {"prompt": "fast", "subagent_type": "general"}),
        ),
        [Wait(event=slow), *text_response("slow report")],
        [Wait(event=fast), *text_response("fast report")],
        text_response("root done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    running = asyncio.create_task(session.send("go").__anext__())
    await wait_for(lambda: len([event for event in session.events if event.type == "agent.spawned"]) == 2)
    # Release the second Task's provider request first; provider scripts are
    # consumed in order, so both gates are released before asserting order.
    fast.set()
    await asyncio.sleep(0.02)
    slow.set()
    await running
    await wait_for(lambda: any(event.type == "turn.completed" for event in session.events))
    events = list(session.events)
    spawned = [event for event in events if event.type == "agent.spawned"]
    completed = [event for event in events if event.type == "agent.completed"]
    assert {e.data["agent"]["parent_call_id"] for e in spawned} == {"slow-call", "fast-call"}
    assert len(completed) == 2
    by_parent_call = {event.data["agent"]["parent_call_id"]: event for event in completed}
    assert events.index(by_parent_call["fast-call"]) < events.index(by_parent_call["slow-call"])
    await runtime.aclose()


async def test_child_tool_arguments_results_and_errors_replay_and_host_view(tmp_path):
    provider = ScriptedProvider(
        tool_response(("task-call", "Task", {"prompt": "inspect", "subagent_type": "general"})),
        tool_response(("read-call", "Read", {"path": "missing.txt"})),
        text_response("child report"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    live_events = await drain(session)
    replayed = list(session.events)
    live_view = fold(live_events)
    replay_view = fold(replayed)
    assert live_view.to_dict() == replay_view.to_dict()
    agent = next(iter(replay_view.agents.values()))
    child_tool = next(tool for tool in agent.body.tools if tool.call_id == "read-call")
    assert child_tool.input == {"path": "missing.txt"}
    assert child_tool.status == "completed"
    assert child_tool.result and "missing.txt" in child_tool.result[0]["text"]
    assert child_tool.is_error
    result_event = next(
        event for event in live_events
        if event.type == "tool.result" and event.data.get("tool_use_id") == "read-call"
    )
    assert result_event.data["is_error"] is True
    from nexus.host.protocol import AgentTranscript

    result = await HostFacade(runtime).handle(AgentTranscript(session="root", agent_id=agent.id))
    assert result.found and result.status == "completed"
    missing = await HostFacade(runtime).handle(AgentTranscript(session="root", agent_id="unknown"))
    assert not missing.found and missing.status == "not_found"
    await runtime.aclose()


async def test_agent_transcript_obeys_fork_boundary(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "inspect", "subagent_type": "general"})),
        text_response("child report"),
        text_response("root report"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)
    spawn = next(event for event in session.events if event.type == "agent.spawned")
    completed = next(event for event in session.events if event.type == "agent.completed")
    fork = runtime.sessions.fork("root", spawn.seq, new_id="fork-at-spawn")
    partial = HostFacade(runtime).agent_transcript(fork.id, spawn.data["agent"]["id"])
    assert partial["found"] is True
    assert partial["status"] == "spawned"
    assert partial["view"]["body"]["messages"] == []
    assert completed.seq > spawn.seq
    await runtime.aclose()


async def test_child_transcript_caps_results_and_redacts_argument_secrets(tmp_path):
    oversized = "x" * (25_000 * 4)
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "inspect", "subagent_type": "general"})),
        tool_response(("bash-call", "Bash", {"command": "echo ok", "api_key": "sk-live-secret-value"})),
        text_response(oversized),
        text_response("root"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)
    agent = next(iter(fold(events).agents.values()))
    tool = next(tool for tool in agent.body.tools if tool.call_id == "bash-call")
    assert tool.input["api_key"] == "***"
    assert tool.result
    assert sum(len(item.get("text", "")) for item in tool.result) <= 25_000 * 4 + 200
    assert "sk-live-secret-value" not in str(events)
    await runtime.aclose()


def test_child_transcript_event_result_payload_is_bounded():
    from nexus.core.loop import _tool_result_event_view

    result = _tool_result_event_view(
        ToolResult(
            tool_use_id="read-call",
            content=[Text(text="z" * 500_000)],
        )
    )
    assert sum(len(item.get("text", "")) for item in result["content"]) <= 100_000 + 32


async def test_running_child_transcript_is_partial(tmp_path):
    gate = asyncio.Event()
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "wait", "subagent_type": "general"})),
        [Wait(event=gate), *text_response("finished")],
        text_response("parent"),
    )
    runtime = make_runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    session = runtime.session("root")
    task = asyncio.create_task(session.send("go").__anext__())
    await wait_for(lambda: any(e.type == "agent.spawned" for e in session.events))
    view, _ = facade.state("root")
    agent = next(iter(view.agents.values()))
    assert agent.status == "spawned"
    assert agent.body.messages or agent.body.turns
    gate.set()
    await task
    await facade.wait_idle(timeout=5)
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Security: a child can never exceed its parent
# ---------------------------------------------------------------------------


async def test_research_parent_cannot_write_through_task(tmp_path):
    provider = ScriptedProvider(
        tool_response(
            (
                "t1",
                "Task",
                {
                    "prompt": "write a file",
                    "subagent_type": "general",
                    "tools": ["Write", "Edit", "Read"],
                },
            )
        ),
        text_response("nothing written"),
        text_response("done"),
    )
    runtime = make_runtime(
        tmp_path, provider, config=make_config(profile="research")
    )
    session = runtime.session("root")
    await drain(session)

    result = tool_result_for(session, "t1")
    assert result is not None and result.is_error is False
    assert "write" in result.content[0].text
    assert not (tmp_path / "written.txt").exists()
    await runtime.aclose()


async def test_explore_and_planner_have_no_shell_or_write_path(tmp_path):
    for role in ("explore", "planner"):
        provider = ScriptedProvider(
            tool_response(("t1", "Task", {"prompt": "x", "subagent_type": role})),
            text_response("read-only report"),
            text_response("done"),
        )
        runtime = make_runtime(tmp_path, provider)
        session = runtime.session("root")
        await drain(session)
        spawned = [
            e for e in session.events if e.type == "agent.spawned"
        ]
        assert spawned, role
        tools = set(spawned[-1].data["tools"])
        assert not (tools & {"write", "edit", "multiedit", "bash"}), role
        assert tools <= {"read", "glob", "grep", "todowrite", "skill", "subagent"}, role
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_parent_cancel_cancels_the_child(tmp_path):
    started = asyncio.Event()

    async def slow_child(request):
        started.set()
        await asyncio.sleep(30)
        return text_response("late")

    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "slow", "subagent_type": "general"})),
        [slow_child],
        text_response("never"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await session.start_turn("go")
    await asyncio.wait_for(started.wait(), timeout=5)

    session.cancel("user stop")
    await asyncio.wait_for(session.wait_idle(), timeout=5)

    assert session.events[-1].type == "turn.cancelled"
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Lifecycle hooks: block and modify
# ---------------------------------------------------------------------------


async def test_pretooluse_hook_blocks_a_write(tmp_path):
    write_hook_module(
        tmp_path,
        "blocker",
        "from nexus.hooks.model import HookDecision\n"
        "def _block(inv, ctx):\n"
        "    return HookDecision.block('no writes allowed')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'blocker', "
        "'matcher': 'Write', 'run': _block}]\n",
    )
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "x.txt", "content": "hello"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    assert events[-1].type == "turn.completed"
    assert "hook.blocked" in [e.type for e in events]
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    assert "blocked by PreToolUse hook" in result.content[0].text
    assert "no writes allowed" in result.content[0].text
    assert not (tmp_path / "x.txt").exists()
    await runtime.aclose()


async def test_hook_modify_is_revalidated_and_regated(tmp_path):
    (tmp_path / "sub").mkdir()
    write_hook_module(
        tmp_path,
        "redirect",
        "from nexus.hooks.model import HookDecision\n"
        "def _redirect(inv, ctx):\n"
        "    payload = dict(inv.tool_input)\n"
        "    payload['path'] = 'outside.txt'\n"
        "    return HookDecision.modify(payload, reason='redirect')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'redirect', "
        "'matcher': 'Write', 'run': _redirect}]\n",
    )
    provider = ScriptedProvider(
        tool_response(
            ("c1", "Write", {"path": "sub/ok.txt", "content": "hello"})
        ),
        text_response("ok"),
    )
    runtime = make_runtime(
        tmp_path, provider, config=make_config(write_roots=["sub"])
    )
    session = runtime.session("root")
    await drain(session)

    # The modified key is re-evaluated by the permission gate and denied.
    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    assert "denied" in result.content[0].text
    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path / "sub" / "ok.txt").exists()
    await runtime.aclose()


async def test_hook_modify_is_reapproved_on_the_modified_key(tmp_path):
    write_hook_module(
        tmp_path,
        "redirect",
        "from nexus.hooks.model import HookDecision\n"
        "def _redirect(inv, ctx):\n"
        "    payload = dict(inv.tool_input)\n"
        "    payload['path'] = 'sub/mod.txt'\n"
        "    return HookDecision.modify(payload, reason='redirect')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'redirect', "
        "'matcher': 'Write', 'run': _redirect}]\n",
    )
    (tmp_path / "sub").mkdir()
    provider = ScriptedProvider(
        tool_response(
            ("c1", "Write", {"path": "sub/original.txt", "content": "hello"})
        ),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider, config=make_config(mode="ask"))
    session = runtime.session("root").mark_attended(True)
    await session.start_turn("go")

    async def _pending() -> str:
        while not session.pending_permissions:
            await asyncio.sleep(0.001)
        return session.pending_permissions[0]

    request_id = await asyncio.wait_for(_pending(), timeout=5)
    request = next(
        e
        for e in session.events
        if e.type == "permission.requested" and e.data.get("id") == request_id
    )
    # The approval is for the *modified* path, and the first responder wins.
    assert "sub/mod.txt" in str(request.data)
    assert session.resolve_permission(request_id, "allow_once") is True
    assert session.resolve_permission(request_id, "deny_once") is False
    await asyncio.wait_for(session.wait_idle(), timeout=5)

    assert session.events[-1].type == "turn.completed"
    assert (tmp_path / "sub" / "mod.txt").read_text(encoding="utf-8") == "hello"
    assert not (tmp_path / "sub" / "original.txt").exists()
    await runtime.aclose()


async def test_hook_modify_that_breaks_the_schema_is_rejected(tmp_path):
    write_hook_module(
        tmp_path,
        "corrupt",
        "from nexus.hooks.model import HookDecision\n"
        "def _corrupt(inv, ctx):\n"
        "    payload = dict(inv.tool_input)\n"
        "    payload.pop('content', None)\n"
        "    return HookDecision.modify(payload, reason='drop content')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'corrupt', "
        "'matcher': 'Write', 'run': _corrupt}]\n",
    )
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "x.txt", "content": "hello"})),
        text_response("ok"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    assert not (tmp_path / "x.txt").exists()
    await runtime.aclose()


async def test_hook_events_are_persisted_and_replayable(tmp_path):
    write_hook_module(
        tmp_path,
        "observer",
        "from nexus.hooks.model import HookDecision\n"
        "def _allow(inv, ctx):\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'SessionStart', 'name': 'observer', 'run': _allow}]\n",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    fired = [e for e in events if e.type == "hook.fired"]
    assert fired and fired[0].data["event"] == "SessionStart"
    # Replay from the store yields the same hook event.
    replayed = [
        event
        async for event in runtime.sessions.replay("root")
        if event.type == "hook.fired"
    ]
    assert replayed
    await runtime.aclose()


async def test_pretooluse_batch_completes_before_any_dispatch(tmp_path):
    write_hook_module(
        tmp_path,
        "watch",
        "from nexus.hooks.model import HookDecision\n"
        "def _watch(inv, ctx):\n"
        "    open(ctx.workspace / 'hook-ran.txt', 'a').write(inv.tool + '\\n')\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'watch', 'run': _watch}]\n",
    )
    provider = ScriptedProvider(
        tool_response(
            ("c1", "Read", {"path": "a.txt"}),
            ("c2", "Read", {"path": "b.txt"}),
        ),
        text_response("done"),
    )
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    hook_log = (tmp_path / "hook-ran.txt").read_text(encoding="utf-8").split()
    assert hook_log == ["Read", "Read"]
    assert [r.is_error for r in tool_results(session)] == [False, False]
    await runtime.aclose()


async def test_extension_loaded_hook_fires_on_reload(tmp_path):
    write_hook_module(
        tmp_path,
        "onload",
        "from nexus.hooks.model import HookDecision\n"
        "def _allow(inv, ctx):\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'ExtensionLoaded', 'name': 'onload', 'run': _allow}]\n",
    )
    recorded: list = []
    runtime = Runtime(
        tmp_path,
        config=make_config(),
        providers={"scripted": ScriptedProvider(text_response("ok"))},
        extension_sink=lambda event: recorded.append(event),
    )
    await runtime.ensure_started()

    fired = [
        event
        for event in recorded
        if getattr(event, "type", None) == "hook.fired"
        and event.data.get("event") == "ExtensionLoaded"
    ]
    assert fired and fired[0].data["hook"] == "onload"
    await runtime.aclose()


async def test_command_hook_block_and_nonzero_policy(tmp_path):
    (tmp_path / ".nexus").mkdir()
    (tmp_path / ".nexus" / "hooks.toml").write_text(
        "[[hooks.PreToolUse]]\n"
        "name = 'block'\n"
        "matcher = 'Bash'\n"
        "command = ['/bin/sh', '-c', 'echo blocked-by-command; exit 3']\n"
        "on_nonzero = 'block'\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("c1", "Bash", {"command": "echo hi"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    result = tool_result_for(session, "c1")
    assert result is not None and result.is_error is True
    await runtime.aclose()


# ---------------------------------------------------------------------------
# UserPromptSubmit: before the prompt is durable
# ---------------------------------------------------------------------------


def _user_prompt_hook(body: str) -> str:
    return (
        "from nexus.hooks.model import HookDecision\n"
        f"def _run(inv, ctx):\n{body}\n"
        "HOOKS = [{'event': 'UserPromptSubmit', 'name': 'ups', 'run': _run}]\n"
    )


async def test_user_prompt_submit_block_leaves_no_orphan_for_send(tmp_path):
    write_hook_module(
        tmp_path,
        "blocker",
        _user_prompt_hook("    return HookDecision.block('no prompt for you')"),
    )
    provider = ScriptedProvider(text_response("never"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    types = [event.type for event in events]
    assert "hook.blocked" in types
    assert types[-1] == "turn.failed"
    assert "UserPromptSubmit blocked" in events[-1].data["error"]
    # No user message was persisted and the model was never called.
    assert [m for m in session.messages if m.role == "user"] == []
    assert provider.requests == []
    await runtime.aclose()


async def test_user_prompt_submit_block_leaves_no_orphan_for_start_turn(tmp_path):
    write_hook_module(
        tmp_path,
        "blocker",
        _user_prompt_hook("    return HookDecision.block('blocked')"),
    )
    provider = ScriptedProvider(text_response("never"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await session.start_turn("go")
    await session.wait_idle()

    assert session.events[-1].type == "turn.failed"
    assert [m for m in session.messages if m.role == "user"] == []
    assert provider.requests == []
    await runtime.aclose()


async def test_user_prompt_submit_block_drops_a_queued_prompt(tmp_path):
    write_hook_module(
        tmp_path,
        "blocker",
        _user_prompt_hook("    return HookDecision.block('blocked')"),
    )
    provider = ScriptedProvider(text_response("never"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    session.enqueue("queued")

    # Consuming the queue head runs the gate before anything is durable; a block
    # drops the submission and fails without persisting a prompt.
    await session.start_turn()
    await session.wait_idle()

    dropped = [e for e in session.events if e.type == "input.dropped"]
    assert dropped and "blocked" in dropped[-1].data["reason"]
    assert session.queue_depth == 0
    assert [m for m in session.messages if m.role == "user"] == []
    assert provider.requests == []
    await runtime.aclose()


async def test_user_prompt_submit_block_drops_an_auto_started_queued_prompt(
    tmp_path,
):
    write_hook_module(
        tmp_path,
        "conditional",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    blocks = [b.get('text', '') for b in inv.tool_input.get('content', [])"
        " if isinstance(b, dict)]\n"
        "    if 'BLOCKME' in ' '.join(blocks):\n"
        "        return HookDecision.block('blocked queued')\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'UserPromptSubmit', 'name': 'conditional', 'run': _run}]\n",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await session.start_turn("first")
    session.enqueue("BLOCKME")
    await session.wait_idle()
    await asyncio.sleep(0.1)

    dropped = [e for e in session.events if e.type == "input.dropped"]
    assert dropped and "blocked queued" in dropped[-1].data["reason"]
    assert [e for e in session.events if e.type == "turn.failed"]
    # Only the first (allowed) prompt reached the model.
    assert len(provider.requests) == 1
    await runtime.aclose()


async def test_user_prompt_submit_modify_persists_and_assembles(tmp_path):
    write_hook_module(
        tmp_path,
        "rewriter",
        _user_prompt_hook(
            "    payload = dict(inv.tool_input)\n"
            "    payload['text'] = 'REWRITTEN PROMPT'\n"
            "    return HookDecision.modify(payload, reason='rewrite')"
        ),
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    user_messages = [m for m in session.messages if m.role == "user"]
    assert user_messages
    assert user_messages[0].content[0].text == "REWRITTEN PROMPT"
    assert "REWRITTEN PROMPT" in provider.requests[0].messages[0].content[0].text
    # The un-modified prompt was never persisted.
    assert all(
        "go" != getattr(block, "text", None)
        for message in user_messages
        for block in message.content
    )
    await runtime.aclose()


async def test_user_prompt_submit_modify_accepts_a_block_list(tmp_path):
    write_hook_module(
        tmp_path,
        "rewriter",
        _user_prompt_hook(
            "    return HookDecision.modify("
            "{'content': [{'type': 'text', 'text': 'BLOCK LIST'}]}, reason='blocks')"
        ),
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    user_messages = [m for m in session.messages if m.role == "user"]
    assert user_messages
    assert user_messages[0].content[0].text == "BLOCK LIST"
    await runtime.aclose()


async def test_user_prompt_submit_modify_rejects_a_bad_shape(tmp_path):
    write_hook_module(
        tmp_path,
        "rewriter",
        _user_prompt_hook(
            "    return HookDecision.modify({'nonsense': 1}, reason='bad')"
        ),
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    # The invalid modify is ignored; the original prompt is persisted.
    user_messages = [m for m in session.messages if m.role == "user"]
    assert user_messages[0].content[0].text == "go"
    await runtime.aclose()


async def test_user_prompt_submit_does_not_double_fire_in_the_loop(tmp_path):
    write_hook_module(
        tmp_path,
        "observer",
        _user_prompt_hook("    return HookDecision.allow()"),
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    fired = [
        event
        for event in session.events
        if event.type == "hook.fired" and event.data.get("event") == "UserPromptSubmit"
    ]
    assert len(fired) == 1
    await runtime.aclose()


# ---------------------------------------------------------------------------
# PreCompact
# ---------------------------------------------------------------------------


async def _long_session(session, *, pairs: int = 200, chars: int = 200) -> None:
    for index in range(pairs):
        session.append_message(
            Message(role="user", content=[Text(text=f"q{index} " + "x" * chars)])
        )
        session.append_message(
            Message(
                role="assistant", content=[Text(text=f"a{index} " + "y" * chars)]
            )
        )


def _compact_config(**kwargs) -> Config:
    return make_config(
        max_tokens=20000,
        compact_at_fraction=0.4,
        compaction="drop_oldest",
        **kwargs,
    )


async def test_precompact_block_prevents_compaction_and_fails(tmp_path):
    write_hook_module(
        tmp_path,
        "compactor",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    return HookDecision.block('no compaction today')\n"
        "HOOKS = [{'event': 'PreCompact', 'name': 'compactor', 'run': _run}]\n",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, config=_compact_config())
    session = runtime.session("root")
    await _long_session(session)

    events = await drain(session)

    assert events[-1].type == "turn.failed"
    assert "PreCompact blocked" in events[-1].data["error"]
    assert "no compaction today" in events[-1].data["error"]
    fired = [
        event
        for event in events
        if event.type == "hook.fired" and event.data.get("event") == "PreCompact"
    ]
    assert fired
    # No summarization artifact and the model was never called.
    assert session.summaries == []
    assert provider.requests == []
    await runtime.aclose()


async def test_precompact_modify_applies_typed_options(tmp_path):
    write_hook_module(
        tmp_path,
        "compactor",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    return HookDecision.modify({'keep': 6}, reason='keep more')\n"
        "HOOKS = [{'event': 'PreCompact', 'name': 'compactor', 'run': _run}]\n",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, config=_compact_config())
    session = runtime.session("root")
    await _long_session(session)

    events = await drain(session)

    assert events[-1].type == "turn.completed"
    context = next(
        e.data["context"] for e in events if e.type == "context.assembled"
    )
    # The typed modify was honored (keep=6) rather than the budget-derived suffix.
    assert context["history_retained"] == 6
    await runtime.aclose()


async def test_precompact_untyped_modify_is_disallowed(tmp_path):
    write_hook_module(
        tmp_path,
        "compactor",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    return HookDecision.modify({'nonsense': True}, reason='bad')\n"
        "HOOKS = [{'event': 'PreCompact', 'name': 'compactor', 'run': _run}]\n",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, config=_compact_config())
    session = runtime.session("root")
    await _long_session(session)

    events = await drain(session)

    assert events[-1].type == "turn.completed"
    context = next(
        e.data["context"] for e in events if e.type == "context.assembled"
    )
    # Disallowed modify: normal budget-driven compaction still happened.
    assert context["history_dropped"] > 0
    assert context["history_retained"] != 6
    await runtime.aclose()


async def test_precompact_typed_options_contract():
    from nexus.context.manager import CompactionOptions

    assert CompactionOptions.from_mapping({"keep": 3}) == CompactionOptions(keep=3)
    assert CompactionOptions.from_mapping({"strategy": "drop_oldest"}) is not None
    # Unknown keys, an unknown strategy, and bad values are all disallowed.
    assert CompactionOptions.from_mapping({"nonsense": 1}) is None
    assert CompactionOptions.from_mapping({"strategy": "explode"}) is None
    assert CompactionOptions.from_mapping({"keep": -1}) is None
    assert CompactionOptions.from_mapping("not-a-mapping") is None
    assert CompactionOptions.from_mapping(None) is None


async def test_precompact_command_hook_can_block(tmp_path):
    (tmp_path / ".nexus").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".nexus" / "hooks.toml").write_text(
        "[[hooks.PreCompact]]\n"
        "name = 'compactor'\n"
        "command = ['/bin/sh', '-c', 'echo nope; exit 3']\n"
        "on_nonzero = 'block'\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, config=_compact_config())
    session = runtime.session("root")
    await _long_session(session)
    events = await drain(session)
    assert events[-1].type == "turn.failed"
    assert "PreCompact blocked" in events[-1].data["error"]
    await runtime.aclose()


# ---------------------------------------------------------------------------
# SessionEnd
# ---------------------------------------------------------------------------


def _session_end_hook(body: str) -> str:
    return (
        "from nexus.hooks.model import HookDecision\n"
        f"def _run(inv, ctx):\n{body}\n"
        "HOOKS = [{'event': 'SessionEnd', 'name': 'bye', 'run': _run}]\n"
    )


async def test_session_end_fires_once_on_explicit_close(tmp_path):
    marker = tmp_path / "end.txt"
    write_hook_module(
        tmp_path,
        "bye",
        _session_end_hook(
            "    with open(ctx.workspace / 'end.txt', 'a') as f:\n"
            "        f.write((inv.session_id or '?') + '\\n')\n"
            "    return HookDecision.allow()"
        ),
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    session = runtime.session("root")
    await drain(session)

    await session.aclose()
    await session.aclose()  # idempotent

    fired = [
        e
        for e in session.events
        if e.type == "hook.fired" and e.data.get("event") == "SessionEnd"
    ]
    assert len(fired) == 1
    assert marker.read_text(encoding="utf-8").split() == ["root"]
    await runtime.aclose()


async def test_session_end_fires_on_manager_evict(tmp_path):
    marker = tmp_path / "end.txt"
    write_hook_module(
        tmp_path,
        "bye",
        _session_end_hook(
            "    with open(ctx.workspace / 'end.txt', 'a') as f:\n"
            "        f.write('x')\n"
            "    return HookDecision.allow()"
        ),
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    session = runtime.session("root")
    await drain(session)

    evicted = runtime.sessions.evict("root")
    assert evicted is session
    await asyncio.sleep(0.05)  # let the scheduled close run

    assert marker.exists()
    assert session._session_ended is True
    await runtime.aclose()


async def test_runtime_close_session_api_fires_session_end(tmp_path):
    marker = tmp_path / "end.txt"
    write_hook_module(
        tmp_path,
        "bye",
        _session_end_hook(
            "    with open(ctx.workspace / 'end.txt', 'a') as f:\n"
            "        f.write('closed')\n"
            "    return HookDecision.allow()"
        ),
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    session = runtime.session("root")
    await drain(session)

    assert await runtime.close_session("root") is True
    assert marker.read_text(encoding="utf-8") == "closed"
    assert session._session_ended is True
    await runtime.aclose()


async def test_session_end_fires_on_runtime_close_for_every_session(tmp_path):
    marker = tmp_path / "end.txt"
    write_hook_module(
        tmp_path,
        "bye",
        _session_end_hook(
            "    with open(ctx.workspace / 'end.txt', 'a') as f:\n"
            "        f.write((inv.session_id or '?') + '\\n')\n"
            "    return HookDecision.allow()"
        ),
    )
    runtime = make_runtime(
        tmp_path,
        ScriptedProvider(text_response("a"), text_response("b")),
    )
    first = runtime.session("alpha")
    second = runtime.session("beta")
    await drain(first)
    await drain(second)

    await runtime.aclose()

    assert sorted(marker.read_text(encoding="utf-8").split()) == ["alpha", "beta"]
    assert first._session_ended and second._session_ended


async def test_session_end_timeout_does_not_wedge_close(tmp_path):
    write_hook_module(
        tmp_path,
        "slow",
        "import asyncio\n"
        "from nexus.hooks.model import HookDecision\n"
        "async def _run(inv, ctx):\n"
        "    await asyncio.sleep(30)\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'SessionEnd', 'name': 'slow', 'run': _run}]\n",
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    session = runtime.session("root")
    await drain(session)

    # A hanging hook is cancelled at the close deadline; close still returns.
    await asyncio.wait_for(session.aclose(hook_timeout=0.05), timeout=2)
    assert session._session_ended is True
    await runtime.aclose()


async def test_no_session_start_on_reopen(tmp_path):
    write_hook_module(
        tmp_path,
        "observer",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'SessionStart', 'name': 'observer', 'run': _run}]\n",
    )
    runtime = make_runtime(
        tmp_path, ScriptedProvider(text_response("one"), text_response("two"))
    )
    first = runtime.session("root")
    await drain(first)
    await first.aclose()

    reopened = runtime.session("root")
    await drain(reopened)

    starts = [
        e
        for e in reopened.events
        if e.type == "hook.fired" and e.data.get("event") == "SessionStart"
    ]
    assert len(starts) == 1  # only the original open, never on reopen
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Ordering and terminal coverage
# ---------------------------------------------------------------------------


async def test_lifecycle_ordering_end_to_end(tmp_path):
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "ContextAssembled",
        "PreToolUse",
        "PostToolUse",
        "TurnEnd",
        "SessionEnd",
    ):
        write_hook_module(
            tmp_path,
            f"h_{event}",
            "from nexus.hooks.model import HookDecision\n"
            "def _run(inv, ctx):\n"
            "    return HookDecision.allow()\n"
            f"HOOKS = [{{'event': {event!r}, 'name': {event!r}, 'run': _run}}]\n",
        )
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a.txt"})),
        text_response("done"),
    )
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)
    await session.aclose()

    order = [
        event.data.get("event")
        for event in session.events
        if event.type == "hook.fired"
    ]
    first_seen: list = []
    for name in order:
        if name not in first_seen:
            first_seen.append(name)
    assert first_seen == [
        "SessionStart",
        "UserPromptSubmit",
        "ContextAssembled",
        "PreToolUse",
        "PostToolUse",
        "TurnEnd",
        "SessionEnd",
    ]
    await runtime.aclose()


async def test_post_tool_use_receives_the_actual_result(tmp_path):
    write_hook_module(
        tmp_path,
        "observer",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    result = inv.data.get('result', {})\n"
        "    text = '|'.join(result.get('content', []))\n"
        "    with open(ctx.workspace / 'post.txt', 'w') as f:\n"
        "        f.write(text)\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PostToolUse', 'name': 'observer', 'run': _run}]\n",
    )
    (tmp_path / "a.txt").write_text("HELLO-RESULT", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "a.txt"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    assert "HELLO-RESULT" in (tmp_path / "post.txt").read_text(encoding="utf-8")
    await runtime.aclose()


async def test_turn_end_fires_on_cancel_and_fail(tmp_path):
    marker = tmp_path / "phases.txt"
    write_hook_module(
        tmp_path,
        "observer",
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    with open(ctx.workspace / 'phases.txt', 'a') as f:\n"
        "        f.write(str(inv.data.get('phase')) + '\\n')\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'TurnEnd', 'name': 'observer', 'run': _run}]\n",
    )

    async def slow(request):
        await asyncio.sleep(30)
        return text_response("late")

    cancelling = make_runtime(
        tmp_path, ScriptedProvider([slow], text_response("never"))
    )
    session = cancelling.session("cancel")
    await session.start_turn("go")
    await asyncio.sleep(0.02)
    session.cancel("stop")
    await session.wait_idle()
    assert "cancelled" in marker.read_text(encoding="utf-8")
    await cancelling.aclose()

    failing = make_runtime(tmp_path, ScriptedProvider())
    failed_session = failing.session("fail")
    await drain(failed_session)
    assert failed_session.events[-1].type == "turn.failed"
    assert "failed" in marker.read_text(encoding="utf-8")
    await failing.aclose()


# ---------------------------------------------------------------------------
# Hook canonical key + bundle
# ---------------------------------------------------------------------------


async def test_pretooluse_and_posttooluse_receive_canonical_key_and_bundle(tmp_path):
    write_hook_module(
        tmp_path,
        "keys",
        "from nexus.hooks.model import HookDecision\n"
        "def _log(inv, ctx, phase):\n"
        "    with open(ctx.workspace / 'keys.txt', 'a') as f:\n"
        "        f.write(f\"{phase}|{inv.tool}|{inv.key}|{inv.bundle}\\n\")\n"
        "    return HookDecision.allow()\n"
        "def _pre(inv, ctx):\n"
        "    return _log(inv, ctx, 'pre')\n"
        "def _post(inv, ctx):\n"
        "    return _log(inv, ctx, 'post')\n"
        "HOOKS = [\n"
        "    {'event': 'PreToolUse', 'name': 'pre_py', "
        "'matcher': 'Write(**/*.py)', 'run': _pre},\n"
        "    {'event': 'PreToolUse', 'name': 'pre_exact', "
        "'matcher': 'Read', 'run': _pre},\n"
        "    {'event': 'PostToolUse', 'name': 'post_exact', "
        "'matcher': 'Write', 'run': _post},\n"
        "]\n",
    )
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "pkg/mod.py", "content": "x"})),
        tool_response(("c2", "Read", {"path": "pkg/mod.py"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    lines = [
        line.split("|")
        for line in (tmp_path / "keys.txt").read_text(encoding="utf-8").splitlines()
    ]
    canonical = str((tmp_path / "pkg" / "mod.py").resolve())
    # The canonical key is the absolute path for both hooks, and the bundle is fs.
    assert any(
        row == ["pre", "Write", canonical, "fs"] for row in lines
    ), lines
    assert any(row == ["post", "Write", canonical, "fs"] for row in lines), lines
    # The exact ``Read`` matcher fired too.
    assert any(row[1] == "Read" and row[3] == "fs" for row in lines), lines
    await runtime.aclose()


async def test_pretooluse_modify_recomputes_posttooluse_key(tmp_path):
    write_hook_module(
        tmp_path,
        "redirect",
        "from nexus.hooks.model import HookDecision\n"
        "def _pre(inv, ctx):\n"
        "    payload = dict(inv.tool_input)\n"
        "    payload['path'] = 'pkg/moved.py'\n"
        "    return HookDecision.modify(payload, reason='redirect')\n"
        "def _post(inv, ctx):\n"
        "    with open(ctx.workspace / 'post-key.txt', 'w') as f:\n"
        "        f.write(str(inv.key))\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [\n"
        "    {'event': 'PreToolUse', 'name': 'redirect', "
        "'matcher': 'Write', 'run': _pre},\n"
        "    {'event': 'PostToolUse', 'name': 'post', "
        "'matcher': 'Write', 'run': _post},\n"
        "]\n",
    )
    provider = ScriptedProvider(
        tool_response(("c1", "Write", {"path": "pkg/original.py", "content": "x"})),
        text_response("done"),
    )
    (tmp_path / "pkg").mkdir()
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    # PostToolUse sees the key of the *modified* (canonicalized) input.
    assert (tmp_path / "post-key.txt").read_text(encoding="utf-8") == str(
        (tmp_path / "pkg" / "moved.py").resolve()
    )
    assert (tmp_path / "pkg" / "moved.py").exists()
    assert not (tmp_path / "pkg" / "original.py").exists()
    await runtime.aclose()


async def test_pretooluse_bundle_matcher_fires_for_fs(tmp_path):
    write_hook_module(
        tmp_path,
        "bundle",
        "from nexus.hooks.model import HookDecision\n"
        "def _pre(inv, ctx):\n"
        "    with open(ctx.workspace / 'bundle.txt', 'a') as f:\n"
        "        f.write(inv.tool + '\\n')\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'bundle', "
        "'matcher': 'Bundle:fs', 'run': _pre}]\n",
    )
    reader = tmp_path / "b.txt"
    reader.write_text("hi", encoding="utf-8")
    provider = ScriptedProvider(
        tool_response(("c1", "Read", {"path": "b.txt"})),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    assert (tmp_path / "bundle.txt").read_text(encoding="utf-8").split() == ["Read"]
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Child hooks: a subagent cannot bypass a hook
# ---------------------------------------------------------------------------


async def test_child_pretooluse_hook_blocks_a_write(tmp_path):
    write_hook_module(
        tmp_path,
        "blocker",
        "from nexus.hooks.model import HookDecision\n"
        "def _pre(inv, ctx):\n"
        "    if inv.tool == 'Write':\n"
        "        return HookDecision.block('child cannot write')\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'blocker', "
        "'matcher': 'Write', 'run': _pre}]\n",
    )
    provider = ScriptedProvider(
        tool_response(
            (
                "t1",
                "Task",
                {"prompt": "write", "subagent_type": "general", "tools": ["Write"]},
            )
        ),
        tool_response(("c1", "Write", {"path": "child.txt", "content": "x"})),
        text_response("child done"),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    assert not (tmp_path / "child.txt").exists()
    assert "hook.blocked" in [e.type for e in events]
    await runtime.aclose()


async def test_ad_hoc_child_hook_modifies_its_input(tmp_path):
    (tmp_path / "a.txt").write_text("AAA", encoding="utf-8")
    (tmp_path / "b.txt").write_text("BBB", encoding="utf-8")
    write_hook_module(
        tmp_path,
        "redirect",
        "from nexus.hooks.model import HookDecision\n"
        "def _pre(inv, ctx):\n"
        "    if inv.tool != 'Read':\n"
        "        return HookDecision.allow()\n"
        "    payload = dict(inv.tool_input)\n"
        "    payload['path'] = 'b.txt'\n"
        "    return HookDecision.modify(payload, reason='read b')\n"
        "HOOKS = [{'event': 'PreToolUse', 'name': 'redirect', "
        "'matcher': 'Read', 'run': _pre}]\n",
    )
    provider = ScriptedProvider(
        tool_response(
            ("t1", "Task", {"prompt": "read a", "tools": ["Read"], "subagent_type": "general"})
        ),
        tool_response(("c1", "Read", {"path": "a.txt"})),
        text_response("child done"),
        text_response("parent done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    await drain(session)

    # The child's own log proves the hook rewrote the Read to b.txt.
    child_logs = list((tmp_path / ".nexus" / "sessions" / "agents").glob("*.jsonl"))
    assert child_logs
    child_text = "\n".join(
        path.read_text(encoding="utf-8") for path in child_logs
    )
    assert "BBB" in child_text
    assert "AAA" not in child_text
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Cost budget
# ---------------------------------------------------------------------------


class _PricingRegistry:
    """A minimal registry: legacy routing plus a fixed model price."""

    def __init__(self, *, input_rate=0.0, output_rate=0.0) -> None:
        self._input = input_rate
        self._output = output_rate

    def get(self, ref):
        return None  # keep the router on the legacy provider path

    def model_cost(self, provider, model):
        return Cost(input=self._input, output=self._output)


def _write_concrete_agent(tmp_path: Path, name: str, model: str) -> None:
    directory = tmp_path / ".nexus" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {name}\nmodel: {model}\n---\nbody\n",
        encoding="utf-8",
    )


async def test_child_cost_is_computed_from_registry_pricing(tmp_path):
    _write_concrete_agent(tmp_path, "coster", "scripted/m")
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "go", "subagent_type": "coster"})),
        text_response(
            "child",
            usage=Usage(input=1000, output=2000, cache_read=100, cache_write=50),
        ),
        text_response("parent"),
    )
    runtime = Runtime(
        tmp_path,
        config=make_config(),
        providers={"scripted": provider},
        registry=_PricingRegistry(input_rate=1.0, output_rate=2.0),
    )
    session = runtime.session("root")
    events = await drain(session)

    completed = [e for e in events if e.type == "agent.completed"]
    assert completed
    usage = completed[0].data["usage"]
    assert usage["input_tokens"] == 1000
    assert usage["cost_usd"] == pytest.approx(
        (1000 * 1.0 + 2000 * 2.0) / 1_000_000
    )
    await runtime.aclose()


async def test_cost_budget_exhausts_after_a_priced_child(tmp_path):
    _write_concrete_agent(tmp_path, "coster", "scripted/m")
    # Sequential tasks: the first child settles its priced cost before the second
    # is admitted, so the turn-scoped budget refuses it.
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "one", "subagent_type": "coster"})),
        text_response("child", usage=Usage(input=1000, output=2000)),
        tool_response(("t2", "Task", {"prompt": "two", "subagent_type": "coster"})),
        text_response("parent"),
    )
    runtime = Runtime(
        tmp_path,
        config=make_config(cost_budget=0.001),
        providers={"scripted": provider},
        registry=_PricingRegistry(input_rate=1.0, output_rate=2.0),
    )
    session = runtime.session("root")
    events = await drain(session)

    assert events[-1].type == "turn.completed"
    first = tool_result_for(session, "t1")
    second = tool_result_for(session, "t2")
    assert first is not None and first.is_error is False
    assert second is not None and second.is_error is True
    assert "budget" in second.content[0].text or "exhausted" in second.content[0].text
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Child lifecycle events and close concurrency
# ---------------------------------------------------------------------------


async def test_child_lifecycle_events_are_not_relayed_to_the_parent(tmp_path):
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "go", "subagent_type": "general"})),
        text_response("child"),
        text_response("parent"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    types = [e.type for e in events]
    assert types.count("turn.started") == 1
    assert types.count("turn.completed") == 1
    assert types.count("session.opened") == 0
    # The agent tree is still complete.
    assert "agent.spawned" in types and "agent.completed" in types
    # The child's own log keeps its lifecycle events.
    child_logs = list((tmp_path / ".nexus" / "sessions" / "agents").glob("*.jsonl"))
    assert child_logs
    assert "turn.started" in child_logs[0].read_text(encoding="utf-8")
    await runtime.aclose()


async def test_concurrent_close_runs_session_end_once(tmp_path):
    marker = tmp_path / "end.txt"
    write_hook_module(
        tmp_path,
        "bye",
        _session_end_hook(
            "    with open(ctx.workspace / 'end.txt', 'a') as f:\n"
            "        f.write('x')\n"
            "    return HookDecision.allow()"
        ),
    )
    runtime = make_runtime(tmp_path, ScriptedProvider(text_response("ok")))
    session = runtime.session("root")
    await drain(session)

    await asyncio.gather(
        session.aclose(),
        session.aclose(),
        runtime.aclose(),
        runtime.aclose(),
    )
    assert marker.read_text(encoding="utf-8") == "x"
    assert session._bus.closed is True
    await runtime.aclose()


# ---------------------------------------------------------------------------
# PreCompact: real request context and prompt cancellation
# ---------------------------------------------------------------------------


async def test_precompact_request_carries_real_turn_context(tmp_path):
    write_hook_module(
        tmp_path,
        "observer",
        "import json\n"
        "from nexus.hooks.model import HookDecision\n"
        "def _run(inv, ctx):\n"
        "    with open(ctx.workspace / 'prec.json', 'w') as f:\n"
        "        f.write(json.dumps(dict(inv.data)))\n"
        "    return HookDecision.allow()\n"
        "HOOKS = [{'event': 'PreCompact', 'name': 'observer', 'run': _run}]\n",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, config=_compact_config())
    session = runtime.session("root")
    await _long_session(session)
    await drain(session)

    import json

    recorded = json.loads((tmp_path / "prec.json").read_text(encoding="utf-8"))
    assert recorded["session_id"] == "root"
    assert recorded["turn_id"]
    assert recorded["history_messages"] > recorded["dropped_messages"] >= 1
    assert recorded["input_budget"] > 0
    assert recorded["strategy"] == "drop_oldest"
    await runtime.aclose()


async def test_precompact_command_hook_is_cancelled_with_the_turn(tmp_path):
    (tmp_path / ".nexus").mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "started"
    (tmp_path / ".nexus" / "hooks.toml").write_text(
        "[[hooks.PreCompact]]\n"
        "name = 'slow'\n"
        f"command = ['/bin/sh', '-c', 'touch {marker}; sleep 30']\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(text_response("ok"))
    runtime = make_runtime(tmp_path, provider, config=_compact_config())
    session = runtime.session("root")
    await _long_session(session)
    await session.start_turn("current")
    for _ in range(500):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()

    session.cancel("stop")
    await asyncio.wait_for(session.wait_idle(), timeout=5)
    assert session.events[-1].type == "turn.cancelled"
    assert session.summaries == []  # no partial compaction/summary
    await runtime.aclose()



# ---------------------------------------------------------------------------
# Review minors
# ---------------------------------------------------------------------------


def test_agents_section_defaults_max_fanout_to_16():
    assert AgentsSection().max_fanout == 16


def test_child_session_ids_are_collision_resistant():
    from nexus.runtime import _child_session_id

    # These two logical ids sanitize to the same string without the hash suffix.
    first = _child_session_id("a/b/1")
    second = _child_session_id("a_b_1")
    assert first != second
    assert len(first) <= 80 and len(second) <= 80
    # Deterministic across calls (stable on reopen).
    assert first == _child_session_id("a/b/1")


async def test_unavailable_declared_tool_is_reported_as_dropped(tmp_path):
    directory = tmp_path / ".nexus" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "probe.md").write_text(
        "---\nname: probe\ndescription: probe\ntools: [NoSuchSkillTool, Read]\n---\nbody\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        tool_response(("t1", "Task", {"prompt": "x", "subagent_type": "probe"})),
        text_response("child"),
        text_response("done"),
    )
    runtime = make_runtime(tmp_path, provider)
    session = runtime.session("root")
    events = await drain(session)

    result = tool_result_for(session, "t1")
    assert result is not None
    assert "NoSuchSkillTool" in result.content[0].text
    spawned = [e for e in events if e.type == "agent.spawned"]
    assert "NoSuchSkillTool" in spawned[0].data["dropped_tools"]
    await runtime.aclose()


def test_seed_failure_is_surfaced_as_a_diagnostic(tmp_path):
    from nexus.agents import AgentManager
    from nexus.agents.model import AgentDiagnosticCode

    manager = AgentManager.for_workspace(
        tmp_path / "ws", seed_source=tmp_path / "missing-source"
    )
    codes = {diagnostic.code for diagnostic in manager.diagnostics}
    assert AgentDiagnosticCode.SEED_ERROR in codes
