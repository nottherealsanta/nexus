"""Manual fast-forward integration for owned session worktrees (worktree-sessions/06).

Integration is local and explicit: preview one idle session, bind confirmation to
its current Git state, then fast-forward its original target branch if still safe.
"""
from __future__ import annotations

from pathlib import Path

from .agents import session_worktrees as _service
from .agents.worktrees import WorktreeError
from .agents.session_worktrees import _git, status_counts
from .errors import SessionBusy
from .util import redact_secrets

_MAX_COMMITS = 100
_MAX_FILES = 500


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


def _snapshot(controller, session, *, service_locked: bool = False) -> dict:
    handle = controller.runtime.sessions.open(session, create=False, recover=False)
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
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Commit or discard target checkout changes before integrating")

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
    }


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
