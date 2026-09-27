"""``Task``: spawn a bounded subagent (bundle ``task``, plan sections 5.6/15.6).

One tool with two composable modes:

* **Named type** -- ``Task(subagent_type="explore", prompt="...")``: the system
  prompt, tool set, and model come from the definition file;
* **Ad-hoc** -- ``Task(prompt="...", tools=["Read", "Grep"], model="low")``: a
  throwaway agent with exactly the listed tools; ``subagent_type`` defaults to
  ``general``.

The tool itself holds **no authority**: it reaches the injected
:class:`~nexus.tools.spec.SubagentServiceView` (the runner) through
:attr:`ToolContext.subagents`, never a ``Runtime`` and never an import of
``nexus.agents``. The service computes ``parent_tools & role_tools &
requested_tools``, clamps the tier, enforces the tree budget, and relays the
child's events before the child is ever built (plan section 15.8). The final
report comes back as this tool's :class:`ToolExecutionResult`, with any dropped
tools and any tier clamp reported in the text so the model learns rather than
silently getting less than it asked for.

``permission_key`` returns ``"<subagent_type>:<tier>"`` (or appends
``":worktree"`` for isolated checkout creation) so the existing rule
grammar expresses real policy without new syntax::

    deny  = ["Task(*:high)"]
    allow = ["Task(explore:*)", "Task(*:low)"]

A spec built by :func:`make_task_spec` against a live service resolves the
*effective* tier (the role's declared model included); the static
:data:`TASK_SPEC` falls back to the requested model/``inherit`` because it has
no agent manager to consult. The structural clamp in the runner is the real
guarantee either way.
"""
from __future__ import annotations

import contextlib
import inspect
from collections.abc import Mapping
from typing import Any

