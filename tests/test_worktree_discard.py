from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus.agents import worktrees


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, check=True, stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "Discard Test")
    git(path, "config", "user.email", "discard@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial")
    return path


def setup(tmp_path: Path, *, live: bool = False):
    parent = repository(tmp_path / "parent")
    root = tmp_path / "service"
    service = worktrees.WorktreeService(runtime_ownership=lambda _child: live)
    record = service.create(parent, "child", root=root)
    return parent, root, service, record


def reviewed(service: worktrees.WorktreeService, record: worktrees.WorktreeRecord, root: Path):
    service.mark_finished(record.child_id, {"status": "completed"}, root=root)
    page = service.review(record.child_id, root=root)
    service.acknowledge(record.child_id, page.review_id, page.digest, root=root)
    return page


def test_discard_default_refuses_unreviewed_and_dirty_tree(tmp_path: Path):
    _parent, root, service, record = setup(tmp_path)
    service.mark_finished("child", {"status": "completed"}, root=root)
    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        service.discard("child", root=root)
    assert record.path.is_dir()

    page = reviewed(service, record, root)
    (record.path / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="changed or dirty"):
        service.discard("child", root=root, acknowledged_review_id=page.review_id)
    assert record.path.is_dir()


def test_discard_default_detects_staged_untracked_and_ignored_files(tmp_path: Path):
    for kind in ("staged", "untracked", "ignored"):
        case = tmp_path / kind
        _parent, root, service, record = setup(case)
        page = reviewed(service, record, root)
        if kind == "staged":
            (record.path / "tracked.txt").write_text("staged\n", encoding="utf-8")
            git(record.path, "add", "tracked.txt")
        else:
            name = "ignored.txt" if kind == "ignored" else "new.txt"
            (record.path / name).write_text("untracked\n", encoding="utf-8")
        with pytest.raises(worktrees.WorktreeError, match="changed or dirty"):
            service.discard("child", root=root, acknowledged_review_id=page.review_id)
        assert record.path.is_dir()


def test_discard_refuses_stale_review_acknowledgment(tmp_path: Path):
    _parent, root, service, record = setup(tmp_path)
    page = reviewed(service, record, root)
    replacement = service.review("child", root=root)
    assert replacement.review_id != page.review_id

    with pytest.raises(worktrees.WorktreeError, match="current acknowledged review"):
        service.discard("child", root=root, acknowledged_review_id=page.review_id)
    assert record.path.is_dir()


def test_force_still_refuses_live_child(tmp_path: Path):
    _parent, root, service, _record = setup(tmp_path, live=True)
    with pytest.raises(worktrees.WorktreeError, match="live or unknown"):
        service.discard("child", root=root, force=True)


def test_force_still_refuses_active_lifecycle_with_confirmed_absence(tmp_path: Path):
    _parent, root, service, _record = setup(tmp_path)
    with pytest.raises(worktrees.WorktreeError, match="active child"):
        service.discard("child", root=root, force=True)


