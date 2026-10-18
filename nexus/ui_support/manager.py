"""Manager session presentation shared by the native clients (docs/manager.md, plan 02).

Pure helpers over the ``ManagerState`` host result: the pinned sidebar entry
(status, model, sign-in, Live readiness), the cross-session Live notice, the
commands a manager chat refuses, and the unconditional launch/cancel approval
rows. Nothing here talks to the host. The constants mirror ``nexus.host.facade``
and ``nexus.agents.manager_prompt``; this layer may not import them, so
``tests/test_manager_ui.py`` pins the equality.
"""
from __future__ import annotations

import textwrap
from collections.abc import Mapping
from typing import Any

from .prompts import PromptChoice
from .text import escape_controls

#: The fixed id of the workspace manager session (``nexus.host.facade.MANAGER_SESSION_ID``).
MANAGER_SESSION_ID = "nexus-manager"
#: The model route the manager is pinned to (``manager_prompt.MANAGER_MODEL_REF``).
MANAGER_MODEL_REF = "github-copilot/gpt-6-luna"
#: The manager tools whose every call is confirmed by the human, never remembered.
CONFIRMED_TOOLS = frozenset({"session_launch", "session_cancel"})
#: The manager bundle name; a permission request for these tools carries it.
MANAGER_BUNDLE = "manager"

ENABLE_HINT = "Turn it on with [manager] enabled = true in nexus.toml, then restart the daemon."
DISABLED_SUMMARY = "Off · [manager] enabled = false"
SIGN_IN_HINT = "sign in to GitHub Copilot under Settings > Providers"

#: Commands that configure an ordinary session; the manager is structural, so they are refused here
#: (the host refuses them too, this is the early, readable answer).
REFUSED_COMMANDS: dict[str, str] = {
    "/model": "The manager always runs on its pinned model",
    "/effort": "The manager always runs at its provider default effort",
    "/agent": "The manager has no role; it is not an agent you can switch",
    "/fork": "The manager cannot be forked",
    "/handoff": "The manager cannot be handed off",
}
SHELL_REFUSED = "The manager has no shell: ask it to launch a session instead"

#: Shown in the Live consent dialog for a call on the manager (on top of the base notice).
CONSENT_LINES: tuple[str, ...] = (
    "A call on the Manager also sends OpenAI what the",
    "Manager reads of your OTHER sessions:",
    "  - their titles and status",
    "  - the final reports of sessions it reads",
    "    (file contents and output can be inside)",
    "  - the instructions of sessions it launches",
    "",
    "Anything said aloud to the Manager can start a",
    "session, but launching and cancelling are always",
    "confirmed on screen. The voice can never approve.",
    "",
    "Asked separately from the base Live notice; withdraw",
    "it with /live revoke manager (keeps the base notice).",
)
#: One line next to the Live affordance of the pinned entry.
PRIVACY_NOTE = "Voice calls also share other sessions' titles, status and reports with OpenAI"

_WORKING = {"running", "working", "active"}
_INPUT = {"awaiting_input", "awaiting_permission", "input"}


