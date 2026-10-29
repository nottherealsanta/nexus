"""Read-only lookup in the standard Hugging Face hub cache (PLAN.md P5).

No huggingface_hub import, so a status check stays cheap. Layout:
models--<org>--<name>/refs/<revision> holds a commit hash, snapshots/<hash>/<file>
links into blobs/, and blobs/*.incomplete marks a download in progress.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_HASH = re.compile(r"^[0-9a-f]{40}$")


def cache_dir() -> Path:
    env = os.environ
    if env.get("HF_HUB_CACHE"):
        return Path(env["HF_HUB_CACHE"]).expanduser()
    if env.get("HF_HOME"):
        return Path(env["HF_HOME"]).expanduser() / "hub"
    if env.get("XDG_CACHE_HOME"):
        return Path(env["XDG_CACHE_HOME"]).expanduser() / "huggingface" / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def repo_dir(repo: str) -> Path:
    return cache_dir() / ("models--" + repo.replace("/", "--"))


def incomplete(repo: str) -> bool:
    blobs = repo_dir(repo) / "blobs"
    return blobs.is_dir() and any(blobs.glob("*.incomplete"))


def snapshot(repo: str, revision: str = "main") -> Path | None:
    """The snapshot folder for a revision (branch name or commit hash), if present."""
    root = repo_dir(repo)
    if _HASH.match(revision):
        commit = revision
    else:
        ref = root / "refs" / revision
        try:
            commit = ref.read_text().strip()
        except OSError:
            return None
    path = root / "snapshots" / commit
    return path if path.is_dir() else None


def find(repo: str, revision: str, files: list[str]) -> Path | None:
    """The snapshot folder when every file is present and no download is running."""
    if incomplete(repo):
        return None
    snap = snapshot(repo, revision)
    if snap is None or not all((snap / f).exists() for f in files):
        return None
    return snap


def size(repo: str) -> int:
    blobs = repo_dir(repo) / "blobs"
    if not blobs.is_dir():
        return 0
    return sum(p.stat().st_size for p in blobs.iterdir() if p.is_file())


@dataclass(frozen=True)
class Cached:
    repo: str
    size: int
    incomplete: bool


def scan() -> list[Cached]:
    root = cache_dir()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.glob("models--*")):
        repo = d.name[len("models--"):].replace("--", "/", 1)
        out.append(Cached(repo, size(repo), incomplete(repo)))
    return out
