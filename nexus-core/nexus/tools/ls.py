"""`ls`: one directory, as it is on disk.

Adapted from old Nexus nexus/tools/builtin/ls.py. Unlike `glob` and `grep` it
does not apply `.gitignore`: a listing shows what is there. Directories end in
`/`; a symlink ends in `@` and never reveals a target outside the workspace.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip
from .walk import resolve_dir

MAX_ENTRIES = 1000


def _describe(entry: os.DirEntry[str], workspace: Path) -> str:
    if entry.is_symlink():
        real = Path(os.path.realpath(entry.path))
        if real == workspace or workspace in real.parents:
            return f"{entry.name}@ -> {real.relative_to(workspace).as_posix()}"
        return f"{entry.name}@"
    return entry.name + "/" if entry.is_dir(follow_symlinks=False) else entry.name


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    root = resolve_dir(ctx, args.get("path"))
    hidden = bool(args.get("include_hidden"))
    try:
        with os.scandir(root) as iterator:
            entries = sorted((e for e in iterator if hidden or not e.name.startswith(".")), key=lambda e: e.name)
    except OSError as exc:
        raise ToolError(f"cannot list {args.get('path') or '.'}: {exc}") from exc
    workspace = ctx.workspace.resolve()
    lines = [_describe(entry, workspace) for entry in entries[:MAX_ENTRIES]]
    if len(entries) > MAX_ENTRIES:
        lines.append(f"[showing {MAX_ENTRIES} of {len(entries)} entries; list a narrower directory]")
    return clip("\n".join(lines) if lines else "(empty directory)")


TOOL = Tool(
    ToolSpec(
        name="ls",
        description="List one directory in sorted order. Directories end in `/`, symlinks in `@`.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list. Default: the workspace."},
                "include_hidden": {"type": "boolean", "description": "Include dot-prefixed entries. Default false."},
            },
        },
    ),
    run,
    guidance="List a directory (`ls`); use `glob` or `grep` to search below it.",
)
