"""Git worktrees through subprocesses, with argument arrays (never a shell).

The daemon calls these functions while holding one worktree lock, so a create
and a delete cannot interleave. Every path is resolved and checked against Git's
own `worktree list --porcelain` metadata: a requested path is never trusted to
be a worktree, and removal never deletes anything Git does not list.

`git worktree remove` is called without `--force`, so Git itself also refuses a
dirty or locked worktree. Nexus adds earlier checks to report the reason
clearly and to protect paths used by sessions, but the final say is Git's.
Branches are never deleted.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GIT_TIMEOUT = 120.0
ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
for _inherited in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_NAMESPACE"):
    ENV.pop(_inherited, None)  # a daemon's environment must not redirect every command to another repository


class WorktreeError(ValueError):
    """A Git or path validation failure, reported as a tool error or HTTP 400."""


async def _git(*args: str, cwd: str | Path) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd), env=ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        raise WorktreeError(f"git is not available: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.communicate()
        if isinstance(exc, TimeoutError):
            raise WorktreeError(f"git {args[0]} timed out after {GIT_TIMEOUT:g} s") from exc
        raise
    if proc.returncode != 0:
        message = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip() or "no output"
        raise WorktreeError(f"git {args[0]} failed: {message}")
    return out.decode("utf-8", "replace")


async def _git_ok(*args: str, cwd: str | Path) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd), env=ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as exc:
        raise WorktreeError(f"git is not available: {exc}") from exc
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.communicate()
        if isinstance(exc, TimeoutError):
            raise WorktreeError(f"git {args[0]} timed out after {GIT_TIMEOUT:g} s") from exc
        raise
    return proc.returncode == 0, out.decode("utf-8", "replace").strip()


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    head: str | None = None
    branch: str | None = None
    bare: bool = False
    detached: bool = False
    locked: str | None = None
    prunable: str | None = None
    main: bool = False
    dirty: bool | None = None
    changes: int = 0

    def json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "head": self.head,
            "branch": self.branch,
            "bare": self.bare,
            "detached": self.detached,
            "locked": self.locked,
            "prunable": self.prunable,
            "main": self.main,
            "exists": self.path.exists(),
            "dirty": self.dirty,
            "changes": self.changes,
        }


def _parse_porcelain(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in text.split("\0") + [""]:
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return entries


async def _raw_entries(directory: str | Path) -> list[dict[str, Any]]:
    try:
        out = await _git("worktree", "list", "--porcelain", "-z", cwd=directory)
    except WorktreeError as exc:
        raise WorktreeError(f"{Path(directory)} is not inside a Git repository ({exc})") from exc
    entries = _parse_porcelain(out)
    if not entries:
        raise WorktreeError(f"{Path(directory)} is not inside a Git repository")
    return entries


async def _status(path: Path) -> tuple[bool | None, int]:
    if not path.is_dir():
        return None, 0
    try:
        out = await _git("-C", str(path), "status", "--porcelain", "--untracked-files=normal", cwd=path)
    except WorktreeError:
        return None, 0
    changes = [line for line in out.splitlines() if line.strip()]
    return bool(changes), len(changes)


def main_root(entries: list[dict[str, Any]]) -> Path:
    """The main checkout: Git lists it first, whatever worktree the query ran in."""
    path = entries[0].get("worktree")
    if not path:
        raise WorktreeError("Git did not report a main worktree")
    return Path(path)


def resolve_target(repo_main: Path, path: str) -> Path:
    """Absolute target path; relative paths resolve against the main checkout."""
    if not isinstance(path, str) or not path.strip():
        raise WorktreeError("path must be a non-empty string")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = repo_main / candidate
    return candidate.resolve()


async def _worktrees(directory: str | Path) -> tuple[Path, list[Worktree]]:
    entries = await _raw_entries(directory)
    main = main_root(entries)
    worktrees: list[Worktree] = []
    for index, item in enumerate(entries):
        path = Path(item["worktree"])
        branch = (item.get("branch") or "").removeprefix("refs/heads/") or None
        dirty, changes = (None, 0) if item.get("bare") else await _status(path)
        worktrees.append(Worktree(
            path=path,
            head=item.get("HEAD"),
            branch=branch,
            bare="bare" in item,
            detached="detached" in item,
            locked=(item.get("locked") or "locked") if "locked" in item else None,
            prunable=(item.get("prunable") or "prunable") if "prunable" in item else None,
            main=index == 0,
            dirty=dirty,
            changes=changes,
        ))
    return main, worktrees


async def listing(directory: str | Path) -> dict[str, Any]:
    """Every registered worktree with branch, head and working-tree state."""
    main, worktrees = await _worktrees(directory)
    return {
        "repository": str(main),
        "main": str(main),
        "worktrees": [item.json() for item in worktrees],
    }


async def git_state(directory: str | Path) -> dict[str, Any] | None:
    """Repository, worktree and branch for an announcement; None outside Git."""
    try:
        entries = await _raw_entries(directory)
        main = main_root(entries)
        top = Path((await _git("rev-parse", "--show-toplevel", cwd=directory)).removesuffix("\n"))
    except (WorktreeError, OSError):
        return None
    try:
        branch = (await _git("rev-parse", "--abbrev-ref", "HEAD", cwd=directory)).strip()
    except WorktreeError:
        branch = ""  # a repository with no commit yet still has a path
    return {
        "repository": str(main),
        "main": str(main),
        "worktree": str(top),
        "branch": None if branch in ("", "HEAD") else branch,
    }


async def _check_branch(repo_main: Path, branch: str) -> None:
    if branch.startswith("-"):
        raise WorktreeError(f"invalid branch name {branch!r}")
    ok, _ = await _git_ok("check-ref-format", "--branch", branch, cwd=repo_main)
    if not ok:
        raise WorktreeError(f"invalid branch name {branch!r}")


async def _branch_exists(repo_main: Path, branch: str) -> bool:
    ok, _ = await _git_ok("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=repo_main)
    return ok


async def _check_base(repo_main: Path, base: str) -> None:
    if base.startswith("-"):
        raise WorktreeError(f"invalid base {base!r}")
    ok, _ = await _git_ok("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", cwd=repo_main)
    if not ok:
        raise WorktreeError(f"base {base!r} is not a commit or branch in {repo_main}")


async def create(directory: str | Path, path: str, branch: str | None = None,
                 base: str | None = None) -> dict[str, Any]:
    """Create or reuse a worktree; the session directory is not involved."""
    main, worktrees = await _worktrees(directory)
    target = resolve_target(main, path)
    if branch is not None and (not isinstance(branch, str) or not branch.strip()):
        raise WorktreeError("branch must be a non-empty string when given")
    if base is not None and (not isinstance(base, str) or not base.strip()):
        raise WorktreeError("base must be a non-empty string when given")
    if base and not branch:
        raise WorktreeError("base requires branch: it selects where a new branch starts")

    existing = next((item for item in worktrees if item.path.resolve() == target), None)
    if existing is not None:
        if branch and existing.branch != branch:
            raise WorktreeError(
                f"{target} is already a worktree on branch {existing.branch or '(detached)'}, not {branch!r}")
        if existing.prunable:
            raise WorktreeError(f"{target} is registered but missing or prunable; repair or prune it first")
        return {
            "path": str(existing.path),
            "branch": existing.branch,
            "head": existing.head,
            "repository": str(main),
            "main": str(main),
            "session_directory": None,
            "created": False,
            "reused": True,
        }

    if target.exists() or target.is_symlink():
        raise WorktreeError(
            f"{target} already exists and is not a registered worktree of {main}; refusing to use or replace it")
    if not target.parent.is_dir():
        raise WorktreeError(f"the parent directory of {target} does not exist; create it first")

    if branch:
        await _check_branch(main, branch)
        if base:
            await _check_base(main, base)
        if await _branch_exists(main, branch):
            if base:
                raise WorktreeError(f"branch {branch!r} already exists; base applies only to a new branch")
            args = ("worktree", "add", str(target), branch)
        else:
            args = ("worktree", "add", "-b", branch, str(target), *((base,) if base else ()))
    else:
        args = ("worktree", "add", str(target))
    await _git(*args, cwd=main)

    _, after = await _worktrees(main)
    created = next((item for item in after if item.path.resolve() == target), None)
    if created is None:
        raise WorktreeError(f"git reported success but {target} is not a registered worktree of {main}")
    return {
        "path": str(created.path),
        "branch": created.branch,
        "head": created.head,
        "repository": str(main),
        "main": str(main),
        "session_directory": None,
        "created": True,
        "reused": False,
    }


async def remove(directory: str | Path, path: str) -> dict[str, Any]:
    """Remove a registered, clean, non-main worktree. Never deletes a branch."""
    main, worktrees = await _worktrees(directory)
    target = resolve_target(main, path)
    existing = next((item for item in worktrees if item.path.resolve() == target), None)
    if existing is None:
        raise WorktreeError(f"{target} is not a registered worktree of {main}")
    if existing.main:
        raise WorktreeError(f"refusing to delete the main checkout {target}")
    if existing.bare:
        raise WorktreeError(f"refusing to delete bare worktree {target}")
    if existing.locked:
        raise WorktreeError(f"refusing to delete locked worktree {target} ({existing.locked})")
    if existing.prunable:
        raise WorktreeError(f"{target} is already missing or prunable; run `git worktree prune` instead")
    dirty, changes = await _status(existing.path)
    if dirty is None:
        raise WorktreeError(f"refusing to delete worktree {target}: working-tree status is unavailable")
    if dirty:
        raise WorktreeError(
            f"refusing to delete dirty worktree {target} ({changes} uncommitted change(s)); commit, stash or "
            "remove them first")
    await _git("worktree", "remove", str(target), cwd=main)
    return {
        "path": str(existing.path),
        "branch": existing.branch,
        "head": existing.head,
        "repository": str(main),
        "main": str(main),
        "session_directory": None,
    }
