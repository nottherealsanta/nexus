"""Bounded per-turn Git diffs and isolated revert commits (worktree-sessions/04).

Only worktrees save turns (agent-managed-worktrees 3.5.1): a turn in the main
checkout records nothing. Diffs read the session's ``worktree.committed`` events
and the shared object database, so they keep working after a squash and merge
(hidden ``refs/nexus/turns`` refs keep the commits alive) and after the checkout
is removed. Restores need an idle worktree and a preview bound to its exact HEAD.
"""
from __future__ import annotations

import re
from pathlib import Path

from .agents.session_worktrees import _git
from .util import redact_secrets

NO_MAIN_TURNS = "No saved turns in the main checkout; work in a worktree to get them."
SPANS_CHECKOUTS = "This turn moved between checkouts; view each part separately."
SQUASHED = "Already squashed and merged; this turn can be viewed but not reverted."


def _rows(controller, session, seq):
    if type(seq) is not int or seq < 0:
        raise ValueError("Turn sequence must be a nonnegative integer")
    handle = controller.handle(session)
    return handle, [e for e in handle.events if e.data.get("seq") == seq]


def _repo(controller, session) -> Path:
    # Commit objects live in the common object database, so the project checkout
    # can read them even when the worktree itself was removed.
    workspace = getattr(controller.runtime, "workspace", None)
    return Path(workspace) if workspace is not None else controller.runtime.session_root(session).path


def _turn_commits(rows):
    return [e for e in rows if e.type == "worktree.committed" and not e.data.get("revert_of")]


def _one_worktree(commits) -> None:
    if len({e.data.get("worktree_id") for e in commits if e.data.get("worktree_id")}) > 1:
        raise ValueError(SPANS_CHECKOUTS)


def turn_diff(controller, session, seq, *, path="", max_bytes=262144):
    handle, rows = _rows(controller, session, seq)
    repo = _repo(controller, session)
    commits = _turn_commits(rows)
    if not commits:
        if controller.runtime.session_root(session).kind == "main":
            raise ValueError(NO_MAIN_TURNS)
        raise ValueError("No saved changes for this turn")
    _one_worktree(commits)
    before = _git(repo, "rev-parse", commits[0].data["sha"] + "^1")
    after = commits[-1].data["sha"]
    # Resolve stored refs to object ids before using them as Git arguments.
    before = _git(repo, "rev-parse", "--verify", before + "^{commit}")
    after = _git(repo, "rev-parse", "--verify", after + "^{commit}")
    if path and (not isinstance(path, str) or len(path) > 4096 or "\0" in path):
        raise ValueError("Invalid diff path")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 262144:
        raise ValueError("Diff limit must be between 1 and 262144 bytes")
    stats = []
    raw = _git(repo, "diff", "--no-renames", "--numstat", "-z", before, after, "--")
    for item in raw.split("\0"):
        fields = item.split("\t", 2)
        if len(fields) == 3:
            added, removed, name = fields
            stats.append({"path": redact_secrets(name), "insertions": int(added) if added.isdigit() else 0,
                          "deletions": int(removed) if removed.isdigit() else 0, "binary": added == "-"})
    patch = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", before, after, "--", *([path] if path else []))
    encoded = redact_secrets(patch).encode()
    return {"source": "commits", "refs": [before, after], "files": stats[:500], "foreign": 0, "head_moved": False,
            "patch": encoded[:max_bytes].decode(errors="ignore"), "clipped": len(encoded) > max_bytes or len(stats) > 500}


def _other_session_overlap(root_path: Path, session: str, selected, own_files: set[str]) -> str:
    """Name a session whose later commit on this branch touches the same paths, or ''."""
    oldest = selected[0]
    log = _git(root_path, "log", "-200", "--format=%H%x00%(trailers:key=Nexus-Session,valueonly)%x00",
               f"{oldest}..HEAD")
    fields = log.split("\x00")
    for index in range(0, len(fields) - 1, 2):
        sha = fields[index].strip()
        match = re.search(r"\(([^()]+)\)\s*$", fields[index + 1].strip())
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) or not match or match.group(1) == session:
            continue
        touched = {name for name in _git(root_path, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                                         "--root", sha).split("\0") if name}
        if touched & own_files:
            return match.group(1)
    return ""


