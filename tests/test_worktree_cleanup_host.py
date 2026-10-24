"""Confirmed cleanup keeps saved branches and allows normal reuse."""
from nexus.host import HostFacade, protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_worktree_sessions_commits import repository, git
from test_worktree_sessions_runtime import runtime, drain


async def prepared(tmp_path):
    main = repository(tmp_path / "cleanup-project")
    (main / ".gitignore").write_text(".agents/\nlocal.env\n")
    git(main, "add", ".gitignore")
    git(main, "commit", "-qm", "ignore local state")
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "saved.txt", "content": "saved\n"})),
        text_response("done"), text_response("again"))
    rt = runtime(main, tmp_path, provider)
    handle = rt.session("clean-me")
    facade = HostFacade(rt)
    selected = await facade.handle(p.SessionPromote(session=handle.id, reason="isolate saved changes"))
    assert isinstance(selected, p.SessionWorktreeResult), selected
    assert (await drain(handle, "write"))[-1].type == "turn.completed"
    return rt, facade, handle, rt.session_root(handle.id)


async def test_host_remove_keeps_branch_and_next_turn_recreates_checkout(tmp_path):
    rt, facade, handle, root = await prepared(tmp_path)
    try:
        old_head = git(rt.workspace, "rev-parse", "refs/heads/" + root.branch)
        preview = await facade.handle(p.WorktreeRemove(child_id=root.worktree_id))
        assert isinstance(preview, p.SessionWorktreeResult), preview
        assert preview.state["state"] == "ready", preview.state
        result = await facade.handle(p.WorktreeRemove(child_id=root.worktree_id, confirm_token=preview.state["token"]))
        assert isinstance(result, p.SessionWorktreeResult), result
        assert not root.path.exists()
        assert git(rt.workspace, "rev-parse", "refs/heads/" + root.branch) == old_head
        listing = await facade.handle(p.WorktreeList())
        row = next(row for row in listing.worktrees if row["child_id"] == root.worktree_id)
        assert row["kind"] == "session" and row["exists"] is False
        assert (await drain(handle, "continue"))[-1].type == "turn.completed"
        assert (root.path / "saved.txt").read_text() == "saved\n"
    finally:
        await rt.aclose()


async def test_cleanup_refuses_ignored_files_and_stale_preview(tmp_path):
    rt, facade, handle, root = await prepared(tmp_path)
    try:
        preview = await facade.handle(p.WorktreeRemove(child_id=root.worktree_id))
        assert isinstance(preview, p.SessionWorktreeResult), preview
        (root.path / "local.env").write_text("local private value\n")
        blocked = await facade.handle(p.WorktreeRemove(child_id=root.worktree_id))
        assert isinstance(blocked, p.SessionWorktreeResult), blocked
        assert blocked.state["state"] != "ready"
        stale = await facade.handle(p.WorktreeRemove(child_id=root.worktree_id, confirm_token=preview.state["token"]))
        assert isinstance(stale, p.ErrorResult), stale
        assert (root.path / "local.env").read_text() == "local private value\n"
    finally:
        await rt.aclose()
