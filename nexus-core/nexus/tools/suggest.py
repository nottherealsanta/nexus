"""Close matches for a path that does not exist.

Guessed paths were the most common tool error in a week of sessions (2026-11): `nexus-nsh/src/App.tsx` for
`nexus-nsh/App.tsx`, `tests/test_terminal_api.py` for `tests/test_terminals_http.py`. Naming the likely
file in the error turns a search-and-retry into one retry. Suggestions only read the same Git-aware walk
`glob` uses (ignored files are not offered), and a failure here never hides the original error.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

from .base import ToolContext, ToolError
from .walk import walk

MAX_SUGGESTIONS = 3


async def did_you_mean(ctx: ToolContext, target: Path, *, want_dir: bool | None = None) -> str:
    """`"; did you mean a, b?"` for a missing `target`, or `""`. `want_dir` limits matches to directories
    (True) or files (False)."""
    try:
        root = ctx.workspace.resolve()
        if target != root and root in target.parents:
            return _phrase(await _in_workspace(root, target, want_dir))
        return _phrase(_siblings(target, want_dir))
    except (OSError, ValueError, ToolError):  # a suggestion is a courtesy; the caller's error stands on its own
        return ""


async def _in_workspace(root: Path, target: Path, want_dir: bool | None) -> list[str]:
    wanted = target.relative_to(root).as_posix()
    hidden = any(part.startswith(".") for part in wanted.split("/"))
    entries, _ = await walk(root, root, include_hidden=hidden)
    candidates = [rel for rel, is_dir in entries if want_dir is None or is_dir == want_dir]
    name = target.name.lower()
    stem = name.split(".", 1)[0]  # `nexus/loop` is often `nexus/loop.py`
    same = [rel for rel in candidates
            if (base := rel.rsplit("/", 1)[-1].lower()) == name or base.split(".", 1)[0] == stem]
    if not same:
        names = {rel.rsplit("/", 1)[-1].lower() for rel in candidates}
        close = set(difflib.get_close_matches(name, names, n=8, cutoff=0.75))
        same = [rel for rel in candidates if rel.rsplit("/", 1)[-1].lower() in close]
    return sorted(same, key=lambda rel: -difflib.SequenceMatcher(None, wanted, rel).ratio())[:MAX_SUGGESTIONS]


def _siblings(target: Path, want_dir: bool | None) -> list[str]:
    """Outside the workspace only the nearest existing parent is listed: no tree walk."""
    parent = target.parent
    if not parent.is_dir():
        return []
    entries = [entry for entry in os.scandir(parent)
               if want_dir is None or entry.is_dir(follow_symlinks=False) == want_dir]
    close = difflib.get_close_matches(target.name, [entry.name for entry in entries], n=MAX_SUGGESTIONS, cutoff=0.6)
    return [str(parent / name) for name in close]


def _phrase(paths: list[str]) -> str:
    return f"; did you mean {', '.join(paths)}?" if paths else ""
