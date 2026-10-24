"""Cleanup for clean, authenticated project worktrees (plan 07, agent-managed 3.6).

Removal drops only the linked checkout. The project record and branch remain,
so any session can recreate the checkout from its branch. A worktree belongs
to the project: removal checks that none of the sessions currently in it is
busy, not who created it. Two paths exist:

* explicit ``cleanup_preview`` / ``remove_worktree`` (the user's "Remove checkout…");
* ``auto_cleanup``, run by the controller when the last session leaves a worktree
  that is merged and clean. With ``worktrees.auto_remove_clean``, clean unmerged
  checkouts are also removed. Dirty checkouts are never removed automatically.

The controller supplies which sessions are in a worktree through
``controller.occupants(record_id)`` (derived from each session's latest
``session.root_set``); without it a worktree is treated as empty.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from .agents import session_worktrees as _sessions
from .agents import worktrees as _base

_MAX_SESSIONS = 50


def _parts(controller: Any):
    runtime = getattr(controller, "runtime", controller)
    registry = getattr(controller, "registry", None)
    if registry is None:
        registry = runtime._worktree_root
    registry = Path(registry)
    return runtime, registry


def _occupants(runtime: Any, record_id: str) -> list[str]:
    """Sessions whose current root is this worktree (controller-owned index)."""
    index = getattr(getattr(runtime, "_session_worktrees", None), "occupants", None)
    if not callable(index):
        return []
    return sorted({str(item) for item in index(record_id)})[:_MAX_SESSIONS]


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


def _check_idle(runtime: Any, sessions: Iterable[str]) -> None:
    controller = runtime._session_worktrees
    for session in sessions:
        controller._idle(runtime.sessions.open(session, create=False, recover=False))


def _snapshot(runtime: Any, registry: Path, record_id: str) -> dict[str, Any]:
    """Read a previewable state while the registry lock protects the record."""
    from .util import redact_secrets

    try:
        # Explain active or queued work in any session in the checkout before
        # offering a confirmation. Repeated while holding the locks at removal.
        record = runtime._worktree_service.get(record_id, root=registry)
        _check_idle(runtime, _occupants(runtime, record.child_id))
    except Exception as exc:
        raise _base.WorktreeError(redact_secrets(str(exc))[:500]) from exc
    service_root, _records = _base._service_layout(registry)
    with _base._locked(service_root):
        _service, _key, record = _sessions._read_record(service_root, record_id)
        if record.parent_workspace.resolve() != Path(runtime.workspace).resolve():
            raise _base.WorktreeError("session worktree belongs to a different project")
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
            "owner_session": "", "sessions": [], "path_display": "", "branch": "", "head": "",
            "status": {},
        }


def _remove(runtime: Any, registry: Path, record_id: str, preview: dict[str, Any]) -> dict[str, Any]:
    from .errors import SessionBusy

    sessions = [str(item) for item in preview.get("sessions", [])]
    handles = [runtime.sessions.open(session, create=False, recover=False) for session in sessions]
    held = []
    try:
        # Session locks first (sorted, so concurrent removals cannot deadlock),
        # then the registry lock. This serializes cleanup against turns and root
        # changes of every session in the checkout; the record is re-read only
        # after all are held.
        for handle in handles:
            handle._lock.acquire(shared=False, blocking=False)
            held.append(handle)
        controller = runtime._session_worktrees
        for handle in handles:
            controller._idle(handle)
            if runtime.sessions._has_pending_work(handle.id):
                raise SessionBusy("Worktree cleanup requires idle sessions without pending drafts")
        service_root, _records = _base._service_layout(registry)
        with _base._locked(service_root):
            _service, _key, record = _sessions._read_record(service_root, record_id)
            if record.parent_workspace.resolve() != Path(runtime.workspace).resolve():
                raise _base.WorktreeError("session worktree belongs to a different project")
            if record.lifecycle != "active":
                raise _base.WorktreeError("only an active Nexus session worktree can be removed")
            if _occupants(runtime, record.child_id) != sessions:
                raise _base.WorktreeError("sessions in the checkout changed after preview; request a fresh preview")
            current = _snapshot_locked(runtime, service_root, record, sessions=sessions)
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
            # recreate path can reattach any session from its branch.
            return {**current, "state": "removed", "reason": "Checkout removed; branch and worktree record kept."}
    finally:
        for handle in reversed(held):
            handle._lock.release()


def _snapshot_locked(runtime: Any, service_root: Path, record: _sessions.SessionWorktreeRecord,
                     *, sessions: list[str] | None = None) -> dict[str, Any]:
    """Build the preview snapshot using an already-held registry lock."""
    # _verify performs the exact recorded path, common repository, ref and
    # registration checks without acquiring another registry lock.
    _sessions._verify(record, service_root)
    if sessions is None:
        sessions = _occupants(runtime, record.child_id)
    if not record.path.exists():
        return {"state": "already_removed", "reason": "The registered checkout path is already missing.",
                "record_id": record.child_id, "owner_session": "", "sessions": sessions,
                "path_display": str(record.path),
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
            "reason": "The checkout has uncommitted or untracked files; preserve or resolve them first." if dirty else "Clean Nexus checkout; its branch will be kept.",
            "record_id": record.child_id, "owner_session": "", "sessions": sessions,
            "path_display": str(record.path),
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


def auto_cleanup(controller: Any, record_id: str, *, leaving: str = "",
                 remaining: Iterable[str] | None = None) -> dict[str, Any]:
    """Remove a clean worktree once its last session left, subject to policy.

    Called by the controller after a move away (``leaving`` already points at its
    new root; the caller holds that session's lock). ``remaining`` lists the
    other sessions still in the worktree, defaulting to the controller's index
    minus ``leaving``. Only the checkout goes: the record and branch are kept so
    the worktree can be recreated. By default it must also be merged; opting into
    ``worktrees.auto_remove_clean`` skips that requirement. Never raises;
    ``state`` is ``removed`` or ``kept`` with the reason (other sessions,
    unmerged, dirty, ...).
    """
    runtime, registry = _parts(controller)
    try:
        others = sorted(set(remaining) if remaining is not None else set(_occupants(runtime, record_id)))
        others = [item for item in others if item != leaving]
        if others:
            return {"state": "kept", "reason": "Other sessions are still in this worktree.",
                    "record_id": record_id, "sessions": others[:_MAX_SESSIONS]}
        service_root, _records = _base._service_layout(registry)
        with _base._locked(service_root):
            _service, _key, record = _sessions._read_record(service_root, record_id)
            if record.parent_workspace.resolve() != Path(runtime.workspace).resolve():
                return {"state": "kept", "reason": "Worktree belongs to a different project.", "record_id": record_id}
            if record.lifecycle != "active" or record.child_id.startswith("session-"):
                return {"state": "kept", "reason": "Only an active project worktree is cleaned up.", "record_id": record_id}
            current = _snapshot_locked(runtime, service_root, record, sessions=[])
            if current["state"] == "already_removed":
                return {**current, "state": "kept", "reason": "Checkout is already removed."}
            if current["state"] != "ready":
                return {**current, "state": "kept"}
            auto_remove_clean = runtime._session_worktrees.policy().auto_remove_clean
            if not auto_remove_clean and not _sessions.merge_state(record)["merged"]:
                return {**current, "state": "kept", "reason": "Unmerged work; the checkout is kept."}
            _sessions._git(record.parent_workspace, "worktree", "remove", str(record.path), observational=False)
            if record.path.exists():
                return {**current, "state": "kept", "reason": "Git reported success but the checkout path remains."}
            reason = "Clean" if auto_remove_clean else "Merged and clean"
            return {**current, "state": "removed", "reason": f"{reason}; checkout removed, branch and record kept."}
    except Exception as exc:  # cleanup is best effort and must not fail a move
        return {"state": "kept", "reason": str(exc)[:500], "record_id": record_id}


def modern_worktree_row(record: _sessions.SessionWorktreeRecord, *, root: Path) -> SimpleNamespace:
    """Project a record for modern worktree listings without changing legacy rows."""
    from .agents.session_worktrees import session_status

    status = session_status(record.child_id, root=root)
    uncommitted = status.get("uncommitted", {})
    dirty = bool(not status.get("missing", False) and any(
        uncommitted.get(key, 0) for key in ("modified", "added", "deleted", "untracked", "conflicted", "clipped", "ignored")
    ))
    return SimpleNamespace(
        child_id=record.child_id, kind="session", session_id=record.session_id,
        created_by=record.created_by, name=record.name,
        branch=record.branch, lifecycle=record.lifecycle,
        exists=record.path.is_dir(), dirty=dirty, head=status.get("head", ""),
        merged=bool(status.get("merged", False)), unmerged_commits=int(status.get("unmerged_commits", 0)),
        status=status,
    )
