"""Shared Rust build folder for every worktree of a repository (plan 36).

Processes Core starts in a workspace (bash, background jobs, `!` commands, integrated
terminals) get `CARGO_BUILD_BUILD_DIR=<cargo home>/build/nexus-<hash>`, where the hash is
of the repository's Git common dir. The main checkout and all its linked worktrees share
one common dir, so they share compiled dependencies; unrelated repositories never do.
Final binaries stay in each worktree's own `target/`.

Nothing is added when the user already chose: `CARGO_BUILD_BUILD_DIR` or `CARGO_TARGET_DIR`
in the daemon's environment, a `build-dir` in any Cargo config file Cargo would read, or
`tools.shared_rust_builds = false` in config.toml. Outside Git nothing is added.
"""
from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

VAR = "CARGO_BUILD_BUILD_DIR"
PREFIX = "nexus-"
_BUILD_DIR = re.compile(r"^\s*build-dir\s*=", re.MULTILINE)


def cargo_home() -> Path:
    return Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo")


@lru_cache(maxsize=256)
def git_common_dir(workspace: str) -> Path | None:
    """The repository's common dir, found from files alone (no subprocess)."""
    for folder in (Path(workspace).resolve(), *Path(workspace).resolve().parents):
        dot = folder / ".git"
        if dot.is_dir():
            return dot.resolve()
        if dot.is_file():
            text = dot.read_text("utf-8", "replace").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = (folder / text.removeprefix("gitdir:").strip()).resolve()
            common = gitdir / "commondir"
            if common.is_file():
                return (gitdir / common.read_text("utf-8", "replace").strip()).resolve()
            return gitdir
    return None


def _cargo_config_sets_build_dir(workspace: Path) -> bool:
    folders = [workspace, *workspace.parents]
    files = [folder / ".cargo" / name for folder in folders for name in ("config.toml", "config")]
    files += [cargo_home() / "config.toml", cargo_home() / "config"]
    for path in files:
        try:
            if _BUILD_DIR.search(path.read_text("utf-8", "replace")):
                return True
        except OSError:
            continue
    return False


def build_dir(workspace: str | Path) -> Path | None:
    common = git_common_dir(str(workspace))
    if common is None:
        return None
    digest = hashlib.sha256(str(common).encode()).hexdigest()[:12]
    return cargo_home() / "build" / f"{PREFIX}{digest}"


def enabled() -> bool:
    from .config import ConfigFile
    try:
        return bool(ConfigFile().read()["values"]["tools"]["shared_rust_builds"])
    except OSError:  # an unreadable config keeps the default
        return True


def extra_env(workspace: str | Path) -> dict[str, str]:
    """Environment to add for a process started in `workspace`; empty when nothing applies."""
    if VAR in os.environ or "CARGO_TARGET_DIR" in os.environ or not enabled():
        return {}
    path = build_dir(workspace)
    if path is None or _cargo_config_sets_build_dir(Path(workspace).resolve()):
        return {}
    return {VAR: str(path)}


def process_env(workspace: str | Path) -> dict[str, str] | None:
    """A full environment for `create_subprocess_exec(env=...)`, or None to inherit unchanged."""
    extra = extra_env(workspace)
    return {**os.environ, **extra} if extra else None


def folders() -> list[Path]:
    """Every shared build folder Nexus has pointed Cargo at (for `nexus doctor`)."""
    root = cargo_home() / "build"
    return sorted(root.glob(f"{PREFIX}*")) if root.is_dir() else []
