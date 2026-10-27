"""`read`: a text file with line numbers, by line window."""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, clip, resolve

DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or DEFAULT_LIMIT)
    if offset < 1 or limit < 1:
        raise ToolError("offset and limit must be positive")
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return clip(f"{args['path']} is a directory ({len(entries)} entries):\n" + "\n".join(entries))
    if not path.is_file():
        raise ToolError(f"{args['path']} does not exist")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"{args['path']} looks binary ({len(data)} bytes)")
    lines = data.decode("utf-8", "replace").splitlines()
    if lines and offset > len(lines):
        raise ToolError(f"offset {offset} is past the end of {args['path']} ({len(lines)} lines)")
    window = lines[offset - 1 : offset - 1 + limit]
    out = []
    for number, line in enumerate(window, start=offset):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + f" [… line clipped at {MAX_LINE_CHARS} of {len(line)} characters]"
        out.append(f"{number:>6}\t{line}")
    end = offset - 1 + len(window)
    if not lines:
        return f"{args['path']} is empty"
    footer = f"\n[lines {offset}–{end} of {len(lines)}]" if (offset > 1 or end < len(lines)) else ""
    return clip("\n".join(out) + footer)


TOOL = Tool(
    ToolSpec(
        name="read",
        description="Read a text file (with line numbers) or list a directory. Paths are relative to the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path."},
                "offset": {"type": "integer", "description": "First line to read, 1-based. Default 1."},
                "limit": {"type": "integer", "description": f"Number of lines. Default {DEFAULT_LIMIT}."},
            },
            "required": ["path"],
        },
    ),
    run,
)
