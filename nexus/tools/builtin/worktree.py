"""The session ``checkout`` tool, bound to narrow Runtime callbacks (agent-managed worktrees plan §3.3).

One tool replaces ``worktree_list``, ``worktree_create``, ``worktree_move`` and
``worktree_commit`` (plan.md §9a): ``checkout`` takes an ``action``:

- ``status``: where this session works, who else is in that checkout, which
  uncommitted files there are this session's and which are not, and every
  worktree of the project. Read-only.
- ``isolate``: create a worktree and move this session into it.
- ``switch``: move to ``main`` or an existing worktree.
- ``commit``: commit in the current worktree (refused in the main checkout).

The schema and description never depend on where the session is, so a move
never changes the tool definitions already sent to the provider (the cached
prefix stays valid); where the session is comes back in results and notices.

This module owns only the model-facing schema, argument bounds and result text.
Git, path checks, durable event emission and root transitions stay behind the
callbacks captured by :func:`build_checkout_tools`; no Runtime is exposed.

``status``, ``isolate`` and ``switch`` only read or switch checkouts in
Nexus-owned space, so the permission engine never asks for them (the
permission key is the action); ``commit`` follows the normal rules. Isolate and
switch return at once; the move is applied at the next step boundary of the
running turn.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ...errors import ToolError
from ...ext.quarantine import sanitize_text
from ..spec import RegisteredTool, ToolContext, ToolExecutionResult, ToolSpec

__all__ = [
    "CHECKOUT_ACTIONS", "CHECKOUT_NEVER_ASK_ACTIONS", "CHECKOUT_TOOL_NAME", "LEGACY_WORKTREE_TOOL_NAMES",
    "WORKTREE_TOOL_NAMES", "build_checkout_tools", "format_checkout_status", "format_worktree_list",
]

Callback = Callable[..., ToolExecutionResult | str | Awaitable[ToolExecutionResult | str]]

CHECKOUT_TOOL_NAME = "checkout"
#: Every action, in the order the schema lists them.
CHECKOUT_ACTIONS = ("status", "isolate", "switch", "commit")
#: Actions that only read or switch Nexus-owned checkouts: the permission engine never asks.
CHECKOUT_NEVER_ASK_ACTIONS = frozenset({"status", "isolate", "switch"})
#: Every worktree tool the Runtime may add to a root session.
WORKTREE_TOOL_NAMES = frozenset({CHECKOUT_TOOL_NAME})
#: The four tools ``checkout`` replaced; still kept out of subagent catalogs and
#: recognised in old session logs, never offered to a model.
LEGACY_WORKTREE_TOOL_NAMES = frozenset({"worktree_list", "worktree_create", "worktree_move", "worktree_commit"})

#: Fields each action reads; any other supplied field is ignored.
_ACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "status": (),
    "isolate": ("reason", "name", "base"),
    "switch": ("to", "reason"),
    "commit": ("message", "paths"),
}

_MOVE_WARNING = (
    "A move happens after the current step: do not run tools that depend on the new directory in the "
    "same step; other tool calls of this step still run in the old directory."
)


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(CHECKOUT_ACTIONS)},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500,
                       "description": "isolate, switch: why (shown to the user)"},
            "name": {"type": "string", "minLength": 1, "maxLength": 60,
                     "description": "isolate: worktree name (letters, digits, '-', '_'); optional"},
            "base": {"type": "string", "minLength": 1, "maxLength": 200,
                     "description": "isolate: base branch; defaults to the current base"},
            "to": {"type": "string", "minLength": 1, "maxLength": 200,
                   "description": "switch: \"main\" or a worktree name from status"},
            "message": {"type": "string", "minLength": 1, "maxLength": 4000,
                        "description": "commit: the commit message"},
            "paths": {"type": "array", "maxItems": 500, "items": {"type": "string", "maxLength": 4096},
                      "description": "commit: paths to commit; omit to commit all changes"},
        },
        "required": ["action"], "additionalProperties": False,
    }


def _description() -> str:
    return (
        "Where this session works: the main checkout or a Git worktree of the project. Actions:\n"
        "- status: your checkout, the other sessions in it (and whether they are active), which uncommitted "
        "files there are yours and which are not, and every worktree of the project. Read-only.\n"
        "- isolate {reason, name?, base?}: create a worktree (branch nexus/wt/<name>) and move into it. Files you "
        "wrote in the main checkout are carried over. From a worktree, the new one branches from the updated base "
        "and the current one is left as it is (commit first).\n"
        "- switch {to, reason}: move to \"main\" or an existing worktree by its name from status. Leaving a "
        "worktree needs its changes committed; unmerged commits are fine and the branch is kept.\n"
        "- commit {message, paths?}: commit in the worktree you are in (not available in the main checkout); omit "
        "paths to commit everything. Nexus adds its session, turn and agent trailers.\n"
        "After a move, the result and a notice give your new working directory: from then on file edits, relative "
        "paths and shell commands run there.\n"
        "When to isolate: before your first edit when another active session is in your checkout; for changes "
        "across files, refactors, dependency or build changes, experiments, or work the user may want to review "
        "or throw away as a unit. Stay for questions, reading, reviews, running commands and small edits the user "
        "asked for directly. Call status when unsure. " + _MOVE_WARNING
    )


def _spec() -> ToolSpec:
    return ToolSpec(
        name=CHECKOUT_TOOL_NAME,
        description=_description(),
        input_schema=_schema(),
        bundle="task",
        mutates=True,
        concurrency="exclusive",
        permission_key=lambda args: _action_key(args),
    )


def _action_key(args: Mapping[str, Any]) -> str:
    action = args.get("action") if isinstance(args, Mapping) else None
    return action if isinstance(action, str) and action in CHECKOUT_ACTIONS else "unknown"


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


def _names(items: object, limit: int = 10) -> str:
    rows = [str(item) for item in items] if isinstance(items, (list, tuple)) else []
    shown = ", ".join(sanitize_text(row, limit=200) for row in rows[:limit])
    return shown + (f", +{len(rows) - limit} more" if len(rows) > limit else "")


def format_checkout_status(state: Mapping[str, Any]) -> str:
    """Labelled fields for ``checkout status``: this session's checkout first, then every worktree."""
    here = state.get("here") if isinstance(state.get("here"), Mapping) else {}
    others = [item for item in here.get("others") or [] if isinstance(item, Mapping)]
    active = [item for item in others if item.get("active")]
    mine = here.get("mine") or []
    not_mine = here.get("not_mine") or []
    lines = [
        f"you are in: {sanitize_text(str(here.get('checkout') or state.get('current', '?')), limit=200)}"
        f" (branch {sanitize_text(str(here.get('branch') or '(detached)'), limit=200)})",
        f"other sessions here: {_sessions(others)}",
    ]
    if here.get("changes_unknown"):
        lines.append("uncommitted changes here: unknown (git status failed)")
    elif not mine and not not_mine:
        lines.append("uncommitted changes here: none")
    else:
        lines.append(f"uncommitted changes here: {here.get('mine_count', len(mine))} written by you, "
                     f"{here.get('not_mine_count', len(not_mine))} not yours")
        if mine:
            lines.append(f"  yours: {_names(mine)}")
        if not_mine:
            lines.append(f"  not yours: {_names(not_mine)}")
    if active:
        lines.append(f"note: {len(active)} other active session{'s' if len(active) != 1 else ''} in this checkout; "
                     "isolate before editing unless the user wants the changes here.")
    if here.get("kind") == "worktree" and here.get("unmerged_commits"):
        lines.append(f"note: this worktree has {here['unmerged_commits']} commits not in its base.")
    lines += ["", format_worktree_list(state)]
    return "\n".join(lines)


