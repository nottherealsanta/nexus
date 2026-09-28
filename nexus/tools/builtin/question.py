"""``question``: ask the operator one question and wait for the answer.

TOOLS_PLAN "question": a prompt with one to three choices, or free text. The
builtin reaches the operator only through
:class:`~nexus.tools.spec.QuestionServiceView` (a root-session binding of
:class:`~nexus.tools.questions.QuestionBroker`), so it never imports the runtime
or a UI. Lifecycle events go through the call's own emitter, so the question
and its answer are durable in the asking turn's log (and, for a child agent,
relayed to the root). An answer is data for the model, never a permission grant.

With no attended operator the call fails fast; a cancelled, abandoned, or
timed-out question emits a closing ``question.resolved`` so replay never shows
it as pending.
"""
from __future__ import annotations

from typing import Any

from ...errors import OperationCancelled, ToolError
from ..questions import (
    MAX_QUESTION_PROMPT_CHARS,
    QuestionBrokerError,
    QuestionChoice,
    QuestionTimeout,
)
from ..spec import ToolContext, ToolExecutionResult, ToolSpec
from .read import _error

__all__ = ["ANSWER_WINDOW_S", "MAX_CHOICES", "SPEC", "run"]

MAX_CHOICES = 3
#: How long a person gets to answer; the runtime's broker uses the same bound.
ANSWER_WINDOW_S = 15 * 60.0
MAX_CHOICE_LABEL_CHARS = 200

SPEC = ToolSpec(
    name="question",
    description=(
        "Ask the user one question and wait for the answer. Use it only when a "
        "real decision is theirs to make and cannot be settled from the request, "
        "the code, or a sensible default. Offer 1-3 short options, or omit "
        "options to ask for free text. Returns the user's answer."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The complete question, ending with a question mark.",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_CHOICES,
                "description": (
                    "1-3 distinct answer labels, recommended first. Omit for a "
                    "free-text answer."
                ),
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    bundle="task",
    mutates=False,
    concurrency="exclusive",
    # The tool's own deadline is the broker's; the manager must not cut it short.
    timeout_s=ANSWER_WINDOW_S + 30.0,
    max_result_tokens=2_000,
)


def _choices(raw: object) -> tuple[QuestionChoice, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_CHOICES:
        raise ToolError(f"options must be a list of at most {MAX_CHOICES} strings")
    labels: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ToolError("each option must be a non-empty string")
        label = " ".join(item.split())
        if len(label) > MAX_CHOICE_LABEL_CHARS:
            raise ToolError(f"options are limited to {MAX_CHOICE_LABEL_CHARS} characters")
        if label in labels:
            raise ToolError("options must be distinct")
        labels.append(label)
    return tuple(QuestionChoice(id=str(i + 1), label=label) for i, label in enumerate(labels))


async def _close(ctx: ToolContext, status: str, reason: str) -> None:
    """Record that the question ended unanswered (keyed by the call id)."""
    if ctx.emit is None:
        return
    outcome = ctx.emit("question.resolved", {"status": status, "reason": reason})
    if hasattr(outcome, "__await__"):
        await outcome


async def run(args: dict[str, Any], ctx: ToolContext) -> ToolExecutionResult:
    prompt = args.get("question")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("question must be a non-empty string")
    if len(prompt) > MAX_QUESTION_PROMPT_CHARS:
        return _error(f"question is limited to {MAX_QUESTION_PROMPT_CHARS} characters")
    try:
        choices = _choices(args.get("options"))
    except (ToolError, QuestionBrokerError) as exc:
        return _error(str(exc))
    service = ctx.questions
    if service is None or not service.attended:
        return _error(
            "No user is attached to answer questions. Proceed with your best "
            "judgment and state the assumption you made."
        )
    try:
        answer = await service.ask(
            source_session_id=ctx.session_id or "session",
            turn_id=ctx.turn_id or "turn",
            agent_id=ctx.agent_id or "root",
            call_id=ctx.call_id or "call",
            prompt=prompt.strip(),
            choices=choices,
            emit=ctx.emit,
            cancel=ctx.cancel_token,
        )
    except QuestionTimeout:
        await _close(ctx, "timeout", "no answer before the deadline")
        return _error(
            "The user did not answer in time. Proceed with your best judgment "
            "and state the assumption you made."
        )
    except OperationCancelled as exc:
        await _close(ctx, "cancelled", str(exc) or "cancelled")
        if ctx.cancel_token is not None and ctx.cancel_token.cancelled:
            raise
        return _error(
            "The question was dismissed without an answer. Proceed with your "
            "best judgment and state the assumption you made."
        )
    except QuestionBrokerError as exc:
        return _error(f"question is invalid: {exc}")
    label = next((choice.label for choice in choices if choice.id == answer), answer)
    return ToolExecutionResult.text(
        f"The user answered: {label}", display=label, metrics={"answer": label}
    )
