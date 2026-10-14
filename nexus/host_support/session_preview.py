"""Bounded, redacted, toolkit-free session projection for the sessions dashboard.

Contract (plans/in-progress/GROK_BUILD_TUI_PLAN.md section 6.4): a client shows
a read-only mini transcript of the highlighted session without reading that
session's records itself. :func:`project_preview` folds nothing; it takes the
reducer's :class:`~nexus.view.ConversationView` (the same projection every
surface renders) and returns plain data. Text is passed through
``redact_secrets``, control characters become spaces, and every count is capped,
so a huge or hostile transcript cannot grow the reply.

The module imports no UI and no wire types; :class:`~nexus.host.facade.HostFacade`
wraps the returned dict in the protocol structs.
"""
from __future__ import annotations

import unicodedata
from typing import Any

from ..util import redact_secrets
from ..view import ConversationView

__all__ = [
    "MAX_PREVIEW_ROWS",
    "USER_ROWS",
    "ASSISTANT_ROWS",
    "project_preview",
    "message_tails",
    "worked_label",
    "clean_multiline",
    "clean_preview_text",
    "session_state",
    "validate_max_rows",
]

MAX_PREVIEW_ROWS = 50
#: The last user prompt shows at most this many lines, the reply or activity this many.
USER_ROWS = 2
ASSISTANT_ROWS = 6
_MAX_LINE_CHARS = 200
_MAX_SOURCE_CHARS = 4000  # text scanned per message before wrapping into lines
_MAX_PENDING = 3
_MAX_OPTIONS = 8
_MAX_SCAN_DEPTH = 8


def validate_max_rows(max_rows: Any) -> int:
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_PREVIEW_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_PREVIEW_ROWS}")
    return max_rows


def clean_preview_text(value: Any, limit: int = _MAX_LINE_CHARS) -> str:
    """Redact and bound a display string, replacing all Unicode controls."""
    text = redact_secrets(str(value or ""))
    text = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return text[:limit]


_clean = clean_preview_text


def _lines(value: str, count: int) -> tuple[list[str], bool]:
    """The first ``count`` non-empty lines of ``value`` and whether more were cut."""
    source = value[:_MAX_SOURCE_CHARS]
    found = [" ".join(_clean(line, _MAX_SOURCE_CHARS).split()) for line in redact_secrets(source).split("\n")]
    found = [line[:_MAX_LINE_CHARS] for line in found if line]
    return found[:count], len(found) > count or len(value) > _MAX_SOURCE_CHARS


def _pending_questions(view: Any, depth: int = 0) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for turn in getattr(view, "turns", ()):
        for tool in turn.tools:
            if getattr(tool, "name", "") != "question" or getattr(tool, "status", "") != "running":
                continue
            data = getattr(tool, "input", None) or {}
            prompt = data.get("question")
            if not isinstance(prompt, str) or not prompt.strip() or not tool.call_id:
                continue
            raw = data.get("options")
            options = [
                _clean(" ".join(item.split()), 80)
                for item in (raw if isinstance(raw, list) else ())
                if isinstance(item, str) and item.strip()
            ][:_MAX_OPTIONS]
            found.append({"kind": "question", "id": _clean(tool.call_id), "text": _clean(" ".join(prompt.split())), "options": options})
    if depth < _MAX_SCAN_DEPTH:
        for child in (getattr(view, "agents", None) or {}).values():
            found.extend(_pending_questions(child.body, depth + 1))
    return found


def _pending(view: ConversationView) -> list[dict[str, Any]]:
    found = [
        {
            "kind": "permission",
            "id": _clean(item.id),
            "text": _clean(" ".join(filter(None, [item.tool, item.preview or item.bundle or ""]))),
            "options": [],
        }
        for item in view.pending_permissions
    ]
    found.extend(_pending_questions(view))
    return found


def session_state(view: ConversationView, *, active: bool = False) -> str:
    """``needs_input`` (approval or question open), ``working`` (turn active) or ``idle``."""
    if _pending(view) or view.phase in {"awaiting_input", "awaiting_permission"}:
        return "needs_input"
    if active or view.phase == "running" or view.active_turn is not None:
        return "working"
    return "idle"


def _context(view: ConversationView) -> tuple[int | None, int | None]:
    data = view.context if isinstance(view.context, dict) else {}
    data = data.get("context", data)
    if not isinstance(data, dict):
        return None, None

    def number(key: str) -> int | None:
        value = data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    measured = number("measured_tokens")
    used = measured if measured is not None else number("used_tokens")
    return used, number("context_window") or number("input_budget")


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


def project_preview(view: ConversationView, *, max_rows: int = 12, active: bool = False) -> dict[str, Any]:
    """Project ``view`` into ``rows``, ``pending`` and header facts, at most ``max_rows`` rows.

    Rows are single display lines ``{"kind", "text"}`` with kind ``user``,
    ``assistant`` or ``activity``: the last user prompt (two lines), then the
    running tool group label while a turn works, else the last reply excerpt
    (six lines).
    """
    max_rows = validate_max_rows(max_rows)
    rows: list[dict[str, str]] = []
    truncated = False

    users = [m for m in view.messages if m.role == "user" and m.text.strip()]
    if users:
        lines, cut = _lines(users[-1].text, USER_ROWS)
        rows.extend({"kind": "user", "text": line} for line in lines)
        truncated = truncated or cut

    state = session_state(view, active=active)
    running = [t for t in view.tools if t.status in {"requested", "running"}]
    if state == "working" and running:
        tool = running[-1]
        label = " ".join(filter(None, [tool.name, tool.target or ""]))
        rows.append({"kind": "activity", "text": _clean(label)})
    else:
        replies = [m for m in view.messages if m.role == "assistant" and m.text.strip()]
        if replies:
            lines, cut = _lines(replies[-1].text, ASSISTANT_ROWS)
            rows.extend({"kind": "assistant", "text": line} for line in lines)
            truncated = truncated or cut

    if len(rows) > max_rows:
        rows, truncated = rows[:max_rows], True

    pending = _pending(view)
    truncated = truncated or len(pending) > _MAX_PENDING
    used, window = _context(view)
    fraction = min(1.0, used / window) if used is not None and window else None
    tails = message_tails(view)
    turn = next((t for t in reversed(view.turns) if t.agent), None)
    agent = _clean((turn.agent or {}).get("name"), 80) if turn is not None else ""
    model_info = view.model if isinstance(view.model, dict) else {}
    model = _clean(
        model_info.get("reference")
        or "/".join(filter(None, [model_info.get("provider"), model_info.get("model")])),
        120,
    )
    return {
        "state": state,
        "agent": agent,
        "model": model,
        "context_used": used,
        "context_window": window,
        "context_fraction": fraction,
        "last_seq": view.last_seq,
        "rows": rows,
        "pending": pending[:_MAX_PENDING],
        "truncated": truncated,
        "last_user": tails["last_user"],
        "last_agent": tails["last_agent"],
        "activity": tails["activity"],
        "worked": worked_label(view),
    }
