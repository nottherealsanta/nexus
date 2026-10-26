"""Top-bar worktree label for the session on screen (agent-managed worktrees).

Contract: ``session_worktree_label(state, git_branch, workspace_worktree)`` maps the session's root
facts (``session_worktree_state``, read from ``ConversationView.root``, never a host call) to the top
bar's ``(branch, worktree)`` texts. A session whose root is a worktree shows ``⎇ <name>`` and the
worktree's branch; a session in the main checkout of a Git workspace (``kind == "main"``) shows
``main checkout`` (plain text, so ASCII mode needs no variant) so the location is always stated;
anything else (unknown state, non-Git workspace, external root) keeps the workspace's own values.
``session_worktree_kind`` gives the matching renderer style key (``worktree`` | ``main`` | ``""``)
so Rust never parses the label. Missing state keys count as empty/False.

The bar follows the view: ``sync_bar_with_root`` compares ``ConversationView.root`` with the root it
last drew and redraws the bar fields when an agent move (``session.root_set``) changed it, so no tab
switch or ``!`` command is needed. The Git branch itself is still read off the hot path by
``refresh_branch`` and remembered on the shell.
"""
from __future__ import annotations

import os


def _glyph() -> str:
    return "wt:" if os.environ.get("NEXUS_ASCII") == "1" else "⎇ "


def session_worktree_label(state: dict, git_branch: str, workspace_worktree: str) -> tuple[str, str]:
    """The top bar's ``(branch, worktree)`` for ``state`` (the session's root facts)."""
    if not isinstance(state, dict):
        return git_branch, workspace_worktree
    if state.get("kind") == "worktree":
        name = str(state.get("worktree_name") or "")
        branch = str(state.get("branch") or git_branch)
        return branch, (_glyph() + name)[:80] if name else workspace_worktree
    if state.get("kind") == "main" and state.get("git") is not False:
        return git_branch, "main checkout"
    return git_branch, workspace_worktree


def session_worktree_kind(state: dict, workspace_worktree: str = "") -> str:
    """Style key for the top-bar location text: ``worktree``, ``main`` or ``""``."""
    if not isinstance(state, dict):
        return "worktree" if workspace_worktree else ""
    if state.get("kind") == "worktree":
        return "worktree" if (state.get("worktree_name") or workspace_worktree) else ""
    if state.get("kind") == "main" and state.get("git") is not False:
        return "main"
    return "worktree" if workspace_worktree else ""


def session_worktree_state(shell, *, root=None) -> dict:
    """The on-screen session's root facts, from ``ConversationView.root`` (``{}`` before a view exists).

    ``git`` stays false until the first branch read finds a Git repository, so a non-Git workspace
    never shows "main checkout".
    """
    if root is None:
        root = getattr(getattr(getattr(shell, "controller", None), "view", None), "root", None)
    if root is None:
        return {}
    name = root.path_display.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if root.kind == "worktree" else ""
    return {"kind": root.kind, "worktree_name": name, "branch": root.branch, "base_branch": root.base_ref,
            "worktree_id": root.worktree_id, "git": bool(getattr(shell, "_git_branch", ""))}


def apply_bar(shell, *, root=None) -> bool:
    """Write the bar fields from the current root and remembered Git facts; whether any changed."""
    state = session_worktree_state(shell, root=root)
    git_branch, git_worktree = getattr(shell, "_git_branch", ""), getattr(shell, "_git_worktree", "")
    label_branch, label_worktree = session_worktree_label(state, git_branch, git_worktree)
    workspace = str(getattr(shell, "workspace", "") or "")
    fields = {"branch": label_branch, "worktree": label_worktree,
              "worktree_kind": session_worktree_kind(state, git_worktree),
              "breadcrumb": workspace + (" › " + label_branch if label_branch else "")}
    if all(getattr(shell, name, None) == value for name, value in fields.items()):
        return False
    for name, value in fields.items():
        setattr(shell, name, value)
    return True


def sync_bar_with_root(shell, view) -> bool:
    """Redraw the bar when ``view.root`` differs from the root last drawn (synchronous, no I/O)."""
    root = getattr(view, "root", None)
    if root is None or root == getattr(shell, "_bar_root", None):
        return False
    shell._bar_root = root
    return apply_bar(shell, root=root)


def root_move_text(move) -> str:
    """The timeline row for one create or move: what happened, from which base, and the stated reason."""
    if move.kind == "worktree":
        base = f" · from {move.base_ref} @ {move.base_sha[:7]}" if move.base_ref else ""
        verb = "Created worktree" if move.action == "created" else "Moved to worktree"
        text = f"{_glyph()}{verb} {move.name or move.branch}{base}"
    elif move.kind == "main":
        text = f"{_glyph()}Moved to the main checkout"
    else:
        text = f"{_glyph()}Moved to an external checkout"
    return text + (f" · reason: \"{move.reason}\"" if move.reason else "")


async def refresh_session_worktree(shell) -> None:
    """Re-read the workspace's Git facts into the top bar."""
    from .prototype import refresh_branch
    await refresh_branch(shell)


def merge_instruction(state: dict, note: str = "") -> str:
    """The user turn ``/merge`` sends (shown verbatim in the transcript); ``note`` is appended."""
    base = str((state or {}).get("base_branch") or "").strip() or "its base branch"
    text = (f"Merge this worktree into {base}: make sure all work is committed and checks pass, "
            "squash the branch into one commit on top of the base, then land it with "
            "`git -C <main checkout> merge --ff-only <worktree branch>` in the shell, "
            "one command per shell call (no `&&` chaining, no `cd` first). "
            f"If {base} has diverged, "
            "merge it into this branch first, resolve conflicts, rerun checks, squash again, and retry. "
            "Landing uses plain Git only: no Nexus CLI merge command exists or is required, "
            "so a `nexus` binary without `merge` is not a blocker.")
    note = note.strip()
    return text + ("\n\nNote from the user: " + note if note else "")
