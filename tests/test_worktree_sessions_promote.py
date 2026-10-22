"""Promotion carries only proven tool writes and leaves the source checkout intact."""
from __future__ import annotations

import pytest

from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_worktree_sessions_runtime import drain, git, project as _project, runtime


@pytest.fixture
def project(tmp_path):
    return _project.__wrapped__(tmp_path)


async def write_turn(rt, handle):
    events = await drain(handle, "write a result")
    assert events[-1].type == "turn.completed", events[-1]
    assert any(e.type == "worktree.file_written" for e in handle.events)


async def test_promote_carries_owned_files_and_leaves_foreign_main_changes(project, tmp_path):
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "owned.txt", "content": "session\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("promote")
        await write_turn(rt, handle)
        (project / "foreign.txt").write_text("user\n")
        before = git(project, "status", "--porcelain")
        await rt.promote_session(handle.id, reason="isolate the next changes")
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        assert (root.path / "owned.txt").read_text() == "session\n"
        assert not (root.path / "foreign.txt").exists()
        assert git(project, "status", "--porcelain") == before
        assert (project / "owned.txt").read_text() == "session\n"
        assert "carry over changes" in git(root.path, "log", "-1", "--format=%s")
        assert git(root.path, "status", "--porcelain") == ""
        result = next(e for e in handle.events if e.type == "worktree.promoted")
        assert result.data["files"] == ["owned.txt"]
        assert "foreign.txt" in result.data["excluded"]
        assert "owned.txt" not in result.data["excluded"]
    finally:
        await rt.aclose()


async def test_promote_refuses_a_foreign_edit_to_the_same_file(project, tmp_path):
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "owned.txt", "content": "session\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("foreign-edit")
        await write_turn(rt, handle)
        (project / "owned.txt").write_text("someone else\n")
        with pytest.raises(ValueError, match="separate"):
            await rt.promote_session(handle.id)
        assert rt.session_root(handle.id).kind == "main"
        assert not rt._worktree_root.exists()
        assert (project / "owned.txt").read_text() == "someone else\n"
    finally:
        await rt.aclose()


async def test_promotion_continuation_has_durable_origin(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")))
    try:
        handle = rt.session("continuation")
        await drain(handle, "hello")
        await rt.promote_session(handle.id, continuation="continue in isolation")
        queued = [e for e in handle.events if e.type == "input.queued"]
        assert queued[-1].data["origin"]["kind"] == "promote"
        assert rt.session_root(handle.id).kind == "worktree"
    finally:
        await rt.aclose()


async def test_worktree_promote_requires_confirmation_even_with_allow_mode(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider())
    try:
        handle = rt.session("approval")
        turn = rt._make_tool_turn(config=rt._load_config(), session=handle, turn_id="approval-turn", attended=True)
        assert "worktree_promote" in turn.manager.names
        assert "worktree_promote" in turn.engine._force_ask_tools
    finally:
        await rt.aclose()


async def test_interrupted_promotion_request_replays_after_reopen(project, tmp_path):
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "owned.txt", "content": "session\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    handle = rt.session("recover-request")
    await write_turn(rt, handle)
    handle.emit_session_event("worktree.promotion_requested", {"reason": "isolate", "continuation": "", "request_id": "request-1"})
    await rt.aclose()
    reopened = runtime(project, tmp_path, ScriptedProvider(text_response("resumed")))
    try:
        handle = reopened.session("recover-request")
        events = await drain(handle, "resume")
        assert events[-1].type == "turn.completed", events[-1]
        root = reopened.session_root(handle.id)
        assert root.kind == "worktree"
        assert (root.path / "owned.txt").read_text() == "session\n"
        assert not reopened.session_worktree_status(handle.id)["pending_promotion"]
    finally:
        await reopened.aclose()


async def test_interrupted_carry_commit_reuses_pending_branch(project, tmp_path):
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "owned.txt", "content": "session\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    handle = rt.session("recover-carry")
    await write_turn(rt, handle)
    handle.emit_session_event("worktree.promotion_requested", {"reason": "isolate", "continuation": "", "request_id": "request-2"})
    service = rt._worktree_service
    record = service.create_session(project, handle.id, "recover-carry", root=rt._worktree_root)
    (record.path / "owned.txt").write_text("session\n")
    result = service.commit_session(record.child_id, root=rt._worktree_root,
        message="nexus: carry over changes from the main checkout", session_name="recover-carry",
        turn_seq=2, allow_pending=True)
    assert result["kind"] == "committed"
    branch = record.branch
    sha = result["sha"]
    await rt.aclose()
    reopened = runtime(project, tmp_path, ScriptedProvider(text_response("resumed")))
    try:
        handle = reopened.session("recover-carry")
        events = await drain(handle, "resume")
        assert events[-1].type == "turn.completed", events[-1]
        root = reopened.session_root(handle.id)
        assert root.branch == branch
        assert git(root.path, "rev-parse", "HEAD") == sha
        assert any(e.type == "worktree.committed" and e.data.get("sha") == sha for e in handle.events)
        assert (project / "owned.txt").read_text() == "session\n"
    finally:
        await reopened.aclose()


async def test_approved_agent_promotion_hands_off_after_durable_tool_result(project, tmp_path):
    from nexus.tools.permissions import Decision
    provider = ScriptedProvider(tool_response(
        ("write-handoff", "write", {"path": "handoff.txt", "content": "carry me\n"}),
        ("promote-handoff", "worktree_promote", {"reason": "isolate work"}),
    ))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("handoff").mark_attended()
        approved = False
        events = []
        async for event in handle.send("write then isolate", attended=True):
            events.append(event)
            if event.type == "permission.requested":
                assert handle.resolve_permission(handle.pending_permissions[0], Decision.ALLOW_ONCE)
                approved = True
        assert approved
        assert events[-1].type == "turn.completed", events[-1]
        assert provider.calls == 1
        root = rt.session_root(handle.id)
        assert root.kind == "worktree"
        assert (root.path / "handoff.txt").read_text() == "carry me\n"
        assert (project / "handoff.txt").read_text() == "carry me\n"
        assert not handle.cancel_token.cancelled
        types = [e.type for e in handle.events]
        assert types.index("tool.result") < types.index("worktree.promoted") < types.index("turn.completed")
    finally:
        await rt.aclose()


async def test_promote_tracked_edit_without_foreign_changes(project, tmp_path):
    provider = ScriptedProvider(tool_response(("edit-tracked", "edit", {"path": "tracked.txt", "old_string": "base", "new_string": "session"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("tracked-edit")
        await write_turn(rt, handle)
        await rt.promote_session(handle.id)
        assert (rt.session_root(handle.id).path / "tracked.txt").read_text() == "session\n"
        assert (project / "tracked.txt").read_text() == "session\n"
    finally:
        await rt.aclose()


async def test_promote_refuses_preexisting_foreign_hunks_in_a_touched_file(project, tmp_path):
    (project / "tracked.txt").write_text("base\nforeign hunk\n")
    provider = ScriptedProvider(tool_response(("edit-mixed", "edit", {"path": "tracked.txt", "old_string": "base", "new_string": "session"})), text_response("done"))
    rt = runtime(project, tmp_path, provider)
    try:
        handle = rt.session("mixed-edit")
        await write_turn(rt, handle)
        with pytest.raises(ValueError, match="earlier changes"):
            await rt.promote_session(handle.id)
        assert rt.session_root(handle.id).kind == "main"
        assert (project / "tracked.txt").read_text() == "session\nforeign hunk\n"
        assert not rt._worktree_root.exists()
    finally:
        await rt.aclose()
