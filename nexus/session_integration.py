"""Integration for owned session worktrees (worktree-sessions/06).

Integration is local and explicit. The ``/worktrees`` flow previews one idle
session, binds confirmation to its current Git state, then fast-forwards its
original target branch if still safe. The agent flow (``nexus merge``,
:func:`squash_merge_session`) lands the whole branch as ONE commit built with
``commit-tree`` on top of the target head, fast-forwards the target to it, then
moves the session branch to that commit. Both refuse a diverged target and allow
a dirty target checkout only when no dirty path overlaps a landed file.
"""
from __future__ import annotations

import re
from pathlib import Path

from .agents import session_worktrees as _service
from .agents.worktrees import WorktreeError
from .agents.session_worktrees import _NEXUS_TRAILER, _TRAILER_LINE, _git, status_counts
from .errors import SessionBusy
from .util import redact_secrets

_MAX_COMMITS = 100
_MAX_FILES = 500
_MAX_OVERLAP = 20
_MAX_DIRTY = 50_000


def _operation_in_progress(path: Path) -> bool:
    for marker in (
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
        "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG",
    ):
        marker_path = Path(_git(path, "rev-parse", "--git-path", marker))
        if not marker_path.is_absolute():
            marker_path = path / marker_path
        if marker_path.exists():
            return True
    return False


