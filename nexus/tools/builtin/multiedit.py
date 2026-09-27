"""Built-in ``MultiEdit``: validate every edit, then commit once atomically."""
from __future__ import annotations

from typing import Any

from ...errors import ToolError
from ..permissions import PathSecurityError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from .read import (
    _check_cancel,
    _error,
    _guard,
    _read_text_file,
    _require_str,
    canonical_permission_key,
)
from .write import atomic_write_bytes

_MAX_EDIT_BYTES = 5 * 1024 * 1024

_EDIT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
    "required": ["old_string", "new_string"],
    "additionalProperties": False,
}

_MULTIEDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File to edit, relative to the workspace.",
        },
        "edits": {
            "type": "array",
            "description": (
                "Edits applied in order to the progressively-updated content; "
                "each must match exactly once unless replace_all is true."
            ),
            "items": _EDIT_ITEM_SCHEMA,
        },
    },
    "required": ["path", "edits"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="multiedit",
    description=(
        "Apply several exact string edits to one UTF-8 file in a single atomic "
        "commit. If any edit is invalid, nothing is written."
    ),
    input_schema=_MULTIEDIT_SCHEMA,
    bundle="legacy_fs",
    mutates=True,
    concurrency="exclusive",
    permission_key=canonical_permission_key,
    max_result_tokens=25_000,
)


def _parse_edits(raw_edits: Any) -> list[tuple[str, str, bool]]:
    if not isinstance(raw_edits, list) or not raw_edits:
        raise ToolError("edits must be a non-empty array")
    parsed: list[tuple[str, str, bool]] = []
    for index, item in enumerate(raw_edits):
        if not isinstance(item, dict):
            raise ToolError(f"edits[{index}] must be an object")
        old = item.get("old_string")
        new = item.get("new_string")
        replace_all = item.get("replace_all", False)
        if not isinstance(old, str) or not old:
            raise ToolError(f"edits[{index}].old_string must be a non-empty string")
        if not isinstance(new, str):
            raise ToolError(f"edits[{index}].new_string must be a string")
        if not isinstance(replace_all, bool):
            raise ToolError(f"edits[{index}].replace_all must be a boolean")
        if old == new:
            raise ToolError(
                f"edits[{index}] old_string and new_string are identical"
            )
        parsed.append((old, new, replace_all))
    return parsed


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("MultiEdit arguments must be an object")
    try:
        raw = _require_str(args, "path")
        edits = _parse_edits(args.get("edits"))
    except ToolError as exc:
        return _error(str(exc))

    guard = _guard(ctx)
    try:
        resolved = guard.resolve(raw, for_write=True)
    except PathSecurityError as exc:
        return _error(str(exc))

    try:
        working = _read_text_file(resolved.absolute, _MAX_EDIT_BYTES)
    except ToolError as exc:
        return _error(str(exc))

    total = 0
    for index, (old, new, replace_all) in enumerate(edits):
        _check_cancel(ctx)
        count = working.count(old)
        if replace_all:
            if count == 0:
                return _error(
                    f"MultiEdit edit {index} found no match in {resolved.display}; "
                    "no changes were written"
                )
            working = working.replace(old, new)
            total += count
        else:
            if count != 1:
                return _error(
                    f"MultiEdit edit {index} expected exactly one match in "
                    f"{resolved.display}, found {count}; no changes were written"
                )
            working = working.replace(old, new, 1)
            total += 1

    _check_cancel(ctx)
    data = working.encode("utf-8")
    try:
        resolved = guard.recheck(raw, for_write=True)  # TOCTOU re-check
        await atomic_write_bytes(ctx, resolved.absolute, data, create_parents=False)
    except PathSecurityError as exc:
        return _error(str(exc))
    except ToolError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"MultiEdit failed for {resolved.display}: {exc}")

    body = (
        f"Applied {len(edits)} edit(s) to {resolved.display} "
        f"({total} replacement(s))"
    )
    return ToolExecutionResult.text(
        body,
        display=f"MultiEdit {resolved.display}: {len(edits)} edit(s)",
        metrics={
            "edits": len(edits),
            "replacements": total,
            "bytes": len(data),
            "path": resolved.key,
        },
    )


__all__ = ["SPEC", "run"]
