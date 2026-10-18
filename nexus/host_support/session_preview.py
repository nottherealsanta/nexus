"""Bounded, redacted, toolkit-free text helpers for the sessions dashboard.

Contract: the dashboard list shows a short tail (last prompt, last reply, running
tool) and a "worked" duration for the *current* session, computed from the live
:class:`~nexus.view.ConversationView` the client already holds. There is no
per-session preview request: replaying another session's log to build one was
too slow for the daemon loop. Text is passed through ``redact_secrets``, control
characters become spaces, and every length is capped.

The module imports no UI and no wire types.
"""
from __future__ import annotations

import unicodedata
from typing import Any

from ..util import redact_secrets
from ..view import ConversationView

__all__ = [
    "message_tails",
    "pending_request",
    "worked_label",
    "clean_multiline",
    "clean_preview_text",
]

_MAX_LINE_CHARS = 200


def clean_preview_text(value: Any, limit: int = _MAX_LINE_CHARS) -> str:
    """Redact and bound a display string, replacing all Unicode controls."""
    text = redact_secrets(str(value or ""))
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return text[:limit]



def clean_multiline(value: Any, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Redacted multi-line text keeping newlines (and tabs as spaces); other controls
    become spaces. Keeps the last ``max_lines`` lines and at most ``max_chars`` chars."""
    text = redact_secrets(str(value or "").replace("\r\n", "\n").replace("\r", "\n"))
    text = "".join(
        c if c == "\n" else (" " if unicodedata.category(c).startswith("C") else c) for c in text
    )
    lines = [line.rstrip() for line in text.strip("\n").split("\n")]
    text = "\n".join(lines[-max_lines:])
    return text[-max_chars:].lstrip("\n")


def worked_label(view: ConversationView) -> str:
    """``13s`` / ``2m 05s`` for the last completed agent turn's duration, else empty."""
    for turn in reversed(getattr(view, "turns", ())):
        elapsed = getattr(turn, "elapsed_ms", None)
        if getattr(turn, "kind", "agent") == "agent" and turn.phase == "completed" and elapsed:
            seconds = max(1, round(elapsed / 1000))
            return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"
    return ""


def pending_request(view: ConversationView, *, depth: int = 0) -> dict[str, Any]:
    """The oldest unanswered permission request as ``{request_id, kind, summary, ts}``, else ``{}``.

    The sessions dashboard shows it on a "Needs you" row and answers it without opening
    the session. Root first, then nested agents; the summary is redacted and bounded.
    """
    pending = [item for item in getattr(view, "permissions", ()) if getattr(item, "status", "") == "pending" and item.id]
    if pending:
        item = pending[0]
        what = clean_preview_text(item.preview or item.key or "", 160).strip()
        tool = clean_preview_text(item.tool or "tool", 40)
        return {"request_id": item.id[:128], "kind": "permission",
                "summary": f"Allow {tool}: {what}" if what else f"Allow {tool}", "ts": float(item.ts or 0.0)}
    if depth < 8:
        for agent in getattr(view, "agents", {}).values():
            found = pending_request(agent.body, depth=depth + 1)
            if found:
                return found
    return {}


def message_tails(view: ConversationView, *, limit: int = 400) -> dict[str, str]:
    """Latest user and assistant text, plus the running tool, already cleaned.

    Tool-result user turns have no text here, so they do not replace the last prompt.
    ``activity`` is empty when nothing is requested or running.
    """
    last_user = ""
    last_agent = ""
    for message in getattr(view, "messages", ()):
        text = getattr(message, "text", "") or ""
        if not str(text).strip():
            continue
        role = getattr(message, "role", "")
        if role == "assistant":
            cleaned = clean_multiline(text)
            if cleaned.strip():
                last_agent = cleaned
            continue
        cleaned = clean_preview_text(" ".join(str(text).split()), limit)
        if cleaned and role == "user":
            last_user = cleaned
    activity = ""
    running = [
        tool
        for tool in getattr(view, "tools", ())
        if getattr(tool, "status", "") in {"requested", "running"}
    ]
    if running:
        tool = running[-1]
        label = " ".join(
            filter(None, [getattr(tool, "name", "") or "", getattr(tool, "target", "") or ""])
        )
        activity = clean_preview_text(label)
    return {"last_user": last_user, "last_agent": last_agent, "activity": activity}

