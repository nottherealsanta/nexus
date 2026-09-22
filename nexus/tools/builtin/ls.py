"""Built-in ``LS``: deterministic, bounded directory listing."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...errors import ToolError
from ..permissions import PathSecurityError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from .read import (
    _byte_budget,
    _cap_lines,
    _check_cancel,
    _error,
    _guard,
    _is_denied,
    _opt_bool,
    _opt_str,
    _resolve_directory,
    _truncation_marker,
    _within,
    canonical_root_key,
)

_DEFAULT_MAX_ENTRIES = 1000
_HARD_MAX_ENTRIES = 10_000
_DEFAULT_MAX_BYTES = 128 * 1024

_LS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to list, relative to the workspace (default '.').",
        },
        "include_hidden": {
            "type": "boolean",
            "description": "Include dot-prefixed entries (default false).",
        },
    },
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="LS",
    description=(
        "List a workspace directory in deterministic order. Directories end in "
        "'/'; symlinks end in '@' and never expose an outside target."
    ),
    input_schema=_LS_SCHEMA,
    bundle="fs",
    mutates=False,
    concurrency="parallel",
    permission_key=canonical_root_key,
    max_result_tokens=25_000,
)


def _describe(
    entry: os.DirEntry[str], root: Path, denied_roots: tuple[Path, ...]
) -> str | None:
    """Render one entry, or ``None`` when it must be pruned (denied root)."""
    name = entry.name
    if _is_denied(Path(entry.path), denied_roots):
        return None
    if entry.is_symlink():
        try:
            real = Path(os.path.realpath(entry.path))
        except OSError:
            return f"{name}@"
        if _is_denied(real, denied_roots):
            return None
        if _within(real, root):
            try:
                target = os.path.relpath(real, root)
            except ValueError:
                target = str(real)
            return f"{name}@ -> {target}"
        return f"{name}@"
    try:
        is_dir = entry.is_dir(follow_symlinks=False)
    except OSError:
        is_dir = False
    return f"{name}/" if is_dir else name


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("LS arguments must be an object")
    try:
        root_raw = _opt_str(args, "path", ".") or "."
        include_hidden = _opt_bool(args, "include_hidden", False)
    except ToolError as exc:
        return _error(str(exc))

    guard = _guard(ctx)
    try:
        root = _resolve_directory(guard, root_raw)
    except PathSecurityError as exc:
        return _error(str(exc))
    except ToolError as exc:
        return _error(str(exc))

    _check_cancel(ctx)
    try:
        with os.scandir(root.absolute) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError as exc:
        return _error(f"Cannot list {root.display}: {exc}")

    lines: list[str] = []
    total_entries = 0
    truncated = False
    for entry in entries:
        _check_cancel(ctx)
        if not include_hidden and entry.name.startswith("."):
            continue
        description = _describe(entry, root.absolute, guard.read_denyroots)
        if description is None:
            continue
        total_entries += 1
        if len(lines) < _HARD_MAX_ENTRIES:
            lines.append(description)
        else:
            truncated = True

    max_bytes = _byte_budget(ctx, _DEFAULT_MAX_BYTES)
    shown, byte_truncated = _cap_lines(lines, max_bytes)
    truncated = truncated or byte_truncated or len(shown) < total_entries

    body = "\n".join(shown)
    context_note: str | None = None
    if truncated:
        marker = _truncation_marker(
            "LS",
            len(shown),
            total_entries,
            "entries",
            "list a narrower directory",
        )
        body = f"{body}\n{marker}" if body else marker
        context_note = marker
    elif total_entries or shown:
        context_note = (
            f"[LS {root.display}: {total_entries} entry/entries; re-run LS to see them]"
        )
    display = f"LS {root.display}: {len(shown)} of {total_entries} entries"
    if truncated:
        display += " (truncated)"
    return ToolExecutionResult.text(
        body,
        display=display,
        context_note=context_note,
        metrics={
            "entries": len(shown),
            "total_entries": total_entries,
            "truncated": truncated,
            "path": root.key,
        },
    )


__all__ = ["SPEC", "run"]
