"""End-to-end acceptance for durable session worktree lifecycle boundaries."""

from nexus.host import HostFacade, protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_worktree_sessions_commits import git, repository
from test_worktree_sessions_runtime import drain, runtime


async def test_session_worktree_child_commit_diff_integrate_remove_and_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.runtime.SUBAGENT_WORKTREES", True)  # disabled by default
    main = repository(tmp_path / "acceptance-project")
    (main / ".gitignore").write_text(".agents/\n", encoding="utf-8")
    (main / ".agents").mkdir()
    (main / ".agents" / "project.md").write_text("project instructions\n", encoding="utf-8")
    git(main, "add", ".gitignore")
    git(main, "add", "-f", ".agents/project.md")
    git(main, "commit", "-qm", "track project instructions")
    main_head = git(main, "rev-parse", "HEAD")
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "write child file", "subagent_type": "task", "tools": ["write"]})),
        tool_response(("child-write", "write", {"path": "child.txt", "content": "child saved\n"})),
        text_response("child done"), text_response("root done"), text_response("again"),
    )
    rt = runtime(main, tmp_path, provider)
    facade = HostFacade(rt)
    try:
        handle = rt.session("acceptance")
        assert rt.session_root(handle.id).kind == "main"  # every session starts in the main checkout
        listed = await facade.handle(p.SessionWorktreeList(session=handle.id))
        assert isinstance(listed, p.SessionWorktreeResult), listed
        # No modes and no question: the user (or the agent) creates the worktree when the work needs one.
        selected = await facade.handle(p.SessionPromote(session=handle.id, reason="isolate the child's work"))
        assert isinstance(selected, p.SessionWorktreeResult), selected

        events = await drain(handle, "delegate")
        assert events[-1].type == "turn.completed", events[-1]
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        assert not (main / "child.txt").exists()
        assert git(main, "rev-parse", "HEAD") == main_head
        spawned = next(event for event in handle.events if event.type == "agent.spawned")
        child_path = spawned.data["worktree"]["path"]
        assert child_path != str(root.path)
        assert (root.path / "child.txt").read_text(encoding="utf-8") == "child saved\n"
        assert (root.path / ".agents/project.md").read_text(encoding="utf-8") == "project instructions\n"
        assert git(root.path, "status", "--porcelain") == ""
        committed = [event for event in handle.events if event.type == "worktree.committed"]
        assert committed, [event.type for event in handle.events]
        saved_head = git(root.path, "rev-parse", "HEAD")

        diff = await facade.handle(p.SessionTurnDiff(session=handle.id, seq=committed[-1].data["seq"]))
        assert isinstance(diff, p.SessionWorktreeResult), diff
        assert "child.txt" in diff.state["patch"]
        assert "child saved" in diff.state["patch"]

        preview = await facade.handle(p.SessionIntegrate(session=handle.id))
        assert isinstance(preview, p.SessionWorktreeResult), preview
        assert preview.state["token"]
        assert not (main / "child.txt").exists()
        integrated = await facade.handle(
            p.SessionIntegrate(session=handle.id, confirm_token=preview.state["token"])
        )
        assert isinstance(integrated, p.SessionWorktreeResult), integrated
        assert (main / "child.txt").read_text(encoding="utf-8") == "child saved\n"

        removal = await facade.handle(p.WorktreeRemove(child_id=root.worktree_id))
        assert isinstance(removal, p.SessionWorktreeResult), removal
        assert removal.state["state"] == "ready"
        removed = await facade.handle(
            p.WorktreeRemove(child_id=root.worktree_id, confirm_token=removal.state["token"])
        )
        assert isinstance(removed, p.SessionWorktreeResult), removed
        assert not root.path.exists()
        assert git(main, "rev-parse", f"refs/heads/{root.branch}") == saved_head

        before_followup = len([event for event in handle.events if event.type == "worktree.committed"])
        resumed = await drain(handle, "continue")
        assert resumed[-1].type == "turn.completed", resumed[-1]
        assert (root.path / "child.txt").read_text(encoding="utf-8") == "child saved\n"
        final_commits = [event for event in handle.events if event.type == "worktree.committed"]
        assert len(final_commits) == before_followup
        assert git(root.path, "rev-parse", "HEAD") == saved_head
        assert (main / "child.txt").read_text(encoding="utf-8") == "child saved\n"
    finally:
        await rt.aclose()
