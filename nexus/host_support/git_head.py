"""Subprocess-free Git HEAD projection for the top-bar breadcrumb.

Contract: ``git_head(workspace)`` walks up from the workspace (at most 64
levels) to the first ``.git`` entry, which is a directory (a normal checkout)
or a ``gitdir: <path>`` file (a linked worktree), and reads ``HEAD`` with a
bounded read. It returns ``{"root", "branch", "detached", "worktree",
"worktree_name", "main_root"}`` (``main_root`` is the main checkout of a linked
worktree, else ``root``), or ``{}`` when the workspace is not in a repository or
anything fails. It never raises and never runs a subprocess, so the Doctor
report that UIs poll stays cheap. Every string is stripped of control
characters and bounded to 200 characters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: Directory levels searched upward from the workspace.
MAX_LEVELS = 64
#: Bytes read from ``HEAD`` / the ``.git`` pointer file.
MAX_READ = 4096
#: Longest string surfaced.
MAX_FIELD = 200
_SHA_CHARS = 7


def _clean(value: str) -> str:
    return "".join(ch for ch in value if ch.isprintable())[:MAX_FIELD]


def _read(path: Path) -> str:
    with path.open("rb") as handle:
        return handle.read(MAX_READ).decode("utf-8", "replace")


def _find_dot_git(start: Path) -> tuple[Path, Path] | None:
    current = start
    for _ in range(MAX_LEVELS):
        candidate = current / ".git"
        if candidate.exists():
            return current, candidate
        if current.parent == current:
            return None
        current = current.parent
    return None


def _main_root(git_dir: Path) -> Path:
    """The main checkout of a linked worktree: its ``commondir`` (else
    ``<main>/.git``, two levels above ``.git/worktrees/<name>``) without ``.git``."""
    common = git_dir.parent.parent
    pointer = git_dir / "commondir"
    if pointer.is_file():
        target = Path(_read(pointer).strip())
        common = (target if target.is_absolute() else git_dir / target).resolve()
    return common.parent if common.name == ".git" else common


def git_head(workspace: Path | str | None) -> dict[str, Any]:
    """Return branch/worktree facts for ``workspace`` or ``{}``; never raises."""
    try:
        if workspace is None or not str(workspace):
            return {}
        found = _find_dot_git(Path(workspace).expanduser().resolve())
        if found is None:
            return {}
        root, dot_git = found
        worktree = False
        git_dir = dot_git
        if dot_git.is_file():
            text = _read(dot_git).strip()
            if not text.startswith("gitdir:"):
                return {}
            target = Path(text[len("gitdir:"):].strip())
            git_dir = target if target.is_absolute() else (root / target)
            git_dir = git_dir.resolve()
            worktree = git_dir.parent.name == "worktrees"
        head = _read(git_dir / "HEAD").strip()
        if not head:
            return {}
        if head.startswith("ref:"):
            ref = head[4:].strip()
            prefix = "refs/heads/"
            branch = ref[len(prefix):] if ref.startswith(prefix) else ref
            detached = False
        else:
            branch = head[:_SHA_CHARS]
            detached = True
        branch = _clean(branch)
        if not branch:
            return {}
        return {
            "root": _clean(str(root)),
            "branch": branch,
            "detached": detached,
            "worktree": worktree,
            "worktree_name": _clean(git_dir.name) if worktree else "",
            "main_root": _clean(str(_main_root(git_dir))) if worktree else _clean(str(root)),
        }
    except Exception:  # noqa: BLE001 - health must never raise
        return {}
