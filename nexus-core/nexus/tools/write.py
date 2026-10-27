"""`write`: create or overwrite a whole file.

Creating a new file needs no prior read, but an existing destination is an
overwrite: it needs a complete observation of the current revision from a
direct `read` in this session. After a successful write the observation is
dropped, so the next change needs a fresh read.
"""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, resolve


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    display = str(path) if ctx.cwd_changed else args["path"]
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    guard = ctx.files
    if guard is None:  # a bare tool context (unit use), not a Nexus session
        return _write(display, path, content, path.exists())
    async with guard.mutation():
        existed = path.exists()
        if existed:
            guard.require(display, path, complete=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not existed and path.exists():
            raise ToolError(
                f"{display} appeared while it was being created; read it before overwriting it")
        result = _write(display, path, content, existed)
        guard.forget([path])
        return result


def _write(display: str, path: Any, content: str, existed: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w" if existed else "x") as destination:
            destination.write(content)
    except FileExistsError as exc:
        raise ToolError(f"{display} appeared while it was being created; read it before overwriting it") from exc
    lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return f"{'overwrote' if existed else 'created'} {display} ({lines} lines, {len(content.encode())} bytes)"


TOOL = Tool(
    ToolSpec(
        name="write",
        description="Create a file, or replace a file's entire contents. Prefer `edit` for changes to existing files.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    ),
    run,
    guidance="Create a new file, or replace a file's entire contents. An existing file must be read completely first.",
)
