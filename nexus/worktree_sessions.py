"""Runtime-owned session worktrees and turn boundaries (worktree-sessions/03).

The controller keeps project policy separate from checkout authority. Git writes
are delegated to the authenticated service; main/external checkouts are read only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import shutil
import uuid
import time
from pathlib import Path

from .config.paths import project_state_dir
from .errors import OperationCancelled, SessionBusy
from .events import Event
from .model.message import Text
from .resources import process_slot, resource_environment
from .session.ids import is_valid_session_id
from .session.root import SessionRoot
from .util import redact_secrets


class SessionWorktrees:
    def __init__(self, runtime):
        self.runtime = runtime
        self._promotions: dict[str, dict] = {}
        self._trust_once: set[tuple[str, str]] = set()
        self._skip_once: set[tuple[str, str]] = set()

    @property
    def service(self):
        return self.runtime._worktree_service

    @property
    def registry(self):
        return self.runtime._worktree_root

    def policy(self):
        from .config.schema import WorktreesSection
        return getattr(self.runtime._load_config().v2, "worktrees", None) or WorktreesSection()

    def handle(self, session):
        return self.runtime.sessions.open(session, create=False, recover=False)

    def mode(self):
        """Project-local UI preference; the legacy boolean remains compatible."""
        home = getattr(self.runtime, "_home", None)
        if home is None:
            return "always" if self.policy().sessions else "ask"
        path = project_state_dir(self.runtime.workspace, home) / "worktree-mode.json"
        try:
            if not path.is_symlink() and path.stat().st_size <= 128:
                value = json.loads(path.read_text())
                if value in {"none", "ask", "always"}:
                    return value
        except (OSError, ValueError, TypeError):
            pass
        return "always" if self.policy().sessions else "ask"

    def choice(self, session, mode=""):
        if not mode:
            return self.status(session)
        if mode not in {"none", "ask", "always"}:
            raise ValueError("Worktree mode must be none, ask, or always")
        handle = self.handle(session)
        self._idle(handle)
        handle._lock.acquire(shared=False, blocking=False)
        try:
            if self.runtime.sessions._has_pending_work(session):
                raise SessionBusy("Worktree choice requires an idle session")
            editable = not self.locked(handle)
            if editable and mode == "always" and not (self.runtime.workspace / ".git").exists():
                raise ValueError("Worktrees need a Git repository")
            path = project_state_dir(self.runtime.workspace, self.runtime._home) / "worktree-mode.json"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    os.chmod(temporary, 0o600)
                    stream.write(json.dumps(mode) + "\n")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            if editable:
                kept = self.selection(handle).get("name", "") if mode == "always" else ""
                handle.emit_session_event("session.worktree_set", {
                    "enabled": mode == "always", "base": self.policy().base,
                    "choice_pending": mode == "ask", **({"name": kept} if kept else {}),
                })
        finally:
            handle._lock.release()
        return self.status(session)

    def selection(self, handle):
        policy = self.policy()
        root = self.runtime.session_root(handle.id)
        selected = {"enabled": bool(root.kind == "worktree" or (self.mode() == "always" and root.kind == "main" and not self.locked(handle))), "base": policy.base, "name": ""}
        for event in handle.events:
            if event.type == "session.worktree_set":
                name = event.data.get("name", "")
                selected = {"enabled": bool(event.data.get("enabled")), "base": event.data.get("base", "current"),
                            "name": name if isinstance(name, str) else ""}
            elif event.type == "worktree.created":
                selected["enabled"] = True
            elif event.type == "worktree.detached":
                selected["enabled"] = False
        return selected

    @staticmethod
    def locked(handle):
        return any(e.type == "worktree.created" or
                   (e.type == "turn.started" and not e.data.get("preparation")) for e in handle.events)

    @staticmethod
    def pending_request(handle):
        pending = None
        for event in handle.events:
            if event.type == "worktree.promotion_requested":
                pending = {"reason": event.data.get("reason", ""), "continuation": event.data.get("continuation", ""), "request_id": event.data.get("request_id", event.id)}
            elif event.type in {"worktree.promoted", "worktree.promotion_failed"}:
                pending = None
        return pending

    def required(self, handle):
        if getattr(handle, "kind", None) == "manager" or getattr(handle, "_kind", None) == "manager":
            return False
        root = self.runtime.session_root(handle.id)
        return bool(self.pending_request(handle)) or root.kind == "worktree" or (self.selection(handle)["enabled"] and not self.locked(handle))

    def _idle(self, handle):
        if handle.active or handle.queued_ids or self.runtime.session_has_active_jobs(handle.id):
            raise SessionBusy("Worktree actions require a turn boundary without jobs or queued inputs")
        running = set()
        for event in handle.events:
            if event.type == "shell.started":
                running.add(event.data.get("shell_id"))
            elif event.type == "shell.completed":
                running.discard(event.data.get("shell_id"))
        if running:
            raise SessionBusy("Worktree actions require composer shell commands to finish")

    def set(self, session, enabled, *, base="current", name=""):
        handle = self.handle(session)
        if not isinstance(name, str):
            raise ValueError("Worktree name must be a string")
        name = name if enabled else ""
        self._idle(handle)
        if type(enabled) is not bool or not isinstance(base, str) or not base or len(base) > 256 or any(ord(c) < 32 for c in base):
            raise ValueError("Worktree selection requires enabled and a bounded base branch")
        handle._lock.acquire(shared=False, blocking=False)
        try:
            if self.runtime.sessions._has_pending_work(session):
                raise SessionBusy("Worktree selection requires an idle draft")
            if self.locked(handle):
                raise ValueError("Worktree selection is locked after the first turn; use Move to worktree or Detach")
            if enabled and not (self.runtime.workspace / ".git").exists():
                raise ValueError("Worktrees need a Git repository")
            data = {"enabled": enabled, "base": base}
            if name:
                if self.runtime.session_root(session).kind != "main":
                    raise ValueError("A worktree name can only be chosen before the session has a working root")
                self.service.check_session_name_free(self.runtime.workspace, name, root=self.registry)
                data["name"] = name
            handle.emit_session_event("session.worktree_set", data)
            if name:
                self._title(session, name)
        finally:
            handle._lock.release()
        return self.status(session)

    def _title(self, session, name):
        """Store the name as a user title (never replaced by auto-titling).

        A draft has no session row until its first message, so this is best
        effort here and repeated when the worktree is created on the first turn.
        """
        from .errors import SessionError
        from .host_support.auto_title import set_user_title
        try:
            set_user_title(self.runtime.sessions, session, name)
        except SessionError:
            pass

    def _base_branch(self, root, selected):
        """Branch the worktree is (or will be) created from; "" when unknown."""
        ref = root.base_ref if root.kind == "worktree" else selected.get("base", "")
        if ref == "current":
            ref = self._git(self.runtime.workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref

    def environment_facts(self, session_id):
        """Labelled facts for the model when the session works in a worktree; () otherwise."""
        root = self.runtime.session_root(session_id)
        if root.kind != "worktree":
            return ()
        return (
            ("Worktree directory", str(root.path)),
            ("Main checkout directory", str(self.runtime.workspace)),
            ("Worktree branch", root.branch),
            ("Base branch", self._base_branch(root, {})),
            ("Base commit", root.base_sha),
        )

    def _git(self, directory, *args, check=True):
        from .agents.session_worktrees import _git
        return _git(Path(directory), *args, check=check)

    @staticmethod
    def _display_command(command):
        if isinstance(command, str):
            return redact_secrets(command)
        if isinstance(command, list):
            return [redact_secrets(value) for value in command]
        return command

    def status(self, session):
        handle = self.handle(session)
        root = self.runtime.session_root(session)
        selected = self.selection(handle)
        is_git = (self.runtime.workspace / ".git").exists()
        choice = next((event for event in reversed(handle.events) if event.type == "session.worktree_set"), None)
        chosen = root.kind == "external" or self.locked(handle) or bool(choice is not None and not choice.data.get("choice_pending", False))
        result = {**{k: v for k, v in selected.items() if k != "name"}, "mode": self.mode(), "chosen": chosen,
                  "editable": not self.locked(handle), "git": is_git,
                  "disabled_reason": "" if is_git else "Worktrees need a Git repository",
                  "pending_promotion": bool(self.pending_request(handle)),
                  "setup": self._display_command(self.policy().setup), "setup_shell": self.policy().setup_shell, "submodules": self.policy().submodules,
                  "kind": root.kind, "worktree_id": root.worktree_id, "branch": root.branch, "base_ref": root.base_ref,
                  "path_display": root.to_data(self.runtime.workspace)["path_display"], "branches": [],
                  "selected": bool(selected["enabled"]), "pending_name": "" if root.kind == "worktree" else selected.get("name", ""),
                  "worktree_name": root.path.name if root.kind == "worktree" else "",
                  "main_path_display": str(self.runtime.workspace),
                  "base_branch": self._base_branch(root, selected)}
        if is_git:
            result["branches"] = self._git(self.runtime.workspace, "for-each-ref", "--count=200", "--format=%(refname:short)", "refs/heads").splitlines()[:200]
        if root.kind == "worktree":
            try:
                result.update(self.service.session_status(root.worktree_id, root=self.registry))
                result["commits"] = [{"sha": row["sha"], "subject": redact_secrets(row["subject"])[:200]} for row in result.get("commits", [])[:5]]
                base_ref = result.get("base_ref", "")
                is_sha = len(base_ref) in {40, 64} and all(c in "0123456789abcdefABCDEF" for c in base_ref)
                target = base_ref if base_ref.startswith("refs/") or base_ref == "HEAD" or is_sha else "refs/heads/" + base_ref
                tip = self._git(self.runtime.workspace, "rev-parse", "--verify", target + "^{commit}")
                behind, ahead = self._git(root.path, "rev-list", "--left-right", "--count", tip + "...HEAD").split()
                result.update(target_tip=tip, ahead=int(ahead), behind=int(behind), queue_position=None, pending_base_moved=[])
            except Exception as exc:
                result.update(state="unavailable", warning=redact_secrets(str(exc))[:500])
        if is_git and root.kind != "worktree":
            try:
                self.runtime.validate_session_root(session)
                branch = self._git(root.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
                head = self._git(root.path, "rev-parse", "--verify", "HEAD^{commit}", check=False).strip()
                from .agents.session_worktrees import status_counts
                uncommitted = status_counts(root.path)
                base = selected["base"]
                if base == "current":
                    base = self._git(self.runtime.workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or "HEAD"
                base_ref = base if base.startswith("refs/") or base == "HEAD" else "refs/heads/" + base
                tip = self._git(self.runtime.workspace, "rev-parse", "--verify", base_ref + "^{commit}", check=False).strip()
                result.update(branch=branch, head=head, base_ref=base_ref, target_tip=tip,
                    uncommitted=uncommitted, commits=[], ahead=0, behind=0, queue_position=None, pending_base_moved=[])
                if head:
                    fields = self._git(root.path, "log", "-5", "--format=%H%x00%<(200,trunc)%s%x00").split("\0")
                    result["commits"] = [{"sha": fields[i].strip(), "subject": redact_secrets(fields[i+1])[:200]} for i in range(0, len(fields)-1, 2)]
                if tip and head:
                    behind, ahead = self._git(root.path, "rev-list", "--left-right", "--count", tip + "..." + head).split()
                    result.update(ahead=int(ahead), behind=int(behind))
            except Exception as exc:
                result.update(state="unavailable", warning=redact_secrets(str(exc))[:500])
        result["setup_digest"] = self._setup_digest()
        return result

    async def _publish_root(self, handle, record, *, created=True, dirty=None):
        root = SessionRoot(kind="worktree", path=Path(record.path), project_key=self.runtime.session_root(handle.id).project_key,
                           branch=record.branch, base_ref=record.base_ref, base_sha=record.base_commit, worktree_id=record.child_id)
        if created:
            handle.emit_session_event("worktree.created", {"branch": record.branch, "base_ref": record.base_ref,
                "base_sha": record.base_commit, "dirty_main": dirty or {"modified": 0, "untracked": 0},
                "path_display": root.to_data(self.runtime.workspace)["path_display"]})
        handle.emit_session_event("session.root_set", root.to_data(self.runtime.workspace))
        await self._thread(handle.id, self.service.activate_session, record.child_id, root=self.registry)
        self.runtime._session_roots.pop(handle.id, None)
        handle._turn_root_agents_key = None
        from .tools.builtin._shell_logs import write_session_cwd
        try:
            write_session_cwd(self.runtime.workspace, handle.id, str(root.path), home=self.runtime._home)
        except OSError:
            self.notice(handle, "Working root selected; shell cwd could not be saved")
        return root

    @staticmethod
    def notice(handle, text):
        handle.emit_session_event("context.notice", {"kind": "worktree", "message": redact_secrets(text)[:2000]})

    def _dirty(self):
        from .agents.session_worktrees import status_counts
        return status_counts(self.runtime.workspace)

    async def _thread(self, session, callback, *args, **kwargs):
        # Shield process-backed work from task cancellation until its worker exits.
        async with process_slot(session):
            task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _create(self, handle, *, base, name=""):
        policy = self.policy()
        dirty = await self._thread(handle.id, self._dirty)
        record = await self._thread(handle.id, self.service.create_session, self.runtime.workspace, handle.id,
            self._name(handle), root=self.registry, base=base, max_worktrees=policy.max_worktrees,
            **({"exact_name": name} if name else {"random_words": True}))
        published = False
        if name:
            self._title(handle.id, name)
        try:
            await self.setup(handle, record, reason="created")
            handle.cancel_token.raise_if_cancelled()
            await self._publish_root(handle, record, dirty=dirty)
            published = True
        finally:
            if not published and not any(e.type == "worktree.created" and e.data.get("branch") == record.branch for e in handle.events):
                await self._thread(handle.id, self.service.abort_session, record.child_id, root=self.registry)
        changes = sum(dirty.get(key, 0) for key in ("modified", "added", "deleted", "untracked", "conflicted"))
        if changes or dirty.get("clipped"):
            count = str(changes) + ("+" if dirty.get("clipped") else "")
            self.notice(handle, f"{count} uncommitted changes in the main checkout are not included; based on {record.base_ref} @ {record.base_commit[:7]}")
        return self.runtime.session_root(handle.id)

    async def prepare(self, handle, lease, content):
        if getattr(handle, "_kind", None) == "manager":
            return
        pending = self.pending_request(handle)
        if pending is not None:
            if self.runtime.session_root(handle.id).kind == "worktree":
                self._complete_promotion(handle, pending)
            else:
                await self._promote(handle, **pending)
        root = self.runtime.session_root(handle.id)
        if root.kind == "worktree":
            record = await self._thread(handle.id, self.service.get, root.worktree_id, root=self.registry)
            if getattr(record, "session_id", None) != handle.id:
                # A fork copies durable root metadata, but never borrows write
                # authority to its parent's owned branch.
                root = await self._create(handle, base=record.branch)
                self.notice(handle, "Fork isolated from the parent branch's current committed tip; uncommitted parent changes are not included")
            else:
                missing = not root.path.is_dir()
                record = await self._thread(handle.id, self.service.recreate_session, root.worktree_id, root=self.registry)
                if missing:
                    await self.setup(handle, record, reason="recreated")
                self.runtime.validate_session_root(handle.id)
        elif self.selection(handle)["enabled"] and not self.locked(handle):
            chosen = self.selection(handle)
            root = await self._create(handle, base=chosen["base"], name=chosen.get("name", ""))
        disabled = any(e.type == "checkpoint.skipped" and e.data.get("reason") in {"disabled", "not_git"} for e in handle.events)
        if root.kind in {"main", "external"} and self.policy().checkpoints and not disabled:
            seq, _ = self._turn_data(handle, lease.turn_id)
            git_root = await self._thread(handle.id, self._git, root.path, "rev-parse", "--show-toplevel", check=False)
            if not git_root.strip():
                # Report at the terminal boundary so non-Git turns retain their
                # normal turn.started-first stream ordering.
                handle._checkpoint_not_git = True
            else:
                baseline_ok = await self._thread(handle.id, self._checkpoint_baseline, root.path, handle.id, seq)
                if baseline_ok:
                    head = await self._thread(handle.id, self._git, root.path, "rev-parse", "--verify", "HEAD^{commit}", check=False)
                    self._emit(handle, "checkpoint.started", {"seq": seq, "head": head.strip()}, lease.turn_id)
                else:
                    self._emit(handle, "checkpoint.skipped", {"seq": seq, "reason": "git_error", "detail": "could not capture turn baseline"}, lease.turn_id)
        if root.kind == "worktree":
            info = await self._thread(handle.id, self.service.session_status, root.worktree_id, root=self.registry)
            seq, _ = self._turn_data(handle, lease.turn_id)
            self._emit(handle, "worktree.turn_started", {"head": info.get("head", ""), "seq": seq, "worktree_id": root.worktree_id}, lease.turn_id)

    @staticmethod
    def _name(handle):
        row = handle._store.session_row(handle.id) or {}
        return str(row.get("name") or row.get("friendly_name") or handle.id)[:200]

    def _turn_data(self, handle, turn_id):
        prompt = ""
        seq = 0
        for record in handle.read(force=True).records:
            message = getattr(record, "message", None)
            if message is not None and message.role == "user" and getattr(message.meta, "turn_id", None) == turn_id:
                prompt = " ".join(block.text for block in message.content if isinstance(block, Text))
                seq = record.seq
                break
        return seq, prompt

    async def commit(self, handle, turn_id, *, message=None, paths=None, agent="root", by="harness"):
        root = self.runtime.session_root(handle.id)
        if root.kind != "worktree" or root.worktree_id != "session-" + handle.id:
            raise ValueError("Only this session's Nexus-owned branch can be committed")
        if self.runtime.session_has_active_jobs(handle.id):
            payload = {"reason": "git_error", "detail": "Background shell jobs are running; finish them before committing"}
            handle.emit_session_event("worktree.commit_skipped", payload)
            return {"kind": "skipped", **payload}
        seq, prompt = self._turn_data(handle, turn_id)
        policy = self.policy()
        result = await self._thread(handle.id, self.service.commit_session, root.worktree_id, root=self.registry,
            message=message if message is not None else "nexus: " + (prompt.splitlines()[0] if prompt else "session changes")[:72],
            session_name=self._name(handle), turn_seq=seq, agent_id=agent, paths=paths,
            max_files=policy.commit_max_files, max_file_mb=policy.commit_max_file_mb, by=by)
        self._commit_event(handle, result, seq=seq, agent=agent, turn_id=turn_id)
        return result

    def _commit_event(self, handle, result, *, seq, agent, turn_id):
        kind = result.get("kind")
        payload = {k:v for k,v in result.items() if k not in {"kind", "body"}}
        if isinstance(payload.get("subject"), str):
            payload["subject"] = redact_secrets(payload["subject"])[:200]
        if isinstance(payload.get("files"), list):
            payload["files"] = [redact_secrets(str(path))[:1024] for path in payload["files"][:200]]
        if kind == "committed":
            self._emit(handle, "worktree.committed", {**payload, "seq": seq, "agent_id": agent}, turn_id)
            if result.get("index_refresh_failed"):
                self.notice(handle, "Commit saved, but Git index refresh failed. Review the worktree status before staging more changes.")
            if result.get("identity_fallback") and not any(e.type == "worktree.identity_fallback" for e in handle.events):
                handle.emit_session_event("worktree.identity_fallback", {})
                self.notice(handle, "Git identity is unset; session commits use Nexus <nexus@localhost>")
        elif kind == "skipped":
            self._emit(handle, "worktree.commit_skipped", payload, turn_id)
            self.notice(handle, "Worktree commit skipped: " + str(payload.get("detail", payload.get("reason", "Git error"))))

    @staticmethod
    def _emit(handle, kind, data, turn_id):
        event = Event(type=kind, data=data, session=handle.id, turn=turn_id)
        record = handle.append_event(event)
        handle._observe_event(record.event)
        handle._publish(record.event)
        fanout = getattr(handle, "_attached_fanout", None)
        if fanout is not None:
            fanout.put_nowait(record.event)

    async def finish(self, handle, terminal):
        pending = None
        try:
            root = self.runtime.session_root(handle.id)
            if root.kind == "worktree":
                seq, _ = self._turn_data(handle, terminal.turn)
                since = next((e.data.get("head", "") for e in reversed(handle.events) if e.type == "worktree.turn_started" and e.turn == terminal.turn), "")
                if since:
                    commits = await self._thread(handle.id, self.service.session_commits, root.worktree_id, root=self.registry, since=since)
                    known = {e.data.get("sha") for e in handle.events if e.type == "worktree.committed"}
                    for result in commits:
                        if result.get("sha") not in known:
                            self._commit_event(handle, {**result, "kind": "committed", "by": "agent", "via": "shell"}, seq=seq, agent="root", turn_id=terminal.turn)
                await self.commit(handle, terminal.turn)
                landing = await self._thread(handle.id, self._landing, handle, root)
                if landing is not None:
                    self._emit(handle, "worktree.merged", landing, terminal.turn)
                    self.notice(handle, f"Merged into {landing['target_branch']} @ {landing['sha'][:12]}: {landing['subject']}")
            elif root.kind in {"main", "external"} and self.policy().checkpoints:
                already_not_git = any(e.type == "checkpoint.skipped" and e.data.get("reason") == "not_git" for e in handle.events)
                if getattr(handle, "_checkpoint_not_git", False) and not already_not_git:
                    seq, _ = self._turn_data(handle, terminal.turn)
                    self._emit(handle, "checkpoint.skipped", {"seq": seq, "reason": "not_git", "detail": ""}, terminal.turn)
                elif not any(e.type == "checkpoint.skipped" and e.data.get("reason") in {"disabled", "not_git"} for e in handle.events):
                    seq, _ = self._turn_data(handle, terminal.turn)
                    result = await self._thread(handle.id, self._checkpoint_capture, root.path, handle.id, seq, handle, terminal.turn)
                    if result.get("kind") == "captured":
                        self._emit(handle, "checkpoint.captured", result["event"], terminal.turn)
                        if result["event"].get("duration_ms", 0) > 5000:
                            self._emit(handle, "checkpoint.skipped", {"seq": seq, "reason": "disabled",
                                "detail": "capture exceeded 5 seconds; disabled for this session"}, terminal.turn)
                            self.notice(handle, "Checkpoints disabled for this session because capture exceeded 5 seconds")
                    elif result.get("kind") == "skipped":
                        self._emit(handle, "checkpoint.skipped", result["event"], terminal.turn)
            pending = self._promotions.pop(handle.id, None)
            if pending is not None:
                await self._promote(handle, **pending)
        except Exception as exc:
            if pending is not None:
                handle.emit_session_event("worktree.promotion_failed", {"request_id": pending.get("request_id"), "detail": redact_secrets(str(exc))[:500]})
            self._emit(handle, "worktree.commit_skipped", {"reason": "git_error", "detail": redact_secrets(str(exc))[:500]}, terminal.turn)
            self.notice(handle, "Worktree action failed: " + str(exc))

    def _checkpoint_ref(self, session, seq, suffix=""):
        safe = ''.join(c for c in session if c.isalnum() or c in '-_')[:190]
        return f"refs/nexus/checkpoints/{safe}/{seq:010d}{suffix}"

    def _checkpoint_snapshot(self, directory, session, seq, suffix, *, base_ref=""):
        """Build a commit object from a private index; never touch the user's index/ref."""
        from .agents.session_worktrees import _git
        directory = Path(directory)
        common = Path(_git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        index = Path(_git(directory, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        temp = common / f"nexus-checkpoint-index-{uuid.uuid4().hex}"
        ref = self._checkpoint_ref(session, seq, suffix)
        env = {"GIT_INDEX_FILE": str(temp), "GIT_AUTHOR_NAME": "Nexus", "GIT_AUTHOR_EMAIL": "nexus@localhost",
               "GIT_COMMITTER_NAME": "Nexus", "GIT_COMMITTER_EMAIL": "nexus@localhost"}
        try:
            if index.exists():
                shutil.copyfile(index, temp)
            else:
                head = _git(directory, "rev-parse", "--verify", "HEAD^{commit}", check=False).strip()
                _git(directory, "read-tree", head if head else "--empty", extra_env={"GIT_INDEX_FILE": str(temp)})
            status = _git(directory, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            status_rows = [row for row in status.split("\0") if len(row) >= 4]
            candidates = []
            row_index = 0
            while row_index < len(status_rows):
                row = status_rows[row_index]
                candidates.append(row[3:])
                if "R" in row[:2] or "C" in row[:2]:
                    row_index += 1
                    if row_index < len(status_rows):
                        candidates.append(status_rows[row_index][3:] if len(status_rows[row_index]) >= 4 else status_rows[row_index])
                row_index += 1
            if len(candidates) > self.policy().commit_max_files:
                raise OverflowError("changed file count exceeds configured limit")
            for relative in candidates:
                path = directory / relative
                if not path.is_symlink() and path.is_file() and path.stat().st_size > self.policy().commit_max_file_mb * 1024 * 1024:
                    raise OverflowError("a changed file exceeds configured size limit")
            _git(directory, "add", "-A", "--", ".", extra_env={"GIT_INDEX_FILE": str(temp)})
            tree = _git(directory, "write-tree", extra_env={"GIT_INDEX_FILE": str(temp)})
            if base_ref:
                base_tree = _git(directory, "rev-parse", "--verify", base_ref + "^{tree}", check=False).strip()
                if base_tree and base_tree == tree:
                    return "", "", "", []
            head = _git(directory, "rev-parse", "--verify", "HEAD^{commit}", check=False).strip()
            parents = ["-p", head] if head else []
            commit = _git(directory, "commit-tree", tree, *parents, "-m", f"nexus checkpoint {session} {seq}", extra_env=env)
            _git(directory, "update-ref", ref, commit, "")
            return ref, commit, head, []
        finally:
            temp.unlink(missing_ok=True)
            Path(str(temp) + ".lock").unlink(missing_ok=True)

    def _checkpoint_baseline(self, directory, session, seq):
        from .agents.session_worktrees import _git
        existing = _git(directory, "for-each-ref", "--count=1", "--format=%(refname)", f"refs/nexus/checkpoints/{session}").strip()
        if existing:
            return True
        try:
            self._checkpoint_snapshot(directory, session, seq, "-base")
            return True
        except Exception:
            # Capture emits the actionable skip; baseline errors must not block a turn.
            return False

    def _checkpoint_capture(self, directory, session, seq, handle, turn_id=""):
        from .agents.session_worktrees import _git
        started = time.monotonic()
        try:
            refs = _git(directory, "for-each-ref", "--sort=-refname", "--format=%(refname)", f"refs/nexus/checkpoints/{session}").splitlines()
            prior = next((value for value in refs if not value.endswith("-base")),
                         next((value for value in refs if value.endswith("-base")), self._checkpoint_ref(session, seq, "-base")))
            if not _git(directory, "rev-parse", "--verify", prior + "^{commit}", check=False).strip():
                return {"kind": "skipped", "event": {"seq": seq, "reason": "git_error", "detail": "turn baseline is unavailable"}}
            ref, commit, head, _ = self._checkpoint_snapshot(directory, session, seq, "", base_ref=prior)
            if not ref:
                return {"kind": "skipped", "event": {"seq": seq, "reason": "no_changes", "detail": ""}}
            refs = _git(directory, "for-each-ref", "--sort=-refname", "--format=%(refname)", f"refs/nexus/checkpoints/{session}").splitlines()
            raw = _git(directory, "diff", "--name-only", "-z", prior, commit).split("\0")
            changed = [value for value in raw if value]
            numstat = _git(directory, "diff", "--numstat", "-z", prior, commit).split("\0")
            insertions = deletions = 0
            for line in numstat:
                fields = line.split("\t", 2)
                if len(fields) == 3:
                    if fields[0].isdigit(): insertions += int(fields[0])
                    if fields[1].isdigit(): deletions += int(fields[1])
            numbered = [value for value in refs if not value.endswith("-base")]
            for stale in numbered[500:]:
                _git(directory, "update-ref", "-d", stale)
            self._prune_checkpoint_sessions(directory)
            touched = {e.data.get("path") for e in handle.events if e.type == "worktree.file_written" and e.turn == turn_id}
            touched.update(path for event in handle.events
                if event.type == "context.notice" and event.turn == turn_id and event.data.get("kind") == "subagent_merged"
                for path in event.data.get("files", []))
            shell_ran = any(e.type == "shell.started" and e.turn == turn_id for e in handle.events)
            owned = [path for path in changed if shell_ran or path in touched]
            start = next((e.data for e in reversed(handle.events) if e.type == "checkpoint.started" and e.turn == turn_id), {})
            start_head = start.get("head", "") if isinstance(start, dict) else ""
            head_moved = bool(start_head and head and start_head != head)
            return {"kind": "captured", "event": {"seq": seq, "ref": ref, "commit": commit, "head": head,
                "base_ref": prior, "files": owned[:200], "files_clipped": max(0, len(owned)-200),
                "foreign": max(0, len(changed)-len(owned)), "insertions": insertions, "deletions": deletions,
                "head_moved": head_moved, "head_start": start_head,
                "duration_ms": int((time.monotonic()-started)*1000)}}
        except OverflowError as exc:
            return {"kind": "skipped", "event": {"seq": seq, "reason": "too_large", "detail": str(exc)[:500]}}
        except Exception as exc:
            return {"kind": "skipped", "event": {"seq": seq, "reason": "git_error", "detail": redact_secrets(str(exc))[:500]}}

    def _prune_checkpoint_sessions(self, directory):
        """Keep refs for the most recently checkpointed configured session count."""
        from .agents.session_worktrees import _git
        rows = _git(directory, "for-each-ref", "--format=%(refname) %(committerdate:unix)",
                    "refs/nexus/checkpoints").splitlines()
        activity = {}
        for row in rows[:100_000]:
            try:
                ref, timestamp = row.rsplit(" ", 1)
                session = ref.split("/", 4)[3]
                activity[session] = max(activity.get(session, 0), int(timestamp))
            except (ValueError, IndexError):
                continue
        keep = self.policy().checkpoint_keep
        expired = {session for session, _ in sorted(activity.items(), key=lambda item: (item[1], item[0]))[:-keep]}
        for row in rows[:100_000]:
            ref = row.rsplit(" ", 1)[0]
            try:
                session = ref.split("/", 4)[3]
            except IndexError:
                continue
            if session in expired:
                _git(directory, "update-ref", "-d", ref)

    def _setup_digest(self):
        policy = self.policy()
        value = [policy.setup, policy.setup_shell, policy.submodules, policy.copy, policy.share_dirs]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _trust_path(self):
        return project_state_dir(self.runtime.workspace, self.runtime._home) / "worktree-trust.json"

    def _trusted(self, session, digest):
        if (session, digest) in self._trust_once:
            self._trust_once.remove((session, digest))
            return True
        try:
            path = self._trust_path()
            if path.is_symlink() or path.stat().st_size > 65536:
                return False
            values = json.loads(path.read_text())
            return digest in values.get("approved", [])
        except (OSError, ValueError, TypeError):
            return False

    def approve(self, session, decision, *, digest=None, _during_setup=False):
        handle = self.handle(session)
        if not _during_setup:
            self._idle(handle)
        current_digest = self._setup_digest()
        if digest is not None and digest != current_digest:
            raise ValueError("Worktree setup changed; review it again before approving")
        digest = current_digest
        if decision == "allow_once":
            if len(self._trust_once) >= 512:
                raise ValueError("Too many pending one-time setup approvals")
            self._trust_once.add((session, digest))
        elif decision == "allow_project":
            path = self._trust_path()
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.is_symlink():
                raise ValueError("Worktree trust file must not be a symlink")
            values = []
            try:
                if path.stat().st_size < 65536:
                    values = json.loads(path.read_text()).get("approved", [])
            except (OSError, ValueError, TypeError):
                pass
            import tempfile
            fd, temporary_name = tempfile.mkstemp(prefix="worktree-trust-", dir=path.parent)
            temporary = Path(temporary_name)
            with os.fdopen(fd, "w") as stream:
                json.dump({"approved": list(dict.fromkeys([*values[-199:], digest]))}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        elif decision == "skip":
            if len(self._skip_once) >= 512:
                raise ValueError("Too many pending setup skips")
            self._skip_once.add((session, digest))
        else:
            raise ValueError("Setup decision must be allow_once, allow_project or skip")
        handle.emit_session_event("worktree.setup_trust", {"digest": digest, "decision": decision})
        return self.status(session)

    def _project_setup(self):
        from .config.layers import read_toml
        for path in (self.runtime.workspace / "nexus.toml", self.runtime.workspace / ".nexus/nexus.toml", self.runtime.workspace / ".agents/nexus.toml"):
            if path.exists() and any(key in read_toml(path).get("worktrees", {}) for key in ("setup", "setup_shell", "submodules")):
                return True
        # An injected config with no provenance is untrusted like a project file.
        return self.runtime._config is not None and not getattr(self.runtime._config, "source", None)

    @staticmethod
    def _plain_path(root, relative):
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise ValueError("Invalid worktree file path")
        path = Path(relative)
        if path.is_absolute() or any(p in {"..", ".git"} for p in path.parts):
            raise ValueError("Worktree file paths must remain inside their checkout")
        target = root
        for part in path.parts:
            target = target / part
            if target.is_symlink():
                raise ValueError(f"Worktree file crosses a symlink: {relative}")
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("Worktree path escapes its checkout")
        return target

    def _copy_setup(self, record, policy):
        source = self.runtime.workspace
        destination = Path(record.path)
        copied = []
        shared = []
        total = 0
        for pattern in policy.copy:
            self._plain_path(source, pattern)
            for candidate in source.glob(pattern):
                if len(copied) >= 1000:
                    raise ValueError("Worktree copy exceeds 1,000 files")
                relative = candidate.relative_to(source).as_posix()
                candidate = self._plain_path(source, relative)
                if not candidate.is_file():
                    continue
                # Tracked content is already provided by Git; never overwrite it.
                tracked = self._git(source, "ls-files", "--", relative)
                if tracked:
                    continue
                size = candidate.stat().st_size
                total += size
                if total > policy.copy_max_mb * 1024 * 1024:
                    raise ValueError("Worktree copy exceeds copy_max_mb")
                target = self._plain_path(destination, relative)
                if target.exists():
                    continue
                with candidate.open("rb") as stream:
                    data = stream.read(size + 1)
                if len(data) != size:
                    raise ValueError("Worktree copy source changed while reading")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                os.chmod(target, stat.S_IMODE(candidate.stat().st_mode))
                copied.append(relative)
        for relative in policy.share_dirs:
            candidate = self._plain_path(source, relative)
            target = self._plain_path(destination, relative)
            if candidate.is_dir() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(candidate, target_is_directory=True)
                shared.append(relative)
        return copied, shared

    async def setup(self, handle, record, *, reason, honor_cancel=True):
        import signal
        policy = self.policy()
        started = time.monotonic()
        copied, shared = await self._thread(handle.id, self._copy_setup, record, policy)
        command = policy.setup
        digest = self._setup_digest()
        skip_key = (handle.id, digest)
        skipped = skip_key in self._skip_once
        self._skip_once.discard(skip_key)
        trusted = not skipped and (not self._project_setup() or self._trusted(handle.id, digest))
        if (command or policy.submodules != "none") and not trusted and not skipped and handle.attended:
            approved = await self.runtime._ask_worktree_setup(handle, policy, digest)
            trusted = approved and self._trusted(handle.id, digest)
        if (command or policy.submodules != "none") and not trusted:
            self.notice(handle, "Worktree setup skipped pending approval. Review the setup command and use the worktree setup trust action before retrying.")
            handle.emit_session_event("worktree.setup_ran", {"command": self._display_command(command), "exit": None, "reason": reason,
                "skipped": "approval_required", "digest": digest, "copied": copied[:50], "shared": shared[:50], "output_tail": ""})
            return
        output = bytearray()
        code = 0
        if policy.submodules != "none":
            args = ["submodule", "update", "--init"]
            if policy.submodules == "recursive":
                args.append("--recursive")
            try:
                await self._thread(handle.id, self._git, record.path, *args)
            except Exception as exc:
                code = 1
                output.extend(str(exc).encode()[:8192])
        if command and not code:
            env = dict(resource_environment())
            env.update(NEXUS_WORKTREE="1", NEXUS_PROJECT_ROOT=str(self.runtime.workspace), NEXUS_WORKSPACE=str(record.path))
            argv = ["/bin/sh", "-c", command] if policy.setup_shell else list(command)
            async with process_slot(handle.id):
                process = await asyncio.create_subprocess_exec(*argv, cwd=record.path, env=env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
                async def drain():
                    emitted = 0
                    while chunk := await process.stdout.read(4096):
                        output.extend(chunk)
                        del output[:-8192]
                        if emitted < 200:
                            self._emit(handle, "worktree.setup_output", {"text": redact_secrets(chunk.decode(errors="replace"))[:4096]}, handle.active_turn_id)
                            emitted += 1
                reader = asyncio.create_task(drain())
                waiter = asyncio.create_task(process.wait())
                stopper = asyncio.create_task(handle.cancel_token.wait()) if honor_cancel else None
                async def stop():
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await waiter
                try:
                    watched = {waiter, stopper} if stopper else {waiter}
                    done, _ = await asyncio.wait(watched, timeout=policy.setup_timeout, return_when=asyncio.FIRST_COMPLETED)
                    if waiter in done:
                        code = process.returncode
                    else:
                        await stop()
                        if stopper and stopper in done:
                            raise OperationCancelled("cancelled during worktree setup")
                        code = 124
                except asyncio.CancelledError:
                    await stop()
                    raise
                finally:
                    if stopper:
                        stopper.cancel()
                    await reader
        handle.emit_session_event("worktree.setup_ran", {"command": self._display_command(command), "exit": code,
            "duration_ms": int((time.monotonic()-started)*1000), "copied": copied[:50], "copied_clipped": max(0,len(copied)-50),
            "shared": shared[:50], "submodules": policy.submodules, "reason": reason,
            "output_tail": redact_secrets(output.decode(errors="replace"))[-8192:]})
        if code:
            if policy.setup_on_fail == "fail":
                raise ValueError(f"Worktree setup failed (exit {code}); {output.decode(errors='replace')[-500:]}")
            self.notice(handle, f"Worktree setup failed (exit {code}); continuing per setup_on_fail=warn")

    def record_write(self, session, turn_id, call_id, path, data, before=None):
        root = self.runtime.session_root(session)
        if root.kind == "worktree":
            return
        path = Path(path)
        try:
            relative = path.relative_to(root.path).as_posix()
        except ValueError:
            return
        self._plain_path(root.path, relative)
        handle = self.handle(session)
        # One receipt per successful atomic write; only successful tool results
        # authorize promotion. Bytes never enter the log, only their digest.
        receipts = [e for e in handle.events if e.type == "worktree.file_written" and e.turn == turn_id]
        if len(receipts) >= 500:
            if not any(e.type == "worktree.provenance_clipped" and e.turn == turn_id for e in handle.events):
                self._emit(handle, "worktree.provenance_clipped", {"owner_session": session}, turn_id)
            return
        self._emit(handle, "worktree.file_written", {"path": relative, "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
            "deleted": data is None, "call_id": call_id, "owner_session": session, "before_oids": before,
            "after_oids": self._blob_oids(data), "root_display": root.to_data(self.runtime.workspace)["path_display"]}, turn_id)

    @staticmethod
    def _blob_oids(data):
        if data is None:
            return None
        header = f"blob {len(data)}\0".encode()
        hashes = {"sha1": hashlib.sha1(header), "sha256": hashlib.sha256(header)}
        for value in hashes.values():
            value.update(data)
        return {key: value.hexdigest() for key, value in hashes.items()}

    def _owned_changes(self, handle, root):
        successful = {(e.turn, e.data.get("tool_use_id")) for e in handle.events if e.type == "tool.result" and not e.data.get("is_error")}
        receipts = {}
        for event in handle.events:
            if event.type in {"checkpoint.created", "worktree.promoted"}:
                receipts.clear()
            elif event.type == "worktree.provenance_clipped" and event.data.get("owner_session") == handle.id:
                raise ValueError("Can't separate session changes: file provenance exceeded 500 writes")
            elif (event.type == "worktree.file_written" and (event.turn, event.data.get("call_id")) in successful
                    and event.data.get("owner_session") == handle.id):
                receipts.setdefault(event.data["path"], []).append(event.data)
        if len(receipts) > 500:
            raise ValueError("Promotion is bounded to 500 files")
        status = self._git(root.path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        dirty = set()
        rows = iter(status.split("\0"))
        for row in rows:
            if not row:
                continue
            dirty.add(row[3:])
            if row[0] in {"R", "C"} or row[1] in {"R", "C"}:
                dirty.add(next(rows, ""))
        owned = {}
        total = 0
        for relative in sorted(dirty & receipts.keys()):
            chain = receipts[relative]
            receipt = chain[-1]
            expected = self._git(root.path, "rev-parse", "--verify", "HEAD:" + relative, check=False).strip()
            initial = chain[0].get("before_oids")
            if (expected and (not initial or expected not in initial.values())) or (not expected and initial is not None):
                raise ValueError(f"Can't separate this session's changes in {relative}; it contained earlier changes")
            previous = chain[0].get("after_oids")
            for write in chain[1:]:
                if write.get("before_oids") != previous:
                    raise ValueError(f"Can't separate this session's changes in {relative}; another writer changed it between edits")
                previous = write.get("after_oids")
            if receipt.get("root_display") != root.to_data(self.runtime.workspace)["path_display"]:
                raise ValueError(f"Can't separate this session's changes in {relative}; root changed")
            source = self._plain_path(root.path, relative)
            data = None
            if source.exists():
                if not source.is_file() or source.stat().st_size > self.policy().commit_max_file_mb * 1024 * 1024:
                    raise ValueError(f"Can't carry over non-file or oversized {relative}")
                with source.open("rb") as stream:
                    data = stream.read(self.policy().commit_max_file_mb * 1024 * 1024 + 1)
                total += len(data)
                if total > 128 * 1024 * 1024:
                    raise ValueError("Promotion exceeds 128 MiB; commit or move large files yourself")
            digest = hashlib.sha256(data).hexdigest() if data is not None else None
            if digest != receipt.get("sha256"):
                raise ValueError(f"Can't separate this session's changes in {relative}; commit or move them yourself")
            owned[relative] = data
        return owned, sorted(dirty - owned.keys())[:500]

    async def promote(self, session, *, reason="", continuation=""):
        if not isinstance(reason, str) or len(reason) > 500 or not isinstance(continuation, str) or len(continuation) > 16000:
            raise ValueError("Promotion reason/continuation exceeds its limit")
        handle = self.handle(session)
        self._idle(handle)
        handle._lock.acquire(shared=False, blocking=False)
        try:
            if self.runtime.sessions._has_pending_work(session):
                raise SessionBusy("Promotion requires a turn boundary without pending work")
            from uuid import uuid4
            if self.runtime.session_root(session).kind == "worktree":
                raise ValueError("Session already runs in an owned worktree")
            pending = {"reason": reason, "continuation": continuation, "request_id": str(uuid4())}
            handle.emit_session_event("worktree.promotion_requested", pending)
            try:
                return await self._promote(handle, **pending)
            except BaseException as exc:
                handle.emit_session_event("worktree.promotion_failed", {"request_id": pending["request_id"], "detail": redact_secrets(str(exc))[:500]})
                raise
        finally:
            handle._lock.release()

    async def _promote(self, handle, *, reason="", continuation="", request_id=""):
        if not isinstance(reason, str) or len(reason) > 500 or not isinstance(continuation, str) or len(continuation) > 16000:
            raise ValueError("Promotion reason/continuation exceeds its limit")
        if handle.active:
            handle.cancel_token.raise_if_cancelled()
        root = self.runtime.validate_session_root(handle.id)
        if root.kind == "worktree":
            raise ValueError("Session already runs in an owned worktree")
        if self.runtime.session_has_active_jobs(handle.id):
            raise SessionBusy("Finish background jobs before moving to a worktree")
        owned, excluded = await self._thread(handle.id, self._owned_changes, handle, root)
        base_sha = await self._thread(handle.id, self._git, self.runtime.workspace, "rev-parse", "--verify", "HEAD^{commit}")
        # The service validates the base; internal base_sha is an immutable commit,
        # while the eventual land target remains the main checkout's branch.
        policy = self.policy()
        record = None
        if request_id and self.registry.exists():
            from .agents.worktrees import WorktreeError
            try:
                candidate = await self._thread(handle.id, self.service.get, "session-" + handle.id, root=self.registry)
                if candidate.lifecycle == "pending" and candidate.session_id == handle.id:
                    record = candidate
            except WorktreeError as exc:
                if "no owned" not in str(exc) and "service root" not in str(exc):
                    raise
        if record is None:
            record = await self._thread(handle.id, self.service.create_session, self.runtime.workspace, handle.id, self._name(handle),
                root=self.registry, base="current", max_worktrees=policy.max_worktrees, random_words=True)
        published = False
        try:
            if record.base_commit != base_sha:
                raise ValueError("Main checkout HEAD moved while preparing promotion; retry")
            await self.setup(handle, record, reason="created", honor_cancel=handle.active)
            if handle.active:
                handle.cancel_token.raise_if_cancelled()
            # Revalidate source hashes immediately before copying. No source file
            # is restored, reset, staged or committed by promotion.
            current, _ = await self._thread(handle.id, self._owned_changes, handle, root)
            if current != owned:
                raise ValueError("Session files changed while promotion was being prepared")
            for relative, data in owned.items():
                target = self._plain_path(Path(record.path), relative)
                if data is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            if owned:
                seq, _ = self._turn_data(handle, handle.active_turn_id or "")
                result = await self._thread(handle.id, self.service.commit_session, record.child_id, root=self.registry,
                    message="nexus: carry over changes from the main checkout", session_name=self._name(handle),
                    turn_seq=seq, agent_id="root", paths=list(owned), max_files=policy.commit_max_files,
                    max_file_mb=policy.commit_max_file_mb, by="harness", allow_pending=True)
                already_committed = result.get("kind") == "unchanged" and self._git(record.path, "log", "-1", "--format=%s") == "nexus: carry over changes from the main checkout"
                if result.get("kind") != "committed" and not already_committed:
                    raise ValueError("Could not commit carried session changes: " + str(result.get("detail", result.get("kind"))))
                if already_committed:
                    previous = await self._thread(handle.id, self.service.session_commits, record.child_id, root=self.registry, since=record.base_commit)
                    if previous:
                        restored = previous[0]
                        trailer = next((line.removeprefix("Nexus-Turn-Seq: ").strip() for line in restored.get("body", "").splitlines() if line.startswith("Nexus-Turn-Seq: ")), "")
                        seq = int(trailer) if trailer.isdigit() else seq
                        result = {**restored, "kind": "committed", "by": "harness", "recovered": True}
                self._commit_event(handle, result, seq=seq, agent="root", turn_id=handle.active_turn_id)
            await self._publish_root(handle, record)
            published = True
            if continuation:
                handle.enqueue(continuation, origin={"kind": "promote", "request_id": request_id})
            handle.emit_session_event("worktree.promoted", {"reason": redact_secrets(reason), "request_id": request_id, "files": list(owned), "excluded": excluded,
                "source_display": root.to_data(self.runtime.workspace)["path_display"]})
            self.notice(handle, f"Moved to {record.branch}. These files remain modified in the original checkout: {', '.join(owned) or '(none)'}. Revert them when ready. Excluded other changes: {', '.join(excluded) or '(none)'}")
            return self.status(handle.id)
        finally:
            if not published and not any(e.type == "worktree.created" and e.data.get("branch") == record.branch for e in handle.events):
                await self._thread(handle.id, self.service.abort_session, record.child_id, root=self.registry)

    def _complete_promotion(self, handle, pending):
        request_id = pending.get("request_id", "")
        continuation = pending.get("continuation", "")
        queued = any(e.type == "input.queued" and e.data.get("origin", {}).get("request_id") == request_id for e in handle.events)
        if continuation and not queued:
            handle.enqueue(continuation, origin={"kind": "promote", "request_id": request_id})
        handle.emit_session_event("worktree.promoted", {"request_id": request_id, "reason": pending.get("reason", ""), "recovered": True})

    async def request_promote(self, args, ctx):
        handle = self.handle(ctx.session_id)
        if handle.id in self._promotions:
            raise ValueError("A promotion is already pending")
        if self.runtime.session_root(handle.id).kind == "worktree":
            raise ValueError("Session already runs in an owned worktree")
        pending = {"reason": args["reason"], "continuation": args.get("continuation", ""), "request_id": ctx.call_id}
        self._emit(handle, "worktree.promotion_requested", pending, ctx.turn_id)
        self._promotions[handle.id] = pending
        # The sink marks a generic loop handoff only after the tool result
        # is durable. This leaves Stop available during promotion setup.
        handle._deferred_turn_end = (ctx.call_id, "Moving session to a worktree")
        return {"state": "promotion_pending", "reason": pending["reason"], "continuation_queued": bool(pending["continuation"])}

    def _landing(self, handle, root):
        """Payload for ``worktree.merged`` when the agent landed the session branch with Git; else ``None``.

        The agent runs ``git -C <main checkout> merge --ff-only <session branch>``;
        the host notices instead of being told (checked at turn end). A landing means
        the session head has commits beyond the recorded base commit and is now an
        ancestor of (or equal to) the target branch head. ``head`` (the landed session
        head) dedupes: one event per landed head, so a later move of the target does
        not repeat it. Best effort: any Git failure returns ``None``.
        """
        from .agents.session_worktrees import _git
        from .agents.worktrees import WorktreeError

        def ancestor(directory, older, newer):
            try:
                _git(directory, "merge-base", "--is-ancestor", older, newer)
                return True
            except WorktreeError:
                return False

        try:
            record = self.service.get(root.worktree_id, root=self.registry)
            target_branch = record.base_ref.removeprefix("refs/heads/")
            if record.lifecycle != "active" or not target_branch or target_branch == "HEAD":
                return None
            head = _git(record.path, "rev-parse", "--verify", "HEAD^{commit}")
            base = record.base_commit
            if not base or head == base:
                return None
            landed = [e for e in handle.events if e.type == "worktree.merged"]
            if any(e.data.get("head") == head for e in landed):
                return None
            target = Path(record.parent_workspace)
            target_head = _git(target, "rev-parse", "--verify", f"refs/heads/{target_branch}^{{commit}}")
            if ancestor(record.path, head, base) or not ancestor(record.path, head, target_head):
                return None
            prior = landed[-1].data.get("head", "") if landed else base
            before = prior if prior and ancestor(record.path, prior, head) else _git(record.path, "rev-parse", "--verify", "HEAD^", check=False) or base
            rows = [row for row in _git(record.path, "log", "-101", "--first-parent", "--format=%H", f"{before}..{head}").split("\n") if row]
            names = [name for name in _git(record.path, "diff", "--no-renames", "--name-only", "-z", before, head, "--").split("\0") if name]
            subject = redact_secrets(_git(record.path, "log", "-1", "--format=%s", head))[:200]
        except (WorktreeError, OSError, ValueError):
            return None
        return {"target_branch": target_branch, "session_branch": record.branch, "sha": target_head, "before": before,
                "subject": subject, "commits": len(rows), "files": [redact_secrets(name)[:1024] for name in names[:200]],
                "clipped": len(names) > 200 or len(rows) > 100, "head": head}

    def detach(self, session):
        handle = self.handle(session)
        self._idle(handle)
        root = self.runtime.session_root(session)
        if root.kind != "worktree":
            raise ValueError("Session has no Nexus-owned worktree to detach")
        self.runtime.set_session_root(session, self.runtime.workspace, kind="main")
        handle.emit_session_event("worktree.detached", {"branch": root.branch, "worktree_id": root.worktree_id})
        self.service.detach_session(root.worktree_id, root=self.registry)
        return self.status(session)

    def tools(self, session):
        if not (self.runtime.workspace / ".git").exists():
            return ()
        from .tools.builtin.worktree import build_worktree_tools
        owned = self.runtime.session_root(session).kind == "worktree"
        def check(ctx):
            if ctx.session_id != session:
                raise ValueError("Session worktree tools cannot act for another session")
        async def status(args, ctx):
            check(ctx)
            return await self._thread(session, self.status, session)
        async def commit(args, ctx):
            check(ctx)
            guard = self.runtime.session_path_guard(session)
            paths = args.get("paths")
            if paths is not None:
                paths = [str(guard.resolve(path, for_write=True).absolute.relative_to(ctx.workspace)) for path in paths]
            return await self.commit(self.handle(session), ctx.turn_id, message=args["message"], paths=paths,
                                     agent=ctx.agent_id, by="agent")
        async def promote(args, ctx):
            check(ctx)
            return await self.request_promote(args, ctx)
        return build_worktree_tools(status, commit, promote, owned=owned)

    async def recover(self):
        if getattr(self, "_recovered", False):
            return
        if not hasattr(self, "_recovery_lock"):
            self._recovery_lock = asyncio.Lock()
        async with self._recovery_lock:
            if getattr(self, "_recovered", False):
                return
            if self.registry.exists():
                from .agents import worktrees
                records = list((self.registry / "records").glob("*.json"))
                if len(records) > 10000:
                    raise ValueError("Worktree recovery exceeds 10,000 records")
                created = set()
                pending_ids = set()
                for path in records:
                    payload = json.loads(worktrees._read_metadata_bytes(path)).get("record", {})
                    if payload.get("kind") != "session":
                        continue
                    session = payload.get("session_id")
                    # Saved child work uses `<parent>/sub/<n>` ids, which are not
                    # root session ids; the session store would reject them.
                    if (not isinstance(session, str) or not is_valid_session_id(session)
                            or not self.runtime.sessions.exists(session)):
                        continue
                    handle = self.handle(session)
                    if self.pending_request(handle):
                        pending_ids.add(session)
                    if any(e.type == "worktree.created" and e.data.get("branch") == payload.get("branch") for e in handle.events):
                        created.add(session)
                recovered = await self._thread("", self.service.recover_sessions, root=self.registry, created_ids=created, pending_ids=pending_ids)
                for record in recovered:
                    if record.session_id in created and record.lifecycle == "active":
                        handle = self.handle(record.session_id)
                        root = self.runtime.session_root(record.session_id)
                        if root.kind != "worktree" and not any(e.type == "worktree.detached" for e in handle.events):
                            created_at = max((i for i, e in enumerate(handle.events) if e.type == "worktree.created" and e.data.get("branch") == record.branch), default=-1)
                            root_at = max((i for i, e in enumerate(handle.events) if e.type == "session.root_set"), default=-1)
                            if root_at > created_at:
                                handle.emit_session_event("worktree.detached", {"branch": record.branch, "worktree_id": record.child_id, "recovered": True})
                                await self._thread(handle.id, self.service.detach_session, record.child_id, root=self.registry)
                                continue
                            await self._publish_root(handle, record, created=False)
                        if record.path.is_dir():
                            boundaries = [e for e in handle.events if e.type == "worktree.turn_started"]
                            terminals = {e.turn for e in handle.events if e.type in {"turn.completed", "turn.failed", "turn.cancelled"}}
                            if boundaries and boundaries[-1].turn not in terminals:
                                boundary = boundaries[-1]
                                commits = await self._thread(handle.id, self.service.session_commits, record.child_id, root=self.registry, since=boundary.data["head"])
                                known = {e.data.get("sha") for e in handle.events if e.type == "worktree.committed"}
                                for commit in commits:
                                    if commit.get("sha") not in known:
                                        self._commit_event(handle, {**commit, "kind": "committed", "by": "agent", "via": "shell", "recovered": True},
                                            seq=boundary.data.get("seq", 0), agent="root", turn_id=boundary.turn)
            self._recovered = True