def test_force_removes_only_owned_worktree_and_exact_branch(tmp_path: Path):
    parent, root, service, record = setup(tmp_path)
    foreign_path = tmp_path / "foreign"
    foreign_path.mkdir()
    sentinel = foreign_path / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    unrelated_branch = "user/keep"
    git(parent, "branch", unrelated_branch)
    service.mark_finished("child", {"status": "completed"}, root=root)
    (record.path / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    discarded = service.discard("child", root=root, force=True)

    assert discarded.lifecycle == "discarded"
    assert not record.path.exists()
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{record.branch}"],
        cwd=parent, check=False, stdin=subprocess.DEVNULL,
    ).returncode == 1
    assert git(parent, "show-ref", "--verify", "--quiet", f"refs/heads/{unrelated_branch}") == ""
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert (parent / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == ""


def test_discard_rejects_tampered_record_and_foreign_worktree(tmp_path: Path):
    _parent, root, service, record = setup(tmp_path)
    record_path = root / "records" / worktrees._record_name("child")
    envelope = json.loads(record_path.read_text(encoding="utf-8"))
    envelope["record"]["path"] = str(tmp_path / "outside")
    record_path.write_text(json.dumps(envelope), encoding="utf-8")
    record_path.chmod(0o600)

    with pytest.raises(worktrees.WorktreeError, match="authentication"):
        service.discard("child", root=root, force=True)
    assert record.path.is_dir()


def test_discard_rejects_foreign_checkout_at_recorded_path(tmp_path: Path):
    parent, root, service, record = setup(tmp_path)
    git(parent, "worktree", "remove", "--force", str(record.path))
    foreign = repository(record.path)

    with pytest.raises(worktrees.WorktreeError, match="different Git repository|register"):
        service.discard("child", root=root, force=True)
    assert (foreign / "tracked.txt").read_text(encoding="utf-8") == "base\n"


def test_discard_refuses_changed_branch_ref_after_worktree_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    parent, root, service, record = setup(tmp_path)
    service.mark_finished("child", {"status": "completed"}, root=root)
    tree = git(parent, "rev-parse", "HEAD^{tree}")
    new_oid = subprocess.run(
        ["git", "commit-tree", tree, "-p", git(parent, "rev-parse", "HEAD"), "-m", "branch moved"],
        cwd=parent, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True,
    ).stdout.strip()
    real_git = worktrees._git
    changed = False

    def change_branch_before_delete(cwd: Path, *args: str, check: bool = True, observational: bool = True):
        nonlocal changed
        if args[:2] == ("update-ref", "-d") and not changed:
            git(parent, "update-ref", f"refs/heads/{record.branch}", new_oid)
            changed = True
        return real_git(cwd, *args, check=check, observational=observational)

    monkeypatch.setattr(worktrees, "_git", change_branch_before_delete)
    with pytest.raises(worktrees.WorktreeError, match="cleanup is pending"):
        service.discard("child", root=root, force=True)
    assert not record.path.exists()
    assert git(parent, "rev-parse", f"refs/heads/{record.branch}") == new_oid
    assert service.get("child", root=root).lifecycle == "cleanup_pending"


def test_post_remove_failure_is_journaled_and_retry_finishes_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    parent, root, service, record = setup(tmp_path)
    service.mark_finished("child", {"status": "completed"}, root=root)
    real_replace = worktrees._replace_record
    failed = False

    def fail_after_remove(path, updated, key):
        nonlocal failed
        if updated.discard_state == "worktree_removed" and not failed:
            failed = True
            raise OSError("injected lifecycle publication failure")
        return real_replace(path, updated, key)

    monkeypatch.setattr(worktrees, "_replace_record", fail_after_remove)
    with pytest.raises(worktrees.WorktreeError, match="cleanup is pending"):
        service.discard("child", root=root, force=True)
    assert not record.path.exists()
    pending = worktrees._read_record(
        root / "records" / worktrees._record_name("child"), "child",
        (root / ".ownership-key").read_bytes(),
    )
    assert pending.lifecycle == "cleanup_pending"

    monkeypatch.setattr(worktrees, "_replace_record", real_replace)
    result = service.discard("child", root=root, force=False)
    assert result.lifecycle == "discarded"
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{record.branch}"],
        cwd=parent, check=False, stdin=subprocess.DEVNULL,
    ).returncode == 1


def test_cancellation_before_intent_changes_nothing_and_interruption_after_intent_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _parent, root, service, record = setup(tmp_path)
    service.mark_finished("child", {"status": "completed"}, root=root)

    class CancelBefore:
        def raise_if_cancelled(self):
            raise RuntimeError("cancelled before cleanup")

    with pytest.raises(RuntimeError, match="cancelled"):
        service.discard("child", root=root, force=True, cancel=CancelBefore())
    assert record.path.is_dir()
    assert service.get("child", root=root).lifecycle == "finalized"

    real_git = worktrees._git
    interrupted = False

    def interrupt_after_worktree_remove(cwd: Path, *args: str, check: bool = True, observational: bool = True):
        nonlocal interrupted
        result = real_git(cwd, *args, check=check, observational=observational)
        if args[:2] == ("worktree", "remove") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("interrupted after Git removed the worktree")
        return result

    monkeypatch.setattr(worktrees, "_git", interrupt_after_worktree_remove)
    result = service.discard("child", root=root, force=True)
    assert result.lifecycle == "discarded"
    assert not record.path.exists()
