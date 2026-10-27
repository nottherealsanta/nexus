"""`write`: create or overwrite a whole file."""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, resolve


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return f"{'overwrote' if existed else 'created'} {args['path']} ({lines} lines, {len(content.encode())} bytes)"


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
    guidance="Create a new file, or replace a file's entire contents.",
)
