"""Real turn lifecycle tests for session worktrees (plan03)."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection, WorktreesSection
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.runtime import Runtime


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Tests")
    git(root, "config", "user.email", "tests@example.invalid")
    (root / "tracked.txt").write_text("base\n")
    (root / "AGENTS.md").write_text("root instructions\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    return root


def runtime(project, tmp_path, provider, **policy):
    return Runtime(project, home=tmp_path / "home", providers={"scripted": provider},
        config=Config(model="scripted/m", version=2, v2=ConfigV2(model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="allow"), worktrees=WorktreesSection(**policy))))


async def drain(handle, prompt):
    return [event async for event in handle.send(prompt)]


async def test_first_changed_turn_commits_in_owned_root_and_leaves_main_untouched(project, tmp_path):
    (project / "foreign.txt").write_text("user dirty\n")
    provider = ScriptedProvider(tool_response(("write-1", "write", {"path": "result.txt", "content": "hello\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        handle = rt.session("first")
        assert rt.session_root(handle.id).kind == "main"
        assert not rt._worktree_root.exists()
        events = await drain(handle, "implement the change")
        assert events[-1].type == "turn.completed", events[-1]
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        assert (root.path / "result.txt").read_text() == "hello\n"
        assert not (project / "result.txt").exists()
        assert (project / "foreign.txt").read_text() == "user dirty\n"
        assert not (root.path / "foreign.txt").exists()
        commits = [e for e in handle.events if e.type == "worktree.committed"]
        assert len(commits) == 1
        assert commits[0].data["by"] == "harness"
        assert git(root.path, "status", "--porcelain") == ""
        body = git(root.path, "log", "-1", "--format=%B")
        assert "Nexus-Session:" in body and "Nexus-Agent: root" in body
        types = [e.type for e in handle.events]
        assert types.index("worktree.committed") < types.index("turn.completed")
        assert provider.requests[0].system is not None
    finally:
        await rt.aclose()


async def test_draft_toggle_creates_nothing_and_unchanged_turn_has_no_commit(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")), sessions=True)
    try:
        handle = rt.session("toggle")
        rt.set_session_worktree(handle.id, False)
        assert not rt._worktree_root.exists()
        await drain(handle, "hello")
        assert rt.session_root(handle.id).kind == "main"
        assert not any(e.type == "worktree.created" for e in handle.events)
        with pytest.raises(ValueError, match="locked"):
            rt.set_session_worktree(handle.id, True)
    finally:
        await rt.aclose()


async def test_missing_owned_directory_recreated_before_next_context(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("one"), text_response("two")), sessions=True)
    try:
        handle = rt.session("recreate")
        await drain(handle, "hello")
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        shutil.rmtree(root.path)
        events = await drain(handle, "again")
        assert events[-1].type == "turn.completed", events[-1]
        assert root.path.is_dir()
        assert any(e.type == "worktree.setup_ran" and e.data.get("reason") == "recreated" for e in handle.events)
        assert not any(e.type == "worktree.committed" for e in handle.events)
    finally:
        await rt.aclose()


async def test_setup_failure_keeps_selection_editable_for_retry(project, tmp_path):
    import sys
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")), sessions=True,
                 setup=[sys.executable, "-c", "raise SystemExit(7)"])
    try:
        handle = rt.session("setup-fail")
        rt.approve_worktree_setup(handle.id, "allow_once")
        events = await drain(handle, "hello")
        assert events[-1].type == "turn.failed"
        assert "setup failed" in events[-1].data["error"]
        assert rt.session_worktree_status(handle.id)["editable"]
        rt.set_session_worktree(handle.id, False)
        events = await drain(handle, "retry without worktree")
        assert events[-1].type == "turn.completed"
    finally:
        await rt.aclose()


async def test_agent_commit_then_harness_commit_are_both_recorded(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("write-a", "write", {"path": "first.txt", "content": "one"})),
        tool_response(("commit", "worktree_commit", {"message": "agent commit\n\nNexus-Agent: forged"})),
        tool_response(("write-b", "write", {"path": "second.txt", "content": "two"})), text_response("done"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        handle = rt.session("mid-turn")
        events = await drain(handle, "make both changes")
        assert events[-1].type == "turn.completed", events[-1]
        commits = [e for e in handle.events if e.type == "worktree.committed"]
        assert [e.data["by"] for e in commits] == ["agent", "harness"]
        assert commits[0].data["stripped_nexus_trailers"]
        assert commits[0].data["seq"] == commits[1].data["seq"]
        root = rt.session_root(handle.id)
        assert "forged" not in git(root.path, "log", "-2", "--format=%B")
        assert git(root.path, "status", "--porcelain") == ""
    finally:
        await rt.aclose()


async def test_shell_commits_are_detected_at_turn_end(project, tmp_path):
    provider = ScriptedProvider(tool_response(("shell", "shell", {"command": "printf shell > shell.txt; git add shell.txt; git commit -m 'agent shell'", "timeout_s": 10})), text_response("done"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        handle = rt.session("shell-commit")
        events = await drain(handle, "make a shell commit")
        assert events[-1].type == "turn.completed", events[-1]
        commits = [e for e in handle.events if e.type == "worktree.committed"]
        assert len(commits) == 1
        assert commits[0].data["via"] == "shell"
        assert commits[0].data["by"] == "agent"
        assert commits[0].data["sha"] == git(rt.session_root(handle.id).path, "rev-parse", "HEAD")
        assert not (project / "shell.txt").exists()
    finally:
        await rt.aclose()


async def test_fork_gets_its_own_branch_at_first_turn(project, tmp_path):
    provider = ScriptedProvider(text_response("parent"), tool_response(("write-fork", "write", {"path": "fork.txt", "content": "fork\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        parent = rt.session("parent")
        await drain(parent, "hello")
        parent_root = rt.session_root(parent.id)
        fork = rt.sessions.fork(parent.id, new_id="fork")
        with pytest.raises(ValueError, match="Fork needs"):
            rt.validate_session_root(fork.id)
        events = await drain(fork, "change independently")
        assert events[-1].type == "turn.completed", events[-1]
        fork_root = rt.session_root(fork.id)
        assert fork_root.path != parent_root.path
        assert fork_root.branch != parent_root.branch
        assert (fork_root.path / "fork.txt").read_text() == "fork\n"
        assert not (parent_root.path / "fork.txt").exists()
    finally:
        await rt.aclose()


async def test_stop_during_setup_cancels_and_removes_pending_allocation(project, tmp_path):
    import asyncio
    import sys
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("unused")), sessions=True,
                 setup=[sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"])
    try:
        handle = rt.session("cancel-setup")
        rt.approve_worktree_setup(handle.id, "allow_once")
        await handle.start_turn("start setup")
        async with asyncio.timeout(10):
            while not any(e.type == "worktree.setup_output" for e in handle.events):
                await asyncio.sleep(0.01)
        handle.cancel("user stopped")
        async with asyncio.timeout(10):
            while handle.active:
                await asyncio.sleep(0.01)
        terminal = [e for e in handle.events if e.type in {"turn.failed", "turn.cancelled", "turn.completed"}]
        assert terminal[-1].type == "turn.cancelled"
        assert rt.session_root(handle.id).kind == "main"
        assert not list((rt._worktree_root / "worktrees").iterdir())
        assert not git(project, "branch", "--list", "nexus/session/*")
        assert rt.session_worktree_status(handle.id)["editable"]
    finally:
        await rt.aclose()


async def test_default_worktrees_applies_to_drafts_not_started_main_sessions(project, tmp_path):
    from nexus.events import Event
    from nexus.model.message import Message, Text
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("continue")), sessions=True)
    try:
        handle = rt.session("existing-main")
        handle.append_message(Message(role="user", content=[Text(text="prior prompt")]))
        handle.append_event(Event(type="turn.started", session=handle.id, turn="prior", data={}))
        handle.append_event(Event(type="turn.completed", session=handle.id, turn="prior", data={}))
        assert not rt.session_worktree_status(handle.id)["editable"]
        events = await drain(handle, "continue here")
        assert events[-1].type == "turn.completed", events[-1]
        assert rt.session_root(handle.id).kind == "main"
        assert not rt._worktree_root.exists()
    finally:
        await rt.aclose()


async def test_status_selection_tracks_creation_and_detach(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")), sessions=True)
    try:
        handle = rt.session("status-state")
        await drain(handle, "hello")
        state = rt.session_worktree_status(handle.id)
        assert state["enabled"] and state["kind"] == "worktree" and not state["editable"]
        state = rt.detach_session_worktree(handle.id)
        assert not state["enabled"] and state["kind"] == "main" and not state["editable"]
    finally:
        await rt.aclose()
