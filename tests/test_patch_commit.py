from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from nexus.tools.builtin import _patch_commit as patch_commit
from nexus.tools.builtin import _patch_stage as patch_stage
from nexus.tools.permissions import PathGuard


def _staged(*triples: tuple[str, bytes | None, bytes | None]) -> patch_stage.StagedChanges:
    changes = tuple(
        patch_stage.StagedChange(
            path,
            old,
            new,
            "add" if old is None else "delete" if new is None else "update",
        )
        for path, old, new in triples
    )
    return patch_stage.StagedChanges(changes, "")


@pytest.mark.asyncio
async def test_commits_mixed_changes_in_deterministic_order_and_preserves_modes(
    tmp_path: Path,
) -> None:
    (tmp_path / "update.txt").write_bytes(b"old")
    (tmp_path / "update.txt").chmod(0o640)
    (tmp_path / "delete.txt").write_bytes(b"remove")
    staged = _staged(
        ("update.txt", b"old", b"new"),
        ("add.txt", None, b"added"),
        ("delete.txt", b"remove", None),
    )

    statuses = await patch_commit.commit_staged(staged, PathGuard(tmp_path))

    assert [(item.path, item.status) for item in statuses] == [
        ("add.txt", "added"),
        ("delete.txt", "deleted"),
        ("update.txt", "updated"),
    ]
    assert (tmp_path / "add.txt").read_bytes() == b"added"
    assert not (tmp_path / "delete.txt").exists()
    assert (tmp_path / "update.txt").read_bytes() == b"new"
    assert (tmp_path / "update.txt").stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
async def test_move_destination_inherits_source_mode(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_bytes(b"moved")
    (tmp_path / "source.txt").chmod(0o755)

    await patch_commit.commit_staged(
        patch_stage.StagedChanges(
            (
                patch_stage.StagedChange("source.txt", b"moved", None, "move"),
                patch_stage.StagedChange("destination.txt", None, b"moved", "move"),
            ),
            "",
        ),
        PathGuard(tmp_path),
    )

    assert not (tmp_path / "source.txt").exists()
    assert (tmp_path / "destination.txt").read_bytes() == b"moved"
    assert (tmp_path / "destination.txt").stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
async def test_delete_directory_fsync_failure_rolls_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "delete.txt"
    target.write_bytes(b"original")
    real_fsync = patch_commit.os.fsync
    fsynced_directories: list[tuple[int, int]] = []
    failed = False

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal failed
        info = patch_commit.os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            fsynced_directories.append((info.st_dev, info.st_ino))
            if not failed:
                failed = True
                raise OSError("injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(patch_commit.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("delete.txt", b"original", None)), PathGuard(tmp_path)
        )

    assert caught.value.failure_path == "delete.txt"
    assert caught.value.committed_paths == ("delete.txt",)
    assert caught.value.restored_paths == ("delete.txt",)
    assert caught.value.unrestored_paths == ()
    assert target.read_bytes() == b"original"
    # The failed fsync followed the unlink; rollback then durably restored it.
    assert len(fsynced_directories) == 2
    assert fsynced_directories[0] == fsynced_directories[1]


@pytest.mark.asyncio
async def test_rollback_unlink_fsyncs_the_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "later.txt").write_bytes(b"old")
    real_unlink = patch_commit.os.unlink
    real_fsync = patch_commit.os.fsync
    events: list[tuple[str, str, tuple[int, int] | None]] = []

    def observe_unlink(path, *, dir_fd=None):
        info = patch_commit.os.fstat(dir_fd) if dir_fd is not None else None
        identity = (info.st_dev, info.st_ino) if info is not None else None
        real_unlink(path, dir_fd=dir_fd)
        events.append(("unlink", path, identity))

    def observe_fsync(fd: int) -> None:
        info = patch_commit.os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            events.append(("fsync", "", (info.st_dev, info.st_ino)))
        real_fsync(fd)

    async def fail_update(ctx, target, data, *, create_parents):
        raise OSError("injected later commit failure")

    monkeypatch.setattr(patch_commit.os, "unlink", observe_unlink)
    monkeypatch.setattr(patch_commit.os, "fsync", observe_fsync)
    monkeypatch.setattr(patch_commit, "atomic_write_bytes", fail_update)
    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("added.txt", None, b"added"), ("later.txt", b"old", b"new")),
            PathGuard(tmp_path),
        )

    assert caught.value.restored_paths == ("added.txt",)
    assert caught.value.unrestored_paths == ()
    assert not (tmp_path / "added.txt").exists()
    rollback_unlink = next(
        index
        for index, event in enumerate(events)
        if event[:2] == ("unlink", "added.txt")
    )
    unlink_identity = events[rollback_unlink][2]
    assert events[rollback_unlink + 1] == ("fsync", "", unlink_identity)


