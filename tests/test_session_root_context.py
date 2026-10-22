"""Session-root context and hook routing (worktree-sessions plan 02)."""

import json
import sys
from pathlib import Path

from nexus.config import Config
from nexus.context import ContextManager
from nexus.hooks import HookInvocation, HookManager


def test_context_reads_agents_from_session_root_and_shared_files_from_project(tmp_path):
    project = tmp_path / "project"
    root = tmp_path / "session-root"
    other_root = tmp_path / "other-root"
    for directory in (project, root, other_root):
        directory.mkdir()
    (project / "AGENTS.md").write_text("project instructions", encoding="utf-8")
    (project / "SOUL.md").write_text("project soul", encoding="utf-8")
    (project / "MEMORY.md").write_text("project memory", encoding="utf-8")
    (root / "AGENTS.md").write_text("session instructions", encoding="utf-8")
    (root / "SOUL.md").write_text("wrong session soul", encoding="utf-8")
    (root / "MEMORY.md").write_text("wrong session memory", encoding="utf-8")
    (other_root / "AGENTS.md").write_text("other instructions", encoding="utf-8")

    manager = ContextManager(project, config=Config())
    turn = manager.for_turn(workspace=root)
    assert turn._env.workspace == root
    assert turn._env.agents_text == "session instructions"
    assert turn._env.soul_text == "project soul"
    assert turn._env.memory_text == "project memory"

    # The per-turn snapshot stays stable if AGENTS.md changes mid-turn.
    (root / "AGENTS.md").write_text("changed during turn", encoding="utf-8")
    same_root = turn.for_iteration()
    assert same_root._env.agents_text == "session instructions"
    assert same_root._env.memory_text == "project memory"

    other = turn.for_iteration(workspace=other_root)
    assert other._env.workspace == other_root
    assert other._env.agents_text == "other instructions"
    assert other._env.memory_text == "project memory"
    assert turn._env.agents_text == "session instructions"


def test_explicit_turn_agents_snapshot_wins_over_live_session_root(tmp_path):
    project = tmp_path / "project"
    root = tmp_path / "session-root"
    project.mkdir()
    root.mkdir()
    (root / "AGENTS.md").write_text("turn-start instructions", encoding="utf-8")
    manager = ContextManager(project, config=Config())
    turn = manager.for_turn(workspace=root)
    frozen = turn._env.agents_text

    (root / "AGENTS.md").write_text("changed during turn", encoding="utf-8")
    next_iteration = turn.for_iteration(
        workspace=root,
        system_files={"soul": "", "memory": "", "agents": "manifest snapshot"},
        agents_text=frozen,
    )

    assert next_iteration._env.agents_text == "turn-start instructions"
async def test_hooks_keep_project_discovery_but_run_in_session_root(tmp_path):
    project = tmp_path / "project"
    session_root = tmp_path / "worktree"
    (project / ".agents" / "hooks").mkdir(parents=True)
    (session_root / "nested").mkdir(parents=True)
    project_marker = tmp_path / "command.json"
    python_marker = tmp_path / "python-hook.txt"
    command_script = tmp_path / "capture_hook.py"
    command_script.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
        "'cwd': os.getcwd(), 'workspace': os.environ.get('NEXUS_WORKSPACE'), "
        "'project': os.environ.get('NEXUS_PROJECT_ROOT')}))\n",
        encoding="utf-8",
    )
    (project / ".agents" / "hooks.toml").write_text(
        "[[hooks.SessionStart]]\n"
        "name = 'capture'\n"
        f"command = [{json.dumps(sys.executable)}, {json.dumps(str(command_script))}, "
        f"{json.dumps(str(project_marker))}]\n"
        "cwd = 'nested'\n",
        encoding="utf-8",
    )
    (project / ".agents" / "hooks" / "context.py").write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(python_marker)!r})\n"
        "def _run(invocation, ctx):\n"
        "    MARKER.write_text(str(ctx.workspace))\n"
        "    return None\n"
        "HOOKS = [{'event': 'SessionStart', 'name': 'context', 'run': _run}]\n",
        encoding="utf-8",
    )
    # A worktree-local hook must not be discovered or loaded.
    (session_root / ".agents" / "hooks").mkdir(parents=True)
    (session_root / ".agents" / "hooks" / "ignored.py").write_text(
        "raise AssertionError('session-root extension was imported')\n",
        encoding="utf-8",
    )

    manager = HookManager(project, home=tmp_path)
    manager.refresh()
    assert {spec.name for spec in manager.specs} == {"capture", "context"}
    outcome = await manager.run(
        "SessionStart",
        HookInvocation(event="SessionStart", session_id="s1"),
        workspace=session_root,
        project_root=project,
    )

    assert outcome.allowed
    command_result = json.loads(project_marker.read_text(encoding="utf-8"))
    assert Path(command_result["cwd"]) == session_root / "nested"
    assert Path(command_result["workspace"]) == session_root
    assert Path(command_result["project"]) == project
    assert Path(python_marker.read_text(encoding="utf-8")) == session_root
    assert manager.workspace == project
    await manager.aclose()
