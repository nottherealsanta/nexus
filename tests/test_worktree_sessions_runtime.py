"""Real turn lifecycle tests for agent-managed session worktrees."""
from __future__ import annotations

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
    # Hermetic: an agent-run `git commit` must not depend on the developer's signing setup.
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "tag.gpgsign", "false")
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


def tool_names(request):
    return {tool.name for tool in request.tools}


async def test_new_session_starts_in_main_without_a_prompt_or_checkout(project, tmp_path):
    provider = ScriptedProvider(text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("first")
        assert rt.session_root(handle.id).kind == "main"
        events = await drain(handle, "just a question")
        assert events[-1].type == "turn.completed", events[-1]
        assert rt.session_root(handle.id).kind == "main"
        assert not rt._worktree_root.exists()
        assert not any(e.type.startswith(("checkpoint.", "worktree.")) for e in handle.events)
        # every Git session gets the worktree tools (but no commit tool) and the guidance block
        assert {"worktree_list", "worktree_create", "worktree_move"} <= tool_names(provider.requests[0])
        assert "worktree_commit" not in tool_names(provider.requests[0])
        assert "worktree_status" not in tool_names(provider.requests[0]) and "worktree_promote" not in tool_names(provider.requests[0])
        facts = dict(rt._session_worktree_facts(handle.id))
        assert facts["Current root"] == "main checkout"
        assert "Worktree branch" not in facts
    finally:
        await rt.aclose()


async def test_agent_creates_a_worktree_mid_turn_and_the_turn_carries_on(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("write-1", "write", {"path": "result.txt", "content": "hello\n"}),
                      ("create", "worktree_create", {"reason": "refactor across files", "name": "refactor-1"})),
        tool_response(("write-2", "write", {"path": "second.txt", "content": "two\n"})),
        text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("create")
        events = await drain(handle, "implement the change")
        assert events[-1].type == "turn.completed", events[-1]
        assert provider.calls == 3  # one turn, the step count carried on
        root = rt.session_root(handle.id)
        assert root.kind == "worktree" and root.branch == "nexus/wt/refactor-1"
        assert (root.path / "result.txt").read_text() == "hello\n"  # carried over
        assert (project / "result.txt").read_text() == "hello\n"  # the main checkout keeps it
        assert (root.path / "second.txt").read_text() == "two\n"  # written after the move
        assert not (project / "second.txt").exists()
        types = [e.type for e in handle.events]
        assert types.index("worktree.move_requested") < types.index("session.root_set") < types.index("worktree.turn_started")
        # the move waited for the whole step: the create result is durable before the root changed
        result_positions = [i for i, kind in enumerate(types) if kind == "tool.result"]
        assert types.index("session.root_set") > result_positions[1]
        set_event = next(e for e in handle.events if e.type == "session.root_set")
        assert set_event.data["reason"] == "refactor across files" and set_event.data["worktree_id"] == root.worktree_id
        moved = next(e for e in handle.events if e.type == "worktree.moved")
        assert moved.data["from_kind"] == "main" and moved.data["to_name"] == "refactor-1"
        assert any(e.type == "worktree.committed" and e.data["by"] == "harness" for e in handle.events)
        assert git(root.path, "status", "--porcelain") == ""
        # the model saw the move (durable notice) and the next request had the new tool set and facts
        notices = [m for m in handle.messages if m.meta.notice == "worktree_move"]
        assert notices and "refactor-1" in notices[0].content[0].text
        assert "worktree_commit" not in tool_names(provider.requests[0])
        assert "worktree_commit" in tool_names(provider.requests[1])
        assert dict(rt._session_worktree_facts(handle.id))["Current root"] == "worktree refactor-1"
    finally:
        await rt.aclose()


async def test_sibling_calls_in_the_same_step_still_use_the_old_root(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("create", "worktree_create", {"reason": "isolate", "name": "late"}),
                      ("write-old", "write", {"path": "old-root.txt", "content": "x\n"})),
        text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("siblings")
        events = await drain(handle, "go")
        assert events[-1].type == "turn.completed", events[-1]
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        assert (project / "old-root.txt").read_text() == "x\n"
    finally:
        await rt.aclose()


async def test_full_agent_flow_list_create_edit_merge_and_move_to_main(project, tmp_path):
    # Only the bare merge is exempt from the Git-write ask; commit with worktree_commit.
    land = f"git -C {project} merge --ff-only nexus/wt/e2e"
    provider = ScriptedProvider(
        tool_response(("list", "worktree_list", {})),
        tool_response(("create", "worktree_create", {"reason": "feature work", "name": "e2e"})),
        tool_response(("write", "write", {"path": "e2e.txt", "content": "feature\n"})),
        tool_response(("commit", "worktree_commit", {"message": "add e2e"})),
        tool_response(("land", "shell", {"command": land, "timeout_s": 20})),
        tool_response(("back", "worktree_move", {"to": "main", "reason": "feature merged"})),
        text_response("all done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("e2e")
        events = await drain(handle, "build the feature")
        assert events[-1].type == "turn.completed", events[-1]
        assert not any(e.type == "permission.requested" for e in handle.events)
        assert rt.session_root(handle.id).kind == "main"
        land_result = next(e for e in handle.events if e.type == "tool.result" and e.data.get("tool_use_id") == "land")
        assert not land_result.data.get("is_error"), land_result.data
        assert (project / "e2e.txt").read_text() == "feature\n"
        merged = [e for e in handle.events if e.type == "worktree.merged"]
        assert len(merged) == 1 and merged[0].data["target_branch"] == "main"
        types = [e.type for e in handle.events]
        assert types.count("session.root_set") == 2
        removed = [e for e in handle.events if e.type == "worktree.removed"]
        assert removed and removed[0].data["name"] == "e2e"  # merged + clean + last session left
        assert not list((rt._worktree_root / "worktrees").iterdir())
        assert git(project, "branch", "--list", "nexus/wt/e2e")  # the branch is kept
    finally:
        await rt.aclose()


async def test_move_to_main_leaves_an_unmerged_branch_with_the_warning(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("create", "worktree_create", {"reason": "try it", "name": "wip"})),
        tool_response(("write", "write", {"path": "wip.txt", "content": "w\n"})),
        tool_response(("back", "worktree_move", {"to": "main", "reason": "not ready"})),
        text_response("parked"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("unmerged")
        events = await drain(handle, "start something")
        assert events[-1].type == "turn.completed", events[-1]
        assert rt.session_root(handle.id).kind == "main"
        moved = [e for e in handle.events if e.type == "worktree.moved"]
        assert moved[-1].data["kept_unmerged"] and moved[-1].data["unmerged_commits"] == 1
        assert not any(e.type == "worktree.removed" for e in handle.events)
        assert (rt._worktree_root / "worktrees" / "wip" / "wip.txt").exists()
        text = next(m for m in handle.messages if m.meta.notice == "worktree_move" and "main checkout" in m.content[0].text)
        assert "kept unmerged" in text.content[0].text
    finally:
        await rt.aclose()


async def test_a_lone_session_leaving_commits_its_shell_changes_first(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("create", "worktree_create", {"reason": "isolate", "name": "dirty"})),
        tool_response(("shell", "shell", {"command": "printf x > stray.txt", "timeout_s": 10})),
        tool_response(("back", "worktree_move", {"to": "main", "reason": "leave"})),
        text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("dirty")
        events = await drain(handle, "go")
        assert events[-1].type == "turn.completed", events[-1]
        # the shell wrote an uncommitted file; a lone session's harness commit covers it at the step
        # boundary, so the leave succeeds (a refusal needs another session in the checkout)
        assert rt.session_root(handle.id).kind == "main"
        assert "stray.txt" in git(project, "ls-tree", "-r", "--name-only", "nexus/wt/dirty")
    finally:
        await rt.aclose()


async def test_move_into_unknown_worktree_returns_a_model_readable_error(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("move", "worktree_move", {"to": "nope", "reason": "x"})), text_response("ok"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("unknown")
        events = await drain(handle, "go")
        assert events[-1].type == "turn.completed", events[-1]
        result = next(e for e in handle.events if e.type == "tool.result")
        assert result.data["is_error"]
        assert "worktree_list" in str(result.data["content"])
        assert rt.session_root(handle.id).kind == "main"
        assert not any(e.type == "session.root_set" for e in handle.events)
    finally:
        await rt.aclose()


async def test_creating_past_the_limit_is_refused_with_guidance(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")), max_worktrees=1)
    try:
        first = rt.session("limit-a")
        await rt.promote_session(first.id, reason="first")
        second = rt.session("limit-b")
        with pytest.raises(ValueError, match="limit"):
            await rt.promote_session(second.id, reason="second")
        assert rt.session_root(second.id).kind == "main"
    finally:
        await rt.aclose()


async def test_missing_checkout_is_recreated_before_the_next_context(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("one"), text_response("two")))
    try:
        handle = rt.session("recreate")
        await rt.promote_session(handle.id, reason="isolate")
        await drain(handle, "hello")
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        subprocess.run(["git", "worktree", "remove", "--force", str(root.path)], cwd=project, check=True)
        assert not root.path.exists()
        events = await drain(handle, "again")
        assert events[-1].type == "turn.completed", events[-1]
        assert root.path.is_dir()
        assert any(e.type == "worktree.setup_ran" and e.data.get("reason") == "recreated" for e in handle.events)
    finally:
        await rt.aclose()


async def test_setup_failure_leaves_the_session_in_main(project, tmp_path):
    import sys
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")),
                 setup=[sys.executable, "-c", "raise SystemExit(7)"])
    try:
        handle = rt.session("setup-fail")
        rt.approve_worktree_setup(handle.id, "allow_once")
        with pytest.raises(ValueError, match="setup failed"):
            await rt.promote_session(handle.id, reason="isolate")
        assert rt.session_root(handle.id).kind == "main"
        assert not list((rt._worktree_root / "worktrees").iterdir())
        events = await drain(handle, "carry on in main")
        assert events[-1].type == "turn.completed"
    finally:
        await rt.aclose()


async def test_agent_commit_then_harness_commit_are_both_recorded(project, tmp_path):
    provider = ScriptedProvider(
        tool_response(("write-a", "write", {"path": "first.txt", "content": "one"})),
        tool_response(("commit", "worktree_commit", {"message": "agent commit\n\nNexus-Agent: forged"})),
        tool_response(("write-b", "write", {"path": "second.txt", "content": "two"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("mid-turn")
        await rt.promote_session(handle.id, reason="isolate")
        events = await drain(handle, "make both changes")
        assert events[-1].type == "turn.completed", events[-1]
        commits = [e for e in handle.events if e.type == "worktree.committed"]
        assert [e.data["by"] for e in commits] == ["agent", "harness"]
        assert commits[0].data["stripped_nexus_trailers"]
        assert commits[0].data["seq"] == commits[1].data["seq"]
        root = rt.session_root(handle.id)
        assert "forged" not in git(root.path, "log", "-2", "--format=%B")
        assert git(root.path, "status", "--porcelain") == ""
        # each turn commit is pinned under a hidden ref so it survives a squash
        assert git(project, "for-each-ref", "refs/nexus/turns/")
    finally:
        await rt.aclose()


async def test_shell_commits_are_detected_at_turn_end(project, tmp_path):
    provider = ScriptedProvider(tool_response(("shell", "shell", {"command": "printf shell > shell.txt; git add shell.txt; git commit -m 'agent shell'", "timeout_s": 10})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("shell-commit")
        await rt.promote_session(handle.id, reason="isolate")
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


async def test_fork_stays_in_the_parents_worktree(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("parent"), text_response("fork")))
    try:
        parent = rt.session("parent")
        await rt.promote_session(parent.id, reason="isolate")
        parent_root = rt.session_root(parent.id)
        fork = rt.sessions.fork(parent.id, new_id="fork")
        assert rt.session_root(fork.id).worktree_id == parent_root.worktree_id
        rt.validate_session_root(fork.id)
        events = await drain(fork, "continue")
        assert events[-1].type == "turn.completed", events[-1]
        assert rt.session_root(fork.id).path == parent_root.path
    finally:
        await rt.aclose()


async def test_stop_during_setup_cancels_and_removes_pending_allocation(project, tmp_path):
    import asyncio
    import sys
    provider = ScriptedProvider(
        tool_response(("create", "worktree_create", {"reason": "isolate", "name": "slow"})), text_response("unused"))
    rt = runtime(project, tmp_path, provider,
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
        assert not git(project, "branch", "--list", "nexus/wt/*")
    finally:
        await rt.aclose()


async def test_status_tracks_create_and_move_back(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")))
    try:
        handle = rt.session("status-state")
        state = rt.session_worktree_status(handle.id)
        assert state["kind"] == "main" and "mode" not in state and "editable" not in state
        await rt.promote_session(handle.id, reason="isolate")
        state = rt.session_worktree_status(handle.id)
        assert state["kind"] == "worktree" and state["worktree_name"]
        state = await rt.detach_session_worktree(handle.id)
        assert state["kind"] == "main"
        assert any(e.type == "worktree.moved" for e in handle.events)
    finally:
        await rt.aclose()