async def _call(callback: Callback | None, args: dict[str, Any], ctx: ToolContext, action: str) -> ToolExecutionResult:
    label = f"checkout {action}"
    if callback is None:
        return _error(f"{label}: this action is unavailable for this session")
    try:
        result = callback(args, ctx)
        if inspect.isawaitable(result):
            result = await result
    except ToolError as exc:
        return _error(f"{label}: {sanitize_text(str(exc), limit=500)}")
    except Exception as exc:  # noqa: BLE001 - a callback failure is model-visible
        return _error(f"{label}: {sanitize_text(f'{type(exc).__name__}: {exc}', limit=500)}")
    if isinstance(result, ToolExecutionResult):
        return result
    if isinstance(result, str):
        return ToolExecutionResult.text(sanitize_text(result, limit=16_000))
    if isinstance(result, Mapping):
        if action == "status":
            return ToolExecutionResult.text("\n".join(sanitize_text(line, limit=1000).replace(" ← you are here", "  ← you are here") for line in format_checkout_status(result).splitlines())[:16_000])
        lines = _mapping_lines(result)
        text = "\n".join(sanitize_text(line, limit=1000) for line in lines[:200])
        if len(lines) > 200:
            text += f"\n… {len(lines) - 200} more fields omitted"
        return ToolExecutionResult.text(text or f"{label}: completed")
    return _error(f"{label}: the bound operation returned an unsupported result")


def _bounded(args: dict[str, Any], key: str, limit: int, *, required: bool) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit or "\x00" in value:
        return f"{key} must be {'a non-empty ' if required else 'a '}string of at most {limit} characters"
    return None


def _checked(action: str, args: dict[str, Any]) -> str | None:
    if action == "isolate":
        problem = _bounded(args, "reason", 500, required=True) or _bounded(args, "name", 60, required=False) \
            or _bounded(args, "base", 200, required=False)
    elif action == "switch":
        problem = _bounded(args, "to", 200, required=True) or _bounded(args, "reason", 500, required=True)
    elif action == "commit":
        message, paths = args.get("message"), args.get("paths")
        if not isinstance(message, str) or not message.strip() or len(message) > 4000 or "\x00" in message:
            problem = "commit requires a non-empty message of at most 4,000 characters"
        elif paths is not None and (
            not isinstance(paths, list) or len(paths) > 500
            or any(not isinstance(path, str) or len(path) > 4096 or "\x00" in path for path in paths)
        ):
            problem = "paths must contain at most 500 bounded path strings"
        else:
            problem = None
    else:
        problem = None
    return f"checkout {action}: {problem}" if problem else None


def build_checkout_tools(
    status: Callback,
    commit: Callback | None,
    isolate: Callback | None,
    switch: Callback | None,
) -> tuple[RegisteredTool, ...]:
    """Build the one ``checkout`` capability for one session, capturing only operation callbacks.

    The same definition is offered wherever the session is (the prompt-cache rule);
    the commit callback refuses outside a worktree. Isolate and switch are queued
    by the Runtime and applied at the next step boundary. Each callback gets only
    the fields its action reads.
    """
    callbacks: dict[str, Callback | None] = {"status": status, "isolate": isolate, "switch": switch, "commit": commit}

    async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        action = args.get("action") if isinstance(args, Mapping) else None
        if not isinstance(action, str) or action not in CHECKOUT_ACTIONS:
            return _error(f"checkout: action must be one of {', '.join(CHECKOUT_ACTIONS)}")
        selected = {key: args[key] for key in _ACTION_FIELDS[action] if key in args}
        invalid = _checked(action, selected)
        return _error(invalid) if invalid else await _call(callbacks[action], selected, ctx, action)

    return (RegisteredTool(spec=_spec(), run=run, origin="builtin"),)
