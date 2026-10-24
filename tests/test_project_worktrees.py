"""Project-level worktrees shared by many sessions (agent-managed-worktrees 3.4/3.5)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.agents import session_worktrees as sw
from nexus.agents.worktrees import WorktreeError, WorktreeService


def git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, check=True, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Project Worktree Test")
    git(path, "config", "user.email", "project-worktree@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture
def project(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()
    pending = service.create_session(parent, "creator", "t", root=root, exact_name="shared")
    return parent, root, service, service.activate_session(pending.child_id, root=root)


def commit(service, record, root, session, turn, paths=None, message="work"):
    return service.commit_session(record.child_id, root=root, message=message, session_name=session,
                                  turn_seq=turn, agent_id="root", paths=paths, session_id=session)


def test_record_is_project_level_and_any_session_can_use_it(project) -> None:
    parent, root, service, record = project
    assert record.child_id == "wt-shared" and record.branch == "nexus/wt/shared"
    assert record.created_by == "creator"
    # No owner check: a session that never created it can get, list and commit.
    assert service.get_worktree(record.child_id, root=root, parent_workspace=parent) == record
    assert service.find_worktree(parent, "shared", root=root) == record
    assert service.list_worktrees(parent, root=root) == (record,)
    (record.path / "a.txt").write_text("a\n", encoding="utf-8")
    result = commit(service, record, root, "other-session", 1)
    assert result["kind"] == "committed" and result["session"] == "other-session"
    assert result["worktree_id"] == record.child_id
    assert "(other-session)" in git(record.path, "show", "-s", "--format=%B", "HEAD")
    assert service.session_commits(record.child_id, root=root)[0]["session"] == "other-session"


def test_other_project_record_is_refused(project, tmp_path: Path) -> None:
    _parent, root, service, record = project
    other = repository(tmp_path / "other")
    with pytest.raises(WorktreeError, match="different project"):
        service.get_worktree(record.child_id, root=root, parent_workspace=other)
    assert service.list_worktrees(other, root=root) == ()


def test_list_hides_inactive_and_recreate_restores_checkout(project) -> None:
    parent, root, service, record = project
    second = service.create_session(parent, "creator", "t", root=root, exact_name="second")
    assert service.list_worktrees(parent, root=root) == (record,)
    assert {r.child_id for r in service.list_worktrees(parent, root=root, include_inactive=True)} == {
        "wt-shared", "wt-second"}
    git(parent, "worktree", "remove", "--force", str(record.path))
    rebuilt = service.recreate_worktree(record.child_id, root=root, parent_workspace=parent)
    assert rebuilt.path.is_dir() and git(rebuilt.path, "branch", "--show-current") == "nexus/wt/shared"
    assert second.lifecycle == "pending"


def test_shared_commits_take_only_each_sessions_paths(project) -> None:
    _parent, root, service, record = project
    (record.path / "mine.txt").write_text("mine\n", encoding="utf-8")
    (record.path / "theirs.txt").write_text("half finished\n", encoding="utf-8")
    first = commit(service, record, root, "s1", 1, paths=["mine.txt"])
    assert first["files"] == ["mine.txt"]
    assert git(record.path, "status", "--porcelain") == "?? theirs.txt"
    second = commit(service, record, root, "s2", 2, paths=["theirs.txt"])
    assert second["files"] == ["theirs.txt"]
    assert commit(service, record, root, "s2", 3, paths=[])["kind"] == "unchanged"


def test_lost_compare_and_swap_rebuilds_on_the_new_head(project, monkeypatch) -> None:
    parent, root, service, record = project
    (record.path / "mine.txt").write_text("mine\n", encoding="utf-8")
    original = sw._git
    raced = []

    def racing(cwd, *args, **kwargs):
        if args[:1] == ("update-ref",) and not raced and args[1].startswith("refs/heads/"):
            raced.append(True)
            # Another session lands a commit between our read of HEAD and the swap.
            (record.path / "theirs.txt").write_text("theirs\n", encoding="utf-8")
            git(record.path, "add", "theirs.txt")
            git(record.path, "commit", "-qm", "theirs")
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(sw, "_git", racing)
    result = commit(service, record, root, "s1", 1, paths=["mine.txt"])
    assert raced and result["kind"] == "committed"
    log = git(record.path, "log", "--format=%s", "-3").splitlines()
    assert log[1] == "theirs"  # our commit sits on top of the commit that won the race
    assert git(parent, "rev-parse", f"refs/heads/{record.branch}") == result["sha"]


def test_lost_race_gives_up_after_bounded_attempts(project, monkeypatch) -> None:
    _parent, root, service, record = project
    (record.path / "mine.txt").write_text("mine\n", encoding="utf-8")
    original = sw._git
    attempts = []

    def always_lose(cwd, *args, **kwargs):
        if args[:1] == ("update-ref",) and args[1].startswith("refs/heads/"):
            attempts.append(1)
            name = f"race{len(attempts)}.txt"
            (record.path / name).write_text("x\n", encoding="utf-8")
            git(record.path, "add", name)
            git(record.path, "commit", "-qm", name)
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(sw, "_git", always_lose)
    result = commit(service, record, root, "s1", 1, paths=["mine.txt"])
    assert result["kind"] == "skipped" and len(attempts) == sw._CAS_ATTEMPTS


def test_turn_commits_are_pinned_and_survive_a_squash(project) -> None:
    parent, root, service, record = project
    (record.path / "a.txt").write_text("a\n", encoding="utf-8")
    first = commit(service, record, root, "s1", 1)
    (record.path / "b.txt").write_text("b\n", encoding="utf-8")
    second = commit(service, record, root, "s1", 2)
    assert first["turn_ref"] == "refs/nexus/turns/s1/1"
    assert sw.turn_ref_commit(parent, "s1", 2) == second["sha"]
    # Agent squashes the branch and fast-forwards the base: turn commits unreachable.
    git(record.path, "reset", "--soft", record.base_commit)
    git(record.path, "commit", "-qm", "squashed")
    git(parent, "merge", "--ff-only", record.branch)
    merge = sw.merge_state(sw.get_session(record.child_id, root=root))
    assert merge["merged"] and merge["unmerged_commits"] == 0
    assert subprocess.run(["git", "merge-base", "--is-ancestor", second["sha"], "HEAD"], cwd=parent).returncode == 1
    assert git(parent, "cat-file", "-t", first["sha"]) == "commit"
    assert git(parent, "rev-parse", "refs/nexus/turns/s1/1^{commit}") == first["sha"]


def test_turn_refs_are_bounded_and_deleted_with_the_session(project) -> None:
    parent, _root, _service, _record = project
    head = git(parent, "rev-parse", "HEAD")
    for turn in range(6):
        sw.pin_turn(parent, "s/with odd:chars", turn, head, max_refs=3)
    assert sw._turn_ref_turns(parent, "s/with odd:chars") == [3, 4, 5]
    sw.pin_turn(parent, "keep", 0, head)
    assert sw.delete_turn_refs(parent, "s/with odd:chars") == 3
    assert sw._turn_ref_turns(parent, "s/with odd:chars") == []
    assert sw.turn_ref_commit(parent, "keep", 0) == head


def _cleanup_controller(parent, root, sessions):
    from types import SimpleNamespace

    manager = SimpleNamespace(occupants=lambda _record_id: list(sessions))
    return SimpleNamespace(runtime=SimpleNamespace(workspace=parent, _session_worktrees=manager), registry=root)


def test_auto_cleanup_removes_only_merged_clean_checkout_and_keeps_record_and_branch(project) -> None:
    from nexus.worktree_cleanup import auto_cleanup

    parent, root, service, record = project
    (record.path / "a.txt").write_text("a\n", encoding="utf-8")
    commit(service, record, root, "s1", 1)
    controller = _cleanup_controller(parent, root, ["s1", "s2"])

    # Another session is still in it.
    assert auto_cleanup(controller, record.child_id, leaving="s1")["state"] == "kept"
    controller = _cleanup_controller(parent, root, [])
    # Unmerged work is never removed.
    unmerged = auto_cleanup(controller, record.child_id, leaving="s1")
    assert unmerged["state"] == "kept" and "Unmerged" in unmerged["reason"] and record.path.is_dir()

    git(parent, "merge", "--ff-only", record.branch)
    (record.path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    assert auto_cleanup(controller, record.child_id, leaving="s1")["state"] == "kept"  # dirty
    (record.path / "dirty.txt").unlink()

    removed = auto_cleanup(controller, record.child_id, leaving="s1")
    assert removed["state"] == "removed" and not record.path.exists()
    assert git(parent, "branch", "--list", record.branch)
    kept = service.get(record.child_id, root=root)
    assert kept.lifecycle == "active"
    assert service.recreate_worktree(record.child_id, root=root, parent_workspace=parent).path.is_dir()


def test_merge_state_unmerged_and_status_fields(project) -> None:
    parent, root, service, record = project
    (record.path / "a.txt").write_text("a\n", encoding="utf-8")
    commit(service, record, root, "s1", 1)
    state = sw.merge_state(record)
    assert state["merged"] is False and state["unmerged_commits"] == 1 and state["has_work"]
    status = service.session_status(record.child_id, root=root)
    assert status["merged"] is False and status["unmerged_commits"] == 1
    git(parent, "merge", "--ff-only", record.branch)
    assert service.session_status(record.child_id, root=root)["merged"] is True