def _clean(value: Any, limit: int = 200) -> str:
    text = " ".join(escape_controls(str(value or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def status_of(state: str) -> str:
    """Glyph token for a session state: ``working``, ``input`` or ``idle``."""
    if state in _WORKING:
        return "working"
    if state in _INPUT:
        return "input"
    return "idle"


def live_line(state: Any) -> tuple[str, bool]:
    """``(text, ready)``: the single line saying whether a Live call on the manager can start."""
    if not getattr(state, "codex_signed_in", False):
        return "Voice needs a ChatGPT (Codex) sign-in under Settings > Providers", False
    if not getattr(state, "live_consent", False):
        return "Voice asks for its data notice the first time", False
    if not getattr(state, "live_manager_consent", False):
        return "Voice asks for the cross-session notice the first time", False
    return "Voice ready", True


def entry(state: Any, *, current: str = "", error: str = "") -> dict[str, Any]:
    """The pinned Manager entry (snapshot field ``manager`` and the dashboard header).

    ``state`` is a ``ManagerStateResult`` (or ``None`` before the first read, or after a
    failed read, when ``error`` says why). The entry always says what is missing and the
    exact fix; it never hides the manager just because it is off.
    """
    if state is None:
        summary = _clean(error) if error else "Checking the manager…"
        return {"known": False, "enabled": False, "active": False, "session": "", "status": "idle",
                "tone": "warn" if error else "off", "model": MANAGER_MODEL_REF, "summary": summary,
                "notes": [], "live": "", "live_ready": False, "managed": 0, "can_open": False,
                "privacy": PRIVACY_NOTE}
    enabled = bool(getattr(state, "enabled", False))
    session = _clean(getattr(state, "session", ""), 256)
    model = _clean(getattr(state, "model", "") or MANAGER_MODEL_REF, 120)
    signed_in = bool(getattr(state, "signed_in", False))
    sign_in = _clean(getattr(state, "sign_in", "") or SIGN_IN_HINT)
    managed = len(getattr(state, "managed", None) or [])
    live_text, live_ready = live_line(state)
    notes: list[str] = []
    if not enabled:
        tone, summary, status = "off", DISABLED_SUMMARY, "idle"
        notes.append(ENABLE_HINT)
    else:
        status = status_of(str(getattr(state, "state", "") or ""))
        if not signed_in:
            tone, summary = "warn", f"Not signed in · {sign_in}"
        else:
            tone = "ok"
            summary = {"working": "Working", "input": "Needs you"}.get(status, "Ready")
            if managed:
                summary += f" · {managed} session{'s' if managed != 1 else ''} launched"
        notes.append(f"Model {model} · fixed, no fallback")
        if not signed_in:
            notes.append(f"Opening it works; turns fail until you {sign_in}")
        notes.append(live_text)
        notes.append('Launch: ask "Start a session to do X"; review and confirm the instruction.')
        notes.append('Find: ask "What finished in the last two hours?"; /sessions opens the picker; m returns here.')
    return {
        "known": True, "enabled": enabled, "active": bool(session) and session == current,
        "session": session, "status": status, "tone": tone, "model": model, "summary": summary,
        "notes": notes, "live": live_text if enabled else "", "live_ready": live_ready and enabled,
        "managed": managed, "can_open": enabled, "signed_in": signed_in,
        "privacy": PRIVACY_NOTE,
    }


def status_lines(state: Any) -> dict[str, str]:
    """Labelled rows for ``/manager status`` (every fact the host reports, nothing hidden)."""
    if state is None:
        return {"Manager": "unknown"}
    yes = lambda flag: "yes" if flag else "no"  # noqa: E731
    return {
        "Enabled": yes(getattr(state, "enabled", False)),
        "Session": _clean(getattr(state, "session", "")) or "not created yet",
        "State": _clean(getattr(state, "state", "")) or "none",
        "Model": _clean(getattr(state, "model", "")) or MANAGER_MODEL_REF,
        "GitHub Copilot": "signed in" if getattr(state, "signed_in", False)
        else _clean(getattr(state, "sign_in", "")) or SIGN_IN_HINT,
        "Codex (GPT Live)": "signed in" if getattr(state, "codex_signed_in", False) else "not signed in",
        "Live notice": yes(getattr(state, "live_consent", False)),
        "Cross-session notice": yes(getattr(state, "live_manager_consent", False)),
        "Live ready": yes(getattr(state, "live_ready", False)),
        "Launched sessions": ", ".join(_clean(item, 80) for item in getattr(state, "managed", None) or []) or "none",
        "Launch cap": f"{getattr(state, 'max_launches_per_turn', 0)} per turn, "
                      f"{getattr(state, 'max_live_managed', 0)} running at once",
    }


# -- confirmation ------------------------------------------------------------------------


def is_confirmed(data: Mapping[str, Any]) -> bool:
    """Whether a permission request is a manager launch/cancel (always asked, never remembered)."""
    tool = str(data.get("tool") or "")
    bundle = data.get("bundle")
    return tool in CONFIRMED_TOOLS and (bundle in (None, "", MANAGER_BUNDLE))


def approval_choices(data: Mapping[str, Any]) -> tuple[PromptChoice, ...]:
    """Only the one-off decisions: launch/cancel, or keep as is. No "always" row exists."""
    cancel = str(data.get("tool") or "") == "session_cancel"
    return (
        PromptChoice("Stop that session" if cancel else "Launch this session", "allow_once",
                     "this once; asked again next time", "y"),
        PromptChoice("Keep it running" if cancel else "Do not launch", "deny_once",
                     "the manager is told no", "n"),
    )


_ROW_WIDTH = 72
_ROW_LIMIT = 40
ASKED_EVERY_TIME = "Asked every time · this answer is never remembered"


def _wrapped(text: str) -> list[str]:
    rows: list[str] = []
    for line in str(text).splitlines() or [""]:
        rows.extend(textwrap.wrap(line, _ROW_WIDTH, replace_whitespace=False, drop_whitespace=False,
                                  break_long_words=True) or [""])
    return rows


def confirmation(data: Mapping[str, Any]) -> dict[str, Any]:
    """Prompt fields for a manager launch/cancel: ``title``, ``what`` rows, readable ``lines``.

    The whole instruction (up to the host's 16 KiB) is wrapped into rows; if the rows are cut the
    last one says how many characters were not shown. ``why`` stays empty: there is no rule to remember.
    """
    cancel = str(data.get("tool") or "") == "session_cancel"
    key = escape_controls(str(data.get("key") or data.get("preview") or ""))
    if cancel:
        title = "Stop a session the manager launched?"
        head = "Cancels it and drops its queued input (id: reason):"
    else:
        title = "Launch a new session?"
        head = "Starts a root session with this instruction ([role] first if named):"
    body = _wrapped(key)
    shown = body[:_ROW_LIMIT]
    if len(body) > _ROW_LIMIT:
        shown.append(f"… {len(''.join(body[_ROW_LIMIT:])):,} more characters not shown (the tool call has all of it)")
    what = [head, *shown]
    return {"title": title, "what": what, "why": ASKED_EVERY_TIME, "what_rows": 8,
            "lines": [title, "", *what, "", ASKED_EVERY_TIME]}


__all__ = [
    "ASKED_EVERY_TIME", "CONFIRMED_TOOLS", "CONSENT_LINES", "DISABLED_SUMMARY", "ENABLE_HINT", "MANAGER_BUNDLE",
    "MANAGER_MODEL_REF", "MANAGER_SESSION_ID", "PRIVACY_NOTE", "REFUSED_COMMANDS", "SHELL_REFUSED",
    "approval_choices", "confirmation", "entry", "is_confirmed", "live_line", "status_lines", "status_of",
]
