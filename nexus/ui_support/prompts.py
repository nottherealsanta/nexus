"""UI-neutral choices for operator prompts: approvals and agent questions.

Every attended surface shows both prompts as a list (like the model and
slash-command pickers). This module owns the non-visual part so the TUI and
the web client offer the same rows and shortcuts:

* :func:`approval_choices` turns a ``permission.requested`` payload into the
  four protocol decisions, degrading fail-closed exactly as the CLI approver.
* :func:`pending_questions` finds unanswered ``question`` tool calls in a
  :class:`~nexus.view.ConversationView`, including nested agents. The running
  tool call *is* the pending question, so reconnect and replay need no extra
  view state; clients answer it by ``call_id`` through ``QuestionAnswer``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..host_support.approval import approval_data

MAX_QUESTION_OPTIONS = 3


@dataclass(frozen=True)
class PromptChoice:
    """One row of a list prompt; ``value`` is what the prompt answers."""

    label: str
    value: str
    hint: str = ""
    key: str = ""
    disabled: bool = False


def approval_choices(data: Mapping[str, Any]) -> tuple[PromptChoice, ...]:
    """The four decisions, with unavailable ones disabled or downgraded."""
    data = approval_data(data)
    unavailable = bool(data.get("_targets_unavailable"))
    persistent = bool(data.get("persistence_available", True)) and not unavailable
    rule = str(data.get("default_rule") or "")
    return (
        PromptChoice("Allow once", "allow_once", "run this call", "y", disabled=unavailable),
        PromptChoice(
            "Allow for session" if persistent or unavailable else "Allow this tool once",
            "allow_always" if persistent else "allow_once",
            (rule or "remember this rule") if persistent else "persistence unavailable",
            "a",
            disabled=unavailable,
        ),
        PromptChoice("Deny once", "deny_once", "skip this call", "n"),
        PromptChoice(
            "Deny for session" if persistent or unavailable else "Deny this tool once",
            "deny_always" if persistent or unavailable else "deny_once",
            "never ask again" if persistent or unavailable else "persistence unavailable",
            "d",
        ),
    )


@dataclass(frozen=True)
class PendingQuestion:
    call_id: str
    prompt: str
    options: tuple[str, ...]
    agent: str = ""

    def choices(self) -> tuple[PromptChoice, ...]:
        """Choice ids are 1-based positions, matching the ``question`` tool."""
        return tuple(
            PromptChoice(label, str(index), "", str(index))
            for index, label in enumerate(self.options, 1)
        )


def _question(tool: Any, agent: str) -> PendingQuestion | None:
    if getattr(tool, "name", "") != "question" or getattr(tool, "status", "") != "running":
        return None
    data = getattr(tool, "input", None) or {}
    prompt = data.get("question")
    if not isinstance(prompt, str) or not prompt.strip() or not tool.call_id:
        return None
    raw = data.get("options")
    options = tuple(
        " ".join(item.split()) for item in (raw if isinstance(raw, list) else ())
        if isinstance(item, str) and item.strip()
    )[:MAX_QUESTION_OPTIONS]
    return PendingQuestion(tool.call_id, prompt.strip(), options, agent)


def pending_questions(view: Any, *, agent: str = "", depth: int = 0) -> list[PendingQuestion]:
    """Unanswered questions, root first, then each nested agent in order."""
    found = [
        question
        for turn in getattr(view, "turns", ())
        for tool in turn.tools
        if (question := _question(tool, agent)) is not None
    ]
    if depth < 8:
        for agent_id, child in (getattr(view, "agents", None) or {}).items():
            label = str(getattr(child, "description", "") or getattr(child, "type", "") or agent_id)
            found.extend(pending_questions(child.body, agent=label, depth=depth + 1))
    return found


__all__ = [
    "MAX_QUESTION_OPTIONS",
    "PendingQuestion",
    "PromptChoice",
    "approval_choices",
    "pending_questions",
]
