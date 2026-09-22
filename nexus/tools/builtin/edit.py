"""Built-in ``Edit``: exact, single-commit string replacement."""
from __future__ import annotations

from typing import Any

from ...errors import ToolError
from ..permissions import PathSecurityError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from .read import (
    _check_cancel,
    _error,
    _guard,
    _opt_bool,
    _opt_int,
    _read_text_file,
    _require_str,
    _require_text,
    canonical_permission_key,
)
from .write import atomic_write_bytes

_MAX_EDIT_BYTES = 5 * 1024 * 1024


def _nth_index(text: str, needle: str, occurrence: int) -> int:
    index = -1
    for _ in range(occurrence):
        index = text.index(needle, index + 1)
    return index


_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to edit, relative to the workspace.",
        },
        "old_string": {
            "type": "string",
            "description": "Exact text to replace; must be non-empty.",
        },
        "new_string": {
            "type": "string",
            "description": "Replacement text (may be empty to delete).",
        },
        "replace_all": {
            "type": "boolean",
            "description": "Replace every occurrence instead of exactly one.",
        },
        "occurrence": {
            "type": "integer",
            "description": "1-based occurrence to replace when it is not unique.",
        },
    },
    "required": ["path", "old_string", "new_string"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="Edit",
    description=(
        "Replace exact text in a UTF-8 file. By default old_string must occur "
        "exactly once; use replace_all or occurrence to disambiguate."
    ),
    input_schema=_EDIT_SCHEMA,
    bundle="fs",
    mutates=True,
    concurrency="exclusive",
    permission_key=canonical_permission_key,
    max_result_tokens=25_000,
)


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("Edit arguments must be an object")
    try:
        raw = _require_str(args, "path")
        old_string = _require_str(args, "old_string")
        new_string = _require_text(args, "new_string")
        replace_all = _opt_bool(args, "replace_all", False)
    except ToolError as exc:
        return _error(str(exc))

    occurrence: int | None = None
    if args.get("occurrence") is not None:
        try:
            occurrence = _opt_int(args, "occurrence", 0)
        except ToolError as exc:
            return _error(str(exc))
        if occurrence < 1:
            return _error("Edit occurrence must be >= 1")
    if replace_all and occurrence is not None:
        return _error("Edit accepts either replace_all or occurrence, not both")
    if old_string == new_string:
        return _error("Edit old_string and new_string are identical; nothing to do")

    guard = _guard(ctx)
    try:
        resolved = guard.resolve(raw, for_write=True)
    except PathSecurityError as exc:
        return _error(str(exc))

    try:
        text = _read_text_file(resolved.absolute, _MAX_EDIT_BYTES)
    except ToolError as exc:
        return _error(str(exc))

    count = text.count(old_string)
    if count == 0:
        return _error(f"Edit found no match for old_string in {resolved.display}")
    if replace_all:
        updated = text.replace(old_string, new_string)
        replaced = count
    elif occurrence is not None:
        if occurrence > count:
            return _error(
                f"Edit occurrence {occurrence} exceeds the {count} match(es) in "
                f"{resolved.display}"
            )
        index = _nth_index(text, old_string, occurrence)
        updated = text[:index] + new_string + text[index + len(old_string) :]
        replaced = 1
    else:
        if count != 1:
            return _error(
                f"Edit expected exactly one match for old_string in "
                f"{resolved.display}, found {count}; use replace_all or occurrence"
            )
        updated = text.replace(old_string, new_string, 1)
        replaced = 1

    _check_cancel(ctx)
    data = updated.encode("utf-8")
    try:
        resolved = guard.recheck(raw, for_write=True)  # TOCTOU re-check
        await atomic_write_bytes(ctx, resolved.absolute, data, create_parents=False)
    except PathSecurityError as exc:
        return _error(str(exc))
    except ToolError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"Edit failed for {resolved.display}: {exc}")

    body = f"Edited {resolved.display}: replaced {replaced} occurrence(s)"
    return ToolExecutionResult.text(
        body,
        display=f"Edit {resolved.display}: {replaced} replacement(s)",
        metrics={"replacements": replaced, "bytes": len(data), "path": resolved.key},
    )


__all__ = ["SPEC", "run"]
