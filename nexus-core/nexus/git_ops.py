"""Git worktree reads and index mutations for the desktop control plane.

Callers serialize mutations with the daemon's worktree lock. Every operation
revalidates the target against Git's registered worktrees while holding it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from nexus import files, worktrees


async def registered(project: str | Path, target: str | Path) -> Path:
    """Resolve a registered non-bare checkout belonging to the project's repository."""
    entries = await worktrees._raw_entries(project)
    candidate = Path(target).expanduser().resolve()
    for entry in entries:
        if Path(entry["worktree"]).resolve() == candidate and "bare" not in entry:
            if not candidate.is_dir():
                break
            return candidate
    raise worktrees.WorktreeError("worktree is not a registered checkout of this project")


async def default_worktree(project: str | Path) -> Path:
    """Resolve a project subdirectory to its registered enclosing checkout."""
    directory = Path(project).resolve()
    entries = await worktrees._raw_entries(directory)
    matches = [Path(item["worktree"]).resolve() for item in entries if "bare" not in item
               and directory.is_relative_to(Path(item["worktree"]).resolve())]
    if not matches:
        raise worktrees.WorktreeError("project is not inside a registered checkout")
    return max(matches, key=lambda path: len(path.parts))


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise worktrees.WorktreeError("path must be a non-empty relative Git path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".git") for part in path.parts) or value in (".", ".."):
        raise worktrees.WorktreeError("path must be inside the worktree and outside .git")
    return value


async def status(project: str | Path, target: str | Path) -> dict[str, Any]:
    """Return NUL-safe Git status for a registered worktree."""
    root = await registered(project, target)
    raw = await worktrees._git("status", "--porcelain=v1", "-z", "--branch", "--untracked-files=all", cwd=root)
    records = iter(raw.split("\0"))
    header = next(records, "")
    changes = []
    for record in records:
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        old = next(records, None) if "R" in code or "C" in code else None
        changes.append({"path": path, "status": code, "old_path": old,
                        "staged": code[0] not in (" ", "?"), "unstaged": code[1] != " "})
    stats: dict[str, list[int]] = {}
    for args in (("diff", "--numstat", "-z"), ("diff", "--cached", "--numstat", "-z")):
        entries = iter((await worktrees._git(*args, "--no-ext-diff", "--no-textconv", cwd=root)).split("\0"))
        for entry in entries:
            fields = entry.split("\t", 2)
            if len(fields) != 3:
                continue
            added, removed, path = fields
            if not path:  # A rename's old and new paths follow separately.
                next(entries, None)
                path = next(entries, "")
            total = stats.setdefault(path, [0, 0])
            total[0] += int(added) if added.isdigit() else 0
            total[1] += int(removed) if removed.isdigit() else 0
    for change in changes:
        change["added"], change["removed"] = stats.get(change["path"], (0, 0))
    refs = await worktrees._git("for-each-ref", "--format=%(refname)%00%(upstream:short)%00%(HEAD)",
                               "refs/heads", "refs/remotes", cwd=root)
    branches = []
    for line in refs.splitlines():
        parts = line.split("\0")
        if len(parts) != 3 or parts[0].endswith("/HEAD"):
            continue
        ref, upstream, current = parts
        local = ref.startswith("refs/heads/")
        name = ref.removeprefix("refs/heads/" if local else "refs/remotes/")
        branches.append({"name": name, "local": local, "current": current == "*", "upstream": upstream or None})
    current_branch = next((row for row in branches if row["current"]), None)
    remotes = (await worktrees._git("remote", cwd=root)).splitlines()
    proposal = None
    if current_branch and current_branch["upstream"] is None and len(remotes) == 1:
        proposal = {"remote": remotes[0], "branch": current_branch["name"]}
    return {"worktree": str(root), "branch": header.removeprefix("## "), "changes": changes[:10000],
            "changes_omitted": max(0, len(changes) - 10000), "branches": branches[:10000],
            "branches_omitted": max(0, len(branches) - 10000), "push_upstream": proposal}


async def _ensure_normal(root: Path) -> None:
    git_dir = Path((await worktrees._git("rev-parse", "--absolute-git-dir", cwd=root)).strip())
    if any((git_dir / marker).exists() for marker in ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")):
        raise worktrees.WorktreeError("Git operation in progress; finish or abort it before changing the index")
    if await worktrees._git("diff", "--name-only", "--diff-filter=U", "-z", cwd=root):
        raise worktrees.WorktreeError("unmerged files must be resolved before changing the index")


async def index(project: str | Path, target: str | Path, action: str, paths: list[str]) -> dict[str, Any]:
    """Stage or unstage explicit literal paths. The caller holds the worktree lock."""
    root = await registered(project, target)
    if action not in ("stage", "unstage") or not isinstance(paths, list) or not paths or len(paths) > 1000:
        raise worktrees.WorktreeError("stage or unstage requires 1 to 1000 paths")
    names = [_path(path) for path in paths]
    await _ensure_normal(root)
    # Literal pathspecs prevent names containing wildcard or magic syntax expanding.
    if action == "stage":
        await worktrees._git("--literal-pathspecs", "add", "--", *names, cwd=root)
    else:
        await worktrees._git("--literal-pathspecs", "reset", "-q", "--", *names, cwd=root)
    return await status(project, root)


async def action(project: str | Path, target: str | Path, operation: str, *, value: str = "") -> dict[str, Any]:
    """Perform a fixed Git action after caller-held locking and live-session confirmation.

    This primitive is deliberately not routed: callers must provide durable mutation
    handling and reject stale live-session confirmations before invoking it.
    """
    root = await registered(project, target)
    await _ensure_normal(root)
    if not isinstance(value, str) or "\0" in value:
        raise worktrees.WorktreeError("invalid Git action value")
    if operation == "commit":
        if not value.strip() or len(value.encode()) > 65536:
            raise worktrees.WorktreeError("commit message must contain 1 to 65536 bytes")
        args = ("commit", "-m", value)
    elif operation == "fetch":
        args = ("fetch",)
    elif operation == "pull":
        args = ("pull", "--ff-only")
    elif operation == "push":
        current = await status(project, root)
        proposal = current["push_upstream"]
        if proposal is not None:
            if value != proposal["remote"]:
                raise worktrees.WorktreeError("push requires explicit confirmation of the proposed upstream remote")
            args = ("push", "--set-upstream", proposal["remote"], "HEAD")
        elif value:
            raise worktrees.WorktreeError("upstream proposal is no longer available")
        elif not any(row["current"] and row["upstream"] for row in current["branches"]):
            raise worktrees.WorktreeError("no unambiguous upstream is configured or can be proposed")
        else:
            args = ("push",)
    elif operation in ("switch", "branch_create", "branch_delete", "branch_delete_force"):
        if not value or value.startswith("-") or "\n" in value or "@{" in value:
            raise worktrees.WorktreeError("a branch name is required")
        await worktrees._git("check-ref-format", "--branch", value, cwd=root)
        if operation == "switch":
            args = ("switch", value)
        elif operation == "branch_create":
            args = ("branch", value)
        else:
            args = ("branch", "-D" if operation == "branch_delete_force" else "-d", value)
    else:
        raise worktrees.WorktreeError("unknown Git action")
    output = await worktrees._git(*args, cwd=root, timeout=300.0 if operation in ("fetch", "pull", "push") else 120.0)
    return {"output": output, "status": await status(project, root)}


async def discard(project: str | Path, target: str | Path, paths: list[str]) -> dict[str, Any]:
    """Discard explicit working-tree changes; the caller holds the lock and confirms."""
    root = await registered(project, target)
    if not isinstance(paths, list) or not paths or len(paths) > 1000:
        raise worktrees.WorktreeError("discard requires 1 to 1000 paths")
    names = [_path(path) for path in paths]
    await _ensure_normal(root)
    changed = {item["path"]: item for item in (await status(project, root))["changes"]}
    if any(name not in changed or not changed[name]["unstaged"] for name in names):
        raise worktrees.WorktreeError("discard requires current unstaged or untracked paths")
    tracked = [name for name in names if changed[name]["status"] != "??"]
    untracked = [name for name in names if changed[name]["status"] == "??"]
    opened: list[tuple[int, str]] = []
    try:
        # Validate every untracked path before changing tracked files, and keep
        # parent descriptors open so a parent symlink cannot redirect deletion.
        for name in untracked:
            path = PurePosixPath(name)
            parent = files._open(root, str(path.parent) if str(path.parent) != "." else "", directory=True)
            opened.append((parent, path.name))
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise worktrees.WorktreeError("only regular untracked files can be discarded")
        if tracked:
            await worktrees._git("--literal-pathspecs", "restore", "--worktree", "--", *tracked, cwd=root)
        for parent, leaf in opened:
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise worktrees.WorktreeError("only regular untracked files can be discarded")
            os.unlink(leaf, dir_fd=parent)
    except OSError as exc:
        raise worktrees.WorktreeError(str(exc)) from exc
    finally:
        for parent, _ in opened:
            os.close(parent)
    return await status(project, root)


async def diff(project: str | Path, target: str | Path, path: str) -> dict[str, Any]:
    """Bounded staged and unstaged diffs for one literal worktree path."""
    root = await registered(project, target)
    name = _path(path)
    async def read(*extra: str) -> tuple[str, bool]:
        text = await worktrees._git("--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", *extra,
                                    "--", name, cwd=root)
        raw = text.encode("utf-8")
        return raw[:1024 * 1024].decode("utf-8", "replace"), len(raw) > 1024 * 1024
    unstaged, unstaged_clipped = await read()
    staged, staged_clipped = await read("--cached")
    return {"path": name, "staged": staged, "unstaged": unstaged,
            "staged_clipped": staged_clipped, "unstaged_clipped": unstaged_clipped}
