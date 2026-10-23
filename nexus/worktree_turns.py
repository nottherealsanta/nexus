"""Bounded per-turn Git diffs and isolated revert commits (worktree-sessions/04).

Read refs only from the durable session log. Restores require an idle owned
worktree and a preview bound to its exact HEAD and selected commits.
"""
from __future__ import annotations

from .agents.session_worktrees import _git
from .util import redact_secrets


def _rows(controller, session, seq):
    if type(seq) is not int or seq < 0:
        raise ValueError("Turn sequence must be a nonnegative integer")
    handle = controller.handle(session)
    return handle, [e for e in handle.events if e.data.get("seq") == seq]


def turn_diff(controller, session, seq, *, path="", max_bytes=262144):
    handle, rows = _rows(controller, session, seq)
    root = controller.runtime.session_root(session)
    controller.runtime.validate_session_root(session)
    checkpoint = next((e.data for e in reversed(rows) if e.type == "checkpoint.captured"), None)
    commits = [e.data["sha"] for e in rows if e.type == "worktree.committed" and not e.data.get("revert_of")]
    if checkpoint:
        before, after = checkpoint["base_ref"], checkpoint["commit"]
    elif commits:
        before = _git(root.path, "rev-parse", commits[0] + "^1")
        after = commits[-1]
    else:
        raise ValueError("No saved changes for this turn")
    # Resolve stored refs to object ids before using them as Git arguments.
    before = _git(root.path, "rev-parse", "--verify", before + "^{commit}")
    after = _git(root.path, "rev-parse", "--verify", after + "^{commit}")
    if path and (not isinstance(path, str) or len(path) > 4096 or "\0" in path):
        raise ValueError("Invalid diff path")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 262144:
        raise ValueError("Diff limit must be between 1 and 262144 bytes")
    stats = []
    raw = _git(root.path, "diff", "--no-renames", "--numstat", "-z", before, after, "--")
    for item in raw.split("\0"):
        fields = item.split("\t", 2)
        if len(fields) == 3:
            added, removed, name = fields
            stats.append({"path": redact_secrets(name), "insertions": int(added) if added.isdigit() else 0,
                          "deletions": int(removed) if removed.isdigit() else 0, "binary": added == "-"})
    patch = _git(root.path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", before, after, "--", *([path] if path else []))
    encoded = redact_secrets(patch).encode()
    return {"source": "checkpoint" if checkpoint else "commits", "refs": [before, after],
            "files": stats[:500], "foreign": checkpoint.get("foreign", 0) if checkpoint else 0,
            "head_moved": checkpoint.get("head_moved", False) if checkpoint else False,
            "patch": encoded[:max_bytes].decode(errors="ignore"), "clipped": len(encoded) > max_bytes or len(stats) > 500}


def revert_preview(controller, session, seq, include_later=False):
    handle, rows = _rows(controller, session, seq)
    controller._idle(handle)
    root = controller.runtime.session_root(session)
    if root.kind != "worktree" or root.worktree_id != "session-" + session:
        raise ValueError("Restore is available only for sessions in their own worktree")
    controller.runtime.validate_session_root(session)
    if _git(root.path, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Commit uncommitted changes before restoring a turn")
    if _git(root.path, "rev-parse", "--verify", "MERGE_HEAD", check=False):
        raise ValueError("Finish the merge before restoring a turn")
    selected = [e for e in rows if e.type == "worktree.committed" and "revert_of" not in e.data]
    if not selected:
        raise ValueError("No commits for this turn")
    later = [e for e in handle.events if e.type == "worktree.committed" and e.data.get("seq", -1) > seq]
    own_files = {p for e in selected for p in e.data.get("files", [])}
    if not include_later and any(own_files.intersection(e.data.get("files", [])) or e.data.get("files_clipped") for e in later):
        raise ValueError("Later turns touch these files; select revert this and later turns")
    if include_later:
        selected += later
    head = _git(root.path, "rev-parse", "HEAD")
    return {"head": head, "seq": seq, "commits": [e.data["sha"] for e in reversed(selected)], "include_later": include_later}


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
            max_files=controller.policy().commit_max_files, max_file_mb=controller.policy().commit_max_file_mb, revert_of=state["seq"])
        result["revert_of"] = state["seq"]
        controller._commit_event(handle, result, seq=state["seq"], agent="root", turn_id=None)
        return result
    finally:
        handle._lock.release()
