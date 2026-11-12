"""Review a worktree's branch against its base, and integrate it with Git itself.

Control-plane only (never a task tool). The caller holds the daemon's worktree lock.
Every path is checked against Git's registered worktrees (`git_ops.registered`).

Redesign of old Nexus's `worktree_review.py` / `worktree_integrate.py`, which copied a frozen
file review into the parent checkout through a private journal. Here the work is Git's: a
fast-forward or a `--no-ff` merge in the base checkout. The user's review is pinned by
`expected_head`, the branch tip they saw, so a branch that moved after the review is refused.
Nothing is forced, and a failed merge is aborted so the base checkout is left as it was.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import git_ops, worktrees
from .worktrees import WorktreeError

MAX_COMMITS = 50
MAX_FILES = 500
MODES = ("ff", "merge")
_HEX = re.compile(r"[0-9a-f]{40,64}\Z")


async def _entry(project: str | Path, target: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    entries = await worktrees._raw_entries(project)
    main = entries[0]
    found = next((e for e in entries if Path(e["worktree"]).resolve() == target), None)
    if found is None:
        raise WorktreeError("worktree is not a registered checkout of this project")
    return main, found, entries


def _branch_of(entry: dict[str, Any]) -> str | None:
    return (entry.get("branch") or "").removeprefix("refs/heads/") or None


async def _head(root: Path, ref: str) -> str:
    return (await worktrees._git("rev-parse", "--verify", f"refs/heads/{ref}^{{commit}}", cwd=root)).strip()


async def review(project: str | Path, target: str | Path, base: str | None = None) -> dict[str, Any]:
    """What integrating the branch checked out in `target` into `base` would do. Reads only."""
    root = await git_ops.registered(project, target)
    main, entry, entries = await _entry(project, root)
    main_root = Path(main["worktree"]).resolve()
    if root == main_root:
        raise WorktreeError("the main checkout has nothing to integrate into itself")
    branch = _branch_of(entry)
    if branch is None:
        raise WorktreeError("the worktree is on a detached HEAD; check out a branch first")
    if base is None:
        base = _branch_of(main)
        if base is None:
            raise WorktreeError("the main checkout is not on a branch; name a base branch")
    elif not isinstance(base, str):
        raise TypeError("base must be a branch name")
    else:
        await worktrees._check_branch(main_root, base)
    if base == branch:
        raise WorktreeError("the worktree is already on the base branch")
    base_entry = next((e for e in entries if _branch_of(e) == base), None)
    if base_entry is None:
        raise WorktreeError(f"base branch {base!r} is not checked out in any worktree; a merge needs its checkout")
    base_root = Path(base_entry["worktree"]).resolve()
    branch_head, base_head = await _head(root, branch), await _head(root, base)
    ok, merge_base = await worktrees._git_ok("merge-base", base_head, branch_head, cwd=root)
    if not ok or not merge_base:
        raise WorktreeError(f"{branch!r} and {base!r} share no history")
    behind, ahead = (int(n) for n in (await worktrees._git(
        "rev-list", "--left-right", "--count", f"{base_head}...{branch_head}", cwd=root)).split())

    commits = []
    log = await worktrees._git("log", "-z", f"-n{MAX_COMMITS}", "--format=%H%x1f%an%x1f%at%x1f%s",
                               f"{base_head}..{branch_head}", cwd=root)
    for record in filter(None, log.split("\0")):
        sha, author, when, subject = (record.split("\x1f") + ["", "", "", ""])[:4]
        commits.append({"sha": sha, "author": author, "time": int(when or 0), "subject": subject})

    status = await worktrees._git("diff", "--name-status", "-z", "--no-renames", f"{merge_base}..{branch_head}", cwd=root)
    numstat = await worktrees._git("diff", "--numstat", "-z", "--no-renames", f"{merge_base}..{branch_head}", cwd=root)
    kinds: dict[str, str] = {}
    fields = status.split("\0")
    for code, path in zip(fields[0::2], fields[1::2], strict=False):
        kinds[path] = code[:1]
    files = []
    for record in filter(None, numstat.split("\0")):
        added, deleted, path = record.split("\t", 2)
        files.append({"path": path, "status": kinds.get(path, "M"),
                      "added": None if added == "-" else int(added), "deleted": None if deleted == "-" else int(deleted)})
    omitted = max(0, len(files) - MAX_FILES)

    conflicts: list[str] | None = None
    ok, output = await worktrees._git_ok("merge-tree", "--write-tree", "--name-only", "--no-messages",
                                         base_head, branch_head, cwd=root)
    lines = output.splitlines()
    if ok:
        conflicts = []
    elif lines and _HEX.match(lines[0]):
        conflicts = [line for line in lines[1:] if line]
    # otherwise this Git cannot predict a merge (before 2.38): unknown, the merge itself still refuses

    blockers: list[str] = []
    if ahead == 0:
        blockers.append("the branch has no commits the base lacks")
    for label, where in (("worktree", root), ("base checkout", base_root)):
        dirty, changes = await worktrees._status(where)
        if dirty is None:
            blockers.append(f"the {label} status is unavailable")
        elif dirty:
            blockers.append(f"the {label} has {changes} uncommitted change(s)")
    try:
        await git_ops._ensure_normal(base_root)
    except WorktreeError as exc:
        blockers.append(f"base checkout: {exc}")
    if conflicts:
        blockers.append(f"merging would conflict in {len(conflicts)} file(s)")
    return {
        "worktree": str(root), "branch": branch, "head": branch_head,
        "base": base, "base_head": base_head, "base_worktree": str(base_root), "merge_base": merge_base,
        "ahead": ahead, "behind": behind, "fast_forward": behind == 0,
        "commits": commits, "commits_omitted": max(0, ahead - len(commits)),
        "files": files[:MAX_FILES], "files_omitted": omitted,
        "conflicts": conflicts, "blockers": blockers,
    }


async def integrate(project: str | Path, target: str | Path, *, expected_head: str, mode: str,
                    base: str | None = None, message: str | None = None) -> dict[str, Any]:
    """Merge the reviewed branch into its base checkout. The branch and worktree stay; removal is the caller's.

    `expected_head` is the branch tip the user reviewed. `ff` needs a base that has not moved on; `merge` makes a
    `--no-ff` merge commit (`message` or Git's default). A refused or conflicting merge changes nothing.
    """
    if mode not in MODES:
        raise WorktreeError(f"mode must be one of: {', '.join(MODES)}")
    if not isinstance(expected_head, str) or not _HEX.match(expected_head):
        raise WorktreeError("expected_head must be the full commit id of the reviewed branch")
    if message is not None and (not isinstance(message, str) or "\0" in message or len(message.encode()) > 65536):
        raise WorktreeError("message must be text of at most 65536 bytes")
    current = await review(project, target, base)
    if current["head"] != expected_head:
        raise WorktreeError("the branch changed after the review; review it again")
    if current["blockers"]:
        raise WorktreeError("cannot integrate: " + "; ".join(current["blockers"]))
    if mode == "ff" and not current["fast_forward"]:
        raise WorktreeError(f"cannot fast-forward: {current['base']!r} has {current['behind']} commit(s) the branch "
                            "lacks; merge it with a merge commit, or update the branch first")
    base_root = Path(current["base_worktree"])
    branch = current["branch"]
    args = ("merge", "--ff-only", expected_head) if mode == "ff" else (
        "merge", "--no-ff", "-m", message or f"Merge branch '{branch}' into {current['base']}", expected_head)
    try:
        await worktrees._git(*args, cwd=base_root, timeout=300.0)
    except WorktreeError:
        await worktrees._git_ok("merge", "--abort", cwd=base_root)  # a no-op when no merge started
        raise
    head = (await worktrees._git("rev-parse", "HEAD", cwd=base_root)).strip()
    return {"merged": True, "mode": mode, "branch": branch, "base": current["base"], "worktree": current["worktree"],
            "base_worktree": str(base_root), "previous_base_head": current["base_head"], "head": head,
            "branch_head": expected_head}


async def delete_merged_branch(base_root: str | Path, branch: str) -> None:
    """`git branch -d` run in the base checkout, so Git checks the branch is merged into the base. Never forces."""
    await worktrees._check_branch(Path(base_root), branch)
    await worktrees._git("branch", "-d", branch, cwd=base_root)
