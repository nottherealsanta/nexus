"""Built-in ``apply_patch``: validate and apply bounded multi-file patches."""
from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any

from ...errors import OperationCancelled
from ..permissions import PathGuard, PathSecurityError
from ..spec import PathTarget, ToolContext, ToolExecutionResult, ToolSpec
from ._patch_commit import PatchCommitError, _directory_fd, commit_staged
from ._patch_parse import (
    PatchOperation,
    PatchParseError,
    operation_path_refs,
    parse_patch,
)
from ._patch_stage import PatchStageError, stage_patch
from .read import _permissions

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RESULT_CHARS = 2_100_000


def _operation_targets(operations: tuple[PatchOperation, ...]) -> tuple[PathTarget, ...]:
    targets: list[PathTarget] = []
    for operation in operations:
        roles = ("source", "destination") if operation.kind == "move" else (
            ("destination",) if operation.kind == "add" else ("source",)
        )
        targets.extend(
            PathTarget(role, path)
            for role, path in zip(roles, operation_path_refs(operation))
        )
    return tuple(targets)


def _resolve_targets(data: dict[str, Any]) -> tuple[PathTarget, ...]:
    return _operation_targets(parse_patch(data["patch"]))


def _permission_key(_data: dict[str, Any]) -> str:
    # Multi-target authorization is authoritative; this key exists only to
    # satisfy the scalar permission contract and is never used for path grants.
    return "apply_patch"


SPEC = ToolSpec(
    name="apply_patch",
    description=(
        "Apply a strict multi-file patch containing add, update, delete, and "
        "move operations. Hunk context must match exactly."
    ),
    input_schema={
        "type": "object",
        "properties": {"patch": {"type": "string"}},
        "required": ["patch"],
        "additionalProperties": False,
    },
    bundle="patch",
    mutates=True,
    concurrency="exclusive",
    permission_key=_permission_key,
    multi_path_targets=_resolve_targets,
)


def _guard(ctx: ToolContext) -> PathGuard:
    permissions = _permissions(ctx)
    return PathGuard(
        ctx.workspace,
        write_roots=tuple(permissions.write_roots) or ("./",),
        read_denyroots=tuple(permissions.read_denyroots),
    )


def _cancel(ctx: ToolContext) -> None:
    if ctx.cancel_token is not None:
        ctx.cancel_token.raise_if_cancelled()


def _reject_symlink_components(guard: PathGuard, raw: str) -> Path:
    """Resolve a workspace-relative leaf and refuse symlinks in every component."""
    resolved = guard.recheck(raw, for_write=True)
    candidate = Path(raw)
    if candidate.is_absolute():
        absolute = candidate
    else:
        absolute = guard.workspace / candidate
    try:
        relative = absolute.relative_to(guard.workspace)
    except ValueError as exc:
        raise PathSecurityError("Patch path is outside the workspace", code="write_root") from exc

    current = guard.workspace
    for component in relative.parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            # Remaining suffixes cannot contain an existing symlink. The
            # commit helper separately requires the parent to exist.
            break
        if stat.S_ISLNK(info.st_mode):
            raise PathSecurityError("Patch paths must not contain symlinks", code="symlink_target")
    if resolved.absolute != absolute:
        raise PathSecurityError("Patch path resolved through a symlink", code="symlink_target")
    return absolute


def _snapshot(path: Path) -> bytes | None:
    directory_fd = _directory_fd(path.parent)
    try:
        try:
            info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise PathSecurityError("Patch paths must not contain symlinks", code="symlink_target")
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"patch target is not a regular file: {path.name}")
        if info.st_size > MAX_FILE_BYTES:
            raise ValueError(f"patch file exceeds {MAX_FILE_BYTES} byte limit: {path.name}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path.name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError(f"patch target changed while opening: {path.name}")
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"patch target is not a regular file: {path.name}")
            if before.st_size > MAX_FILE_BYTES:
                raise ValueError(f"patch file exceeds {MAX_FILE_BYTES} byte limit: {path.name}")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, MAX_FILE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError(f"patch file exceeds {MAX_FILE_BYTES} byte limit: {path.name}")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise ValueError(f"patch target changed while reading: {path.name}")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _bounded_result(status_text: str, diff: str) -> tuple[str, bool]:
    prefix = f"Applied patch.\n{status_text}\n\n"
    available = MAX_RESULT_CHARS - len(prefix)
    if len(diff) <= available:
        return prefix + diff, False
    marker = "\n[combined diff truncated]\n"
    if available <= len(marker):
        return prefix + marker[: max(0, available)], True
    return prefix + diff[: available - len(marker)] + marker, True


def _is_cancellation(exc: BaseException) -> BaseException | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (asyncio.CancelledError, OperationCancelled)):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict) or not isinstance(args.get("patch"), str):
        return ToolExecutionResult.text("apply_patch requires a string patch", is_error=True)
    try:
        _cancel(ctx)
        operations = parse_patch(args["patch"])
        targets = _operation_targets(operations)
        guard = _guard(ctx)
        snapshots: dict[str, bytes | None] = {}
        total = 0
        for target in targets:
            _cancel(ctx)
            absolute = _reject_symlink_components(guard, target.path)
            data = _snapshot(absolute)
            if data is not None:
                total += len(data)
                if total > MAX_TOTAL_BYTES:
                    raise ValueError(f"patch snapshots exceed {MAX_TOTAL_BYTES} byte total limit")
            snapshots[target.path] = data
        staged = stage_patch(operations, snapshots)
        _cancel(ctx)
        statuses = await commit_staged(
            staged, guard, ctx.workspace, ctx=ctx, cancellation=ctx.cancel_token
        )
    except (asyncio.CancelledError, OperationCancelled):
        raise
    except PatchCommitError as exc:
        cancellation = _is_cancellation(exc)
        if cancellation is not None:
            raise cancellation
        status_by_path = {path: "unchanged" for path in snapshots}
        for path in exc.committed_paths:
            status_by_path[path] = "committed"
        for path in exc.restored_paths:
            status_by_path[path] = "restored"
        for path in exc.unrestored_paths:
            status_by_path[path] = "unrestored"
        partial = ", ".join(f"{path}={state}" for path, state in status_by_path.items())
        message = f"apply_patch commit failed; exact partial state: {partial}; {exc}"
        return ToolExecutionResult.text(message, is_error=True)
    except (PatchParseError, PatchStageError, PathSecurityError, ValueError, OSError) as exc:
        return ToolExecutionResult.text(f"apply_patch failed: {exc}", is_error=True)

    status_text = "\n".join(f"{item.path}: {item.status}" for item in statuses)
    bounded, truncated = _bounded_result(status_text, staged.diff)
    return ToolExecutionResult.text(
        bounded,
        display=f"apply_patch: {len(statuses)} file change(s)" + (" (diff truncated)" if truncated else ""),
        metrics={"files": len(statuses), "diff_truncated": truncated},
    )


__all__ = ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "SPEC", "run"]
