"""Guarded commit and rollback for immutable staged patch changes."""
from __future__ import annotations

import asyncio
import os
import secrets
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from nexus.tools.permissions import PathGuard, PathSecurityError

from ._patch_stage import StagedChange, StagedChanges

__all__ = ["PatchCommitError", "PatchFileStatus", "commit_staged"]


@dataclass(frozen=True, slots=True)
class PatchFileStatus:
    path: str
    status: str


class PatchCommitError(RuntimeError):
    """Commit failed; attributes precisely describe the resulting file state."""

    def __init__(
        self,
        message: str,
        *,
        failure_path: str | None,
        committed_paths: tuple[str, ...],
        restored_paths: tuple[str, ...],
        unrestored_paths: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.failure_path = failure_path
        self.committed_paths = committed_paths
        self.restored_paths = restored_paths
        self.unrestored_paths = unrestored_paths


@dataclass(frozen=True, slots=True)
class _Snapshot:
    change: StagedChange
    target: Path
    identity: tuple[int, int] | None
    mode: int | None
    parent_identity: tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Committed:
    snapshot: _Snapshot
    identity: tuple[int, int] | None


@dataclass(slots=True)
class _WriteContext:
    cancel_token: object | None = None


def _cancel(token: object | None) -> None:
    if token is None:
        return
    check = getattr(token, "raise_if_cancelled", None)
    if callable(check):
        check()


def _directory_fd(parent: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # Open every component without following symlinks. Opening just the final
    # directory with O_NOFOLLOW leaves an ancestor-swap window.
    fd = os.open(parent.anchor or "/", flags)
    try:
        for component in parent.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        opened = os.fstat(fd)
        current = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode) or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(f"parent directory changed: {parent}")
        return fd
    except BaseException:
        os.close(fd)
        raise


async def _add_no_replace(fd: int, name: str, data: bytes) -> None:
    """Create a file with mode 0600 without ever replacing a raced-in leaf."""
    temp_name = f".nexus-patch-{secrets.token_hex(12)}"
    temp_fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=fd,
    )
    try:
        with os.fdopen(temp_fd, "wb") as handle:
            view = memoryview(data)
            while view:
                written = handle.write(view[:64 * 1024])
                if written is None or written <= 0:
                    raise OSError("short write while creating patch target")
                view = view[written:]
                await asyncio.sleep(0)
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) is atomic and fails with EEXIST if another actor creates the
        # destination after preflight. Unlike replace(), it cannot clobber it.
        os.link(
            temp_name,
            name,
            src_dir_fd=fd,
            dst_dir_fd=fd,
            follow_symlinks=False,
        )
        os.unlink(temp_name, dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(temp_name, dir_fd=fd)
        except OSError:
            pass
        raise


async def atomic_write_bytes(
    ctx: object, target: Path, data: bytes, *, create_parents: bool
) -> None:
    """FD-relative atomic replacement used by patch commit and rollback."""
    if create_parents:
        raise ValueError("patch commits do not create parent directories")
    fd = _directory_fd(target.parent)
    temp_name = f".nexus-patch-{secrets.token_hex(12)}"
    try:
        opened = os.fstat(fd)
        current = os.stat(target.parent, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError(
                f"parent path changed during patch write: {target.parent}; "
                "ancestor swaps around the writer remain a residual filesystem race"
            )
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=fd,
        )
        try:
            with os.fdopen(temp_fd, "wb") as handle:
                view = memoryview(data)
                while view:
                    _cancel(getattr(ctx, "cancel_token", None))
                    written = handle.write(view[:64 * 1024])
                    if written is None or written <= 0:
                        raise OSError("short write while replacing patch target")
                    view = view[written:]
                    await asyncio.sleep(0)
                handle.flush()
                os.fsync(handle.fileno())
            _cancel(getattr(ctx, "cancel_token", None))
            os.replace(temp_name, target.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=fd)
            except OSError:
                pass
            raise
    finally:
        os.close(fd)


def _verify_parent_path(snapshot: _Snapshot, fd: int) -> None:
    opened = os.fstat(fd)
    try:
        current = os.stat(snapshot.target.parent, follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"parent path changed during patch commit: {snapshot.change.path}") from exc
    if (opened.st_dev, opened.st_ino) != snapshot.parent_identity or (
        current.st_dev,
        current.st_ino,
    ) != snapshot.parent_identity:
        raise OSError(
            f"parent path changed during patch commit: {snapshot.change.path}; "
            "ancestor swaps around the atomic writer remain a residual filesystem race"
        )


def _leaf_state(fd: int, name: str) -> tuple[bytes, tuple[int, int], int] | None:
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"patch target is not a regular file: {name}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_fd = os.open(name, flags, dir_fd=fd)
    try:
        opened = os.fstat(file_fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError(f"patch target changed while opening: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_fd)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError(f"patch target changed while reading: {name}")
        return b"".join(chunks), (opened.st_dev, opened.st_ino), stat.S_IMODE(opened.st_mode)
    finally:
        os.close(file_fd)


def _status_matches(
    state: tuple[bytes, tuple[int, int], int] | None,
    expected_bytes: bytes | None,
    expected_identity: tuple[int, int] | None,
) -> bool:
    if expected_bytes is None:
        return state is None and expected_identity is None
    return (
        state is not None
        and state[0] == expected_bytes
        and state[1] == expected_identity
    )


def _capture(guard: PathGuard, change: StagedChange) -> _Snapshot:
    resolved = guard.recheck(change.path, for_write=True)
    target = resolved.absolute
    # PathGuard canonicalizes a leaf symlink to its target. Keep the leaf name
    # visible so a symlink at the requested path can never be mistaken for it.
    raw = Path(change.path)
    if not raw.is_absolute():
        raw = guard.workspace / raw
    if raw.name in ("", ".", ".."):
        raise PathSecurityError("Patch target must name a file", code="bad_path")
    parent = guard.recheck(str(raw.parent), for_write=True).absolute
    leaf_target = parent / raw.name
    if leaf_target != target:
        raise PathSecurityError("Patch target is a symlink", code="symlink_target")

    fd = _directory_fd(parent)
    try:
        state = _leaf_state(fd, raw.name)
        parent_info = os.fstat(fd)
    finally:
        os.close(fd)
    actual = state[0] if state is not None else None
    if actual != change.old_bytes:
        raise OSError(f"staged snapshot no longer matches: {change.path}")
    return _Snapshot(
        change,
        target,
        state[1] if state is not None else None,
        state[2] if state is not None else None,
        (parent_info.st_dev, parent_info.st_ino),
    )


def _check_snapshot(guard: PathGuard, snapshot: _Snapshot) -> int:
    resolved = guard.recheck(snapshot.change.path, for_write=True)
    if resolved.absolute != snapshot.target:
        raise PathSecurityError("Patch target changed after staging", code="path_changed")
    fd = _directory_fd(snapshot.target.parent)
    try:
        _verify_parent_path(snapshot, fd)
        state = _leaf_state(fd, snapshot.target.name)
        if not _status_matches(state, snapshot.change.old_bytes, snapshot.identity):
            raise OSError(f"staged snapshot changed before commit: {snapshot.change.path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _capture_committed(snapshot: _Snapshot) -> tuple[int, int] | None:
    fd = _directory_fd(snapshot.target.parent)
    try:
        _verify_parent_path(snapshot, fd)
        state = _leaf_state(fd, snapshot.target.name)
        if snapshot.change.new_bytes is None:
            if state is not None:
                raise OSError(f"deleted target reappeared: {snapshot.change.path}")
            return None
        if state is None or state[0] != snapshot.change.new_bytes:
            raise OSError(f"committed target could not be verified: {snapshot.change.path}")
        return state[1]
    finally:
        os.close(fd)


def _is_restored(snapshot: _Snapshot, guard: PathGuard) -> bool:
    resolved = guard.recheck(snapshot.change.path, for_write=True)
    if resolved.absolute != snapshot.target:
        return False
    fd = _directory_fd(snapshot.target.parent)
    try:
        _verify_parent_path(snapshot, fd)
        state = _leaf_state(fd, snapshot.target.name)
        if snapshot.change.old_bytes is None:
            return state is None
        return (
            state is not None
            and state[0] == snapshot.change.old_bytes
            and (snapshot.mode is None or state[2] == snapshot.mode)
        )
    finally:
        os.close(fd)


async def _restore_one(item: _Committed, guard: PathGuard) -> None:
    snapshot = item.snapshot
    # Rollback re-authorizes the exact target and refuses to overwrite any
    # replacement made after our commit.
    resolved = guard.recheck(snapshot.change.path, for_write=True)
    if resolved.absolute != snapshot.target:
        raise PathSecurityError("Patch target changed during rollback", code="path_changed")
    fd = _directory_fd(snapshot.target.parent)
    try:
        _verify_parent_path(snapshot, fd)
        state = _leaf_state(fd, snapshot.target.name)
        if snapshot.change.new_bytes is None:
            if state is not None:
                raise OSError(f"rollback target was recreated: {snapshot.change.path}")
        elif not _status_matches(state, snapshot.change.new_bytes, item.identity):
            raise OSError(f"rollback target was replaced: {snapshot.change.path}")
        if snapshot.change.old_bytes is None:
            if state is not None:
                os.unlink(snapshot.target.name, dir_fd=fd)
                os.fsync(fd)
        else:
            await atomic_write_bytes(
                _WriteContext(), snapshot.target, snapshot.change.old_bytes, create_parents=False
            )
            if snapshot.mode is not None:
                restored = _leaf_state(fd, snapshot.target.name)
                if restored is None:
                    raise OSError(f"rollback output disappeared: {snapshot.change.path}")
                mode_fd = os.open(
                    snapshot.target.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
                try:
                    current = os.fstat(mode_fd)
                    if (current.st_dev, current.st_ino) != restored[1]:
                        raise OSError(f"rollback output changed: {snapshot.change.path}")
                    os.fchmod(mode_fd, snapshot.mode)
                finally:
                    os.close(mode_fd)
    finally:
        os.close(fd)


async def commit_staged(
    staged: StagedChanges,
    guard: PathGuard,
    workspace: Path | str | None = None,
    *,
    ctx: Any | None = None,
    cancellation: object | None = None,
) -> tuple[PatchFileStatus, ...]:
    """Commit staged files in path order, rolling back verified writes on failure.

    ``workspace`` is accepted for callers that naturally carry it; the guard is
    authoritative and a supplied workspace must match it. ``ctx`` is passed to
    the existing atomic writer for cooperative cancellation.
    """
    if workspace is not None and Path(workspace).resolve() != guard.workspace:
        raise ValueError("workspace does not match PathGuard")
    changes = sorted(staged.changes, key=lambda change: change.path)
    if len({change.path for change in changes}) != len(changes):
        raise ValueError("staged patch contains duplicate paths")
    token = cancellation if cancellation is not None else getattr(ctx, "cancel_token", None)
    write_ctx = _WriteContext(token)

    snapshots: list[_Snapshot] = []
    try:
        for change in changes:
            _cancel(token)
            snapshots.append(_capture(guard, change))
        snapshot_by_path = {snapshot.change.path: snapshot for snapshot in snapshots}
        move_modes: dict[str, int] = {}
        for index, change in enumerate(staged.changes):
            if (
                change.operation_status == "move"
                and change.old_bytes is not None
                and change.new_bytes is None
                and index + 1 < len(staged.changes)
            ):
                destination = staged.changes[index + 1]
                source_snapshot = snapshot_by_path[change.path]
                if (
                    destination.operation_status == "move"
                    and destination.old_bytes is None
                    and destination.new_bytes == change.old_bytes
                    and source_snapshot.mode is not None
                ):
                    move_modes[destination.path] = source_snapshot.mode
        snapshots = [
            replace(snapshot, mode=move_modes[snapshot.change.path])
            if snapshot.change.operation_status == "move"
            and snapshot.change.old_bytes is None
            and snapshot.change.new_bytes is not None
            and snapshot.change.path in move_modes
            else snapshot
            for snapshot in snapshots
        ]
        targets = [snapshot.target for snapshot in snapshots]
        if len(set(targets)) != len(targets):
            raise ValueError("staged patch paths resolve to the same target")
    except (Exception, asyncio.CancelledError) as exc:
        failed = changes[len(snapshots)].path if len(snapshots) < len(changes) else None
        raise PatchCommitError(
            f"Patch preflight failed: {exc}",
            failure_path=failed,
            committed_paths=(),
            restored_paths=(),
            unrestored_paths=(),
        ) from exc

    committed: list[_Committed] = []
    failure_path: str | None = None
    failure: BaseException | None = None
    failed_during_mutation = False
    failed_target_unrestored = False
    for snapshot in snapshots:
        failure_path = snapshot.change.path
        mutation_started = False
        try:
            _cancel(token)
            fd = _check_snapshot(guard, snapshot)
            try:
                if snapshot.change.new_bytes is None:
                    # Delete only the inode and content that were staged.
                    state = _leaf_state(fd, snapshot.target.name)
                    if not _status_matches(state, snapshot.change.old_bytes, snapshot.identity):
                        raise OSError(f"staged snapshot changed before delete: {snapshot.change.path}")
                    mutation_started = True
                    os.unlink(snapshot.target.name, dir_fd=fd)
                    os.fsync(fd)
                else:
                    mutation_started = True
                    if snapshot.change.old_bytes is None:
                        await _add_no_replace(fd, snapshot.target.name, snapshot.change.new_bytes)
                    else:
                        await atomic_write_bytes(
                            write_ctx,
                            snapshot.target,
                            snapshot.change.new_bytes,
                            create_parents=False,
                        )
                    if snapshot.mode is not None:
                        current = _capture_committed(snapshot)
                        mode_fd = os.open(
                            snapshot.target.name,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=fd,
                        )
                        try:
                            info = os.fstat(mode_fd)
                            if (info.st_dev, info.st_ino) != current:
                                raise OSError(f"committed target changed: {snapshot.change.path}")
                            os.fchmod(mode_fd, snapshot.mode)
                        finally:
                            os.close(mode_fd)
            finally:
                os.close(fd)
            identity = _capture_committed(snapshot)
            committed.append(_Committed(snapshot, identity))
        except BaseException as exc:  # noqa: BLE001 - rollback is mandatory on cancellation
            failed_during_mutation = mutation_started
            failure = exc
            break

    if failure is None:
        return tuple(
            PatchFileStatus(
                snapshot.change.path,
                "deleted" if snapshot.change.new_bytes is None else (
                    "added" if snapshot.change.old_bytes is None else "updated"
                ),
            )
            for snapshot in snapshots
        )

    # An atomic writer may replace the target and then fail (for example on
    # directory fsync). Include that in-flight target before reverse rollback.
    if failed_during_mutation:
        snapshot = next(item for item in snapshots if item.change.path == failure_path)
        try:
            resolved = guard.recheck(snapshot.change.path, for_write=True)
            if resolved.absolute != snapshot.target:
                raise OSError("failed target was redirected")
            fd = _directory_fd(snapshot.target.parent)
            try:
                state = _leaf_state(fd, snapshot.target.name)
            finally:
                os.close(fd)
            unchanged = _status_matches(state, snapshot.change.old_bytes, snapshot.identity)
            if not unchanged:
                if snapshot.change.new_bytes is None:
                    is_committed = state is None
                    identity = None
                else:
                    is_committed = (
                        state is not None and state[0] == snapshot.change.new_bytes
                    )
                    identity = state[1] if state is not None else None
                if is_committed:
                    committed.append(_Committed(snapshot, identity))
                else:
                    failed_target_unrestored = True
        except BaseException:  # noqa: BLE001 - report exact unresolved state
            failed_target_unrestored = True

    restored: list[str] = []
    unrestored: list[str] = []
    for item in reversed(committed):
        rollback_task = asyncio.create_task(_restore_one(item, guard))
        try:
            while not rollback_task.done():
                try:
                    await asyncio.shield(rollback_task)
                except asyncio.CancelledError:
                    # Cancellation must not interrupt restoration halfway
                    # through; keep waiting and report the resulting state.
                    continue
            rollback_task.result()
            restored.append(item.snapshot.change.path)
        except BaseException:  # noqa: BLE001 - report rollback failure, including cancellation
            if _is_restored(item.snapshot, guard):
                restored.append(item.snapshot.change.path)
            else:
                unrestored.append(item.snapshot.change.path)
    if not failed_during_mutation:
        # The failing path may have been changed by another actor after the
        # whole-plan preflight. Do not overwrite it, but report that the staged
        # transaction's original state is no longer present.
        failed_snapshot = next(
            item for item in snapshots if item.change.path == failure_path
        )
        try:
            fd = _directory_fd(failed_snapshot.target.parent)
            try:
                current = _leaf_state(fd, failed_snapshot.target.name)
            finally:
                os.close(fd)
            if not _status_matches(
                current, failed_snapshot.change.old_bytes, failed_snapshot.identity
            ):
                unrestored.append(failure_path)
        except BaseException:  # noqa: BLE001 - cannot verify means unresolved state
            unrestored.append(failure_path)
    if failed_target_unrestored:
        unrestored.append(failure_path)
    raise PatchCommitError(
        f"Patch commit failed at {failure_path}: {failure}; "
        f"committed={tuple(item.snapshot.change.path for item in committed)!r}; "
        f"restored={tuple(restored)!r}; unrestored={tuple(dict.fromkeys(unrestored))!r}",
        failure_path=failure_path,
        committed_paths=tuple(item.snapshot.change.path for item in committed),
        restored_paths=tuple(restored),
        unrestored_paths=tuple(dict.fromkeys(unrestored)),
    ) from failure
