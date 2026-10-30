"""`edit`: exact string replacement in one file (`patch` is the multi-file tool).

`path` + `old_string` + `new_string` (+ `replace_all`): `old_string` must occur
exactly once unless `replace_all` is true.
"""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, line_span, resolve

# -- Replacement ------------------------------------------------------------


def _occurrences(text: str, old: str, limit: int | None) -> list[int]:
    found: list[int] = []
    at = 0
    while limit is None or len(found) < limit:
        at = text.find(old, at)
        if at < 0:
            break
        found.append(at)
        at += len(old)
    return found


async def _replace(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    display = str(path) if ctx.cwd_changed else args["path"]
    old, new = args.get("old_string"), args.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not old:
        raise ToolError("old_string (non-empty) and new_string must be strings")
    if old == new:
        raise ToolError("old_string and new_string are identical")
    guard = ctx.files
    if guard is None:  # a bare tool context (unit use), not a Nexus session
        return _replace_now(display, path, old, new, args.get("replace_all"))
    async with guard.mutation():
        if not path.is_file():
            raise ToolError(f"{display} does not exist")
        text = path.read_text(newline="")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"old_string was not found in {display}")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string occurs {count} times in {display}; add context or set replace_all")
        replaced = count if args.get("replace_all") else 1
        positions = _occurrences(text, old, None if args.get("replace_all") else 1)
        affected = [line_span(text, at, at + len(old)) for at in positions]
        guard.require(display, path, affected=affected)
        path.write_text(text.replace(old, new, replaced))
        guard.forget([path])
        return f"edited {display} ({replaced} replacement{'s' if replaced != 1 else ''})"


def _replace_now(display: str, path: Any, old: str, new: str, replace_all: Any) -> str:
    if not path.is_file():
        raise ToolError(f"{display} does not exist")
    text = path.read_text(newline="")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old_string was not found in {display}")
    if count > 1 and not replace_all:
        raise ToolError(f"old_string occurs {count} times in {display}; add context or set replace_all")
    replaced = count if replace_all else 1
    path.write_text(text.replace(old, new, replaced))
    return f"edited {display} ({replaced} replacement{'s' if replaced != 1 else ''})"


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    if args.get("patch") is not None:
        raise ToolError("edit takes path, old_string and new_string; for a patch use the `patch` tool")
    return await _replace(args, ctx)


TOOL = Tool(
    ToolSpec(
        name="edit",
        description=(
            "Replace exact text in one existing file. `old_string` must match the file exactly and be unique, "
            "unless `replace_all` is true. The lines you change must have been read in this session, and after "
            "any change or external edit you must read again before the next one. For several changes or "
            "several files, use `patch`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string", "description": "The exact text to replace."},
                "new_string": {"type": "string", "description": "The replacement text."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    ),
    run,
)
