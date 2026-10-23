"""Saved-turn diffs and isolated restore against real repositories."""
from types import SimpleNamespace

import pytest

from nexus.config.schema import WorktreesSection
from nexus.events import Event
from nexus.worktree_turns import revert_preview, revert_turn, turn_diff
from test_worktree_sessions_commits import active_session, git


def controller(tmp_path):
    parent, registry, service, record = active_session(tmp_path)
    events = []
    root = SimpleNamespace(kind="worktree", path=record.path, worktree_id=record.child_id)
    handle = SimpleNamespace(events=events, _lock=SimpleNamespace(acquire=lambda **kw: None, release=lambda: None))
    ctrl = SimpleNamespace(handle=lambda session: handle, _idle=lambda h: None,
        runtime=SimpleNamespace(session_root=lambda session: root, validate_session_root=lambda s: None),
        service=service, registry=registry, policy=lambda: WorktreesSection(), _name=lambda h: "commit-session",
        _commit_event=lambda h, result, **kw: events.append(Event(type="worktree.committed", data={**result, **kw})))
    return ctrl, root, events


def add_commit(ctrl, root, events, seq, text):
    (root.path / "tracked.txt").write_text(text)
    row = ctrl.service.commit_session(root.worktree_id, root=ctrl.registry, message=f"turn {seq}",
        session_name="commit-session", turn_seq=seq, max_file_mb=50)
    events.append(Event(type="worktree.committed", data={**row, "seq": seq}))
    return row


def test_saved_diff_and_restore_commit(tmp_path):
    ctrl, root, events = controller(tmp_path)
    add_commit(ctrl, root, events, 1, "changed\n")
    diff = turn_diff(ctrl, "commit-session", 1, max_bytes=20)
    assert diff["source"] == "commits"
    assert diff["files"][0]["path"] == "tracked.txt"
    assert len(diff["patch"].encode()) <= 20 and diff["clipped"]
    state = revert_preview(ctrl, "commit-session", 1)
    result = revert_turn(ctrl, "commit-session", state)
    assert result["kind"] == "committed"
    assert result["revert_of"] == 1
    assert "Nexus-Revert-Of-Turn: 1" in git(root.path, "show", "-s", "--format=%B", "HEAD")
    assert (root.path / "tracked.txt").read_text() == "base\n"
    assert git(root.path, "rev-parse", "HEAD") != state["head"]


def test_restore_refuses_shared_dirty_and_overlapping_turns(tmp_path):
    ctrl, root, events = controller(tmp_path)
    add_commit(ctrl, root, events, 1, "one\n")
    add_commit(ctrl, root, events, 2, "two\n")
    with pytest.raises(ValueError, match="Later turns"):
        revert_preview(ctrl, "commit-session", 1)
    state = revert_preview(ctrl, "commit-session", 1, True)
    assert len(state["commits"]) == 2
    (root.path / "dirty").write_text("dirty")
    with pytest.raises(ValueError, match="uncommitted"):
        revert_preview(ctrl, "commit-session", 1, True)
    root.kind = "main"
    with pytest.raises(ValueError, match="own worktree"):
        revert_preview(ctrl, "commit-session", 1)


def test_restore_refuses_stale_preview(tmp_path):
    ctrl, root, events = controller(tmp_path)
    add_commit(ctrl, root, events, 1, "one\n")
    state = revert_preview(ctrl, "commit-session", 1)
    (root.path / "other").write_text("other")
    git(root.path, "add", "other")
    git(root.path, "commit", "-qm", "outside commit")
    with pytest.raises(ValueError, match="changed since"):
        revert_turn(ctrl, "commit-session", state)


async def test_real_runtime_checkpoint_first_edit_after_read_only_turn(tmp_path):
    from test_worktree_sessions_runtime import runtime, drain
    from test_worktree_sessions_commits import repository
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
    from nexus.host import HostFacade, protocol as p

    project = repository(tmp_path / "runtime-project")
    provider = ScriptedProvider(text_response("read only"),
        tool_response(("w1", "write", {"path": "owned.txt", "content": "saved\n"})), text_response("done"))
    rt = runtime(project, tmp_path, provider, sessions=False)
    try:
        handle = rt.session("checkpoints")
        assert (await drain(handle, "read"))[-1].type == "turn.completed"
        events = await drain(handle, "write")
        assert events[-1].type == "turn.completed", events[-1]
        checkpoint = next(e for e in handle.events if e.type == "checkpoint.captured")
        facade = HostFacade(rt)
        result = await facade.handle(p.SessionTurnDiff(session=handle.id, seq=checkpoint.data["seq"]))
        assert isinstance(result, p.SessionWorktreeResult), result
        assert result.state["source"] == "checkpoint"
        assert "saved" in result.state["patch"]
        assert checkpoint.data["files"] == ["owned.txt"]
        refused = await facade.handle(p.SessionTurnRevert(session=handle.id, seq=checkpoint.data["seq"]))
        assert isinstance(refused, p.ErrorResult)
        assert "own worktree" in refused.message
    finally:
        await rt.aclose()
