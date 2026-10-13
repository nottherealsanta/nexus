"""``escalate``: a subagent tells its parent it is blocked and needs a decision.

Subagents only: the runtime adds this tool to a child's catalog and never to a
root agent's. A root already answers to the user (it escalates by replying), so
this is the child -> root half of the attention route
(docs/agents.md "Attention goes to the root").

The tool holds **no authority**: it grants nothing and asks the user nothing. It
records ``needed`` and ``why`` through an injected callback; the runtime folds
them into the child's ``SubagentOutcome.attention``, which the root reads and
either resolves or passes on to the user. The child is told to stop and report,
because a blocked child ends and is re-delegated rather than paused.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..spec import RegisteredTool, ToolContext, ToolExecutionResult, ToolSpec
from .read import _error

__all__ = ["MAX_FIELD_CHARS", "SPEC", "build_escalate_tool"]

MAX_FIELD_CHARS = 600
#: Bound on distinct escalations kept per child.
MAX_ESCALATIONS = 5

SPEC = ToolSpec(
    name="escalate",
    description=(
        "Report to the parent agent that you are blocked and cannot finish without a "
        "decision, permission or resource you lack (a denied tool, a path outside your "
        "scope, missing credentials, an ambiguous requirement only the user can settle). "
        "State exactly what is needed and why. This grants nothing and does not wait: "
        "after calling it, stop working, and summarize what you finished and what is "
        "left. Do not use it for ordinary errors you can work around."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "needed": {
                "type": "string",
                "minLength": 1,
                "description": "What is needed: the specific action, access or decision.",
            },
            "why": {
                "type": "string",
                "minLength": 1,
                "description": "Why the task cannot be completed without it.",
            },
        },
        "required": ["needed", "why"],
        "additionalProperties": False,
    },
    bundle="task",
    mutates=False,
    concurrency="exclusive",
    max_result_tokens=1_000,
)


def _field(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())[:MAX_FIELD_CHARS]


def build_escalate_tool(record: Callable[[str], bool]) -> RegisteredTool:
    """A tool bound to one child; ``record`` stores the escalation (False = limit hit)."""

    async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
        needed, why = _field(args, "needed"), _field(args, "why")
        if needed is None or why is None:
            return _error("escalate needs non-empty 'needed' and 'why'")
        if not record(f"{needed} (why: {why})"):
            return _error(
                f"escalation limit reached ({MAX_ESCALATIONS}); stop and report what you have"
            )
        return ToolExecutionResult.text(
            "Escalation recorded for your parent agent. It grants nothing. Stop now and give "
            "a final report: what you finished, what is left, and what you need.",
            display=f"escalated: {needed}",
            metrics={"escalated": True},
        )

    return RegisteredTool(spec=SPEC, run=run, origin="builtin")
