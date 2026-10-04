"""Pure session-card facts shared by the Textual and native session sidebars.

Status in words, relative age and the card's second line; no UI toolkit here.
"""
from __future__ import annotations

from collections.abc import Mapping
import time
from typing import Any


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def session_status(summary: Any, seen: Mapping[str, int], current: str) -> str:
    """``working``, ``input``, ``done`` (finished since last viewed), or ``idle``."""
    state = getattr(summary, "state", "idle")
    if state == "running":
        return "working"
    if state in {"awaiting_permission", "awaiting_input"}:
        return "input"
    last_seen = seen.get(summary.id)
    # Current SessionSummary values always have this field. The fallback is
    # only for third-party/legacy summary-like values without that schema.
    activity_seq = getattr(summary, "completion_seq", summary.last_seq)
    if summary.id != current and last_seen is not None and activity_seq > last_seen:
        return "done"
    return "idle"


def relative_time(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return ""
    seconds = max(0.0, (now or time.time()) - float(ts))
    if seconds < 45:
        return "just now"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = round(minutes / 60)
    return f"{hours}h ago" if hours < 24 else f"{round(hours / 24)}d ago"


SESSION_WORDS = {"working": "working now", "input": "needs input", "done": "finished", "archived": "archived"}


def session_subline(summary: Any, status: str, now: float | None = None, *, compact: bool = False) -> str:
    """The card's second line: status in words (or the message count), then age."""
    words = SESSION_WORDS.get(status)
    if words is None:
        count = _int(getattr(summary, "message_count", 0))
        words = f"{count} message{'' if count == 1 else 's'}" if count else "no messages"
    age = relative_time(getattr(summary, "last_activity", 0.0), now)
    if compact:
        words = str(_int(getattr(summary, "message_count", 0)))
        age = age.removesuffix(" ago").replace("just now", "now")
    return f"{words} · {age}" if age else words
