"""``Task``: spawn a bounded subagent (bundle ``task``, plan sections 5.6/15.6).

One tool with two composable modes:

* **Named type** -- ``Task(subagent_type="advisor", prompt="...")``: the system
  prompt, tool set, and model come from the definition file;
* **Ad-hoc** -- ``Task(prompt="...", tools=["Read", "Grep"], model="low")``: a
  throwaway agent with exactly the listed tools; ``subagent_type`` defaults to
  ``task``.

Follow-ups to a finished subagent are a separate tool, ``subagent_continue``
(``agent_id`` + ``prompt`` only), so a model that fills every optional field of
this tool can still start a new subagent. Empty or placeholder optionals here
(``tools: []``, ``model: ""``, a punctuation-only ``description``) mean absent.

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
    allow = ["Task(advisor:*)", "Task(*:low)"]

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
    "CONTINUE_SPEC",
    "CONTINUE_TOOL_NAME",
    "DEFAULT_SUBAGENT_TYPE",
    "TASK_SPEC",
    "build_continue_tool",
    "build_task_tool",
    "build_task_tools",
    "make_continue_spec",
    "make_task_spec",
    "run",
]

#: The role an ad-hoc request uses when none is named (plan section 15.7).
DEFAULT_SUBAGENT_TYPE = "task"
#: Gate key for a resume whose child's role is unknown (mirrors the runner's).
_RESUME_UNRESOLVED_KEY = "resume:unresolved"
#: The fallback cap on the returned report, matching the other builtins.
_FALLBACK_MAX_RESULT_TOKENS = 25_000


def _resume_of(data: Mapping[str, Any]) -> object:
    """A stray ``resume`` on the new-subagent tool: a real handle, else ``None``.

    ``resume`` left the tool's schema (follow-ups are ``subagent_continue``), so
    live calls carrying it fail validation. This stays for old transcripts and
    direct callers: a value shaped like a child session id (``<parent>/sub/<n>``)
    still routes to the runner's resume path (which refuses it beside other
    arguments); anything else (``"none"``, ``","``, ``"new"``) is a placeholder and is
    ignored, so a fresh subagent starts.
    """
    resume = data.get("resume")
    if isinstance(resume, str) and "/sub/" in resume:
        return resume
    return None


def _meaningful(text: object) -> bool:
    """Whether ``text`` carries any letter or digit (``". "`` and ``""`` do not)."""
    return isinstance(text, str) and any(ch.isalnum() for ch in text)


def _normalized(data: Mapping[str, Any]) -> dict[str, Any]:
    """The call with empty and placeholder optionals removed (treated as absent).

    Some models fill every optional parameter on every call. An empty ``tools``
    list, a blank ``model``, a blank ``subagent_type`` and a ``description``
    without a letter or digit are all "not given"; an empty ``tools`` list must
    never mean "no tools".
    """
    out = dict(data)
    tools = out.get("tools")
    if isinstance(tools, (list, tuple)) and not tools:
        del out["tools"]
    for name in ("model", "subagent_type"):
        value = out.get(name)
        if isinstance(value, str) and not value.strip():
            del out[name]
    if "description" in out and isinstance(out["description"], str) and not _meaningful(out["description"]):
        del out["description"]
    return out


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
            "description": (
                "Name of an available agent listed in this tool's description. "
                "Defaults to the configured default role."
            ),
        },
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional narrowing set of canonical lowercase tool names. Can only reduce the "
                "role's set, never add a tool the parent lacks. Omit it (an empty list "
                "also means omitted, not 'no tools')."
            ),
        },
        "model": {
            "type": "string",
            "description": (
                "Optional. A tier name from the chosen agent's tier list "
                "('low', 'medium' or 'high'), or a concrete provider/model. "
                "Omit it to use the agent's default tier. A tier or model "
                "outside the agent's list runs on the nearest allowed tier "
                "instead, and the result says so. The configured max tier "
                "still applies."
            ),
        },
        "description": {
            "type": "string",
            "description": "Short label for the agent tree; not sent to the child.",
        },
    },
    "required": ["prompt"],
    "additionalProperties": False,
}

_TASK_DESCRIPTION = (
    "Start a subagent on a self-contained task. The call blocks and returns its "
    "report directly. Start independent subagents together in one batch of "
    "calls so they run concurrently; the turn continues when every report is in. "
    "The subagent does not see this conversation, so the prompt must carry the "
    "goal, relevant paths, constraints, and what to report. Subagents that edit "
    "files list them in their report. A subagent inherits only the parent's "
    "authority and cannot exceed it."
    " Every field except `prompt` is optional: omit what you do not need. "
    "To send a follow-up to a subagent that already finished (its report "
    "footer says 'continuable: yes'), use the `subagent_continue` tool with its "
    "`agent_id`; this tool always starts a new subagent."
    " Tiers are for subagents only. If a provider or model fails as a subagent, "
    "call the next model in line for that tier. Do not repeat a failed route; "
    "include the failure and any partial work in the next subagent's prompt."
)

#: Added when the agent roster is shown: how to choose a model tier. Each agent
#: lists the tiers it may use; the first is its default (plan: Part 3).
_TIER_GUIDANCE = (
    "Choosing a tier: each agent lists the model tiers it may use; the first is "
    "its default. Omit `model` to use the default. Pass a higher allowed tier "
    "only when the job needs it:\n"
    "- low: lookups, file searches, mechanical edits, short summaries, anything "
    "you could specify exactly.\n"
    "- medium: multi-step changes, debugging with a known reproduction, work "
    "that needs judgment about the code.\n"
    "- high: hard design decisions, bugs nobody can explain, reviewing risky or "
    "wide-reaching changes. Use sparingly; it is the slowest and most expensive.\n"
    "Prefer the cheapest tier that will do the job well. A tier outside an "
    "agent's list is moved to the nearest allowed tier and the result says so."
)


def _describe(service: SubagentServiceView | None) -> str:
    """The tool description, with the live role roster when one is bound.

    Roles load like skills: the bound service's ``role_index`` supplies sanitized
    ``name: description`` lines for every agent eligible as a subagent, so new
    ``.agents/agents/<name>.md`` files appear without editing any prompt. Test
    doubles and the static spec may lack it; the base description stands alone.
    """
    role_index = getattr(service, "role_index", None)
    if not callable(role_index):
        return _TASK_DESCRIPTION
    try:
        roster = str(role_index() or "").strip()
    except Exception:  # noqa: BLE001 - a broken roster must not drop the tool
        roster = ""
    if not roster:
        return _TASK_DESCRIPTION
    default = getattr(service, "default_type", DEFAULT_SUBAGENT_TYPE)
    return (
        f"{_TASK_DESCRIPTION}\n\n{_TIER_GUIDANCE}\n\n"
        f"Available agents (subagent_type), default {default!r}:\n{roster}"
    )


def _static_permission_key(data: Mapping[str, Any]) -> str:
    """The gate key when no live service is bound.

    The tier component is the requested model when it is a reserved tier name,
    otherwise ``"auto"`` for a concrete reference or ``"inherit"`` for none.
    """
    data = _normalized(data)
    if _resume_of(data):
        # Without the service the continued child's role cannot be read, and the
        # default role must not stand in for it: fail closed to a key no role rule names.
        return _RESUME_UNRESOLVED_KEY
    subagent_type = data.get("subagent_type")
    if not isinstance(subagent_type, str) or not subagent_type.strip():
        subagent_type = DEFAULT_SUBAGENT_TYPE
    model = data.get("model")
    if isinstance(model, str):
        model = model.strip()
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
            request = _normalized(data)
            if _resume_of(request) is None:
                request.pop("resume", None)
            if not request.get("resume"):
                # A resume names no role: the service reads the continued
                # child's recorded one, so the default must not be applied.
                request.setdefault("subagent_type", service.default_type)
            resolved = service.permission_key(request)
            return (
                f"{resolved}:worktree"
                if request.get("worktree") is True
                and not getattr(service, "managed_worktrees", False)
                else resolved
            )
    return ToolSpec(
        name="subagent",
        description=_describe(service),
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


# ---------------------------------------------------------------------------
# subagent_continue: follow-ups to a finished subagent
# ---------------------------------------------------------------------------

#: Registered name of the follow-up tool (same bundle and authority as ``subagent``).
CONTINUE_TOOL_NAME = "subagent_continue"

_CONTINUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "agent_id": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The agent_id from an earlier subagent report whose footer said "
                "'continuable: yes'."
            ),
        },
        "prompt": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The follow-up message for that subagent. It remembers its earlier "
                "work but not this conversation."
            ),
        },
    },
    "required": ["agent_id", "prompt"],
    "additionalProperties": False,
}

_CONTINUE_DESCRIPTION = (
    "Send a follow-up to a subagent that already finished, using the `agent_id` "
    "from its report (only when the footer said 'continuable: yes'). The subagent "
    "keeps its role, tools, model, workspace and memory of its earlier work, so "
    "there is nothing else to set. The call blocks and returns its new report. "
    "To start a different or unrelated subagent, use the `subagent` tool instead."
)


def make_continue_spec(service: SubagentServiceView | None = None) -> ToolSpec:
    """Build the ``subagent_continue`` spec; it shares ``subagent``'s bundle and gate."""
    if service is None:
        def key(data: Mapping[str, Any]) -> str:
            return _RESUME_UNRESOLVED_KEY
    else:
        def key(data: Mapping[str, Any]) -> str:
            return service.permission_key(_continue_request(data))
    return ToolSpec(
        name=CONTINUE_TOOL_NAME,
        description=_CONTINUE_DESCRIPTION,
        input_schema=_CONTINUE_SCHEMA,
        bundle="task",
        mutates=False,
        concurrency="parallel",
        max_result_tokens=_FALLBACK_MAX_RESULT_TOKENS,
        permission_key=key,
    )


