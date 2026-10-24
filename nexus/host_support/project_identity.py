"""Bounded project identity resolution (worktree-sessions/01 §3).

Paths are never display-truncated. Linked worktrees share their owning checkout;
submodules remain independent projects. No subprocess or process environment.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..config.paths import project_key
from .git_head import MAX_LEVELS, _read


@dataclass(frozen=True)
class ProjectIdentity:
    key: str
    root: Path
    kind: Literal["git", "plain"]
    launch_root: Path
    launch_worktree: str | None = None


def resolve_project(path: str | Path) -> ProjectIdentity:
    raw = str(path)
    if not raw or len(raw.encode("utf-8")) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("Project path must be non-empty and at most 4096 bytes without control characters")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Project path must be absolute")
    launch = candidate.resolve()
    if not launch.is_dir():
        raise ValueError(f"Project directory does not exist: {launch}")
    current = launch
    for _ in range(MAX_LEVELS):
        dot = current / ".git"
        if dot.exists():
            gitdir = dot
            if dot.is_file():
                text = _read(dot).strip()
                if not text.startswith("gitdir:"):
                    break
                target = Path(text[7:].strip())
                gitdir = (current / target).resolve()
            if not (gitdir / "HEAD").is_file():
                break
            root = current
            worktree = None
            common = gitdir / "commondir"
            if gitdir.parent.name == "worktrees" and common.is_file():
                common_dir = (gitdir / _read(common).strip()).resolve()
                # A submodule gitdir has no commondir and must stay independent.
                if common_dir.name == ".git" and common_dir.is_dir():
                    root = common_dir.parent
                    worktree = str(current)
            return ProjectIdentity(project_key(root), root, "git", launch, worktree)
        if current.parent == current:
            break
        current = current.parent
    return ProjectIdentity(project_key(launch), launch, "plain", launch)
