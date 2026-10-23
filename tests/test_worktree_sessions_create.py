from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.agents.worktrees import WorktreeError, WorktreeService
from nexus.agents.session_worktrees import session_record_id


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Session Worktree Test")
    git(path, "config", "user.email", "session-worktree@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    return path


def test_create_session_pending_activation_and_dispatch(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon" / "worktree-service"
    git(parent, "switch", "-c", "release")
    (parent / "release.txt").write_text("release-only\n", encoding="utf-8")
    git(parent, "add", "release.txt")
    git(parent, "commit", "-qm", "release base")
    git(parent, "switch", "main")
    (parent / "dirty.txt").write_text("leave main untouched\n", encoding="utf-8")
    before = git(parent, "status", "--porcelain", "--untracked-files=all")
    service = WorktreeService()

    pending = service.create_session(parent, "session-1", "calm-otter", root=root, base="release")

    assert pending.lifecycle == "pending"
    assert pending.child_id == session_record_id("session-1")
    assert pending.kind == "session"
    assert pending.session_id == "session-1"
    assert pending.base_ref == "release"
    assert pending.path.is_dir()
    assert git(pending.path, "branch", "--show-current") == pending.branch
    assert git(pending.path, "rev-parse", "HEAD") == pending.base_commit
    assert (pending.path / "release.txt").read_text(encoding="utf-8") == "release-only\n"
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == before

    active = service.activate_session(pending.child_id, root=root)
    assert active.lifecycle == "active"
    assert service.get(pending.child_id, root=root) == active
    assert service.inspect(pending.child_id, root=root) == active
    assert active in service.list(root=root)


def test_recovery_activates_logged_pending_and_removes_unlogged_pending(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()
    created = service.create_session(parent, "created", "blue-fox", root=root)
    orphan = service.create_session(parent, "orphan", "red-bird", root=root)
    promotion = service.create_session(parent, "promotion", "silver-owl", root=root)

    recovered = service.recover_sessions(
        root=root, created_ids={"created"}, pending_ids={"promotion"}
    )

    by_id = {record.session_id: record for record in recovered}
    assert by_id["created"].lifecycle == "active"
    assert by_id["created"].path.is_dir()
    assert by_id["orphan"].lifecycle == "removed"
    assert by_id["promotion"].lifecycle == "pending"
    assert not orphan.path.exists()
    assert promotion.path.is_dir()
    ref_check = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{orphan.branch}"],
        cwd=parent, stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
    )
    assert ref_check.returncode == 1
    assert service.get(created.child_id, root=root).lifecycle == "active"
    assert service.abort_session(promotion.child_id, root=root).lifecycle == "removed"


def test_recreate_missing_active_session_checkout(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()
    pending = service.create_session(parent, "recreate", "green-badger", root=root)
    record = service.activate_session(pending.child_id, root=root)
    git(parent, "worktree", "remove", "--force", str(record.path))

    assert service.get(record.child_id, root=root).lifecycle == "active"
    rebuilt = service.recreate_session(record.child_id, root=root)

    assert rebuilt.path.is_dir()
    assert git(rebuilt.path, "branch", "--show-current") == rebuilt.branch


def test_creation_failure_leaves_no_record_or_branch_when_cleanup_is_exact(tmp_path: Path, monkeypatch) -> None:
    from nexus.agents import session_worktrees

    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    original = session_worktrees._git
    seen = []

    def fail_add(cwd, *args, **kwargs):
        if args[:2] == ("worktree", "add"):
            record_files = tuple((root / "records").glob("*.json"))
            seen.append(bool(record_files))
            raise WorktreeError("injected add failure")
        return original(cwd, *args, **kwargs)

    monkeypatch.setattr(session_worktrees, "_git", fail_add)
    with pytest.raises(WorktreeError, match="injected add failure"):
        WorktreeService().create_session(parent, "failed", "purple-panda", root=root)

    assert seen == [True]
    payloads = [p for p in (root / "records").glob("*.json") if p.is_file()]
    assert payloads == []


def test_create_session_refuses_when_worktree_limit_is_reached(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()
    existing = service.create_session(parent, "first", "blue-fox", root=root, max_worktrees=1)

    with pytest.raises(WorktreeError, match="maximum session worktrees"):
        service.create_session(parent, "second", "red-bird", root=root, max_worktrees=1)

    assert existing.path.is_dir()
    assert service.list(root=root) == (existing,)


def test_missing_active_checkout_does_not_consume_worktree_slot(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()
    first = service.create_session(parent, "first", "blue-fox", root=root, max_worktrees=1)
    first = service.activate_session(first.child_id, root=root)
    git(parent, "worktree", "remove", "--force", str(first.path))

    second = service.create_session(parent, "second", "red-bird", root=root, max_worktrees=1)

    assert not first.path.exists()
    assert second.path.is_dir()
    assert service.get(first.child_id, root=root).lifecycle == "active"


def test_session_record_timestamps_advance_on_activation_and_detach(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = WorktreeService()

    pending = service.create_session(parent, "timestamps", "blue-fox", root=root)
    assert isinstance(pending.created_at, float)
    assert isinstance(pending.updated_at, float)
    assert pending.updated_at == pending.created_at
    assert pending.parent_record == ""

    active = service.activate_session(pending.child_id, root=root)
    assert active.updated_at > pending.updated_at
    detached = service.detach_session(active.child_id, root=root)
    assert detached.updated_at > active.updated_at


def test_status_counts_streams_and_clips_without_git_output_cap(tmp_path: Path, monkeypatch) -> None:
    from nexus.agents import session_worktrees

    parent = repository(tmp_path / "parent")
    (parent / "tracked.txt").write_text("modified\n", encoding="utf-8")
    for index in range(4):
        (parent / f"untracked-{index}.txt").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(session_worktrees, "_MAX_OUTPUT", 8)

    counts = session_worktrees.status_counts(parent, max_entries=2)

    assert counts == {
        "modified": 1, "added": 0, "deleted": 0, "untracked": 1,
        "conflicted": 0, "clipped": 1,
    }


@pytest.mark.parametrize("setting,value", [
    ("core.worktree", "/tmp/not-the-checkout"),
    ("core.sparseCheckout", "true"),
    ("extensions.partialClone", "origin"),
])
def test_create_session_refuses_unsupported_repository_layout(
    tmp_path: Path, setting: str, value: str,
) -> None:
    parent = repository(tmp_path / setting.replace(".", "-"))
    git(parent, "config", "--local", setting, value)

    with pytest.raises(WorktreeError, match="unsupported repository layout"):
        WorktreeService().create_session(parent, f"unsupported-{setting}", "blue-fox", root=tmp_path / "daemon")
