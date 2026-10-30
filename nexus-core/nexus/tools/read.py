"""`read`: a text file with line numbers, by line window.

A direct, model-visible file read records an observation for this session: the
content hash, total lines, the line window that entered context and any clipped
lines. Directory listings are not file reads. A read inside a `code` kernel is
retained by the kernel, not shown to the model, so it records nothing.
"""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import MAX_OUTPUT_CHARS, Tool, ToolContext, ToolError, clip, resolve

DEFAULT_LIMIT = 2000
MAX_LINE_CHARS = 2000


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    display = str(path) if ctx.cwd_changed else args["path"]
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or DEFAULT_LIMIT)
    if offset < 1 or limit < 1:
        raise ToolError("offset and limit must be positive")
    if path.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return clip(f"{display} is a directory ({len(entries)} entries):\n" + "\n".join(entries))
    if not path.is_file():
        raise ToolError(f"{display} does not exist")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ToolError(f"{display} looks binary ({len(data)} bytes)")
    lines = data.decode("utf-8", "replace").splitlines()
    if not lines:
        _record(ctx, path, data, lines=0, ranges=[], partial=[], clipped=False)
        return f"{display} is empty"
    if offset > len(lines):
        raise ToolError(f"offset {offset} is past the end of {display} ({len(lines)} lines)")
    window = lines[offset - 1 : offset - 1 + limit]
    out = []
    partial: list[int] = []
    for number, line in enumerate(window, start=offset):
        if len(line) > MAX_LINE_CHARS:
            partial.append(number)
            line = line[:MAX_LINE_CHARS] + f" [… line clipped at {MAX_LINE_CHARS} of {len(line)} characters]"
        out.append(f"{number:>6}\t{line}")
    end = offset - 1 + len(window)
    footer = f"\n[lines {offset}–{end} of {len(lines)}]" if (offset > 1 or end < len(lines)) else ""
    body = "\n".join(out) + footer
    clipped = len(body) > MAX_OUTPUT_CHARS
    # A clipped whole-output read establishes no coverage: the middle never
    # reached the model. Reading smaller windows does.
    _record(ctx, path, data, lines=len(lines), ranges=[] if clipped else [(offset, end)],
            partial=partial, clipped=clipped)
    prefix = f"[path: {display}]\n" if ctx.cwd_changed else ""
    return clip(prefix + body)


def _record(ctx: ToolContext, path: Any, data: bytes, *, lines: int, ranges: list[tuple[int, int]],
            partial: list[int], clipped: bool) -> None:
    if ctx.files is None or not ctx.model_visible:
        return
    ctx.files.record_read(path, data, lines=lines, ranges=ranges, partial=partial,
                          clipped=clipped, call_id=ctx.call_id)


TOOL = Tool(
    ToolSpec(
        name="read",
        description="Read a text file (with line numbers) or list a directory. Paths are relative to the workspace. "
                    "Use `offset` and `limit` for large files. Read a file before you change it.",
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
