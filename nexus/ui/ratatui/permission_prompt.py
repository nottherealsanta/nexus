"""Structured fields of the permission prompt (plan: DESIGN OVERHAUL step 9, plate P-PRM).

The Rust client draws ``title`` (``Allow <tool> <target>?``), a ``waiting on you`` timer counted
from ``since`` (when the request was asked, ``PermissionView.ts``), ``what`` rows (the targets or
the preview the request carries) and ``why`` (the rule that asked). ``lines`` stays in the prompt
for older clients. Everything is bounded and control characters are escaped.
"""
from __future__ import annotations

from ...ui_support.text import escape_controls

TITLE_LIMIT = 120
ROW_LIMIT = 200
WHAT_LIMIT = 12  # the client clips what it cannot show with "… N more"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _what(permission) -> list[str]:
    targets = getattr(permission, "targets", None) or []
    if targets:
        rows = [f"{item.role}: {item.path}" + (f" — {item.reason}" if item.reason else "") for item in targets]
    else:
        text = permission.preview or permission.key or ""
        rows = [line for line in str(text).splitlines() if line.strip()]
    return [escape_controls(_clip(row, ROW_LIMIT)) for row in rows[:WHAT_LIMIT]]


def permission_fields(permission) -> dict:
    """``title``, ``since``, ``what`` and ``why`` for one pending ``PermissionView``."""
    tool = _clip(permission.tool or "tool", 40)
    targets = getattr(permission, "targets", None) or []
    if targets:
        target = targets[0].path + (f" (+{len(targets) - 1} more)" if len(targets) > 1 else "")
    else:
        target = (str(permission.preview or permission.key or "").splitlines() or [""])[0]
    title = f"Allow {tool} {_clip(target, TITLE_LIMIT)}?" if target.strip() else f"Allow {tool}?"
    return {
        "title": escape_controls(_clip(title, TITLE_LIMIT + 50)),
        "since": float(permission.ts or 0.0),
        "what": _what(permission),
        "why": escape_controls(_clip(permission.default_rule or "", ROW_LIMIT)),
    }