def revert_preview(controller, session, seq, include_later=False):
    handle, rows = _rows(controller, session, seq)
    controller._idle(handle)
    root = controller.runtime.session_root(session)
    if root.kind == "main":
        raise ValueError(NO_MAIN_TURNS)
    if root.kind != "worktree":
        raise ValueError("Restore is available only in a Nexus worktree")
    controller.runtime.validate_session_root(session)
    if _git(root.path, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Commit uncommitted changes before restoring a turn")
    if _git(root.path, "rev-parse", "--verify", "MERGE_HEAD", check=False):
        raise ValueError("Finish the merge before restoring a turn")
    selected = _turn_commits(rows)
    if not selected:
        raise ValueError("No commits for this turn")
    _one_worktree(selected)
    if any(e.data.get("worktree_id") not in (None, "", root.worktree_id) for e in selected):
        raise ValueError("This turn was saved in a different worktree; move there to restore it")
    # A squash rewrites the branch, so its turn commits are no longer reachable
    # from it: they stay viewable through the hidden refs but cannot be reverted.
    for event in selected:
        if not _is_ancestor(root.path, event.data["sha"]):
            raise ValueError(SQUASHED)
    own_files = {name for e in selected for name in e.data.get("files", [])}
    # A later commit from another session in this shared worktree that touches
    # the same paths cannot be undone without undoing that session's work.
    other = _other_session_overlap(root.path, session, [e.data["sha"] for e in selected], own_files)
    if other:
        raise ValueError(f"Session {other} committed later changes to these files in this worktree; "
                         "ask it to revert, or revert by hand")
    later = [e for e in handle.events if e.type == "worktree.committed" and e.data.get("seq", -1) > seq
             and e.data.get("worktree_id") in (None, "", root.worktree_id)]
    if not include_later and any(own_files.intersection(e.data.get("files", [])) or e.data.get("files_clipped") for e in later):
        raise ValueError("Later turns touch these files; select revert this and later turns")
    if include_later:
        selected += later
    head = _git(root.path, "rev-parse", "HEAD")
    return {"head": head, "seq": seq, "commits": [e.data["sha"] for e in reversed(selected)], "include_later": include_later}


def _is_ancestor(path: Path, sha: str) -> bool:
    import subprocess
    from .agents import worktrees as _base

    probe = subprocess.run(_base._git_argv("merge-base", "--is-ancestor", sha, "HEAD"), cwd=path,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=_base._git_environment(), timeout=60)
    return probe.returncode == 0


def revert_turn(controller, session, state):
    handle = controller.handle(session)
    handle._lock.acquire(shared=False, blocking=False)
    try:
        current = revert_preview(controller, session, state["seq"], state["include_later"])
        if current != state:
            raise ValueError("Worktree changed since restore preview")
        root = controller.runtime.session_root(session)
        # Resolve and verify first-parent commits; merge commits need an explicit
        # mainline policy and are refused before touching any files.
        for sha in state["commits"]:
            parents = _git(root.path, "rev-list", "--parents", "-n", "1", sha).split()
            if len(parents) != 2:
                raise ValueError("Restore requires ordinary turn commits; this turn includes a merge")
        try:
            _git(root.path, "revert", "--no-commit", *state["commits"], observational=False)
        except Exception:
            _git(root.path, "revert", "--abort", check=False, observational=False)
            raise
        _git(root.path, "revert", "--quit", observational=False)
        result = controller.service.commit_session(root.worktree_id, root=controller.registry,
            message=f"revert: turn {state['seq']}\n\nNexus-Revert-Of-Turn: {state['seq']}",
            session_name=controller._name(handle), turn_seq=state["seq"], agent_id="root", by="harness",
            max_files=controller.policy().commit_max_files, max_file_mb=controller.policy().commit_max_file_mb,
            revert_of=state["seq"], session_id=session)
        result["revert_of"] = state["seq"]
        controller._commit_event(handle, result, seq=state["seq"], agent="root", turn_id=None)
        return result
    finally:
        handle._lock.release()
