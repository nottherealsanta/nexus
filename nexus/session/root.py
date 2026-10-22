"""Immutable per-session working roots (worktree-sessions plan, F2).

The event representation uses a bounded path relative to the project parent,
so logs can reconstruct roots without exposing a machine-specific absolute path.
"""
from __future__ import annotations

from dataclasses import dataclass
from os.path import relpath
from pathlib import Path, PurePosixPath
from collections.abc import Mapping

from ..config.paths import project_key as make_project_key

_KINDS = frozenset({"main", "worktree", "external"})
_MAX_PATH_DISPLAY = 1024
_MAX_BRANCH = 200
_MAX_BASE_REF = 200
_MAX_BASE_SHA = 128
_MAX_WORKTREE_ID = 200


def _bounded_text(data: Mapping[str, object], key: str, limit: int) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"invalid session root {key}")
    if "\x00" in value:
        raise ValueError(f"invalid session root {key}")
    return value


@dataclass(frozen=True)
class SessionRoot:
    """A session's canonical filesystem root and bounded Git provenance."""

    kind: str
    path: Path
    project_key: str
    branch: str = ""
    base_ref: str = ""
    base_sha: str = ""
    worktree_id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError("invalid session root kind")
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("session root path must be absolute")
        for key, value, limit in (
            ("project_key", self.project_key, 200),
            ("branch", self.branch, _MAX_BRANCH),
            ("base_ref", self.base_ref, _MAX_BASE_REF),
            ("base_sha", self.base_sha, _MAX_BASE_SHA),
            ("worktree_id", self.worktree_id, _MAX_WORKTREE_ID),
        ):
            if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                raise ValueError(f"invalid session root {key}")
        if self.kind == "worktree" and not self.worktree_id:
            raise ValueError("worktree session root requires worktree_id")

    @classmethod
    def main(cls, project_root: Path, project_key: str | None = None) -> SessionRoot:
        """Return the canonical project root."""
        root = Path(project_root).resolve()
        return cls("main", root, project_key or make_project_key(root))

    @classmethod
    def from_data(cls, project_root: Path, data: object, project_key: str | None = None) -> SessionRoot:
        """Reconstruct a root from its durable, relative event data."""
        if not isinstance(data, Mapping):
            raise ValueError("session root data must be an object")
        kind = data.get("kind")
        if kind not in _KINDS:
            raise ValueError("invalid session root kind")
        root = Path(project_root).resolve()
        display = _bounded_text(data, "path_display", _MAX_PATH_DISPLAY)
        if not display or display.startswith("/") or "\\" in display:
            raise ValueError("session root path_display must be relative")
        rel = PurePosixPath(display)
        if any(part in {"", "."} for part in display.split("/")):
            raise ValueError("invalid session root path_display")
        branch = _bounded_text(data, "branch", _MAX_BRANCH)
        base_ref = _bounded_text(data, "base_ref", _MAX_BASE_REF)
        base_sha = _bounded_text(data, "base_sha", _MAX_BASE_SHA)
        worktree_id = _bounded_text(data, "worktree_id", _MAX_WORKTREE_ID)
        key = project_key or make_project_key(root)
        if kind == "main":
            return cls.main(root, key)
        path = (root.parent / Path(*rel.parts)).resolve()
        return cls(kind, path, key, branch, base_ref, base_sha, worktree_id)

    def to_data(self, project_root: Path) -> dict[str, str]:
        """Return event-safe root data with a relative display path only."""
        project = Path(project_root).resolve()
        display = relpath(self.path, project.parent).replace("\\", "/")
        if len(display) > _MAX_PATH_DISPLAY:
            raise ValueError("session root display path is too long")
        return {
            "kind": self.kind,
            "path_display": display,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "worktree_id": self.worktree_id,
        }