from ...errors import OperationCancelled, ToolError
from ...model.tiers import TIER_ORDER
from ..spec import (
    RegisteredTool,
    SubagentServiceView,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

__all__ = [
    "DEFAULT_SUBAGENT_TYPE",
    "TASK_SPEC",
    "build_task_tool",
    "make_task_spec",
    "run",
]

#: The role an ad-hoc request uses when none is named (plan section 15.7).
DEFAULT_SUBAGENT_TYPE = "general"
#: The fallback cap on the returned report, matching the other builtins.
_FALLBACK_MAX_RESULT_TOKENS = 25_000


class _TaskToolError(ToolError):
    """A model-visible failure while building or spawning a subagent."""


_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The task for the subagent. Be specific and self-contained: the "
                "subagent does not see the parent conversation."
            ),
        },
        "subagent_type": {
            "type": "string",
            "minLength": 1,
            "description": (
                "A seeded role (general/explore/planner) or any discovered "
                ".nexus/agents/<name>. Defaults to 'general'."
            ),
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional narrowing set of canonical lowercase tool names. Can only reduce the "
                "role's set, never add a tool the parent lacks."
            ),
        },
        "model": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional tier name ('low'/'medium'/'high'), provider/model, or "
                "bare id. Clamped to the configured max tier."
            ),
        },
        "description": {
            "type": "string",
            "minLength": 1,
            "description": "Short label for the agent tree; not sent to the child.",
        },
        "worktree": {
            "type": "boolean",
            "default": False,
            "description": "Run the subagent in an isolated Git worktree.",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

_TASK_DESCRIPTION = (
    "Spawn a subagent to carry out a self-contained task and return its report. "
    "Use a named role (general/explore/planner) or narrow ad-hoc tools/model. "
    "A subagent inherits only the parent's authority and cannot exceed it."
)


def _static_permission_key(data: Mapping[str, Any]) -> str:
    """The gate key when no live service is bound.

    The tier component is the requested model when it is a reserved tier name,
    otherwise ``"auto"`` for a concrete reference or ``"inherit"`` for none.
    """
    subagent_type = data.get("subagent_type")
    if not isinstance(subagent_type, str) or not subagent_type.strip():
        subagent_type = DEFAULT_SUBAGENT_TYPE
    model = data.get("model")
    if isinstance(model, str) and model in TIER_ORDER:
        tier = model
    elif isinstance(model, str) and model.strip():
        tier = "auto"
    else:
        tier = "inherit"
    key = f"{subagent_type}:{tier}"
    return f"{key}:worktree" if data.get("worktree") is True else key


def make_task_spec(service: SubagentServiceView | None = None) -> ToolSpec:
    """Build the ``Task`` spec, optionally bound to a live subagent service.

    A bound service's :meth:`permission_key` resolves the effective
    ``<subagent_type>:<tier>`` (so ``deny = ["Task(*:high)"]`` catches a role
    whose *declared* model is high even when the call names no model).
    """
    if service is None:
        key = _static_permission_key
    else:
        # The bound service owns the configured default. Apply it before the
        # permission key is resolved so omitting ``subagent_type`` cannot route
        # around a rule for a non-general default role.
        def key(data: Mapping[str, Any]) -> str:
            request = dict(data)
            request.setdefault("subagent_type", service.default_type)
            resolved = service.permission_key(request)
            return f"{resolved}:worktree" if request.get("worktree") is True else resolved
    return ToolSpec(
        name="subagent",
        description=_TASK_DESCRIPTION,
        input_schema=_TASK_SCHEMA,
        bundle="task",
        mutates=False,
        concurrency="parallel",
        max_result_tokens=_FALLBACK_MAX_RESULT_TOKENS,
        permission_key=key,
    )


#: The static spec (no bound service). The runtime replaces it with
#: :func:`make_task_spec` against the turn's runner when it wires ``Task``.
TASK_SPEC = make_task_spec()


def build_task_tool(
    service: SubagentServiceView | None = None,
) -> RegisteredTool:
    """A ready-to-register ``Task`` tool, bound to ``service`` when supplied."""
    return RegisteredTool(spec=make_task_spec(service), run=run, origin="builtin")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _service(ctx: ToolContext) -> SubagentServiceView | None:
    candidate = getattr(ctx, "subagents", None)
    if candidate is not None and callable(getattr(candidate, "spawn", None)):
        return candidate
    return None


def _build_request(args: Mapping[str, Any]) -> dict[str, Any]:
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _TaskToolError("'prompt' must be a non-empty string")
    request: dict[str, Any] = {"prompt": prompt}
    for name in ("subagent_type", "model", "description"):
        value = args.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise _TaskToolError(f"'{name}' must be a non-empty string when given")
        request[name] = value
    worktree = args.get("worktree", False)
    if not isinstance(worktree, bool):
        raise _TaskToolError("'worktree' must be a boolean")
    if "worktree" in args:
        request["worktree"] = worktree
    tools = args.get("tools")
    if tools is not None:
        if not isinstance(tools, (list, tuple)) or not all(
            isinstance(item, str) and item for item in tools
        ):
            raise _TaskToolError("'tools' must be an array of non-empty tool names")
        request["tools"] = [str(item) for item in tools]
    return request


def _usage_dict(usage: object) -> dict[str, Any]:
    to_dict = getattr(usage, "to_dict", None)
    if callable(to_dict):
        with contextlib.suppress(Exception):
            return dict(to_dict())
    if isinstance(usage, Mapping):
        return dict(usage)
    return {}


def _to_result(outcome: object) -> ToolExecutionResult:
    render = getattr(outcome, "render", None)
    text = ""
    if callable(render):
        try:
            text = str(render())
        except Exception:  # noqa: BLE001 - fall back to the raw text
            text = ""
    if not text:
        text = str(getattr(outcome, "text", "") or outcome)
    is_error = bool(getattr(outcome, "is_error", False))
    status = getattr(outcome, "status", "completed")
    if not isinstance(status, str):
        status = "completed"
    agent = getattr(outcome, "agent", "") or "subagent"
    session_id = getattr(outcome, "session_id", "") or ""
    dropped = [str(name) for name in (getattr(outcome, "dropped_tools", ()) or ())]
    clamped = bool(getattr(outcome, "clamped", False))
    metrics: dict[str, Any] = {
        "agent": agent,
        "session": session_id,
        "status": status,
        "usage": _usage_dict(getattr(outcome, "usage", None)),
        "dropped_tools": dropped,
        "clamped": clamped,
    }
    tier = getattr(outcome, "tier", None)
    if isinstance(tier, str):
        metrics["tier"] = tier
    requested_tier = getattr(outcome, "requested_tier", None)
    if isinstance(requested_tier, str):
        metrics["requested_tier"] = requested_tier
    worktree = getattr(outcome, "worktree", None)
    if isinstance(worktree, Mapping):
        metrics["worktree"] = dict(worktree)
    if not text.strip():
        text = f"Task: subagent {agent} returned {status}"
    context_note = (
        f"[Task {agent} ({session_id}): {status}; re-run to see the full report]"
        if session_id
        else f"[Task {agent}: {status}; re-run to see the full report]"
    )
    return ToolExecutionResult.text(
        text,
        is_error=is_error,
        display=f"Task {agent}: {status}",
        context_note=context_note,
        metrics=metrics,
    )


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    if not isinstance(args, Mapping):
        return ToolExecutionResult.text(
            "Task: arguments must be an object", is_error=True
        )
    try:
        request = _build_request(args)
    except _TaskToolError as exc:
        return ToolExecutionResult.text(f"Task: {exc}", is_error=True)

    service = _service(ctx)
    if service is not None:
        try:
            request.setdefault("subagent_type", service.default_type)
            spawn = service.spawn
            kwargs = {"cancel": ctx.cancel_token, "emit": ctx.emit}
            # Keep structural test doubles and older internal callers working;
            # production SubagentRunner accepts the exact model tool call id.
            try:
                signature = inspect.signature(spawn)
                if "call_id" in signature.parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                ):
                    kwargs["call_id"] = ctx.call_id
            except (TypeError, ValueError):
                kwargs["call_id"] = ctx.call_id
            outcome = await spawn(request, **kwargs)
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible
            return ToolExecutionResult.text(
                f"Task: spawn failed: {type(exc).__name__}: {_safe(exc)}",
                is_error=True,
            )
        return _to_result(outcome)

    spawn_agent = getattr(ctx, "spawn_agent", None)
    if callable(spawn_agent):
        try:
            outcome = spawn_agent(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible
            return ToolExecutionResult.text(
                f"Task: spawn failed: {type(exc).__name__}: {_safe(exc)}",
                is_error=True,
            )
        return _to_result(outcome)

    return ToolExecutionResult.text(
        "Task: no subagent service is available for this call; the harness was "
        "not given a SubagentRunner (this is a configuration error, not a "
        "missing agent)",
        is_error=True,
    )


def _safe(value: object, *, limit: int = 300) -> str:
    text = str(value).replace("\x00", "") if value is not None else ""
    return text if len(text) <= limit else text[:limit] + "…"
