"""`multiedit`: several exact replacements in one file, all or nothing.

Adapted from old Nexus nexus/tools/builtin/multiedit.py. Edits apply in order
to the progressively edited text, so later edits can match earlier results.
Positions in the intermediate text do not map to the lines the model read, so
unlike `edit` this needs a complete observation of the file's current revision.
"""
from __future__ import annotations

from typing import Any

from ..llmhub.types import ToolSpec
from .base import Tool, ToolContext, ToolError, resolve


def _parse(raw: Any) -> list[tuple[str, str, bool]]:
    if not isinstance(raw, list) or not raw:
        raise ToolError("edits must be a non-empty array")
    parsed = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ToolError(f"edits[{index}] must be an object")
        old, new, every = item.get("old_string"), item.get("new_string"), item.get("replace_all", False)
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise ToolError(f"edits[{index}]: old_string (non-empty) and new_string must be strings")
        if not isinstance(every, bool):
            raise ToolError(f"edits[{index}].replace_all must be a boolean")
        if old == new:
            raise ToolError(f"edits[{index}]: old_string and new_string are identical")
        parsed.append((old, new, every))
    return parsed


def _apply(display: str, text: str, edits: list[tuple[str, str, bool]]) -> tuple[str, int]:
    total = 0
    for index, (old, new, every) in enumerate(edits):
        count = text.count(old)
        if count == 0:
            raise ToolError(f"edit {index}: old_string was not found in {display}; nothing was written")
        if count > 1 and not every:
            raise ToolError(f"edit {index}: old_string occurs {count} times in {display}; add context or set "
                            "replace_all; nothing was written")
        text = text.replace(old, new) if every else text.replace(old, new, 1)
        total += count if every else 1
    return text, total


async def run(args: dict[str, Any], ctx: ToolContext) -> str:
    path = resolve(ctx, args.get("path"))
    display = str(path) if ctx.cwd_changed else args["path"]
    edits = _parse(args.get("edits"))
    guard = ctx.files

    def apply() -> str:
        if not path.is_file():
            raise ToolError(f"{display} does not exist")
        if guard is not None:
            guard.require(display, path, complete=True)
        updated, total = _apply(display, path.read_text(newline=""), edits)
        path.write_text(updated)
        if guard is not None:
            guard.forget([path])
        return f"edited {display} ({len(edits)} edits, {total} replacement{'s' if total != 1 else ''})"

    if guard is None:
        return apply()
    async with guard.mutation():
        return apply()


TOOL = Tool(
    ToolSpec(
        name="multiedit",
        description="Apply several exact string replacements to one file in order, in one step. "
                    "If any edit fails, nothing is written.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "description": "Applied in order to the progressively edited text. Each old_string must "
                                   "occur exactly once unless replace_all is true.",
                    "items": {
                        "type": "object",
                        "properties": {"old_string": {"type": "string"}, "new_string": {"type": "string"},
                                       "replace_all": {"type": "boolean"}},
                        "required": ["old_string", "new_string"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    ),
    run,
    guidance="Several replacements in one file at once, all or nothing. The whole file must have been read in "
             "this session; use `edit` for a single change.",
)
