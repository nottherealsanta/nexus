"""Session worktree tools, bound to narrow Runtime callbacks (agent-managed worktrees plan §3.3).

This module owns only model-facing schemas, argument bounds and result text.
Git, path checks, durable event emission and root transitions stay behind the
callbacks captured by :func:`build_worktree_tools`; no Runtime is exposed.

``worktree_list``, ``worktree_create`` and ``worktree_move`` only create or
switch checkouts in Nexus-owned space, so the permission engine never asks for
them. Create and move return at once; the move is applied at the next step
boundary of the running turn.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ...errors import ToolError
from ...ext.quarantine import sanitize_text
from ..spec import RegisteredTool, ToolContext, ToolExecutionResult, ToolSpec

__all__ = ["WORKTREE_TOOL_NAMES", "build_worktree_tools", "format_worktree_list"]

Callback = Callable[..., ToolExecutionResult | str | Awaitable[ToolExecutionResult | str]]

#: Every worktree tool the Runtime may add to a root session.
WORKTREE_TOOL_NAMES = frozenset({"worktree_list", "worktree_create", "worktree_move", "worktree_commit"})

_LIST_SCHEMA: dict[str, Any] = {
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
_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        "name": {"type": "string", "minLength": 1, "maxLength": 60},
        "base": {"type": "string", "minLength": 1, "maxLength": 200},
    },
    "required": ["reason"], "additionalProperties": False,
}
_MOVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "minLength": 1, "maxLength": 200},
        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["to", "reason"], "additionalProperties": False,
}

_MOVE_WARNING = (
    "The move happens after this step. Do not run tools that depend on the new directory in the "
    "same step; other tool calls of this step still run in the old directory."
)

_LIST_SPEC = ToolSpec(
    name="worktree_list",
    description=(
        "List the project's Git worktrees: the main checkout and every Nexus worktree (name, branch, "
        "base, dirty, merged or unmerged, the sessions in each, last activity), marking where you are. "
        "Call it before worktree_create to continue an existing worktree when the work belongs there. "
        "This tool only reads repository state."
    ),
    input_schema=_LIST_SCHEMA,
    bundle="task",
    mutates=False,
    concurrency="parallel",
)
_COMMIT_SPEC = ToolSpec(
    name="worktree_commit",
    description=(
        "Commit selected changes in the worktree you are in. Omit paths to commit all changes. "
        "Nexus adds its session, turn and agent trailers; supplied Nexus-* trailers are removed."
    ),
    input_schema=_COMMIT_SCHEMA,
    bundle="task",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda _args: "worktree_commit",
)
_CREATE_SPEC = ToolSpec(
    name="worktree_create",
    description=(
        "Create a new Git worktree (branch nexus/wt/<name>) and move this session into it. Changed files "
        "you wrote in the main checkout are carried over. From a worktree, the new one branches from the "
        "updated base and the current worktree is left as it is (commit first). `name` is optional "
        "(letters, digits, '-', '_'); `base` defaults to the current base branch. " + _MOVE_WARNING
    ),
    input_schema=_CREATE_SCHEMA,
    bundle="task",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda _args: "worktree_create",
)
_MOVE_SPEC = ToolSpec(
    name="worktree_move",
    description=(
        "Move this session to the main checkout (`to` = \"main\") or to an existing Nexus worktree (`to` = its "
        "name from worktree_list), whoever created it and whoever else is in it. Leaving a worktree needs its "
        "changes committed; unmerged commits are fine and the branch is kept. " + _MOVE_WARNING
    ),
    input_schema=_MOVE_SCHEMA,
    bundle="task",
    mutates=True,
    concurrency="exclusive",
    permission_key=lambda _args: "worktree_move",
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


def _sessions(items: object) -> str:
    rows = [item for item in items if isinstance(item, Mapping)] if isinstance(items, (list, tuple)) else []
    if not rows:
        return "none"
    shown = [f"{sanitize_text(str(item.get('title', '')), limit=60)}{' (active)' if item.get('active') else ''}" for item in rows[:5]]
    return ", ".join(shown) + (f", +{len(rows) - 5} more" if len(rows) > 5 else "")


def format_worktree_list(state: Mapping[str, Any]) -> str:
    """Labelled fields for ``worktree_list`` (no raw JSON)."""
    main = state.get("main") or {}
    rows = [row for row in state.get("worktrees") or [] if isinstance(row, Mapping) and row.get("kind") != "main"]
    lines = [f"current: {sanitize_text(str(state.get('current', '?')), limit=100)}",
             f"worktrees: {len(rows)} of at most {state.get('max_worktrees', '?')} "
             f"({state.get('present', '?')} checkouts present)", ""]
    lines += [
        f"- main{'  ← you are here' if main.get('current') else ''}",
        f"  branch: {sanitize_text(str(main.get('branch') or '(detached)'), limit=200)}",
        f"  path: {sanitize_text(str(main.get('path', '')), limit=500)}",
        f"  dirty: {'yes (' + str(main.get('dirty')) + ' changed files)' if main.get('dirty') else 'no'}",
        f"  sessions: {_sessions(main.get('sessions'))}",
    ]
    for row in rows:
        if row.get("merged"):
            merge = "merged"
        elif row.get("has_work"):
            merge = f"unmerged ({row.get('unmerged_commits', 0)} commits not in the base)"
        else:
            merge = "no commits of its own yet"
        lines += [
            "", f"- {sanitize_text(str(row.get('name', '?')), limit=100)}{'  ← you are here' if row.get('current') else ''}",
            f"  branch: {sanitize_text(str(row.get('branch', '')), limit=200)}",
            f"  base: {sanitize_text(str(row.get('base_branch', '')), limit=200)} @ {str(row.get('base_commit', ''))[:7]}",
            f"  checkout: {'present' if row.get('present') else 'removed (recreated when you move in)'}",
            f"  dirty: {'yes (' + str(row.get('dirty')) + ' changed files)' if row.get('dirty') else 'no'}",
            f"  state: {merge}",
            f"  sessions: {_sessions(row.get('sessions'))}",
        ]
    if state.get("clipped"):
        lines += ["", f"… {state['clipped']} more worktrees not shown (limit {state.get('max_worktrees')})."]
    external = [row for row in (state.get("external") or []) if isinstance(row, Mapping)]
    if external:
        lines += ["", "external Git worktrees (not created by Nexus; read-only info, you cannot move into them):"]
        lines += [f"- {sanitize_text(str(row.get('path', '')), limit=300)} ({sanitize_text(str(row.get('branch') or 'detached'), limit=100)})"
                  for row in external[:16]]
    return "\n".join(lines)


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
        if name == "worktree_list":
            return ToolExecutionResult.text("\n".join(sanitize_text(line, limit=1000).replace(" ← you are here", "  ← you are here") for line in format_worktree_list(result).splitlines())[:16_000])
        lines = _mapping_lines(result)
        text = "\n".join(sanitize_text(line, limit=1000) for line in lines[:200])
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


def _bounded(args: dict[str, Any], key: str, limit: int, *, required: bool) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit or "\x00" in value:
        return f"{key} must be {'a non-empty ' if required else 'a '}string of at most {limit} characters"
    return None


def _checked_create(args: dict[str, Any]) -> str | None:
    problem = _bounded(args, "reason", 500, required=True) or _bounded(args, "name", 60, required=False) \
        or _bounded(args, "base", 200, required=False)
    return f"worktree_create: {problem}" if problem else None


def _checked_move(args: dict[str, Any]) -> str | None:
    problem = _bounded(args, "to", 200, required=True) or _bounded(args, "reason", 500, required=True)
    return f"worktree_move: {problem}" if problem else None


def build_worktree_tools(
    listing: Callback,
    commit: Callback | None,
    create: Callback | None,
    move: Callback | None,
    *,
    in_worktree: bool,
) -> tuple[RegisteredTool, ...]:
    """Build capabilities for one session, capturing only operation callbacks.

    List, create and move are available to every root session of a Git project;
    commit only while the session is in a worktree. Create and move are queued
    by the Runtime and applied at the next step boundary. None of these asks for
    permission (the permission engine allows them by name; deny rules still win).
    """

    async def list_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        return await _call(listing, args, ctx, "worktree_list")

    async def create_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        invalid = _checked_create(args)
        return _error(invalid) if invalid else await _call(create, args, ctx, "worktree_create")

    async def move_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        invalid = _checked_move(args)
        return _error(invalid) if invalid else await _call(move, args, ctx, "worktree_move")

    tools = [
        RegisteredTool(spec=_LIST_SPEC, run=list_run, origin="builtin"),
        RegisteredTool(spec=_CREATE_SPEC, run=create_run, origin="builtin"),
        RegisteredTool(spec=_MOVE_SPEC, run=move_run, origin="builtin"),
    ]
    if in_worktree:
        async def commit_run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
            invalid = _checked_commit(args)
            return _error(invalid) if invalid else await _call(commit, args, ctx, "worktree_commit")

        tools.append(RegisteredTool(spec=_COMMIT_SPEC, run=commit_run, origin="builtin"))
    return tuple(tools)
