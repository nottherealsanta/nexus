"""Creating a worktree from the main checkout carries only proven tool writes and leaves the source intact."""
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
        assert root.branch.startswith("nexus/wt/")
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
        set_event = [e for e in handle.events if e.type == "session.root_set"][-1]
        assert set_event.data["reason"] == "isolate the next changes" and set_event.data["action"] == "create"
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
        assert (project / "owned.txt").read_text() == "someone else\n"
        assert any(e.type == "worktree.move_failed" for e in handle.events)
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


async def test_named_promote_uses_the_exact_name_without_retitling_the_session(project, tmp_path):
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")))
    try:
        handle = rt.session("named")
        await rt.promote_session(handle.id, reason="launch", name="quiet-fox")
        root = rt.session_root(handle.id)
        assert root.path.name == "quiet-fox" and root.branch == "nexus/wt/quiet-fox"
        await drain(handle, "hello")
        assert rt.sessions.title_source("named") != "user"
        assert rt.sessions.summary("named").title != "quiet-fox"
        other = rt.session("named-2")
        with pytest.raises(ValueError, match="quiet-fox"):
            await rt.promote_session(other.id, reason="launch", name="quiet-fox")
    finally:
        await rt.aclose()


async def test_worktree_tools_never_ask_even_in_ask_mode(project, tmp_path):
    from nexus.config import Config
    from nexus.config.schema import ConfigV2, ModelSection, PermissionsSection
    from nexus.runtime import Runtime
    rt = Runtime(project, home=tmp_path / "home", providers={"scripted": ScriptedProvider()},
        config=Config(model="scripted/m", version=2, v2=ConfigV2(model=ModelSection(default="scripted/m"),
            permissions=PermissionsSection(mode="ask"))))
    try:
        handle = rt.session("approval")
        turn = rt._make_tool_turn(config=rt._load_config(), session=handle, turn_id="approval-turn", attended=True)
        assert {"worktree_list", "worktree_create", "worktree_move"} <= set(turn.manager.names)
        assert not turn.engine._force_ask_tools
        from nexus.tools.permissions import Outcome
        from nexus.tools.spec import ToolCall
        specs = {spec.name: spec for spec in turn.manager.specs}
        for name, args in (("worktree_list", {}), ("worktree_create", {"reason": "r"}),
                           ("worktree_move", {"to": "main", "reason": "r"})):
            assert turn.engine.evaluate(ToolCall(id=name, name=name, input=args), specs[name]).outcome is Outcome.ALLOW
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
    finally:
        await rt.aclose()
