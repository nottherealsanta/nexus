"""Built-in ``Glob``: deterministic, workspace-rooted path matching."""
from __future__ import annotations

from typing import Any

from ...errors import ToolError
from ..permissions import PathSecurityError
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from .read import (
    _byte_budget,
    _cap_lines,
    _check_cancel,
    _error,
    _glob_match,
    _guard,
    _opt_bool,
    _opt_str,
    _require_str,
    _resolve_directory,
    _truncation_marker,
    _walk_entries,
    canonical_root_key,
    glob_pattern_error,
)

_DEFAULT_MAX_MATCHES = 1000
_MAX_SCAN_ENTRIES = 50_000
_DEFAULT_MAX_BYTES = 128 * 1024
#: Bound the pattern so matching cost and recursion stay finite.
_MAX_PATTERN_CHARS = 1024
_MAX_PATTERN_SEGMENTS = 64

_GLOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Shell glob relative to the root, e.g. '**/*.py'.",
        },
        "path": {
            "type": "string",
            "description": "Directory to search under, relative to the workspace.",
        },
        "include_hidden": {
            "type": "boolean",
            "description": "Include dot-prefixed entries (default false).",
        },
    },
    "required": ["pattern"],
    "additionalProperties": False,
}

SPEC = ToolSpec(
    name="Glob",
    description=(
        "List workspace files and directories matching a shell glob, sorted "
        "deterministically. Symlinks escaping the workspace are pruned."
    ),
    input_schema=_GLOB_SCHEMA,
    bundle="fs",
    mutates=False,
    concurrency="parallel",
    permission_key=canonical_root_key,
    max_result_tokens=25_000,
)


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, dict):
        return _error("Glob arguments must be an object")
    try:
        pattern = _require_str(args, "pattern")
        root_raw = _opt_str(args, "path", ".") or "."
        include_hidden = _opt_bool(args, "include_hidden", False)
    except ToolError as exc:
        return _error(str(exc))
    if "\x00" in pattern:
        return _error("Glob pattern must not contain a NUL byte")
    if len(pattern) > _MAX_PATTERN_CHARS:
        return _error(
            f"Glob pattern is too long ({len(pattern)} characters; limit "
            f"{_MAX_PATTERN_CHARS})"
        )
    if pattern.startswith("/"):
        return _error("Glob pattern must be relative to the search root")
    pattern = pattern.removeprefix("./")
    if not pattern:
        return _error("Glob pattern must be a non-empty string")
    dir_only = pattern.endswith("/")
    if dir_only:
        pattern = pattern.rstrip("/")
    if not pattern:
        return _error("Glob pattern must name at least one path segment")
    if pattern.count("/") + 1 > _MAX_PATTERN_SEGMENTS:
        return _error(
            f"Glob pattern has too many path segments (limit "
            f"{_MAX_PATTERN_SEGMENTS})"
        )
    glob_error = glob_pattern_error(pattern)
    if glob_error is not None:
        return _error(f"Glob pattern rejected: {glob_error}")

    guard = _guard(ctx)
    try:
        root = _resolve_directory(guard, root_raw)
    except PathSecurityError as exc:
        return _error(str(exc))
    except ToolError as exc:
        return _error(str(exc))

    max_bytes = _byte_budget(ctx, _DEFAULT_MAX_BYTES)
    matches: list[tuple[str, bool]] = []
    total_matches = 0
    truncated = False
    for _path, rel, is_dir, _is_symlink in _walk_entries(
        root.absolute,
        include_hidden=include_hidden,
        denied_roots=guard.read_denyroots,
        max_entries=_MAX_SCAN_ENTRIES,
        ctx=ctx,
    ):
        _check_cancel(ctx)
        if _glob_match(rel, pattern, is_dir=is_dir, dir_only=dir_only):
            total_matches += 1
            if len(matches) < _DEFAULT_MAX_MATCHES:
                matches.append((rel, is_dir))
            else:
                truncated = True

    matches.sort(key=lambda item: (item[0], item[1]))
    lines = [f"{rel}/" if is_dir else rel for rel, is_dir in matches]
    shown, byte_truncated = _cap_lines(lines, max_bytes)
    truncated = truncated or byte_truncated or len(shown) < total_matches

    body = "\n".join(shown)
    context_note: str | None = None
    if truncated:
        marker = _truncation_marker(
            "Glob",
            len(shown),
            total_matches,
            "matches",
            "narrow the pattern or path",
        )
        body = f"{body}\n{marker}" if body else marker
        context_note = marker
    display = f"Glob {pattern!r}: {len(shown)} of {total_matches} matches"
    if truncated:
        display += " (truncated)"
    return ToolExecutionResult.text(
        body,
        display=display,
        context_note=context_note,
        metrics={
            "matches": len(shown),
            "total_matches": total_matches,
            "truncated": truncated,
            "root": root.key,
        },
    )


__all__ = ["SPEC", "run"]
