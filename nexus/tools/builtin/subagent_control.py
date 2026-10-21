"""``subagent_send`` / ``subagent_stop`` / ``subagent_list``: steer persistent subagents.

Contract (plans/done/NONBLOCKING_SUBAGENTS_PLAN.md sections 3.5 to 3.7):

* A subagent started in the background does not die when its run ends: it goes
  idle and keeps its whole conversation. ``subagent_send`` appends a message to
  it and runs another turn (again in the background unless ``wait`` is true, and
  again reported to the root when it ends); ``subagent_stop`` closes it for good
  (cancelling a run in flight); ``subagent_list`` shows every child with its
  state, so the root can recover ids after its own context was compacted.
* Like ``subagent`` these hold **no authority**. They reach the bound
  ``SubagentRunner`` through :attr:`ToolContext.subagents` (probed with
  ``getattr``; the tools layer never imports ``nexus.agents``). Continuing a
  child can only shrink what it could do at spawn; the runner enforces that.
* The permission key of ``send`` and ``stop`` is the child's own
  ``"<role>:<tier>"``, matched by ``Task(...)`` rules, so
  ``deny = ["Task(*:high)"]`` also blocks continuing or stopping a high-tier
  child. ``list`` is read-only and always allowed.
* Root only: the names are in ``ROOT_ONLY_TOOLS``, so no child catalog has them,
  and code mode never calls them.

They are only offered when the bound service is persistent
(``agents.background``): :func:`build_agent_control_tools` returns no tools
otherwise, so the default configuration's tool schema does not change.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...errors import OperationCancelled
from ..spec import RegisteredTool, ToolContext, ToolExecutionResult, ToolSpec
from .task import _call_id_kwargs, _safe, _to_result

__all__ = [
    "LIST_SPEC",
    "SEND_SPEC",
    "STOP_SPEC",
    "build_agent_control_tools",
    "make_send_spec",
    "make_stop_spec",
]

_RESULT_TOKENS = 25_000
#: Most characters of a stop reason accepted.
_REASON_CHARS = 500

_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The id of the subagent, as given when it started or shown by "
                "subagent_list."
            ),
        },
        "message": {
            "type": "string",
            "minLength": 1,
            "description": (
                "What to tell it. It is added to the subagent's own conversation as the "
                "next user message; it already remembers everything it did, so send only "
                "what is new."
            ),
        },
        "wait": {
            "type": "boolean",
            "description": (
                "Wait for this run and return its report as the call's result. Leave it "
                "unset unless you cannot continue without the answer."
            ),
        },
    },
    "required": ["agent_id", "message"],
    "additionalProperties": False,
}

_STOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "description": "The id of the subagent to stop.",
        },
        "reason": {
            "type": "string",
            "description": "Optional. Why it is stopped (kept in the session log).",
        },
    },
    "required": ["agent_id"],
    "additionalProperties": False,
}

_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_SEND_DESCRIPTION = (
    "Continue a subagent you started that has reported (it is idle): send it a message "
    "and it runs again, instead of starting a new subagent. It keeps its whole "
    "conversation, its tools and its worktree; the message is its next user input, so send "
    "only what is new. "
    "It runs in the background and its report arrives as a \"Subagent report\" message, "
    "the same way as the first run's. Refused while the subagent is still running (wait "
    "for its report or stop it), after it was stopped, and for external Claude Code or "
    "Antigravity workers, which run one assignment only. A continuation can only use what "
    "the subagent was given at spawn."
)
_STOP_DESCRIPTION = (
    "Close a subagent you started: cancel it if it is running, and close it so it can no "
    "longer be continued. Its log and any worktree are kept. Stopping a subagent that is "
    "already stopped does nothing. Idle subagents hold one of the limited slots until "
    "closed, so stop each one when you are done with it (when a new subagent needs a slot "
    "and none is free, the longest-idle one is closed for you)."
)
_LIST_DESCRIPTION = (
    "List the subagents of this session: id, role, model, state (running, idle, "
    "interrupted, stopped), number of runs, last status, worktree, token use and age. Use "
    "it to recover an id, to see what is still running, or to find an idle subagent to "
    "continue with subagent_send."
)


def _agent_key(service: object | None):
    def key(data: Mapping[str, Any]) -> str:
        resolver = getattr(service, "agent_permission_key", None)
        if callable(resolver):
            return str(resolver(data.get("agent_id")))
        return "unknown:unknown"

    return key


def make_send_spec(service: object | None = None) -> ToolSpec:
    return ToolSpec(
        name="subagent_send",
        description=_SEND_DESCRIPTION,
        input_schema=_SEND_SCHEMA,
        bundle="task",
        mutates=False,
        concurrency="parallel",
        max_result_tokens=_RESULT_TOKENS,
        permission_key=_agent_key(service),
    )


def make_stop_spec(service: object | None = None) -> ToolSpec:
    return ToolSpec(
        name="subagent_stop",
        description=_STOP_DESCRIPTION,
        input_schema=_STOP_SCHEMA,
        bundle="task",
        mutates=False,
        concurrency="parallel",
        max_result_tokens=_RESULT_TOKENS,
        permission_key=_agent_key(service),
    )


SEND_SPEC = make_send_spec()
STOP_SPEC = make_stop_spec()
LIST_SPEC = ToolSpec(
    name="subagent_list",
    description=_LIST_DESCRIPTION,
    input_schema=_LIST_SCHEMA,
    bundle="task",
    mutates=False,
    concurrency="parallel",
    max_result_tokens=_RESULT_TOKENS,
)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _service(ctx: ToolContext, *methods: str) -> Any | None:
    candidate = getattr(ctx, "subagents", None)
    if candidate is not None and all(callable(getattr(candidate, m, None)) for m in methods):
        return candidate
    return None


def _unavailable(tool: str) -> ToolExecutionResult:
    return ToolExecutionResult.text(
        f"{tool}: persistent subagents are not available in this session "
        "(agents.background is off, or no subagent service is bound)",
        is_error=True,
    )


def _fail(tool: str, exc: Exception) -> ToolExecutionResult:
    # A refusal (ValueError family) is written for the model; anything else is a fault.
    text = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {_safe(exc)}"
    return ToolExecutionResult.text(f"{tool}: {text}", is_error=True)


def _text_arg(args: Mapping[str, Any], name: str) -> str | None:
    value = args.get(name)
    return value if isinstance(value, str) and value.strip() else None


def _started_result(started: object, message_chars: int) -> ToolExecutionResult:
    agent_id = str(getattr(started, "agent_id", "") or "")
    role = str(getattr(started, "role", "") or "subagent")
    run = int(getattr(started, "run", 0) or 0)
    model = getattr(started, "model", None)
    tier = getattr(started, "tier", None)
    label = ", ".join(
        part for part in (
            role,
            model if isinstance(model, str) and model else "",
            f"tier {tier}" if isinstance(tier, str) and tier and tier != model else "",
        ) if part
    )
    where = str(getattr(started, "workspace", "") or "the workspace")
    text = (
        f"Sent your message ({message_chars} characters) to subagent {agent_id} ({label}); "
        f"it is running again (run {run}) in {where}. Its report will arrive as a message "
        'labelled "Subagent report" when this run finishes; you do not need to poll. '
        "Keep working on anything that does not depend on it, or end your turn if there "
        "is nothing else."
    )
    metrics: dict[str, Any] = {
        "agent": role, "agent_id": agent_id, "run": run, "status": "running",
        "state": "running", "background": True, "usage": {},
    }
    if isinstance(tier, str):
        metrics["tier"] = tier
    worktree = getattr(started, "worktree", None)
    if isinstance(worktree, Mapping):
        metrics["worktree"] = dict(worktree)
    return ToolExecutionResult.text(
        text,
        display=f"Sent to {agent_id}: run {run} started",
        context_note=f"[subagent_send {agent_id}: run {run} started in the background]",
        metrics=metrics,
    )


async def run_send(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, Mapping):
        return ToolExecutionResult.text("subagent_send: arguments must be an object", is_error=True)
    agent_id = _text_arg(args, "agent_id")
    message = args.get("message")
    if agent_id is None:
        return ToolExecutionResult.text("subagent_send: 'agent_id' must be a non-empty string", is_error=True)
    if not isinstance(message, str) or not message.strip():
        return ToolExecutionResult.text("subagent_send: 'message' must be a non-empty string", is_error=True)
    wait = args.get("wait")
    if wait is not None and not isinstance(wait, bool):
        return ToolExecutionResult.text("subagent_send: 'wait' must be a boolean", is_error=True)
    service = _service(ctx, "resume")
    if service is None:
        return _unavailable("subagent_send")
    try:
        resume = service.resume
        started = await resume(
            agent_id, message, wait=wait is True, emit=ctx.emit,
            cancel=ctx.cancel_token, **_call_id_kwargs(resume, ctx),
        )
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - refusals and faults are model-visible
        return _fail("subagent_send", exc)
    outcome = getattr(started, "outcome", None)
    if outcome is not None:
        return _to_result(outcome)
    return _started_result(started, len(message))


async def run_stop(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, Mapping):
        return ToolExecutionResult.text("subagent_stop: arguments must be an object", is_error=True)
    agent_id = _text_arg(args, "agent_id")
    if agent_id is None:
        return ToolExecutionResult.text("subagent_stop: 'agent_id' must be a non-empty string", is_error=True)
    reason = args.get("reason")
    if reason is not None and not isinstance(reason, str):
        return ToolExecutionResult.text("subagent_stop: 'reason' must be a string", is_error=True)
    service = _service(ctx, "stop")
    if service is None:
        return _unavailable("subagent_stop")
    try:
        result = await service.stop(agent_id, reason=(reason or "")[:_REASON_CHARS], by="root")
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return _fail("subagent_stop", exc)
    stopped = bool(getattr(result, "stopped", False))
    message = str(getattr(result, "message", "") or ("stopped" if stopped else "not stopped"))
    return ToolExecutionResult.text(
        message,
        is_error=not stopped,
        display=f"Stopped {agent_id}" if stopped else f"Could not stop {agent_id}",
        context_note=f"[subagent_stop {agent_id}: {'stopped' if stopped else 'not stopped'}]",
        metrics={"agent_id": agent_id, "stopped": stopped},
    )


def _age(seconds: object) -> str:
    try:
        value = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if value < 90:
        return f"{value}s"
    if value < 5400:
        return f"{value // 60}m"
    if value < 172800:
        return f"{value // 3600}h"
    return f"{value // 86400}d"


def _row_text(row: object) -> str:
    agent_id = str(getattr(row, "agent_id", "") or "?")
    role = str(getattr(row, "role", "") or "subagent")
    model = getattr(row, "model", None)
    tier = getattr(row, "tier", None)
    route = ", ".join(
        part for part in (
            model if isinstance(model, str) and model else "",
            f"tier {tier}" if isinstance(tier, str) and tier and tier != model else "",
        ) if part
    )
    state = str(getattr(row, "state", "") or "?")
    continuable = bool(getattr(row, "continuable", True))
    if state in {"idle", "interrupted"} and not continuable:
        state = "finished (not continuable)"
    runs = int(getattr(row, "runs", 0) or 0)
    last = getattr(row, "last_status", None)
    parts = [
        f"{agent_id}: {role}" + (f" ({route})" if route else ""),
        f"state {state}",
        f"{runs} run{'s' if runs != 1 else ''}",
    ]
    if last and state != "running":
        parts.append(f"last run {last}")
    stopped_by = getattr(row, "stopped_by", None)
    if state == "stopped" and stopped_by:
        parts.append(f"stopped by {stopped_by}")
    tokens = int(getattr(row, "tokens", 0) or 0)
    parts.append(f"{tokens} tokens")
    parts.append(f"age {_age(getattr(row, 'age_seconds', 0))}")
    if state != "running":
        parts.append(f"idle {_age(getattr(row, 'idle_seconds', 0))}")
    worktree = getattr(row, "worktree", None)
    if isinstance(worktree, Mapping):
        branch = worktree.get("branch") or worktree.get("path") or "worktree"
        life = worktree.get("lifecycle")
        parts.append(f"worktree {branch}" + (f" ({life})" if life else ""))
    if getattr(row, "report_pending", False):
        parts.append("report pending")
    if not continuable:
        reason = str(getattr(row, "not_continuable", "") or "")
        if reason:
            parts.append(f"cannot be continued: {reason}")
    description = str(getattr(row, "description", "") or "")
    if description:
        parts.append(f'"{description[:80]}"')
    return "; ".join(parts)


async def run_list(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    service = _service(ctx, "list_agents")
    if service is None:
        return _unavailable("subagent_list")
    try:
        rows = list(service.list_agents())
    except OperationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return _fail("subagent_list", exc)
    paused = bool(getattr(service, "wake_paused", False))
    lines: list[str] = []
    if not rows:
        lines.append("No subagents in this session.")
    else:
        lines.append(f"{len(rows)} subagent{'s' if len(rows) != 1 else ''} (running first):")
        lines.extend(f"- {_row_text(row)}" for row in rows)
        lines.append(
            "Continue an idle one with subagent_send; free a slot with subagent_stop."
        )
    if paused:
        lines.append(
            "[subagent reports are paused after repeated automatic wake-ups; they are "
            "delivered again when the user sends a message]"
        )
    metrics = {"count": len(rows), "agents": [str(getattr(r, "agent_id", "")) for r in rows]}
    return ToolExecutionResult.text(
        "\n".join(lines),
        display=f"{len(rows)} subagent{'s' if len(rows) != 1 else ''}",
        context_note="[subagent_list: re-run for the current state]",
        metrics=metrics,
    )


def build_agent_control_tools(service: object | None = None) -> tuple[RegisteredTool, ...]:
    """The three control tools bound to ``service``, or none when it is not persistent."""
    if service is None or getattr(service, "persistent", False) is not True:
        return ()
    return (
        RegisteredTool(spec=make_send_spec(service), run=run_send, origin="builtin"),
        RegisteredTool(spec=make_stop_spec(service), run=run_stop, origin="builtin"),
        RegisteredTool(spec=LIST_SPEC, run=run_list, origin="builtin"),
    )
