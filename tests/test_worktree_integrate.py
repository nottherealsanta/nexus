from __future__ import annotations

import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from nexus.agents import worktrees
from nexus.agents.worktree_integrate import (
    WorktreeIntegrationError,
    integrate_review,
    recover_transactions,
)
from nexus.agents.worktree_review import ReviewError, build_review, load_review


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, stdin=subprocess.DEVNULL, check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Integration Test")
    git(path, "config", "user.email", "integration@example.invalid")
    (path / "tracked.txt").write_bytes(b"parent\n")
    (path / "delete.txt").write_bytes(b"delete me\n")
    (path / "script.sh").write_bytes(b"#!/bin/sh\necho base\n")
    (path / "script.sh").chmod(0o755)
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def setup(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    service_root = tmp_path / "service"
    service = worktrees.WorktreeService(runtime_ownership=lambda _child: True)
    record = service.create(parent, "integration-child", root=service_root)
    return parent, service_root, service, record


def make_review(tmp_path: Path):
    parent, service_root, service, record = setup(tmp_path)
    child = record.path
    # Committed, staged, unstaged, and untracked data all resolve to the final
    # checkout snapshot represented by the review.
    (child / "tracked.txt").write_bytes(b"committed\n")
    git(child, "add", "tracked.txt")
    git(child, "commit", "-qm", "child commit")
    (child / "tracked.txt").write_bytes(b"final unstaged\n")
    (child / "staged.txt").write_bytes(b"staged\n")
    git(child, "add", "staged.txt")
    (child / "new.bin").write_bytes(b"\x00\xfffrozen binary\n")
    (child / "delete.txt").unlink()
    (child / "script.sh").chmod(0o644)
    record = service.mark_finished(record.child_id, {"status": "completed"}, root=service_root)
    artifact_root = service_root / "reviews"
    artifact = build_review(record, artifact_root)
    service.review(record.child_id, artifact.review_id, root=service_root)
    record = service.acknowledge(record.child_id, artifact.review_id, artifact.digest, root=service_root)
    artifact = load_review(record, artifact_root, artifact.review_id)
    return parent, service_root, record, artifact


def test_integrates_frozen_mixed_review_unstaged_and_leaves_child_unchanged(tmp_path: Path):
    parent, _, record, artifact = make_review(tmp_path)
    child_state = (git(record.path, "rev-parse", "HEAD"), git(record.path, "status", "--porcelain", "-z"))
    index = (parent / ".git" / "index").read_bytes()

    result = integrate_review(record, artifact, tmp_path / "transactions")

    assert result.status == "integrated"
    assert set(result.changed_paths) == {"tracked.txt", "staged.txt", "new.bin", "delete.txt", "script.sh"}
    assert (parent / "tracked.txt").read_bytes() == b"final unstaged\n"
    assert (parent / "staged.txt").read_bytes() == b"staged\n"
    assert (parent / "new.bin").read_bytes() == b"\x00\xfffrozen binary\n"
    assert not (parent / "delete.txt").exists()
    assert (parent / "script.sh").stat().st_mode & 0o111 == 0
    assert (parent / ".git" / "index").read_bytes() == index
    assert git(parent, "rev-parse", "HEAD") == record.base_commit
    assert set(git(parent, "status", "--porcelain").splitlines())
    assert child_state == (git(record.path, "rev-parse", "HEAD"), git(record.path, "status", "--porcelain", "-z"))


@pytest.mark.parametrize("change", ["dirty", "advanced"])
def test_parent_dirty_or_advanced_fails_before_mutation(tmp_path: Path, change: str):
    parent, _, record, artifact = make_review(tmp_path)
    if change == "dirty":
        (parent / "untracked").write_text("keep", encoding="utf-8")
    else:
        (parent / "advance").write_text("advance", encoding="utf-8")
        git(parent, "add", "advance")
        git(parent, "commit", "-qm", "advance")
    before = {path.name: path.read_bytes() for path in parent.iterdir() if path.is_file() and path.name != "index"}
    with pytest.raises(worktrees.WorktreeError):
        integrate_review(record, artifact, tmp_path / "transactions")
    assert {path.name: path.read_bytes() for path in parent.iterdir() if path.is_file() and path.name != "index"} == before


def test_unacknowledged_or_stale_review_is_refused(tmp_path: Path):
    parent, _, record, artifact = make_review(tmp_path)
    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        integrate_review(replace(record, acknowledged_review_id=None, acknowledged_digest=None), artifact, tmp_path / "tx")
    (record.path / "tracked.txt").write_bytes(b"changed after review")
    with pytest.raises(ReviewError, match="stale"):
        integrate_review(record, artifact, tmp_path / "tx")
    assert (parent / "tracked.txt").read_bytes() == b"parent\n"


def test_service_integrate_requires_explicit_current_ack_and_digest(tmp_path: Path):
    _parent, service_root, service, record = setup(tmp_path)
    (record.path / "new.txt").write_text("frozen\n", encoding="utf-8")
    service.mark_finished(record.child_id, {"status": "completed"}, root=service_root)
    page = service.review(record.child_id, root=service_root)

    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        service.integrate(record.child_id, page.review_id, page.digest, root=service_root)
    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        service.integrate(record.child_id, page.review_id, "0" * 64, root=service_root)

    service.acknowledge(record.child_id, page.review_id, page.digest, root=service_root)
    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        service.integrate(record.child_id, page.review_id, "0" * 64, root=service_root)

    # Building a replacement review invalidates the old acknowledgment.
    newer = service.review(record.child_id, root=service_root)
    with pytest.raises(worktrees.WorktreeError, match="acknowledged"):
        service.integrate(record.child_id, page.review_id, page.digest, root=service_root)
    assert newer.review_id != page.review_id


def test_service_integrate_persists_lifecycle_and_preserves_index_and_child(tmp_path: Path):
    parent, service_root, service, record = setup(tmp_path)
    (record.path / "new.txt").write_text("frozen\n", encoding="utf-8")
    service.mark_finished(record.child_id, {"status": "completed"}, root=service_root)
    page = service.review(record.child_id, root=service_root)
    service.acknowledge(record.child_id, page.review_id, page.digest, root=service_root)
    before_index = (parent / ".git" / "index").read_bytes()
    before_child = (record.path / "new.txt").read_bytes()

    result = service.integrate(record.child_id, page.review_id, page.digest, root=service_root)

    assert result.status == "integrated"
    assert (parent / "new.txt").read_bytes() == before_child
    assert (parent / ".git" / "index").read_bytes() == before_index
    assert git(parent, "rev-parse", "HEAD") == record.base_commit
    assert record.path.is_dir()
    assert (record.path / "new.txt").read_bytes() == before_child
    persisted = worktrees.WorktreeService().get(record.child_id, root=service_root)
    assert persisted.lifecycle == "integrated"
    assert persisted.integrated_review_id == page.review_id
    assert persisted.integrated_digest == page.digest
    assert persisted.integrated_at
    assert persisted.integration_transaction_id == result.transaction_id
    assert service.recover_pending(record.child_id, root=service_root) == persisted


def test_service_integrate_serializes_review_until_transaction_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    _, service_root, service, record = setup(tmp_path)
    (record.path / "new.txt").write_text("frozen\n", encoding="utf-8")
    service.mark_finished(record.child_id, {"status": "completed"}, root=service_root)
    page = service.review(record.child_id, root=service_root)
    service.acknowledge(record.child_id, page.review_id, page.digest, root=service_root)

    entered_apply = threading.Event()
    release_apply = threading.Event()
    real_apply = module._apply_entry

    def blocked_apply(root_fd, entry, data):
        entered_apply.set()
        assert release_apply.wait(timeout=10)
        return real_apply(root_fd, entry, data)

    monkeypatch.setattr(module, "_apply_entry", blocked_apply)
    with ThreadPoolExecutor(max_workers=3) as pool:
        integration = pool.submit(
            service.integrate, record.child_id, page.review_id, page.digest, root=service_root
        )
        assert entered_apply.wait(timeout=10)
        reviewing = pool.submit(service.review, record.child_id, root=service_root)
        acknowledging = pool.submit(
            service.acknowledge,
            record.child_id,
            page.review_id,
            page.digest,
            root=service_root,
        )
        assert not reviewing.done() and not acknowledging.done()
        release_apply.set()
        result = integration.result(timeout=10)
        with pytest.raises(worktrees.WorktreeError, match="finalized"):
            reviewing.result(timeout=10)
        with pytest.raises(worktrees.WorktreeError, match="acknowledge"):
            acknowledging.result(timeout=10)

    assert result.status == "integrated"
    assert service.get(record.child_id, root=service_root).lifecycle == "integrated"


def test_integrated_journal_lifecycle_write_failure_requires_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    parent, service_root, service, record = setup(tmp_path)
    (record.path / "new.txt").write_text("frozen\n", encoding="utf-8")
    service.mark_finished(record.child_id, {"status": "completed"}, root=service_root)
    page = service.review(record.child_id, root=service_root)
    service.acknowledge(record.child_id, page.review_id, page.digest, root=service_root)
    real_replace = worktrees._replace_record

    def fail_integration_promotion(path, updated, key):
        if updated.lifecycle == "integrated":
            raise OSError("simulated record publication failure")
        return real_replace(path, updated, key)

    monkeypatch.setattr(worktrees, "_replace_record", fail_integration_promotion)
    result = service.integrate(record.child_id, page.review_id, page.digest, root=service_root)
    assert result.status == "recovery_required"
    assert "lifecycle metadata needs recovery" in (result.error or "")
    journals = tuple((service_root / "transactions").glob("*/journal.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text(encoding="utf-8"))["state"] == "integrated"
    assert (parent / "new.txt").read_text(encoding="utf-8") == "frozen\n"
    assert service.get(record.child_id, root=service_root).lifecycle == "finalized"

    monkeypatch.setattr(worktrees, "_replace_record", real_replace)
    recovered = worktrees.WorktreeService().recover_pending(record.child_id, root=service_root)
    assert recovered.lifecycle == "integrated"
    assert recovered.integrated_review_id == page.review_id
    assert recovered.integrated_digest == page.digest
    assert recovered.integration_transaction_id == result.transaction_id


@pytest.mark.parametrize("failure_index", range(5))
def test_failure_at_each_apply_step_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_index: int):
    import nexus.agents.worktree_integrate as module

    parent, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    calls = 0

    def fail_at(root_fd, entry, data):
        nonlocal calls
        if calls == failure_index:
            raise OSError("injected apply failure")
        calls += 1
        real_apply(root_fd, entry, data)

    monkeypatch.setattr(module, "_apply_entry", fail_at)
    result = integrate_review(record, artifact, tmp_path / "tx")
    assert result.status == "rolled_back"
    assert (parent / "tracked.txt").read_bytes() == b"parent\n"
    assert (parent / "delete.txt").read_bytes() == b"delete me\n"
    assert not (parent / "staged.txt").exists()
    assert not (parent / "new.bin").exists()
    assert (parent / "script.sh").stat().st_mode & 0o111


def test_cancellation_rolls_back(tmp_path: Path):
    class Cancel:
        calls = 0

        def raise_if_cancelled(self):
            self.calls += 1
            if self.calls == 3:
                raise RuntimeError("cancelled")

    parent, _, record, artifact = make_review(tmp_path)
    result = integrate_review(record, artifact, tmp_path / "tx", cancel=Cancel())
    assert result.status == "rolled_back"
    assert (parent / "tracked.txt").read_bytes() == b"parent\n"


def test_crash_journal_recovery_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    parent, _, record, artifact = make_review(tmp_path)

    real_apply = module._apply_entry
    applied = False

    def crash_after_one(root_fd, entry, data):
        nonlocal applied
        if applied:
            raise KeyboardInterrupt("simulated process interruption")
        real_apply(root_fd, entry, data)
        applied = True

    monkeypatch.setattr(module, "_apply_entry", crash_after_one)
    with pytest.raises(KeyboardInterrupt):
        integrate_review(record, artifact, tmp_path / "tx")
    monkeypatch.undo()
    recovered = recover_transactions(tmp_path / "tx", record)
    assert [item.status for item in recovered] == ["rolled_back"]
    assert recover_transactions(tmp_path / "tx", record) == ()
    assert (parent / "tracked.txt").read_bytes() == b"parent\n"


def test_recovery_refuses_to_restore_after_parent_commit_advanced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    parent, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    applied = False

    def crash_after_one(root_fd, entry, data):
        nonlocal applied
        real_apply(root_fd, entry, data)
        if not applied:
            applied = True
            raise KeyboardInterrupt("simulated process interruption")

    monkeypatch.setattr(module, "_apply_entry", crash_after_one)
    with pytest.raises(KeyboardInterrupt):
        integrate_review(record, artifact, tmp_path / "tx")
    monkeypatch.undo()
    git(parent, "add", "-A")
    git(parent, "-c", "commit.gpgsign=false", "commit", "-qm", "user commit after interruption")
    before = (parent / "tracked.txt").read_bytes()
    head = git(parent, "rev-parse", "HEAD")

    recovered = recover_transactions(tmp_path / "tx", record)

    assert recovered[0].status == "recovery_required"
    assert "HEAD or index changed" in recovered[0].error
    assert (parent / "tracked.txt").read_bytes() == before
    assert git(parent, "rev-parse", "HEAD") == head


def test_orphan_journal_is_reported_and_recovery_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    _, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    applied = False

    def crash_after_one(root_fd, entry, data):
        nonlocal applied
        real_apply(root_fd, entry, data)
        if not applied:
            applied = True
            raise KeyboardInterrupt("simulated process interruption")

    transaction_root = tmp_path / "tx"
    monkeypatch.setattr(module, "_apply_entry", crash_after_one)
    with pytest.raises(KeyboardInterrupt):
        integrate_review(record, artifact, transaction_root)
    monkeypatch.undo()
    orphan = transaction_root / ("f" * 32)
    orphan.mkdir(mode=0o700)
    (orphan / "blobs").mkdir(mode=0o700)

    recovered = recover_transactions(transaction_root, record)

    assert sorted(item.status for item in recovered) == ["recovery_required", "rolled_back"]
    assert any("no journal" in (item.error or "") for item in recovered)
    assert orphan.is_dir()


def test_corrupt_recovery_blob_returns_recovery_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    _, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    applied = False

    def crash_after_one(root_fd, entry, data):
        nonlocal applied
        real_apply(root_fd, entry, data)
        if not applied:
            applied = True
            raise KeyboardInterrupt("simulated process interruption")

    transaction_root = tmp_path / "tx"
    monkeypatch.setattr(module, "_apply_entry", crash_after_one)
    with pytest.raises(KeyboardInterrupt):
        integrate_review(record, artifact, transaction_root)
    monkeypatch.undo()
    journal = next(path for path in transaction_root.iterdir() if path.is_dir() and path.name != ".lock")
    blob = next((journal / "blobs").iterdir())
    blob.write_bytes(b"corrupt")

    recovered = recover_transactions(transaction_root, record)

    assert recovered[0].status == "recovery_required"
    assert "integrity" in (recovered[0].error or "")


def test_integration_rejects_git_path_components_and_case_alias_targets(tmp_path: Path):
    import nexus.agents.worktree_integrate as module

    for path in (".GIT/hooks/config", "nested/.Git/config"):
        with pytest.raises(WorktreeIntegrationError, match="unsafe component"):
            module._path_bytes(path)

    directory = tmp_path / "aliases"
    directory.mkdir()
    (directory / "Tracked.txt").write_text("alias", encoding="utf-8")
    fd = os.open(directory, os.O_RDONLY)
    try:
        with pytest.raises(WorktreeIntegrationError, match="case-alias"):
            module._check_name_alias(fd, b"tracked.txt")
    finally:
        os.close(fd)


def test_stale_journal_blocks_new_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    parent, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    applied = False

    def crash_after_one(root_fd, entry, data):
        nonlocal applied
        real_apply(root_fd, entry, data)
        if not applied:
            applied = True
            raise KeyboardInterrupt("simulated process interruption")

    transaction_root = tmp_path / "tx"
    monkeypatch.setattr(module, "_apply_entry", crash_after_one)
    with pytest.raises(KeyboardInterrupt):
        integrate_review(record, artifact, transaction_root)
    monkeypatch.undo()
    git(parent, "add", "-A")
    git(parent, "-c", "commit.gpgsign=false", "commit", "-qm", "user commit after interruption")
    before = (parent / "tracked.txt").read_bytes()

    result = integrate_review(record, artifact, transaction_root)

    assert result.status == "recovery_required"
    assert "stale transaction recovery" in (result.error or "")
    assert (parent / "tracked.txt").read_bytes() == before


def test_external_change_during_recovery_is_reported_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.agents.worktree_integrate as module

    parent, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    count = 0

    def apply_then_external(root_fd, entry, data):
        nonlocal count
        real_apply(root_fd, entry, data)
        count += 1
        if count == 1:
            (parent / entry.path).write_bytes(b"external writer")

    monkeypatch.setattr(module, "_apply_entry", apply_then_external)
    result = integrate_review(record, artifact, tmp_path / "tx")
    assert result.status == "recovery_required"
    conflicted = result.recovery_required[0]
    before = (parent / conflicted).read_bytes()
    recovered = recover_transactions(tmp_path / "tx", record)
    assert recovered[0].status == "recovery_required"
    assert (parent / conflicted).read_bytes() == before


def test_rollback_failure_keeps_journal_and_reports_exact_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import nexus.agents.worktree_integrate as module

    parent, _, record, artifact = make_review(tmp_path)
    real_apply = module._apply_entry
    real_restore = module._restore_entry
    applied: list[str] = []

    def apply_then_fail(root_fd, entry, data):
        if len(applied) == 1:
            raise OSError("injected apply failure")
        real_apply(root_fd, entry, data)
        applied.append(entry.path)

    def fail_one_restore(root_fd, entry, old_data):
        if entry.path == applied[0]:
            return False
        return real_restore(root_fd, entry, old_data)

    monkeypatch.setattr(module, "_apply_entry", apply_then_fail)
    monkeypatch.setattr(module, "_restore_entry", fail_one_restore)
    result = integrate_review(record, artifact, tmp_path / "tx")

    assert result.status == "recovery_required"
    assert result.recovery_required == (applied[0],)
    applied_row = next(row for row in artifact.manifest["entries"] if row["path"] == applied[0])
    if applied_row["new_sha256"] is None:
        assert not (parent / applied[0]).exists()
    else:
        frozen = (artifact.artifact_path / "blobs" / applied_row["new_sha256"]).read_bytes()
        assert (parent / applied[0]).read_bytes() == frozen
    if applied[0] != "delete.txt":
        assert (parent / "delete.txt").read_bytes() == b"delete me\n"


def test_symlink_target_and_symlink_ancestor_are_refused(tmp_path: Path):
    parent, _, record, artifact = make_review(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"safe")
    (parent / "tracked.txt").unlink()
    (parent / "tracked.txt").symlink_to(outside)
    with pytest.raises(worktrees.WorktreeError, match="clean"):
        integrate_review(record, artifact, tmp_path / "tx")
    assert outside.read_bytes() == b"safe"


def test_parent_operation_and_sparse_checkout_are_refused(tmp_path: Path):
    parent, _, record, artifact = make_review(tmp_path)
    marker = parent / ".git" / "CHERRY_PICK_HEAD"
    marker.write_text("deadbeef", encoding="ascii")
    with pytest.raises(worktrees.WorktreeError, match="in-progress"):
        integrate_review(record, artifact, tmp_path / "tx")
    marker.unlink()
    git(parent, "config", "core.sparseCheckout", "true")
    with pytest.raises(worktrees.WorktreeError, match="sparse"):
        integrate_review(record, artifact, tmp_path / "tx")
