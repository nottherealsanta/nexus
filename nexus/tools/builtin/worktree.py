"""Session worktree tools, bound to narrow Runtime callbacks (worktree plan §4–5).

This module owns only model-facing schemas and argument bounds. Git, session
ownership, path checks, durable event emission and root transitions stay behind
the callbacks captured by :func:`build_worktree_tools`; no Runtime is exposed.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ...errors import ToolError
from ...ext.quarantine import sanitize_text
from ..spec import RegisteredTool, ToolContext, ToolExecutionResult, ToolSpec

__all__ = ["build_worktree_tools"]

Callback = Callable[..., ToolExecutionResult | str | Awaitable[ToolExecutionResult | str]]

_STATUS_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {}, "additionalProperties": False,
}
_COMMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "minLength": 1, "maxLength": 4000},
        "paths": {"type": "array", "maxItems": 500, "items": {"type": "string", "maxLength": 4096}},
    },
    "required": ["message"], "additionalProperties": False,
}
_PROMOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "continuation": {"type": "string", "maxLength": 8000},
    },
    "required": ["reason"], "additionalProperties": False,
}

_STATUS_SPEC = ToolSpec(
    name="worktree_status",
    description=(
        "Report the session's current Git worktree state: branch and base, ahead/behind, "
        "uncommitted counts, and recent commits. This tool only reads repository state."
    ),
    input_schema=_STATUS_SCHEMA,
    bundle="task",
    mutates=False,
    concurrency="parallel",
)
_COMMIT_SPEC = ToolSpec(
    name="worktree_commit",
    description=(
        "Commit selected changes in this session's owned worktree. Omit paths to commit "
        "all changes. Nexus adds its session, turn and agent trailers; supplied Nexus-* "
        "trailers are removed."
    ),
    input_schema=_COMMIT_SCHEMA,
    bundle="task",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda _args: "worktree_commit",
)
_PROMOTE_SPEC = ToolSpec(
    name="worktree_promote",
    description=(
        "Request moving this session from the main checkout into its own worktree. This "
        "changes where the session works and requires explicit user approval. Include a "
        "continuation only when it should be queued after the move."
    ),
    input_schema=_PROMOTE_SCHEMA,
    bundle="task",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda _args: "worktree_promote",
)


def _error(message: str) -> ToolExecutionResult:
    return ToolExecutionResult.text(message, is_error=True)


def _mapping_lines(value: object, prefix: str = "", *, depth: int = 0) -> list[str]:
    if depth > 4:
        return [f"{prefix}: [nested value omitted]"]
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in list(value.items())[:100]:
            label = sanitize_text(str(key), limit=100)
            child = f"{prefix}.{label}" if prefix else label
            lines.extend(_mapping_lines(item, child, depth=depth + 1))
            if len(lines) >= 200:
                lines = lines[:200]
                lines.append(f"{prefix or 'result'}: … additional fields omitted")
                break
        if len(value) > 100:
            lines.append(f"{prefix or 'result'}: … {len(value) - 100} more fields")
        return lines
    if isinstance(value, (list, tuple)):
        lines = []
        for index, item in enumerate(value[:50]):
            if isinstance(item, (Mapping, list, tuple)):
                lines.extend(_mapping_lines(item, f"{prefix}[{index}]", depth=depth + 1))
            else:
                lines.append(f"{prefix}: {sanitize_text(str(item), limit=500)}")
            if len(lines) >= 200:
                lines = lines[:200]
                lines.append(f"{prefix}: … additional items omitted")
                break
        if len(value) > 50:
            lines.append(f"{prefix}: … {len(value) - 50} more items")
        return lines or [f"{prefix}: (none)"]
    if value is None:
        rendered = "unknown"
    elif isinstance(value, (str, int, float, bool)):
        rendered = str(value)
    else:
        rendered = f"<{type(value).__name__}>"
    return [f"{prefix}: {sanitize_text(rendered, limit=500)}"]


async def _call(callback: Callback | None, args: dict[str, Any], ctx: ToolContext, name: str) -> ToolExecutionResult:
    if callback is None:
        return _error(f"{name}: this operation is unavailable for this session")
    try:
        result = callback(args, ctx)
        if inspect.isawaitable(result):
            result = await result
    except ToolError as exc:
        return _error(f"{name}: {sanitize_text(str(exc), limit=500)}")
    except Exception as exc:  # noqa: BLE001 - a callback failure is model-visible
        return _error(f"{name}: {sanitize_text(f'{type(exc).__name__}: {exc}', limit=500)}")
    if isinstance(result, ToolExecutionResult):
        return result
    if isinstance(result, str):
        return ToolExecutionResult.text(sanitize_text(result, limit=16_000))
    if isinstance(result, Mapping):
        lines = _mapping_lines(result)
        text = "\n".join(lines[:200])
        if len(lines) > 200:
            text += f"\n… {len(lines) - 200} more fields omitted"
        return ToolExecutionResult.text(text or f"{name}: completed")
    return _error(f"{name}: the bound operation returned an unsupported result")


def _checked_commit(args: dict[str, Any]) -> str | None:
    message = args.get("message")
    paths = args.get("paths")
    if not isinstance(message, str) or not message.strip() or len(message) > 4000 or "\x00" in message:
        return "worktree_commit requires a non-empty message of at most 4,000 characters"
    if paths is not None and (
        not isinstance(paths, list) or len(paths) > 500
        or any(not isinstance(path, str) or len(path) > 4096 or "\x00" in path for path in paths)
    ):
        return "worktree_commit paths must contain at most 500 bounded path strings"
    return None


def _checked_promote(args: dict[str, Any]) -> str | None:
    reason, continuation = args.get("reason"), args.get("continuation", "")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or "\x00" in reason:
        return "worktree_promote requires a non-empty reason of at most 500 characters"
    if not isinstance(continuation, str) or len(continuation) > 8000 or "\x00" in continuation:
        return "worktree_promote continuation must be at most 8,000 characters"
    return None


def build_worktree_tools(
    status: Callback,
    commit: Callback | None,
    promote: Callback | None,
    *,
    owned: bool,
) -> tuple[RegisteredTool, ...]:
    """Build capabilities for one session, capturing only operation callbacks.

    Status is available for any Git session. Owned worktree sessions receive
    commit; main/external sessions receive promote. Runtime must force an
    explicit confirmation for ``worktree_promote`` through its permission
    engine; the mutating declaration alone is not an approval guarantee.
    """

    async def status_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        return await _call(status, args, ctx, "worktree_status")

    tools = [RegisteredTool(spec=_STATUS_SPEC, run=status_run, origin="builtin")]
    if owned:
        async def commit_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
            invalid = _checked_commit(args)
            return _error(invalid) if invalid else await _call(commit, args, ctx, "worktree_commit")

        tools.append(RegisteredTool(spec=_COMMIT_SPEC, run=commit_run, origin="builtin"))
    else:
        async def promote_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
            invalid = _checked_promote(args)
            return _error(invalid) if invalid else await _call(promote, args, ctx, "worktree_promote")

        tools.append(RegisteredTool(spec=_PROMOTE_SPEC, run=promote_run, origin="builtin"))
    return tuple(tools)
