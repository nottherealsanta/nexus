"""Built-in ``Write``: atomic, symlink-safe file creation and replacement."""
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

from ...errors import ToolError
from ..permissions import PathSecurityError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from .read import (
    _check_cancel,
    _error,
    _guard,
    _opt_bool,
    _require_str,
    _require_text,
    canonical_permission_key,
)

_ATOMIC_CHUNK = 64 * 1024


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; some platforms/filesystems refuse it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


async def atomic_write_bytes(
    ctx: ToolContext,
    target: Path,
    data: bytes,
    *,
    create_parents: bool,
) -> None:
    """Write ``data`` to ``target`` atomically (temp + fsync + replace).

    The parent is opened with ``O_NOFOLLOW`` (and ``O_DIRECTORY`` where
    available) and its device/inode is compared against the path as it resolves
    now, so a parent swapped to a symlink or a different directory after the
    path was validated fails closed. The temporary file is created through the
    open directory descriptor (never by re-resolving the path) and the final
    rename is relative to that same descriptor, so it cannot be redirected
    outside the checked boundary.
    """
    parent = target.parent
    if not parent.exists():
        if not create_parents:
            raise ToolError(
                f"Parent directory does not exist: {parent} "
                "(pass create_parents=true to create it)"
            )
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"Cannot create parent directory {parent}: {exc}") from exc
    if not parent.is_dir():
        raise ToolError(f"Parent is not a directory: {parent}")

    _check_cancel(ctx)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(parent, flags)
    except OSError as exc:
        raise ToolError(f"Cannot open parent directory {parent}: {exc}") from exc
    try:
        opened = os.fstat(directory_fd)
        try:
            current = os.stat(parent)
        except OSError as exc:
            raise ToolError(f"Parent directory vanished: {parent}: {exc}") from exc
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ToolError(
                f"Parent directory changed after validation: {parent}; "
                "refusing to write"
            )
        temp_name = f".nexus-write-{secrets.token_hex(8)}"
        try:
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            raise ToolError(
                f"Cannot create a temporary file in {parent}: {exc}"
            ) from exc
        try:
            with os.fdopen(temp_fd, "wb") as handle:
                view = memoryview(data)
                for start in range(0, len(data), _ATOMIC_CHUNK):
                    _check_cancel(ctx)
                    handle.write(view[start : start + _ATOMIC_CHUNK])
                    await asyncio.sleep(0)
                handle.flush()
                os.fsync(handle.fileno())
            _check_cancel(ctx)
            os.replace(
                temp_name,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            _fsync_directory(parent)
        except BaseException:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(directory_fd)


_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Destination file, relative to the workspace.",
        },
        "content": {
            "type": "string",
            "description": "Full UTF-8 file content to write.",
        },
        "create_parents": {
            "type": "boolean",
            "description": "Create missing parent directories (default false).",
        },
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="Write",
    description=(
        "Write a UTF-8 text file, replacing it atomically. Parent directories "
        "are only created when create_parents is true."
    ),
    input_schema=_WRITE_SCHEMA,
    bundle="fs",
    mutates=True,
    concurrency="exclusive",
    permission_key=canonical_permission_key,
    max_result_tokens=25_000,
)


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("Write arguments must be an object")
    try:
        raw = _require_str(args, "path")
        content = _require_text(args, "content")
        create_parents = _opt_bool(args, "create_parents", False)
    except ToolError as exc:
        return _error(str(exc))

    guard = _guard(ctx)
    try:
        first = guard.resolve(raw, for_write=True)
    except PathSecurityError as exc:
        return _error(str(exc))
    existed = first.absolute.exists()
    data = content.encode("utf-8")

    try:
        resolved = guard.recheck(raw, for_write=True)  # TOCTOU re-check
        await atomic_write_bytes(
            ctx, resolved.absolute, data, create_parents=create_parents
        )
    except PathSecurityError as exc:
        return _error(str(exc))
    except ToolError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"Write failed for {first.display}: {exc}")

    body = f"Wrote {len(data)} bytes to {resolved.display}"
    return ToolExecutionResult.text(
        body,
        display=f"Write {resolved.display}: {len(data)} bytes",
        metrics={
            "bytes": len(data),
            "created": not existed,
            "path": resolved.key,
        },
    )


__all__ = ["SPEC", "atomic_write_bytes", "run"]
