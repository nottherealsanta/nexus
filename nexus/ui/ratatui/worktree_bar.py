"""Top-bar worktree label for the session on screen (named session worktrees).

Contract: ``session_worktree_label(state, git_branch, workspace_worktree)`` maps the host's
``session_worktree_choice`` state to the top bar's ``(branch, worktree)`` texts. A session whose
root is its own worktree shows ``⎇ <name>`` and the session's branch; a draft with a worktree
selected shows ``⎇ <pending name>`` or a placeholder until the first message names it; a session in the
main checkout of a Git workspace (``kind == "main"``) shows ``main checkout`` (plain text, so ASCII
mode needs no variant) so the location is always stated; anything else (unknown state, non-Git workspace) keeps the
workspace's own values. ``session_worktree_kind`` gives the matching renderer style key
(``worktree`` | ``pending`` | ``main`` | ``""``) so Rust never parses the label. Missing state keys count as empty/False, so this works
before the host reports them. ``refresh_session_worktree`` reads the state off the hot path:
cached for a few seconds per session, bounded by a timeout, failures leave the bar unchanged.
"""
from __future__ import annotations

import asyncio
import os
import time


PLACEHOLDER = "worktree · name on first message"
_TTL = 3.0
_WAIT = 2.0


def _glyph() -> str:
    return "wt:" if os.environ.get("NEXUS_ASCII") == "1" else "⎇ "


def session_worktree_label(state: dict, git_branch: str, workspace_worktree: str) -> tuple[str, str]:
    """The top bar's ``(branch, worktree)`` for ``state`` (the host's session worktree facts)."""
    if not isinstance(state, dict):
        return git_branch, workspace_worktree
    if state.get("kind") == "worktree":
        name = str(state.get("worktree_name") or "")
        branch = str(state.get("branch") or git_branch)
        return branch, (_glyph() + name)[:80] if name else workspace_worktree
    if _pending_draft(state):
        pending = str(state.get("pending_name") or "")
        return git_branch, (_glyph() + (pending or PLACEHOLDER))[:80]
    if state.get("kind") == "main" and state.get("git") is not False:
        return git_branch, "main checkout"
    return git_branch, workspace_worktree


def _pending_draft(state: dict) -> bool:
    draft = bool(state.get("editable")) and state.get("git") is not False
    return draft and bool(state.get("selected") or state.get("mode") == "always")


def session_worktree_kind(state: dict, workspace_worktree: str = "") -> str:
    """Style key for the top-bar location text: ``worktree``, ``pending``, ``main`` or ``""``."""
    if not isinstance(state, dict):
        return "worktree" if workspace_worktree else ""
    if state.get("kind") == "worktree":
        return "worktree" if (state.get("worktree_name") or workspace_worktree) else ""
    if _pending_draft(state):
        return "pending"
    if state.get("kind") == "main" and state.get("git") is not False:
        return "main"
    return "worktree" if workspace_worktree else ""


async def session_worktree_state(shell, *, force: bool = False) -> dict:
    session = getattr(getattr(shell, "controller", None), "session", "")
    if not session:
        return {}
    cached = getattr(shell, "_worktree_state_cache", None)
    if not force and cached and cached[0] == session and time.monotonic() - cached[1] < _TTL:
        return cached[2]
    try:
        async with asyncio.timeout(_WAIT):
            state = dict((await shell.client.session_worktree_choice(session)).state or {})
    except Exception:  # noqa: BLE001 - the bar is advisory; keep the workspace values
        state = {}
    shell._worktree_state_cache = (session, time.monotonic(), state)
    return state


async def refresh_session_worktree(shell, *, force: bool = False) -> None:
    """Re-read the workspace and session worktree facts into the top bar (``force`` skips the cache)."""
    from .prototype import refresh_branch
    if force:
        shell._worktree_state_cache = None
    await refresh_branch(shell)


def merge_instruction(state: dict, note: str = "") -> str:
    """The user turn ``/merge`` sends (shown verbatim in the transcript); ``note`` is appended."""
    base = str((state or {}).get("base_branch") or "").strip() or "its base branch"
    text = (f"Merge this worktree into {base}: make sure all work is committed and checks pass, "
            'then run `nexus merge -m "<descriptive message>"` in the shell. '
            f"If {base} has diverged, "
            "merge it into this branch first, resolve conflicts, rerun checks, and retry.")
    note = note.strip()
    return text + ("\n\nNote from the user: " + note if note else "")