def _dirty_paths(target: Path) -> set[str]:
    """Every path the target checkout reports as changed or untracked (renames count both ends)."""
    raw = _git(target, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    parts = raw.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(parts) and len(paths) < _MAX_DIRTY:
        entry = parts[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            if index < len(parts) and parts[index]:
                paths.add(parts[index])
            index += 1
    return paths


def _overlap(dirty: set[str], changed: set[str]) -> list[str]:
    """Dirty paths equal to, inside, or containing a landed path (file/directory clashes)."""
    prefixes: set[str] = set()
    for name in changed:
        parts = name.split("/")
        for i in range(1, len(parts)):
            prefixes.add("/".join(parts[:i]))
    hits = []
    for path in sorted(dirty):
        bare = path.rstrip("/")
        parents = bare.split("/")
        if (bare in changed or bare in prefixes
                or any("/".join(parents[:i]) in changed for i in range(1, len(parents)))):
            hits.append(path)
    return hits


def _snapshot(controller, session, *, service_locked: bool = False, in_turn: bool = False) -> dict:
    handle = controller.runtime.sessions.open(session, create=False, recover=False)
    if not in_turn:
        controller._idle(handle)
    root = controller.runtime.session_root(session)
    if root.kind != "worktree" or root.worktree_id != "session-" + session:
        raise ValueError("Integration is available only for a session in its own worktree")
    if not service_locked:
        controller.runtime.validate_session_root(session)
    manager = controller.runtime._session_worktrees
    if service_locked:
        _service_root, _key, record = _service._read_record(
            Path(manager.registry), root.worktree_id
        )
        _service._verify(record, manager.registry, allow_pending=False)
    else:
        record = manager.service.get(root.worktree_id, root=manager.registry)
    if record.lifecycle != "active":
        raise ValueError("Session worktree is not active")

    target = record.parent_workspace
    if not record.base_ref.startswith("refs/heads/") and record.base_ref == "HEAD":
        raise ValueError("Session target must be a named local branch")
    target_branch = record.base_ref.removeprefix("refs/heads/")
    if not target_branch or target_branch == "HEAD":
        raise ValueError("Session target must be a named local branch")
    target_head = _git(target, "rev-parse", "--verify", "HEAD^{commit}")
    current_branch = _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current_branch != target_branch:
        raise ValueError(f"Check out target branch {target_branch!r} in the project before integrating")
    if _operation_in_progress(target):
        raise ValueError("Finish the Git operation in the target checkout before integrating")
    dirty = _dirty_paths(target)

    _service._verify(record, manager.registry, allow_pending=False)
    session_head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
    session_ref = _git(record.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if session_ref != record.branch:
        raise ValueError("Session checkout is not on its owned branch")
    if _operation_in_progress(record.path):
        raise ValueError("Finish the Git operation in the session checkout before integrating")
    counts = status_counts(record.path)
    if any(counts[key] for key in ("modified", "added", "deleted", "untracked", "conflicted")):
        raise ValueError("Commit or discard session changes before integrating")

    base = record.base_commit
    base_merge = _git(target, "merge-base", base, target_head, check=False)
    if base_merge != base:
        raise ValueError("Target branch no longer contains the session's recorded base")
    ff_merge = _git(target, "merge-base", target_head, session_head, check=False)
    if ff_merge != target_head:
        raise ValueError("Target branch has diverged; integrate it into the session branch first")

    commits = []
    for row in _git(record.path, "log", f"-{_MAX_COMMITS + 1}", "--format=%H%x00%<(200,trunc)%s", f"{target_head}..{session_head}").split("\n"):
        if not row:
            continue
        sha, _, subject = row.partition("\0")
        if sha:
            commits.append({"sha": sha, "subject": redact_secrets(subject[:200])})
    if dirty:
        changed = {name for name in _git(target, "diff", "--no-renames", "--name-only", "-z",
                                         target_head, session_head, "--").split("\0") if name}
        clash = _overlap(dirty, changed)
        if clash:
            shown = ", ".join(clash[:_MAX_OVERLAP]) + (f" (+{len(clash) - _MAX_OVERLAP} more)" if len(clash) > _MAX_OVERLAP else "")
            raise ValueError("Uncommitted changes in the target checkout overlap files this merge would "
                             f"change; commit, stash or discard them first: {shown}")
    raw = _git(target, "diff", "--no-renames", "--numstat", "-z", target_head, session_head, "--")
    files = []
    for item in raw.split("\0"):
        columns = item.split("\t", 2)
        if len(columns) == 3:
            added, deleted, name = columns
            files.append({"path": redact_secrets(name[:4096]), "insertions": int(added) if added.isdigit() else 0,
                          "deletions": int(deleted) if deleted.isdigit() else 0})
    return {
        "ready": True, "reason": "", "session_branch": record.branch,
        "target_branch": target_branch, "target_workspace": str(target),
        "session_head": session_head, "target_head": target_head,
        "commits": commits[:_MAX_COMMITS], "files": files[:_MAX_FILES],
        "clipped": len(commits) > _MAX_COMMITS or len(files) > _MAX_FILES,
        "total_files": len(files), "total_commits": len(commits),
    }


def session_for_checkout(controller, checkout: str | Path) -> str:
    """Return the session id whose active owned worktree is ``checkout`` (or contains it)."""
    manager = controller.runtime._session_worktrees
    here = Path(checkout).resolve()
    for record in _service.list_sessions(root=manager.registry):
        if record.lifecycle != "active":
            continue
        base = Path(record.path).resolve()
        if here == base or base in here.parents:
            return record.session_id
    raise ValueError("This directory is not a Nexus session worktree; pass --session ID")


def integration_preview(controller, session) -> dict:
    """Return a bounded review of one session and its original target branch."""
    try:
        return _snapshot(controller, session)
    except (ValueError, OSError, WorktreeError, SessionBusy) as exc:
        return {"ready": False, "reason": redact_secrets(str(exc))[:500]}


def integrate_session(controller, session, state: dict) -> dict:
    """Recheck the reviewed state and fast-forward only the target branch."""
    handle = controller.runtime.sessions.open(session, create=False, recover=False)
    try:
        handle._lock.acquire(shared=False, blocking=False)
    except (SessionBusy, RuntimeError) as exc:
        del exc
        raise ValueError("Session is busy; try integration again when it is idle")
    try:
        current = _snapshot(controller, session)
        if current != state:
            raise ValueError("Git state changed since integration preview")
        manager = controller.runtime._session_worktrees
        with _service._base._locked(manager.registry):
            fresh = _snapshot(controller, session, service_locked=True)
            if fresh != state:
                raise ValueError("Git state changed since integration preview")
            target = Path(state["target_workspace"])
            _git(target, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                 "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore",
                 state["session_head"], observational=False)
            after = _git(target, "rev-parse", "--verify", "HEAD^{commit}")
            return {"kind": "integrated", "target_branch": state["target_branch"],
                    "before": state["target_head"], "after": after}
    finally:
        handle._lock.release()


def _trailer_value(text: str, limit: int) -> str:
    return re.sub(r"[\r\n\x00-\x1f\x7f]+", " ", text)[:limit]


def squash_merge_session(controller, session, message: str, *, session_name: str = "",
                         agent_id: str = "root", turn_seq: int = 0, in_turn: bool = True) -> dict:
    """Land the session branch on its target as one commit, then sync the session branch.

    ``in_turn`` skips the idle and session-lock checks because the caller (the
    agent's own shell command, or the user's terminal through the host) is already
    the one acting for the session; every other check, including the registry lock
    and record re-verification, still runs.

    Why moving the session branch is safe: the landed commit C has the tree of the
    session head (``commit-tree <head>^{tree}``) and the session worktree is clean,
    so its index and files already equal ``tree(C)``. Pointing the branch at C
    (compare-and-swap on the old head) is therefore a soft reset: no file changes.
    """
    message = (message or "").strip()
    if not message or len(message) > 4000 or "\0" in message:
        raise ValueError("A merge message of 1 to 4,000 characters is required")
    manager = controller.runtime._session_worktrees
    with _service._base._locked(manager.registry):
        state = _snapshot(controller, session, service_locked=True, in_turn=in_turn)
        _service_root, _key, record = _service._read_record(Path(manager.registry), "session-" + session)
        target = Path(state["target_workspace"])
        head, target_head = state["session_head"], state["target_head"]
        if head == target_head or _git(record.path, "rev-parse", f"{head}^{{tree}}") == _git(
                target, "rev-parse", f"{target_head}^{{tree}}"):
            raise ValueError(f"Nothing to merge: the session branch has no changes beyond {state['target_branch']}")
        message = _TRAILER_LINE.sub("", message).strip() if _NEXUS_TRAILER.search(message) else message
        trailers = [f"Nexus-Session: {_trailer_value(session_name or session, 200)} ({session})",
                    f"Nexus-Turn-Seq: {int(turn_seq)}", f"Nexus-Agent: {_trailer_value(agent_id, 128)}",
                    f"Nexus-Squashed-Commits: {state['total_commits']}"]
        config = []
        if not _git(record.path, "config", "--get", "user.name", check=False):
            config += ["-c", "user.name=Nexus"]
        if not _git(record.path, "config", "--get", "user.email", check=False):
            config += ["-c", "user.email=nexus@localhost"]
        config += ["-c", "commit.gpgsign=false"]
        landed = _git(record.path, *config, "commit-tree", f"{head}^{{tree}}", "-p", target_head, "-F", "-",
                      input_text=message + "\n\n" + "\n".join(trailers) + "\n", observational=False)
        _git(target, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", landed, observational=False)
        after = _git(target, "rev-parse", "--verify", "HEAD^{commit}")
        moved, warning = True, ""
        try:
            _git(target, "update-ref", "refs/heads/" + state["session_branch"], landed, head, observational=False)
        except WorktreeError as exc:
            moved, warning = False, "Landed, but the session branch was not moved: " + redact_secrets(str(exc))[:300]
        return {"kind": "merged", "target_branch": state["target_branch"],
                "session_branch": state["session_branch"], "before": target_head, "after": after,
                "commit": landed, "subject": redact_secrets(message.splitlines()[0])[:200],
                "commits": state["commits"], "files": state["files"],
                "total_commits": state["total_commits"], "total_files": state["total_files"],
                "clipped": state["clipped"], "session_branch_moved": moved, "warning": warning}
