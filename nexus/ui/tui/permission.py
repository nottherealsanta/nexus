"""Attended approval and question prompts; arbitration stays daemon-side.

Both are :class:`ListPrompt` lists docked above the composer, like the model
and slash-command pickers. Approvals answer one of the four protocol decision
strings; questions answer by ``call_id`` through ``QuestionAnswer``.
"""

from __future__ import annotations

from typing import Any

from ...client import ClientError
from ...ui_support.prompts import PendingQuestion, approval_choices, pending_questions
from ...ui_support.tui_list import ListPrompt
from ..cli.approve import approval_data, describe


class PermissionScreen(ListPrompt):
    """Present the request and return one of the protocol decision strings."""

    def __init__(self, data: dict) -> None:
        self.data = approval_data(data)
        header, _, body = describe(self.data).partition("\n")
        if self.data.get("_targets_unavailable"):
            body += "\n  approval: unavailable; this request will be denied"
        super().__init__(
            header.replace("Permission requested: ", "Allow ", 1) + "?",
            body.replace("\n  ", "\n").strip(),
            approval_choices(self.data),
            cancel="deny_once",
            cancel_hint="deny",
        )


class QuestionScreen(ListPrompt):
    """One agent question: pick an option, or type a free-text answer."""

    def __init__(self, question: PendingQuestion) -> None:
        self.question = question
        super().__init__(
            f"{question.agent} asks" if question.agent else "Question",
            question.prompt,
            question.choices(),
            free_text=not question.options,
        )


async def ask_pending_question(app: Any) -> None:
    """Show the oldest unanswered question once; later ones queue behind it."""
    if isinstance(app.screen, (QuestionScreen, PermissionScreen)):
        return
    asked: set[str] | None = getattr(app, "_asked_questions", None)
    if asked is None:
        asked = app._asked_questions = set()
    question = next(
        (q for q in pending_questions(app.controller.view) if q.call_id not in asked), None
    )
    if question is None:
        return
    asked.add(question.call_id)

    async def answer(value: str | None) -> None:
        if value is None:
            app._sync_status("Question hidden · it reopens with the next update")
            asked.discard(question.call_id)
            return
        try:
            ok, error = await app.controller.client.answer_question(
                app.controller.session, question.call_id, value
            )
            app._sync_status("Answer sent" if ok else error or "Question already answered")
        except ClientError as exc:
            asked.discard(question.call_id)
            app._sync_status(f"Answer failed · {exc}", error=True)
        await ask_pending_question(app)

    app.push_screen(QuestionScreen(question), callback=answer)


__all__ = ["ListPrompt", "PermissionScreen", "QuestionScreen", "ask_pending_question"]
