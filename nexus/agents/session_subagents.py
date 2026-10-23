"""Save isolated child work and return it to the session (simplified plan 05).

Reuse authenticated session worktree records. Owned session roots receive Git
merges; shared roots receive a checked patch without moving HEAD or the index.
Failed children and conflicts retain their saved branch and checkout.
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from uuid import uuid4

from ..util import redact_secrets
from . import session_worktrees as git
from . import worktrees as base


class SessionSubagentService:
    def __init__(self, controller, parent_session, turn_id=""):
        self.controller = controller
        self.parent_session = parent_session
        self.turn_id = turn_id

    def _parent(self):
        self.controller.runtime.validate_session_root(self.parent_session)
        return self.controller.runtime.session_root(self.parent_session)

    def _handle(self):
        return self.controller.handle(self.parent_session)

    def _seq(self):
        return self.controller._turn_data(self._handle(), self.turn_id or self._handle().active_turn_id)[0]

    def _emit(self, kind, data):
        handle = self._handle()
        self.controller._emit(handle, kind, data, self.turn_id or handle.active_turn_id)

    def _commit(self, record, agent="root"):
        policy = self.controller.policy()
        result = git.commit_session(record.child_id, root=self.controller.registry,
            message=f"nexus: changes by {agent}", session_name=self.controller._name(self._handle()),
            turn_seq=self._seq(), agent_id=agent, by="harness" if agent == "root" else "subagent",
            max_files=policy.commit_max_files, max_file_mb=policy.commit_max_file_mb)
        if result.get("kind") == "skipped":
            raise base.WorktreeError(str(result.get("detail", "could not save child changes")))
        if agent == "root":
            self.controller._commit_event(self._handle(), result, seq=self._seq(), agent=agent,
                turn_id=self.turn_id or self._handle().active_turn_id)
        elif result.get("kind") == "committed":
            facade = getattr(self.controller.runtime, "_child_sessions", None)
            if facade is not None:
                manager = facade.manager
                real_id = manager.store.resolve_child_id(record.session_id)
                manager.open(real_id, create=False).emit_session_event("worktree.committed", result)
        return result

    def create(self, parent_checkout, child_session_id, *, root):
        parent = self._parent()
        if Path(root).resolve() != self.controller.registry.resolve() or Path(parent_checkout).resolve() != parent.path:
            raise base.WorktreeError("child worktree must use its session's root and registry")
        if not child_session_id.startswith(self.parent_session + "/sub/"):
            raise base.WorktreeError("child identity does not belong to this session")
        if self.controller.runtime.session_has_active_jobs(self.parent_session):
            raise base.WorktreeError("finish background jobs before creating a child worktree")
        if parent.kind == "worktree":
            record = git.get_session(parent.worktree_id, root=root)
            self._commit(record)
            baseline = git._git(parent.path, "rev-parse", "HEAD")
        else:
            # Include this checkout's current files, including uncommitted edits,
            # in an isolated commit object without staging or moving user refs.
            _, baseline, _, _ = self.controller._checkpoint_snapshot(parent.path,
                "child-base-" + hashlib.sha256(self.parent_session.encode()).hexdigest()[:24],
                self._seq(), "-" + uuid4().hex[:12])
        record = git.create_session(self.controller.runtime.workspace, child_session_id,
            "child-" + child_session_id.rsplit("/", 1)[-1], root=root, base=baseline,
            max_worktrees=self.controller.policy().max_worktrees)
        try:
            return git.activate_session(record.child_id, root=root)
        except BaseException:
            git.abort_session(record.child_id, root=root)
            raise

    def mark_finished(self, child_id, outcome, *, root):
        if Path(root).resolve() != self.controller.registry.resolve() or not child_id.startswith(self.parent_session + "/sub/"):
            raise base.WorktreeError("child worktree does not belong to this session")
        record = git.get_session(git.session_record_id(child_id), root=root)
        saved = self._commit(record, agent=child_id)
        status = outcome.get("status", "failed") if isinstance(outcome, dict) else getattr(outcome, "status", "failed")
        is_error = outcome.get("is_error", False) if isinstance(outcome, dict) else getattr(outcome, "is_error", False)
        if status == "completed" and not is_error:
            result = self._integrate(record)
        else:
            result = {"state": "kept", "detail": f"Not integrated: child {status}; saved branch {record.branch}", "files": []}
        self._emit("context.notice", {"kind": "subagent_" + result["state"],
            "message": redact_secrets(result["detail"])[:2000], "child": child_id,
            "files": [redact_secrets(p) for p in result.get("files", [])[:200]]})
        return {**dataclasses.asdict(record), "owned_session": True, "merge": result,
            "final_status": status, "final_dirty_status": "", "committed": saved.get("sha", "")}

    def _integrate(self, record):
        parent = self._parent()
        tip = git._git(record.path, "rev-parse", "HEAD")
        files = [p for p in git._git(record.path, "diff", "--name-only", "--no-renames", "-z", record.base_commit, tip).split("\0") if p]
        if not files:
            return {"state": "no_changes", "detail": "No file changes", "files": []}
        if parent.kind == "worktree":
            self._commit(git.get_session(parent.worktree_id, root=self.controller.registry))
        # Children in a batch may finish concurrently. Existing registry flock
        # serializes their integrations without introducing a queue or watcher.
        registry, _ = base._service_layout(self.controller.registry)
        with base._locked(registry):
            if self.controller.runtime.session_has_active_jobs(self.parent_session):
                raise base.WorktreeError("finish background jobs before integrating child work")
            if parent.kind == "worktree":
                _, _, owned = git._read_record(registry, parent.worktree_id)
                git._verify(owned, registry)
                if git._git(parent.path, "symbolic-ref", "--quiet", "HEAD", check=False) != "refs/heads/" + owned.branch:
                    raise base.WorktreeError("session branch changed; saved child branch retained")
                if git._git(parent.path, "status", "--porcelain=v1", "--untracked-files=all"):
                    raise base.WorktreeError("session changed before child merge; saved child branch retained")
                if git._git(parent.path, "rev-parse", "--verify", "MERGE_HEAD", check=False):
                    raise base.WorktreeError("finish the session's existing merge first")
                try:
                    git._git(parent.path, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                        "-c", "user.name=Nexus", "-c", "user.email=nexus@localhost",
                        "merge", "--no-ff", "--no-edit", "-m", f"nexus: merge child {record.session_id}\n\nNexus-Session: {self.parent_session}\nNexus-Turn-Seq: {self._seq()}\nNexus-Agent: root",
                        "refs/heads/" + record.branch, observational=False)
                except base.WorktreeError:
                    conflicts = [p for p in git._git(parent.path, "diff", "--name-only", "--diff-filter=U", "-z").split("\0") if p]
                    if git._git(parent.path, "rev-parse", "--verify", "MERGE_HEAD", check=False):
                        git._git(parent.path, "merge", "--abort", observational=False)
                    if not conflicts:
                        raise
                    return {"state": "conflicts", "files": conflicts[:200], "detail": f"Merge conflict; saved child branch {record.branch} retained"}
            else:
                baseline_parents = git._git(record.path, "rev-list", "--parents", "-n", "1", record.base_commit).split()
                expected_head = baseline_parents[1] if len(baseline_parents) > 1 else ""
                current_head = git._git(parent.path, "rev-parse", "--verify", "HEAD^{commit}", check=False)
                if current_head != expected_head:
                    return {"state": "conflicts", "files": files[:200], "detail": f"Parent HEAD moved; saved child branch {record.branch} retained for review"}
                patch = git._git(record.path, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", record.base_commit, tip)
                try:
                    git._git(parent.path, "apply", "--check", "-", input_text=patch + "\n")
                    git._git(parent.path, "apply", "-", input_text=patch + "\n", observational=False)
                except base.WorktreeError as exc:
                    return {"state": "conflicts", "files": files[:200], "detail": f"Could not apply child changes; saved branch {record.branch} retained: {exc}"}
            return {"state": "merged", "files": files[:200], "clipped": max(0, len(files)-200),
                "detail": f"Integrated child changes into your session: {len(files)} files", "commit": tip}