@pytest.mark.asyncio
async def test_add_race_does_not_replace_concurrently_created_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = patch_commit.os.link

    def create_then_link(src, dst, *, src_dir_fd, dst_dir_fd, follow_symlinks=False):
        raced_fd = patch_commit.os.open(
            dst,
            patch_commit.os.O_WRONLY | patch_commit.os.O_CREAT | patch_commit.os.O_EXCL,
            0o600,
            dir_fd=dst_dir_fd,
        )
        patch_commit.os.write(raced_fd, b"raced")
        patch_commit.os.close(raced_fd)
        return real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(patch_commit.os, "link", create_then_link)
    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("new.txt", None, b"patch")), PathGuard(tmp_path)
        )

    assert (tmp_path / "new.txt").read_bytes() == b"raced"
    assert caught.value.committed_paths == ()
    assert caught.value.unrestored_paths == ("new.txt",)


@pytest.mark.asyncio
async def test_post_write_fchmod_failure_reports_path_once_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"old")
    target.chmod(0o640)
    real_fchmod = patch_commit.os.fchmod
    failed = False

    def fail_once(fd: int, mode: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected chmod failure")
        real_fchmod(fd, mode)

    monkeypatch.setattr(patch_commit.os, "fchmod", fail_once)
    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("file.txt", b"old", b"new")), PathGuard(tmp_path)
        )

    assert caught.value.committed_paths == ("file.txt",)
    assert caught.value.restored_paths == ("file.txt",)
    assert caught.value.unrestored_paths == ()
    assert target.read_bytes() == b"old"
    assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_index", [0, 1, 2])
async def test_commit_failure_rolls_back_committed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_index: int
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_bytes(f"old-{name}".encode())
    real_write = patch_commit.atomic_write_bytes
    call_count = 0

    async def fail_at(ctx, target, data, *, create_parents):
        nonlocal call_count
        call_count += 1
        if call_count == failure_index + 1:
            raise OSError("injected commit failure")
        await real_write(ctx, target, data, create_parents=create_parents)

    monkeypatch.setattr(patch_commit, "atomic_write_bytes", fail_at)
    staged = _staged(*((name, f"old-{name}".encode(), f"new-{name}".encode()) for name in ("c.txt", "a.txt", "b.txt")))

    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(staged, PathGuard(tmp_path))

    assert caught.value.unrestored_paths == ()
    assert caught.value.committed_paths == tuple(("a.txt", "b.txt")[:failure_index])
    for name in ("a.txt", "b.txt", "c.txt"):
        assert (tmp_path / name).read_bytes() == f"old-{name}".encode()


class _CancelAfter:
    def __init__(self, count: int) -> None:
        self.count = count
        self.calls = 0

    def raise_if_cancelled(self) -> None:
        self.calls += 1
        if self.calls >= self.count:
            raise RuntimeError("cancelled")


@pytest.mark.asyncio
async def test_cancellation_mid_commit_rolls_back_prior_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"old-a")
    (tmp_path / "b.txt").write_bytes(b"old-b")
    token = _CancelAfter(7)

    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("a.txt", b"old-a", b"new-a"), ("b.txt", b"old-b", b"new-b")),
            PathGuard(tmp_path),
            cancellation=token,
        )

    assert caught.value.failure_path == "b.txt"
    assert caught.value.unrestored_paths == ()
    assert (tmp_path / "a.txt").read_bytes() == b"old-a"
    assert (tmp_path / "b.txt").read_bytes() == b"old-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["destination", "source"])
