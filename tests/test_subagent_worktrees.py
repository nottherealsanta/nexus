from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nexus.agents import worktrees


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Worktree Test")
    git(path, "config", "user.email", "worktree-test@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-qm", "initial")
    return path


def test_create_persists_ownership_across_service_recreation(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon" / "worktree-service"

    created = worktrees.create(parent, "agent/one", root=root)
    recreated = worktrees.get("agent/one", root=root)

    assert recreated == created
    assert worktrees.inspect("agent/one", root=root) == created
    assert worktrees.list(root=root) == (created,)
    assert created.parent_workspace == parent.resolve()
    assert created.path.is_dir()
    assert git(created.path, "rev-parse", "HEAD") == created.base_commit
    assert git(created.path, "branch", "--show-current") == created.branch
    assert (root.stat().st_mode & 0o777) == 0o700
    record_file = root / "records" / worktrees._record_name("agent/one")
    assert (record_file.stat().st_mode & 0o777) == 0o600


def test_git_environment_cannot_redirect_or_refresh_parent_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = repository(tmp_path / "parent")
    foreign = repository(tmp_path / "foreign")
    root = tmp_path / "daemon"
    index = parent / ".git" / "index"
    before_create = index.read_bytes()

    record = worktrees.create(parent, "child", root=root)
    record_path = root / "records" / worktrees._record_name("child")
    record_bytes = record_path.read_bytes()
    ownership_key = (root / ".ownership-key").read_bytes()
    after_create = index.read_bytes()

    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    assert worktrees._git(parent, "status", "--porcelain", "--untracked-files=all") == ""
    assert worktrees.inspect("child", root=root) == record
    assert worktrees.get("child", root=root) == record
    assert worktrees.list(root=root) == (record,)

    assert after_create == before_create
    assert index.read_bytes() == after_create
    assert record_path.read_bytes() == record_bytes
    assert (root / ".ownership-key").read_bytes() == ownership_key


@pytest.mark.parametrize("version", [True, 1.0, "2"])
def test_record_version_must_be_an_integer_not_a_coercible_value(
    tmp_path: Path, version: object
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    worktrees.create(parent, "child", root=root)
    record_file = root / "records" / worktrees._record_name("child")
    envelope = json.loads(record_file.read_text(encoding="utf-8"))
    payload = envelope["record"]
    payload["version"] = version
    key = (root / ".ownership-key").read_bytes()
    envelope["mac"] = hmac.new(
        key,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    record_file.write_text(json.dumps(envelope), encoding="utf-8")
    record_file.chmod(0o600)

    with pytest.raises(worktrees.WorktreeError, match="identity or version"):
        worktrees.get("child", root=root)


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_create_refuses_dirty_parent_without_changing_it(
    tmp_path: Path, dirty_kind: str
) -> None:
    parent = repository(tmp_path / "parent")
    changed = parent / ("tracked.txt" if dirty_kind == "tracked" else "new.txt")
    changed.write_text("dirty\n", encoding="utf-8")
    before = git(parent, "status", "--porcelain", "--untracked-files=all")

    with pytest.raises(worktrees.WorktreeError, match="must be clean"):
        worktrees.create(parent, "child", root=tmp_path / "daemon")

    assert git(parent, "status", "--porcelain", "--untracked-files=all") == before
    assert not (tmp_path / "daemon" / "worktrees").exists()


def test_create_rejects_non_git_checkout(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-repository"
    parent.mkdir()

    with pytest.raises(worktrees.WorktreeError, match="git"):
        worktrees.create(parent, "child", root=tmp_path / "daemon")


def test_create_requires_top_level_checkout_and_external_private_root(
    tmp_path: Path,
) -> None:
    parent = repository(tmp_path / "parent")
    nested = parent / "nested"
    nested.mkdir()

    with pytest.raises(worktrees.WorktreeError, match="top-level"):
        worktrees.create(nested, "child", root=tmp_path / "daemon")
    with pytest.raises(worktrees.WorktreeError, match="outside"):
        worktrees.create(parent, "child", root=parent / ".nexus" / "worktrees")


def test_concurrent_creates_are_serialized_and_independently_owned(
    tmp_path: Path,
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = tuple(
            pool.map(
                lambda child_id: worktrees.create(parent, child_id, root=root),
                ("child-a", "child-b", "child-c", "child-d"),
            )
        )

    assert len({record.path for record in records}) == len(records)
    assert len({record.branch for record in records}) == len(records)
    assert {record.child_id for record in worktrees.list(root=root)} == {
        "child-a",
        "child-b",
        "child-c",
        "child-d",
    }


def test_root_path_and_branch_collisions_are_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    worktrees_dir = root / "worktrees"
    worktrees_dir.mkdir(parents=True)
    first = "a" * 24
    second = "b" * 24
    collision_path = worktrees_dir / f"child-{first}"
    collision_path.mkdir()
    sentinel = collision_path / "keep.txt"
    sentinel.write_text("pre-existing", encoding="utf-8")
    git(parent, "branch", f"nexus/subagent/child-{second}")
    tokens = iter((first, second, "c" * 24))
    monkeypatch.setattr(worktrees.secrets, "token_hex", lambda _size: next(tokens))

    result = worktrees.create(parent, "child", root=root)

    assert result.path.name == f"child-{'c' * 24}"
    assert result.branch == f"nexus/subagent/child-{'c' * 24}"
    assert sentinel.read_text(encoding="utf-8") == "pre-existing"
    assert git(parent, "show-ref", "--verify", "--quiet", f"refs/heads/nexus/subagent/child-{second}") == ""


def test_inspection_rejects_tampered_metadata(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    worktrees.create(parent, "child", root=root)
    record_file = root / "records" / worktrees._record_name("child")
    envelope = json.loads(record_file.read_text(encoding="utf-8"))
    envelope["record"]["path"] = str(tmp_path / "foreign-checkout")
    record_file.write_text(json.dumps(envelope), encoding="utf-8")
    record_file.chmod(0o600)

    with pytest.raises(worktrees.WorktreeError, match="authentication"):
        worktrees.inspect("child", root=root)


def test_inspection_rejects_detached_or_foreign_worktree(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    record = worktrees.create(parent, "child", root=root)
    git(record.path, "checkout", "--detach", "HEAD")

    with pytest.raises(worktrees.WorktreeError, match="different branch"):
        worktrees.get("child", root=root)


def test_post_add_git_failure_rolls_back_created_worktree_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    real_git = worktrees._git
    failed = False

    def fail_inspection(
        cwd: Path,
        *args: str,
        check: bool = True,
        observational: bool = True,
    ) -> str:
        nonlocal failed
        if not failed and args[:2] == ("symbolic-ref", "--quiet"):
            failed = True
            raise worktrees.WorktreeError("injected git inspection failure")
        return real_git(cwd, *args, check=check, observational=observational)

    monkeypatch.setattr(worktrees, "_git", fail_inspection)

    with pytest.raises(worktrees.WorktreeError, match="injected git inspection failure"):
        worktrees.create(parent, "child", root=root)

    assert git(parent, "worktree", "list", "--porcelain").count("worktree ") == 1
    branches = git(parent, "for-each-ref", "--format=%(refname)", "refs/heads/nexus/subagent")
    assert not branches
    assert not tuple((root / "records").glob("*.json"))


def test_git_add_failure_does_not_remove_preexisting_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    foreign = tmp_path / "pre-existing"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    real_git = worktrees._git

    def fail_add(
        cwd: Path,
        *args: str,
        check: bool = True,
        observational: bool = True,
    ) -> str:
        if args[:2] == ("worktree", "add"):
            raise worktrees.WorktreeError("injected git add failure")
        return real_git(cwd, *args, check=check, observational=observational)

    monkeypatch.setattr(worktrees, "_git", fail_add)
    monkeypatch.setattr(worktrees.secrets, "token_hex", lambda _size: "d" * 24)

    with pytest.raises(worktrees.WorktreeError, match="injected git add failure"):
        worktrees.create(parent, "child", root=root)

    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert git(parent, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not git(parent, "for-each-ref", "--format=%(refname)", "refs/heads/nexus/subagent")
    assert not tuple((root / "records").glob("*.json"))


def test_root_symlink_and_missing_record_are_rejected(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    worktrees.create(parent, "child", root=root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(worktrees.WorktreeError, match="symlink"):
        worktrees.get("child", root=alias)
    with pytest.raises(worktrees.WorktreeError, match="no owned"):
        worktrees.get("missing", root=root)


def test_create_does_not_mutate_parent_head_or_working_tree(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    before_head = git(parent, "rev-parse", "HEAD")
    before_status = git(parent, "status", "--porcelain", "--untracked-files=all")

    worktrees.create(parent, "child", root=tmp_path / "daemon")

    assert git(parent, "rev-parse", "HEAD") == before_head
    assert git(parent, "status", "--porcelain", "--untracked-files=all") == before_status
    assert (parent / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert os.getuid() == worktrees.inspect("child", root=tmp_path / "daemon").owner_uid


def test_service_lifecycle_finalizes_once_and_survives_restart(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    record = service.create(parent, "child", root=root)
    assert service.get("child", root=root).lifecycle == "active"
    (record.path / "child.txt").write_text("retained\n", encoding="utf-8")
    dirty_status = git(record.path, "status", "--porcelain", "--untracked-files=all")

    finished = service.mark_finished(
        "child", {"status": "completed"}, root=root
    )
    repeated = worktrees.WorktreeService().mark_finished(
        "child", {"status": "failed"}, root=root
    )
    restarted = worktrees.WorktreeService()

    assert finished.lifecycle == "finalized"
    assert finished.final_status == "completed"
    assert finished.final_dirty_status == dirty_status
    assert finished.dirty is True
    assert finished.finalized_at
    assert repeated == finished
    assert restarted.get("child", root=root) == finished
    assert restarted.list(root=root) == (finished,)
    assert record.path.is_dir()
    assert (record.path / "child.txt").read_text(encoding="utf-8") == "retained\n"


def test_legacy_v1_requires_confirmed_runtime_absence_to_finalize(
    tmp_path: Path,
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    worktrees.create(parent, "legacy", root=root)
    record_file = root / "records" / worktrees._record_name("legacy")
    envelope = json.loads(record_file.read_text(encoding="utf-8"))
    payload = envelope["record"]
    payload["version"] = 1
    for key in (
        "lifecycle",
        "final_status",
        "final_dirty_status",
        "finalized_at",
        "review_status",
        "reviewed_at",
        "reviewer",
    ):
        payload.pop(key)
    key = (root / ".ownership-key").read_bytes()
    envelope["mac"] = hmac.new(
        key,
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    record_file.write_text(json.dumps(envelope), encoding="utf-8")
    record_file.chmod(0o600)

    unknown_owner = worktrees.WorktreeService(runtime_ownership=lambda _child: None)
    assert unknown_owner.get("legacy", root=root).lifecycle == "active"
    with pytest.raises(worktrees.WorktreeError, match="unknown"):
        unknown_owner.mark_finished("legacy", {"status": "completed"}, root=root)

    inactive_owner = worktrees.WorktreeService(runtime_ownership=lambda _child: False)
    finalized = inactive_owner.mark_finished(
        "legacy", {"status": "completed"}, root=root
    )
    assert finalized.lifecycle == "finalized"
    assert finalized.final_status == "completed"
    assert worktrees.get("legacy", root=root).lifecycle == "finalized"


def test_concurrent_mark_finished_is_serialized_and_idempotent(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    service.create(parent, "parallel", root=root)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = tuple(
            pool.map(
                lambda index: service.mark_finished(
                    "parallel", {"status": f"status-{index}"}, root=root
                ),
                range(6),
            )
        )

    persisted = worktrees.WorktreeService().get("parallel", root=root)
    assert all(result == persisted for result in results)
    assert persisted.lifecycle == "finalized"
    assert persisted.final_status in {f"status-{index}" for index in range(6)}


def test_mark_finished_refuses_tampered_record_and_unknown_id(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    service.create(parent, "tampered", root=root)
    with pytest.raises(worktrees.WorktreeError, match="no owned"):
        service.mark_finished("missing", {"status": "completed"}, root=root)

    record_file = root / "records" / worktrees._record_name("tampered")
    envelope = json.loads(record_file.read_text(encoding="utf-8"))
    envelope["record"]["lifecycle"] = "discarded"
    record_file.write_text(json.dumps(envelope), encoding="utf-8")
    record_file.chmod(0o600)
    with pytest.raises(worktrees.WorktreeError, match="authentication"):
        service.mark_finished("tampered", {"status": "completed"}, root=root)


def test_weird_child_ids_are_hashed_and_paths_stay_inside_service_root(
    tmp_path: Path,
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    child_id = "../../escape / newline\n λ"
    record = worktrees.WorktreeService().create(parent, child_id, root=root)

    assert record.child_id == child_id
    assert (root / "records" / worktrees._record_name(child_id)).is_file()
    assert record.path.parent == (root / "worktrees").resolve()
    assert not (tmp_path / "escape").exists()
    with pytest.raises(worktrees.WorktreeError, match="NUL"):
        worktrees.WorktreeService().get("bad\x00id", root=root)
    with pytest.raises(worktrees.WorktreeError, match="UTF-8"):
        worktrees.WorktreeService().get("\ud800", root=root)


def test_service_review_acknowledgment_is_durable_and_parent_read_only(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    record = service.create(parent, "reviewed", root=root)
    (record.path / "change.txt").write_text("review this\n", encoding="utf-8")
    service.mark_finished("reviewed", {"status": "completed"}, root=root)
    before_parent = (
        git(parent, "rev-parse", "HEAD"),
        git(parent, "status", "--porcelain", "--untracked-files=all"),
        git(parent, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
    )

    page = service.review("reviewed", root=root)
    assert page.review_id
    assert page.digest
    assert page.manifest["child_id"] == "reviewed"
    assert page.artifact_path.parent == root / "reviews"
    assert page.diff_pages

    acknowledged = service.acknowledge(
        "reviewed", page.review_id, page.digest, root=root
    )
    restarted = worktrees.WorktreeService().get("reviewed", root=root)
    assert acknowledged.acknowledged_review_id == page.review_id
    assert acknowledged.acknowledged_digest == page.digest
    assert acknowledged.acknowledged_at
    assert restarted.acknowledged_review_id == page.review_id
    assert restarted.acknowledged_digest == page.digest
    assert restarted.acknowledged_at == acknowledged.acknowledged_at
    assert before_parent == (
        git(parent, "rev-parse", "HEAD"),
        git(parent, "status", "--porcelain", "--untracked-files=all"),
        git(parent, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
    )


def test_service_review_rejects_active_wrong_digest_stale_and_forged_id(
    tmp_path: Path,
) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    record = service.create(parent, "guarded", root=root)

    with pytest.raises(worktrees.WorktreeError, match="finalized"):
        service.review("guarded", root=root)
    with pytest.raises(worktrees.WorktreeError, match="active"):
        service.acknowledge("guarded", "a" * 32, "b" * 64, root=root)

    (record.path / "change.txt").write_text("original\n", encoding="utf-8")
    service.mark_finished("guarded", {"status": "completed"}, root=root)
    page = service.review("guarded", root=root)
    with pytest.raises(worktrees.WorktreeError, match="digest"):
        service.acknowledge("guarded", page.review_id, "0" * 64, root=root)
    assert service.get("guarded", root=root).acknowledged_digest is None

    with pytest.raises(worktrees.WorktreeError, match="review id"):
        service.review("guarded", "../../outside", root=root)
    with pytest.raises(worktrees.WorktreeError, match="review id"):
        service.acknowledge("guarded", "../../outside", page.digest, root=root)

    forged_id = "f" * 32
    forged_path = root / "reviews" / forged_id
    forged_path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(worktrees.WorktreeError, match="symlink"):
        service.review("guarded", forged_id, root=root)

    (record.path / "change.txt").write_text("changed after review\n", encoding="utf-8")
    with pytest.raises(worktrees.WorktreeError, match="stale"):
        service.acknowledge("guarded", page.review_id, page.digest, root=root)
    assert service.get("guarded", root=root).acknowledged_digest is None


def test_new_service_review_invalidates_previous_acknowledgment(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    record = service.create(parent, "again", root=root)
    (record.path / "change.txt").write_text("unchanged\n", encoding="utf-8")
    service.mark_finished("again", {"status": "completed"}, root=root)
    first = service.review("again", root=root)
    service.acknowledge("again", first.review_id, first.digest, root=root)

    second = service.review("again", root=root)

    current = service.get("again", root=root)
    assert second.review_id != first.review_id
    assert current.current_review_id == second.review_id
    assert current.current_review_digest == second.digest
    assert current.acknowledged_review_id is None
    assert current.acknowledged_digest is None
    assert current.acknowledged_at is None


def test_service_review_pagination_is_bounded_and_validates_cursor(tmp_path: Path) -> None:
    parent = repository(tmp_path / "parent")
    root = tmp_path / "daemon"
    service = worktrees.WorktreeService()
    record = service.create(parent, "paged", root=root)
    for index in range(12):
        (record.path / f"change-{index:02d}.txt").write_text(
            f"content {index}\n" + ("x" * 150_000), encoding="utf-8"
        )
    service.mark_finished("paged", {"status": "completed"}, root=root)

    first = service.review("paged", cursor=0, limit=3, root=root)
    second = service.review(
        "paged", first.review_id, cursor=3, limit=3, root=root
    )

    assert len(first.diff_pages) == 3
    assert first.next_cursor == 3
    assert second.cursor == 3
    assert len(second.diff_pages) == 3
    assert second.next_cursor == 6
    with pytest.raises(worktrees.WorktreeError, match="limit"):
        service.review("paged", first.review_id, limit=9, root=root)
    with pytest.raises(worktrees.WorktreeError, match="cursor"):
        service.review("paged", first.review_id, cursor=-1, root=root)
    with pytest.raises(worktrees.WorktreeError, match="final page"):
        service.review("paged", first.review_id, cursor=100, root=root)