CONTINUE_SPEC = make_continue_spec()


def build_continue_tool(
    service: SubagentServiceView | None = None,
) -> RegisteredTool:
    """A ready-to-register ``subagent_continue`` tool, bound to ``service`` when supplied."""
    return RegisteredTool(spec=make_continue_spec(service), run=run_continue, origin="builtin")


def build_task_tools(
    service: SubagentServiceView | None = None,
) -> tuple[RegisteredTool, RegisteredTool]:
    """Both subagent tools (new and continue), bound to the same ``service``."""
    return (build_task_tool(service), build_continue_tool(service))


def _continue_request(args: Mapping[str, Any]) -> dict[str, Any]:
    agent_id = args.get("agent_id")
    prompt = args.get("prompt")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise _TaskToolError("'agent_id' must be a non-empty string")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _TaskToolError("'prompt' must be a non-empty string")
    return {"resume": agent_id.strip(), "prompt": prompt}


async def run_continue(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    """Continue a finished subagent through the runner's existing resume path."""
    if not isinstance(args, Mapping):
        return ToolExecutionResult.text(
            "subagent_continue: arguments must be an object", is_error=True
        )
    try:
        request = _continue_request(args)
    except _TaskToolError as exc:
        return ToolExecutionResult.text(f"subagent_continue: {exc}", is_error=True)
    return await _dispatch(request, ctx)


def _service(ctx: ToolContext) -> SubagentServiceView | None:
    candidate = getattr(ctx, "subagents", None)
    if candidate is not None and callable(getattr(candidate, "spawn", None)):
        return candidate
    return None


def _build_request(args: Mapping[str, Any]) -> dict[str, Any]:
    args = _normalized(args)
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _TaskToolError("'prompt' must be a non-empty string")
    request: dict[str, Any] = {"prompt": prompt}
    resume = _resume_of(args)
    for name in ("subagent_type", "model", "description", "resume"):
        value = resume if name == "resume" else args.get(name)
        if value is None:
            continue
        if name == "model" and isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if not isinstance(value, str) or not value.strip():
            raise _TaskToolError(f"'{name}' must be a non-empty string when given")
        request[name] = value
    worktree = args.get("worktree", False)
    if not isinstance(worktree, bool):
        raise _TaskToolError("'worktree' must be a boolean")
    if worktree:
        # Disabled for now (docs/decisions.md); the schema no longer offers it.
        raise _TaskToolError(
            "Subagent worktrees are disabled; subagents share the session checkout"
        )
    if "worktree" in args:
        request["worktree"] = False
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
    actual_model = getattr(outcome, "actual_model", None)
    metrics["actual_model"] = actual_model if isinstance(actual_model, str) else None
    models = getattr(outcome, "models", None)
    metrics["actual_models"] = (
        [str(model) for model in models] if isinstance(models, (list, tuple)) else []
    )
    worktree = getattr(outcome, "worktree", None)
    if isinstance(worktree, Mapping):
        metrics["worktree"] = dict(worktree)
    if not text.strip():
        text = f"Task: subagent {agent} returned {status}"
    if session_id and status == "failed":
        context_note = (
            f"[Task {agent} ({session_id}): failed; the report above has what it "
            "did. If the work still matters, start a new subagent with what it needs]"
        )
    elif session_id:
        context_note = f"[Task {agent} ({session_id}): {status}; the report was shown in full]"
    else:
        context_note = f"[Task {agent}: {status}; the report was shown in full]"
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
    return await _dispatch(request, ctx)


async def _dispatch(request: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    service = _service(ctx)
    if service is not None:
        try:
            if not request.get("resume"):
                request.setdefault("subagent_type", service.default_type)
            spawn = service.spawn
            kwargs = {"cancel": ctx.cancel_token, "emit": ctx.emit}
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