async def test_symlink_targets_fail_closed(tmp_path: Path, target: str) -> None:
    outside = tmp_path.parent / f"outside-{target}.txt"
    outside.write_bytes(b"outside")
    if target == "destination":
        (tmp_path / "file.txt").symlink_to(outside)
        staged = _staged(("file.txt", b"outside", b"changed"))
    else:
        (tmp_path / "file.txt").symlink_to(outside)
        staged = _staged(("file.txt", None, b"changed"))

    with pytest.raises(patch_commit.PatchCommitError):
        await patch_commit.commit_staged(staged, PathGuard(tmp_path))

    assert outside.read_bytes() == b"outside"
    assert (tmp_path / "file.txt").is_symlink()


@pytest.mark.asyncio
async def test_symlink_swap_of_later_target_aborts_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_bytes(b"old-a")
    (tmp_path / "b.txt").write_bytes(b"old-b")
    outside = tmp_path.parent / "outside-swap.txt"
    outside.write_bytes(b"outside")
    real_write = patch_commit.atomic_write_bytes

    async def swap_after_first(ctx, target, data, *, create_parents):
        await real_write(ctx, target, data, create_parents=create_parents)
        if target.name == "a.txt":
            (tmp_path / "b.txt").unlink()
            (tmp_path / "b.txt").symlink_to(outside)

    monkeypatch.setattr(patch_commit, "atomic_write_bytes", swap_after_first)
    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("a.txt", b"old-a", b"new-a"), ("b.txt", b"old-b", b"new-b")),
            PathGuard(tmp_path),
        )

    assert caught.value.unrestored_paths == ("b.txt",)
    assert (tmp_path / "a.txt").read_bytes() == b"old-a"
    assert outside.read_bytes() == b"outside"
    assert (tmp_path / "b.txt").is_symlink()


@pytest.mark.asyncio
async def test_rollback_failure_reports_exact_unrestored_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_bytes(b"old-a")
    (tmp_path / "b.txt").write_bytes(b"old-b")
    real_write = patch_commit.atomic_write_bytes
    calls = 0

    async def fail_commit_and_rollback(ctx, target, data, *, create_parents):
        nonlocal calls
        calls += 1
        if calls in (2, 3):
            raise OSError("injected failure")
        await real_write(ctx, target, data, create_parents=create_parents)

    monkeypatch.setattr(patch_commit, "atomic_write_bytes", fail_commit_and_rollback)
    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await patch_commit.commit_staged(
            _staged(("a.txt", b"old-a", b"new-a"), ("b.txt", b"old-b", b"new-b")),
            PathGuard(tmp_path),
        )

    assert caught.value.failure_path == "b.txt"
    assert caught.value.committed_paths == ("a.txt",)
    assert caught.value.restored_paths == ()
    assert caught.value.unrestored_paths == ("a.txt",)
    assert (tmp_path / "a.txt").read_bytes() == b"new-a"
    assert (tmp_path / "b.txt").read_bytes() == b"old-b"


@pytest.mark.asyncio
async def test_cancellation_during_rollback_waits_for_exact_restoration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_bytes(b"old-a")
    (tmp_path / "b.txt").write_bytes(b"old-b")
    real_write = patch_commit.atomic_write_bytes
    write_calls = 0
    rollback_started = asyncio.Event()
    continue_rollback = asyncio.Event()
    real_restore = patch_commit._restore_one

    async def fail_second_commit(ctx, target, data, *, create_parents):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise OSError("injected commit failure")
        await real_write(ctx, target, data, create_parents=create_parents)

    async def pause_restore(item, guard):
        rollback_started.set()
        await continue_rollback.wait()
        await real_restore(item, guard)

    monkeypatch.setattr(patch_commit, "atomic_write_bytes", fail_second_commit)
    monkeypatch.setattr(patch_commit, "_restore_one", pause_restore)
    commit_task = asyncio.create_task(
        patch_commit.commit_staged(
            _staged(("a.txt", b"old-a", b"new-a"), ("b.txt", b"old-b", b"new-b")),
            PathGuard(tmp_path),
        )
    )
    await rollback_started.wait()
    commit_task.cancel()
    continue_rollback.set()

    with pytest.raises(patch_commit.PatchCommitError) as caught:
        await commit_task

    assert caught.value.restored_paths == ("a.txt",)
    assert caught.value.unrestored_paths == ()
    assert (tmp_path / "a.txt").read_bytes() == b"old-a"
    assert (tmp_path / "b.txt").read_bytes() == b"old-b"
