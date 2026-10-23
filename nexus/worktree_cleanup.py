"""Explicit cleanup for clean, authenticated session worktrees (plan 07).

Removal drops only the linked checkout. The authenticated session record and
branch remain available to the normal session-worktree recreation path.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .agents import session_worktrees as _sessions
from .agents import worktrees as _base


def _parts(controller: Any):
    runtime = getattr(controller, "runtime", controller)
    registry = Path(getattr(controller, "registry", runtime._worktree_root))
    return runtime, registry


def _owner_session(record: _sessions.SessionWorktreeRecord) -> str:
    owner, marker, _child = record.session_id.partition("/sub/")
    return owner if marker and owner else record.session_id


def _ignored_paths(path: Path) -> int:
    # Git's own porcelain recognizes ignored files that a non-forced removal
    # would otherwise risk dropping. _git bounds output and fails closed.
    output = _sessions._git(path, "status", "--porcelain=v1", "-z",
                            "--ignored=matching", "--untracked-files=all")
    return sum(1 for entry in output.split("\0") if entry.startswith("!! "))


def _operation_markers(path: Path) -> list[str]:
    markers = []
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        marker_path = Path(_sessions._git(path, "rev-parse", "--git-path", marker))
        if not marker_path.is_absolute():
            marker_path = path / marker_path
        if marker_path.exists():
            markers.append(marker)
    return markers


def _snapshot(runtime: Any, registry: Path, record_id: str) -> dict[str, Any]:
    """Read a previewable state while the registry lock protects the record."""
    from .util import redact_secrets

    try:
        # Explain active or queued parent work before offering a confirmation.
        # The same check is repeated while holding the actual owner lock at removal.
        record = runtime._worktree_service.get(record_id, root=registry)
        owner = _owner_session(record)
        runtime._session_worktrees._idle(runtime.sessions.open(owner, create=False, recover=False))
    except Exception as exc:
        raise _base.WorktreeError(redact_secrets(str(exc))[:500]) from exc
    service_root, _records = _base._service_layout(registry)
    with _base._locked(service_root):
        _service, _key, record = _sessions._read_record(service_root, record_id)
        if record.parent_workspace.resolve() != Path(runtime.workspace).resolve():
            raise _base.WorktreeError("session worktree belongs to a different project")
        owner = _owner_session(record)
        if record.lifecycle != "active":
            raise _base.WorktreeError("only an active Nexus session worktree can be removed")
        return _snapshot_locked(runtime, service_root, record)


def cleanup_preview(controller: Any, record_id: str) -> dict[str, Any]:
    """Return a safe removal preview; malformed or unauthenticated ids fail closed."""
    runtime, registry = _parts(controller)
    try:
        return _snapshot(runtime, registry, record_id)
    except Exception as exc:  # previews are user-visible and fail closed
        return {
            "state": "refused", "reason": str(exc)[:500], "record_id": record_id,
            "owner_session": "", "path_display": "", "branch": "", "head": "",
            "status": {},
        }


def _remove(runtime: Any, registry: Path, record_id: str, preview: dict[str, Any]) -> dict[str, Any]:
    from .errors import SessionBusy

    owner = str(preview.get("owner_session", ""))
    handle = runtime.sessions.open(owner, create=False, recover=False)
    # Session lock first, then the registry lock. This serializes cleanup against
    # turns and root changes; the record is re-read only after both are held.
    handle._lock.acquire(shared=False, blocking=False)
    try:
        controller = runtime._session_worktrees
        controller._idle(handle)
        if runtime.sessions._has_pending_work(owner):
            raise SessionBusy("Worktree cleanup requires an idle session without pending drafts")
        service_root, _records = _base._service_layout(registry)
        with _base._locked(service_root):
            _service, _key, record = _sessions._read_record(service_root, record_id)
            if record.parent_workspace.resolve() != Path(runtime.workspace).resolve():
                raise _base.WorktreeError("session worktree belongs to a different project")
            if _owner_session(record) != owner:
                raise _base.WorktreeError("session owner changed after preview")
            if record.lifecycle != "active":
                raise _base.WorktreeError("only an active Nexus session worktree can be removed")
            current = _snapshot_locked(runtime, service_root, record)
            if current != preview:
                raise _base.WorktreeError("checkout state changed after preview; request a fresh preview")
            if current["state"] == "already_removed":
                return current
            if current["state"] != "ready":
                raise _base.WorktreeError(current["reason"])
            # No --force: Git must independently confirm the checkout is clean.
            _sessions._git(record.parent_workspace, "worktree", "remove", str(record.path), observational=False)
            if record.path.exists():
                raise _base.WorktreeError("Git reported success but the checkout path remains")
            # Leave the active authenticated record and branch intact. The normal
            # recreate_session path can reattach this session from its branch.
            return {**current, "state": "removed", "reason": "Checkout removed; branch and session record kept."}
    finally:
        handle._lock.release()


def _snapshot_locked(runtime: Any, service_root: Path, record: _sessions.SessionWorktreeRecord) -> dict[str, Any]:
    """Build the preview snapshot using an already-held registry lock."""
    # _verify performs the exact recorded path, common repository, ref and
    # registration checks without acquiring another registry lock.
    _sessions._verify(record, service_root)
    owner = _owner_session(record)
    if not record.path.exists():
        return {"state": "already_removed", "reason": "The registered checkout path is already missing.",
                "record_id": record.child_id, "owner_session": owner, "path_display": str(record.path),
                "branch": record.branch, "head": "", "status": {"missing": True}}
    head = _sessions._git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
    branch = _sessions._git(record.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    status = _sessions.status_counts(record.path)
    status["ignored"] = _ignored_paths(record.path)
    status["git_operation"] = _operation_markers(record.path)
    if branch != record.branch:
        raise _base.WorktreeError("checkout branch does not match its authenticated Nexus record")
    dirty = bool(status["git_operation"]) or any(
        status[key] for key in ("modified", "added", "deleted", "untracked", "conflicted", "clipped", "ignored")
    )
    return {"state": "refused" if dirty else "ready",
            "reason": "The checkout has uncommitted or untracked files; preserve or resolve them first." if dirty else "Clean Nexus-owned checkout; its branch will be kept.",
            "record_id": record.child_id, "owner_session": owner, "path_display": str(record.path),
            "branch": record.branch, "head": head, "status": status}


def remove_worktree(controller: Any, record_id: str, state: dict[str, Any]) -> dict[str, Any]:
    """Remove a clean registered checkout after revalidating the preview state."""
    runtime, registry = _parts(controller)
    if not isinstance(state, dict) or state.get("state") not in {"ready", "already_removed"}:
        return {"state": "refused", "reason": "A ready cleanup preview is required.", "record_id": record_id}
    if state.get("record_id") != record_id:
        return {"state": "refused", "reason": "Cleanup preview does not match the requested record.", "record_id": record_id}
    try:
        return _remove(runtime, registry, record_id, state)
    except Exception as exc:
        return {"state": "refused", "reason": str(exc)[:500], "record_id": record_id}


def modern_worktree_row(record: _sessions.SessionWorktreeRecord, *, root: Path) -> SimpleNamespace:
    """Project a v3 record for modern worktree listings without changing legacy rows."""
    from .agents.session_worktrees import session_status

    status = session_status(record.child_id, root=root)
    uncommitted = status.get("uncommitted", {})
    dirty = bool(not status.get("missing", False) and any(
        uncommitted.get(key, 0) for key in ("modified", "added", "deleted", "untracked", "conflicted", "clipped", "ignored")
    ))
    return SimpleNamespace(
        child_id=record.child_id, kind="session", session_id=record.session_id,
        branch=record.branch, lifecycle=record.lifecycle,
        exists=record.path.is_dir(), dirty=dirty, head=status.get("head", ""),
        status=status,
    )
