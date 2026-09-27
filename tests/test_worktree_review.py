from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from nexus.agents import worktrees
from nexus.agents.worktree_review import (
    ReviewError,
    ReviewLimits,
    build_review,
    load_review,
)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        stdin=subprocess.DEVNULL,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Review Test")
    git(path, "config", "user.email", "review-test@example.invalid")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / "delete.txt").write_text("remove me\n", encoding="utf-8")
    (path / "script.sh").write_text("#!/bin/sh\necho base\n", encoding="utf-8")
    os.chmod(path / "script.sh", 0o755)
    git(path, "add", "tracked.txt", "delete.txt", "script.sh")
    git(path, "commit", "-qm", "base")
    return path


def finalized_child(tmp_path: Path):
    parent = repository(tmp_path / "parent")
    root = tmp_path / "service"
    service = worktrees.WorktreeService(runtime_ownership=lambda _child: True)
    record = service.create(parent, "review-child", root=root)
    return parent, service, record, root


def finalize(service, record, root):
    return service.mark_finished(record.child_id, {"status": "completed"}, root=root)


def test_review_compares_base_to_final_committed_index_and_untracked_files(tmp_path: Path):
    parent, service, record, root = finalized_child(tmp_path)
    child = record.path

    # Include changes which exist at different layers of the final checkout.
    git(child, "add", "tracked.txt")
    (child / "tracked.txt").write_text("committed update\n", encoding="utf-8")
    git(child, "add", "tracked.txt")
    git(child, "commit", "-qm", "committed update")
    (child / "tracked.txt").write_text("unstaged final\n", encoding="utf-8")
    (child / "staged.txt").write_text("staged content\n", encoding="utf-8")
    git(child, "add", "staged.txt")
    (child / "new.txt").write_text("untracked content\n", encoding="utf-8")
    (child / "delete.txt").unlink()
    os.chmod(child / "script.sh", 0o644)
    finalized = finalize(service, record, root)

    before_parent = (git(parent, "rev-parse", "HEAD"), git(parent, "status", "--porcelain=v1", "-z"))
    before_child = (git(child, "rev-parse", "HEAD"), git(child, "status", "--porcelain=v1", "-z"))
    artifact = build_review(finalized, tmp_path / "reviews")
    after_parent = (git(parent, "rev-parse", "HEAD"), git(parent, "status", "--porcelain=v1", "-z"))
    after_child = (git(child, "rev-parse", "HEAD"), git(child, "status", "--porcelain=v1", "-z"))

    entries = {entry["path"]: entry for entry in artifact.manifest["entries"]}
    assert set(entries) == {"tracked.txt", "staged.txt", "new.txt", "delete.txt", "script.sh"}
    assert entries["delete.txt"]["change"] == "deleted"
    assert entries["new.txt"]["change"] == "added"
    assert entries["script.sh"]["old_mode"] == "100755"
    assert entries["script.sh"]["new_mode"] == "100644"
    assert entries["tracked.txt"]["new_sha256"]
    assert before_parent == after_parent
    assert before_child == after_child
    assert (tmp_path / "reviews" / artifact.review_id / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_review_binary_ignored_warning_and_private_frozen_content(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    (record.path / ".gitignore").write_text("ignored.dat\n", encoding="utf-8")
    (record.path / "ignored.dat").write_bytes(b"do not include\0")
    (record.path / "binary.dat").write_bytes(b"\x00\xffbinary")
    finalized = finalize(service, record, root)

    artifact = build_review(finalized, tmp_path / "reviews")
    entries = {entry["path"]: entry for entry in artifact.manifest["entries"]}
    assert entries["binary.dat"]["binary"] is True
    assert "ignored.dat" in artifact.manifest["ignored_paths"]
    assert artifact.manifest["ignored_files"][0]["path"] == "ignored.dat"
    assert "ignored.dat" not in entries
    for blob in artifact.manifest["blobs"]:
        blob_path = artifact.artifact_path / "blobs" / blob
        assert blob_path.stat().st_mode & 0o777 == 0o600
        assert blob_path.read_bytes()


def test_review_refuses_active_or_symlinked_child(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    with pytest.raises(ReviewError, match="finalized"):
        build_review(record, tmp_path / "reviews")

    (record.path / "link").symlink_to("tracked.txt")
    finalized = finalize(service, record, root)
    with pytest.raises(ReviewError, match="symlink"):
        build_review(finalized, tmp_path / "reviews")


def test_review_refuses_submodule_index_entry(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    git(record.path, "update-index", "--add", "--cacheinfo", "160000", "a" * 40, "vendor")
    finalized = finalize(service, record, root)
    with pytest.raises(ReviewError, match="submodules"):
        build_review(finalized, tmp_path / "reviews")


def test_review_refuses_nested_repository(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    nested = record.path / "vendor"
    nested.mkdir()
    git(nested, "init", "-q")
    finalized = finalize(service, record, root)
    with pytest.raises(ReviewError, match="nested repositories"):
        build_review(finalized, tmp_path / "reviews")


def test_load_review_detects_child_changes_after_snapshot(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    (record.path / "new.txt").write_text("before\n", encoding="utf-8")
    finalized = finalize(service, record, root)
    artifact_root = tmp_path / "reviews"
    artifact = build_review(finalized, artifact_root)

    loaded = load_review(finalized, artifact_root, artifact.review_id)
    assert loaded.digest == artifact.digest
    (record.path / "new.txt").write_text("after\n", encoding="utf-8")
    with pytest.raises(ReviewError, match="stale"):
        load_review(finalized, artifact_root, artifact.review_id)


def test_review_pagination_is_deterministic_and_limits_are_enforced(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    for name in ("z.txt", "a.txt", "m.txt"):
        (record.path / name).write_text(name + "\n" + "x" * 90, encoding="utf-8")
    finalized = finalize(service, record, root)
    limits = ReviewLimits(max_files=4, max_file_bytes=1024, max_total_bytes=4096, page_bytes=128)
    first = build_review(finalized, tmp_path / "reviews-one", limits=limits)
    second = build_review(finalized, tmp_path / "reviews-two", limits=limits)

    assert first.manifest == second.manifest
    assert first.diff_pages == second.diff_pages
    assert all(len(page) <= limits.page_bytes for page in first.diff_pages)
    assert [row["path"] for row in first.manifest["entries"]] == ["a.txt", "m.txt", "z.txt"]
    with pytest.raises(ReviewError, match="changed-file limit"):
        build_review(finalized, tmp_path / "reviews-limited", limits=ReviewLimits(max_files=2))
    with pytest.raises(ReviewError, match="per-file"):
        build_review(finalized, tmp_path / "reviews-small-file", limits=ReviewLimits(max_file_bytes=16))
    with pytest.raises(ReviewError, match="aggregate content limit"):
        build_review(
            finalized,
            tmp_path / "reviews-small-total",
            limits=ReviewLimits(max_total_bytes=128),
        )


def test_load_review_rejects_extra_artifact_files(tmp_path: Path):
    _, service, record, root = finalized_child(tmp_path)
    (record.path / "new.txt").write_text("change\n", encoding="utf-8")
    finalized = finalize(service, record, root)
    artifact_root = tmp_path / "reviews"
    artifact = build_review(finalized, artifact_root)
    sentinel = artifact.artifact_path / "unexpected"
    sentinel.write_text("not part of review", encoding="utf-8")
    sentinel.chmod(0o600)
    with pytest.raises(ReviewError, match="artifact contents"):
        load_review(finalized, artifact_root, artifact.review_id)
