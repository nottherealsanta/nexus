"""Real Git acceptance for the simplified isolated-child service."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from nexus.agents.session_subagents import SessionSubagentService
from nexus.config.schema import WorktreesSection
from nexus.worktree_sessions import SessionWorktrees
from test_worktree_sessions_commits import active_session, git, repository


def controller(main, registry, root):
    runtime = SimpleNamespace(workspace=main, session_root=lambda s: root,
        validate_session_root=lambda s: None, session_has_active_jobs=lambda s: False,
        _load_config=lambda: SimpleNamespace(v2=SimpleNamespace(worktrees=WorktreesSection())))
    ctrl = SessionWorktrees(runtime)
    runtime._worktree_root = registry
    handle = SimpleNamespace(id="commit-session", active_turn_id="turn-one", events=[])
    ctrl.handle = lambda s: handle
    ctrl._turn_data = lambda h, t: (7, "task")
    ctrl._name = lambda h: "parent"
    ctrl._emit = lambda h, kind, data, turn: h.events.append(SimpleNamespace(type=kind, data=data))
    return ctrl, handle


def owned(tmp_path):
    main, registry, service, record = active_session(tmp_path)
    root = SimpleNamespace(kind="worktree", path=record.path, worktree_id=record.child_id)
    ctrl, handle = controller(main, registry, root)
    return ctrl, handle, SessionSubagentService(ctrl, "commit-session", "turn-one"), root


def test_owned_child_sees_parent_leftovers_and_merges_its_saved_commit(tmp_path):
    ctrl, handle, service, root = owned(tmp_path)
    (root.path / "parent.txt").write_text("parent uncommitted\n")
    child = service.create(root.path, "commit-session/sub/1", root=ctrl.registry)
    assert (child.path / "parent.txt").read_text() == "parent uncommitted\n"
    (child.path / "child.txt").write_text("child\n")
    result = service.mark_finished(child.session_id, {"status": "completed"}, root=ctrl.registry)
    assert result["merge"]["state"] == "merged"
    assert result["committed"]
    assert (root.path / "child.txt").read_text() == "child\n"
    assert git(root.path, "status", "--porcelain") == ""
    assert not (ctrl.runtime.workspace / "child.txt").exists()


def test_shared_child_sees_dirty_files_and_applies_without_moving_head_or_index(tmp_path):
    main = repository(tmp_path / "shared")
    registry = tmp_path / "registry"
    root = SimpleNamespace(kind="main", path=main)
    ctrl, _ = controller(main, registry, root)
    service = SessionSubagentService(ctrl, "commit-session", "turn-one")
    (main / "tracked.txt").write_text("user uncommitted\n")
    (main / "staged.txt").write_text("user staged\n")
    git(main, "add", "staged.txt")
    old_head = git(main, "rev-parse", "HEAD")
    index = main / ".git" / "index"
    before = index.read_bytes()
    child = service.create(main, "commit-session/sub/1", root=registry)
    assert (child.path / "tracked.txt").read_text() == "user uncommitted\n"
    (child.path / "tracked.txt").write_text("user uncommitted\nchild addition\n")
    result = service.mark_finished(child.session_id, {"status": "completed"}, root=registry)
    assert result["merge"]["state"] == "merged"
    assert git(main, "rev-parse", "HEAD") == old_head
    assert index.read_bytes() == before
    assert (main / "tracked.txt").read_text() == "user uncommitted\nchild addition\n"


def test_failed_child_saved_but_not_integrated(tmp_path):
    ctrl, _, service, root = owned(tmp_path)
    child = service.create(root.path, "commit-session/sub/1", root=ctrl.registry)
    (child.path / "partial.txt").write_text("partial\n")
    result = service.mark_finished(child.session_id, {"status": "failed"}, root=ctrl.registry)
    assert result["merge"]["state"] == "kept"
    assert result["committed"]
    assert child.path.exists() and not (root.path / "partial.txt").exists()


def test_two_writers_merge_serially_and_conflicts_preserve_second_child(tmp_path):
    ctrl, _, service, root = owned(tmp_path)
    a = service.create(root.path, "commit-session/sub/1", root=ctrl.registry)
    b = service.create(root.path, "commit-session/sub/2", root=ctrl.registry)
    (a.path / "a.txt").write_text("a\n")
    (b.path / "b.txt").write_text("b\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda row: service.mark_finished(row.session_id, {"status": "completed"}, root=ctrl.registry), (a,b)))
    assert all(result["merge"]["state"] == "merged" for result in results)
    assert (root.path / "a.txt").exists() and (root.path / "b.txt").exists()
    c = service.create(root.path, "commit-session/sub/3", root=ctrl.registry)
    d = service.create(root.path, "commit-session/sub/4", root=ctrl.registry)
    (c.path / "tracked.txt").write_text("c\n")
    (d.path / "tracked.txt").write_text("d\n")
    service.mark_finished(c.session_id, {"status": "completed"}, root=ctrl.registry)
    result = service.mark_finished(d.session_id, {"status": "completed"}, root=ctrl.registry)
    assert result["merge"]["state"] == "conflicts"
    assert (root.path / "tracked.txt").read_text() == "c\n"
    assert d.path.exists()
    assert not git(root.path, "status", "--porcelain")


def test_shared_parent_changed_after_spawn_is_not_overwritten(tmp_path):
    main = repository(tmp_path / "shared")
    root = SimpleNamespace(kind="main", path=main)
    ctrl, _ = controller(main, tmp_path / "registry", root)
    service = SessionSubagentService(ctrl, "commit-session")
    child = service.create(main, "commit-session/sub/1", root=ctrl.registry)
    (child.path / "tracked.txt").write_text("child\n")
    (main / "tracked.txt").write_text("parent concurrent edit\n")
    result = service.mark_finished(child.session_id, {"status": "completed"}, root=ctrl.registry)
    assert result["merge"]["state"] == "conflicts"
    assert (main / "tracked.txt").read_text() == "parent concurrent edit\n"


@pytest.mark.parametrize("sessions", [True, False])
async def test_real_runtime_auto_writer_isolated_saved_and_integrated(tmp_path, sessions):
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
    from test_worktree_sessions_runtime import runtime, drain
    main = repository(tmp_path / "real-project")
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "write child file", "subagent_type": "task", "tools": ["write"]})),
        tool_response(("child-write", "write", {"path": "child.txt", "content": "child saved\n"})),
        text_response("child done"), text_response("root done"))
    rt = runtime(main, tmp_path, provider, sessions=sessions)
    try:
        handle = rt.session("auto-child")
        events = await drain(handle, "delegate")
        assert events[-1].type == "turn.completed", events[-1]
        spawned = next(e for e in handle.events if e.type == "agent.spawned")
        assert spawned.data["worktree"], spawned.data
        child_path = spawned.data["worktree"]["path"]
        assert child_path != str(rt.session_root(handle.id).path)
        completed = next(e for e in handle.events if e.type == "agent.completed")
        manager = rt._child_sessions.manager
        real_child = manager.store.resolve_child_id(spawned.data["id"])
        child_events = manager.open(real_child, create=False).events
        assert completed.data["worktree"]["merge"]["state"] == "merged", str([(e.type, e.data) for e in child_events if e.type.startswith("tool.") or e.type == "turn.failed"])
        assert (rt.session_root(handle.id).path / "child.txt").read_text() == "child saved\n"
        assert (main / "child.txt").exists() is (not sessions)
        if not sessions:
            saved_rows = [e for e in handle.events if e.type == "checkpoint.captured"]
            assert saved_rows, [(e.type, e.data) for e in handle.events if e.type.startswith("checkpoint.") or e.type == "worktree.commit_skipped"]
            assert saved_rows[0].data["files"] == ["child.txt"]
    finally:
        await rt.aclose()


async def test_real_runtime_explicit_false_shares_session_checkout(tmp_path):
    from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
    from test_worktree_sessions_runtime import runtime, drain
    main = repository(tmp_path / "shared-child-project")
    provider = ScriptedProvider(
        tool_response(("spawn", "subagent", {"prompt": "write child file", "subagent_type": "task", "tools": ["write"], "worktree": False})),
        tool_response(("child-write", "write", {"path": "shared.txt", "content": "shared\n"})),
        text_response("child done"), text_response("root done"))
    rt = runtime(main, tmp_path, provider, sessions=False)
    try:
        handle = rt.session("shared-child")
        events = await drain(handle, "delegate")
        assert events[-1].type == "turn.completed", events[-1]
        spawned = next(e for e in handle.events if e.type == "agent.spawned")
        assert spawned.data["worktree"] is None
        assert (main / "shared.txt").read_text() == "shared\n"
    finally:
        await rt.aclose()
