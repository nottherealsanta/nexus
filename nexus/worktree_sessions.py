"""Runtime-owned session worktrees: agent-managed create/move/merge (agent-managed-worktrees plan).

Every session starts in the main checkout. The agent (or the user, while the
session is idle) lists, creates and moves between the project's worktrees; a
worktree belongs to the project, and any number of sessions can be in one. The
controller keeps project policy separate from checkout authority. Git writes are
delegated to the authenticated service; main/external checkouts are read only.

A move requested from a tool is queued and applied at the next step boundary
(after every tool call of the model step finished, before the next model
request), so the turn keeps going: see :meth:`SessionWorktrees.step_boundary`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path

from .config.paths import project_state_dir
from .errors import OperationCancelled, SessionBusy
from .events import Event
from .model.message import Message, MessageMeta, Text
from .resources import process_slot, resource_environment
from .session.ids import is_valid_session_id
from .session.root import SessionRoot
from .util import redact_secrets

_DIRTY_KEYS = ("modified", "added", "deleted", "untracked", "conflicted", "clipped")
_MAX_OCCUPANCY_SESSIONS = 100
_NOTICE_KIND = "worktree_move"


def _is_dirty(counts) -> bool:
    return any(counts.get(key) for key in _DIRTY_KEYS)


def _dirty_total(counts) -> int:
    return sum(counts.get(key, 0) for key in _DIRTY_KEYS if key != "clipped")


class SessionWorktrees:
    def __init__(self, runtime):
        self.runtime = runtime
        #: Moves requested by tools and not yet applied (one per session).
        self._moves: dict[str, dict] = {}
        self._trust_once: set[tuple[str, str]] = set()
        self._skip_once: set[tuple[str, str]] = set()
        self._occupancy_cache: tuple[float, dict] = (0.0, {})

    # -- plumbing -----------------------------------------------------------

    @property
    def service(self):
        return self.runtime._worktree_service

    @property
    def registry(self):
        return self.runtime._worktree_root

    @property
    def is_git(self) -> bool:
        return (self.runtime.workspace / ".git").exists()

    def policy(self):
        from .config.schema import WorktreesSection
        return getattr(self.runtime._load_config().v2, "worktrees", None) or WorktreesSection()

    def handle(self, session):
        return self.runtime.sessions.open(session, create=False, recover=False)

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

    def _git(self, directory, *args, check=True, observational=True):
        from .agents.session_worktrees import _git
        return _git(Path(directory), *args, check=check, observational=observational)

    @staticmethod
    def _display_command(command):
        if isinstance(command, str):
            return redact_secrets(command)
        if isinstance(command, list):
            return [redact_secrets(value) for value in command]
        return command

    async def _thread(self, session, callback, *args, **kwargs):
        # Shield process-backed work from task cancellation until its worker exits.
        async with process_slot(session):
            task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    @staticmethod
    def _emit(handle, kind, data, turn_id):
        event = Event(type=kind, data=data, session=handle.id, turn=turn_id)
        record = handle.append_event(event)
        handle._observe_event(record.event)
        handle._publish(record.event)
        fanout = getattr(handle, "_attached_fanout", None)
        if fanout is not None:
            fanout.put_nowait(record.event)

    def _event(self, handle, kind, data):
        """Durable event: part of the running turn when there is one, else turn-less."""
        turn = handle.active_turn_id
        if turn:
            self._emit(handle, kind, data, turn)
        else:
            handle.emit_session_event(kind, data)

    def notice(self, handle, text):
        self._event(handle, "context.notice", {"kind": "worktree", "message": redact_secrets(text)[:2000]})

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

    # -- project records and who is where ----------------------------------

    def project_records(self):
        """Verified, active worktree records of this project (any creator)."""
        if not self.is_git or not self.registry.exists():
            return ()
        try:
            records = self.service.list_worktrees(self.runtime.workspace, root=self.registry)
        except Exception:
            return ()
        return tuple(record for record in records if record.lifecycle == "active")

    def _find_record(self, to):
        try:
            record = self.service.find_worktree(self.runtime.workspace, to, root=self.registry)
        except Exception:
            record = None
        if record is None or record.lifecycle != "active":
            names = ", ".join(r.name for r in self.project_records()) or "none yet"
            raise ValueError(f'No Nexus worktree named {to!r} in this project. Use "main" or one of: {names}. '
                             "Call worktree_list to see them.")
        return record

    def _present_count(self):
        return sum(1 for record in self.project_records() if Path(record.path).is_dir())

    def _limit_message(self):
        policy = self.policy()
        merged = []
        for record in self.project_records():
            if not Path(record.path).is_dir():
                continue
            try:
                from .agents.session_worktrees import merge_state
                if merge_state(record)["merged"]:
                    merged.append(record.name)
            except Exception:
                continue
        text = f"The project already has {policy.max_worktrees} worktree checkouts (the limit)."
        if merged:
            text += (" Merged worktrees whose checkouts can be cleaned up: " + ", ".join(merged[:16]) +
                     ". Move into one of them to reuse it, or ask the user to remove checkouts from /worktrees.")
        else:
            text += " None is merged yet. Continue in an existing worktree (worktree_move) or ask the user to remove one."
        return text

    def is_project_branch(self, branch) -> bool:
        """Whether ``branch`` is the branch of a Nexus worktree of this project (merge exemption)."""
        return isinstance(branch, str) and any(record.branch == branch for record in self.project_records())

    def is_base_branch(self, name) -> bool:
        """Whether ``name`` is a base branch of this project (main's current branch or a worktree base)."""
        if not isinstance(name, str) or not name:
            return False
        if name == self._git(self.runtime.workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip():
            return True
        return any(record.base_ref.removeprefix("refs/heads/") == name for record in self.project_records())

    def occupancy(self, *, fresh=False) -> dict[str, list[dict]]:
        """Sessions per checkout, from each session's latest ``session.root_set``.

        Keys are ``"main"`` or a worktree record id. Rebuilt from the session
        logs (cached for a couple of seconds), so it survives restarts. Bounded
        to the most recent sessions of the project.
        """
        stamp, cached = self._occupancy_cache
        if not fresh and time.monotonic() - stamp < 2.0:
            return cached
        index: dict[str, list[dict]] = {}
        try:
            summaries = self.runtime.sessions.list()[:_MAX_OCCUPANCY_SESSIONS]
        except Exception:
            summaries = []
        for summary in summaries:
            try:
                root = self.runtime.session_root(summary.id)
            except Exception:
                continue
            key = "main" if root.kind == "main" else root.worktree_id if root.kind == "worktree" else ""
            if not key:
                continue
            index.setdefault(key, []).append({
                "id": summary.id, "title": redact_secrets(summary.title or summary.name or summary.id)[:80],
                "active": summary.state != "idle", "last_activity": summary.last_activity,
            })
        # A draft has no session row yet but already has a root: count the live handles too.
        seen = {summary.id for summary in summaries}
        for sid in tuple(getattr(self.runtime.sessions, "live_sessions", ())):
            if sid in seen or "/" in sid:
                continue
            try:
                live = self.runtime.sessions.open(sid, create=False, recover=False)
                if getattr(live, "kind", "root") != "root":
                    continue
                root = self.runtime.session_root(sid)
            except Exception:
                continue
            key = "main" if root.kind == "main" else root.worktree_id if root.kind == "worktree" else ""
            if key:
                index.setdefault(key, []).append({"id": sid, "title": sid[:80], "active": bool(live.active),
                                                  "last_activity": 0.0})
        self._occupancy_cache = (time.monotonic(), index)
        return index

    def occupants(self, record_id):
        """Ids of the sessions whose current root is worktree ``record_id`` (or ``"main"``)."""
        return [item["id"] for item in self.occupancy(fresh=True).get(record_id, [])]

    def _others_in(self, session, key):
        return [item for item in self.occupancy().get(key, []) if item["id"] != session]

    @staticmethod
    def _sessions_text(items):
        if not items:
            return "none"
        shown = [f"{item['title']}{' (active)' if item['active'] else ''}" for item in items[:5]]
        more = f", +{len(items) - 5} more" if len(items) > 5 else ""
        return ", ".join(shown) + more

    # -- git facts ----------------------------------------------------------

    def _merge_state(self, record):
        from .agents.session_worktrees import merge_state
        try:
            return merge_state(record)
        except Exception:
            return {"merged": False, "unmerged_commits": 0, "has_work": False, "head": "", "base_head": ""}

    def _base_branch(self, root):
        """Branch a worktree session was based on ("" when it was a bare commit)."""
        ref = root.base_ref if root.kind == "worktree" else ""
        ref = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        if ref == "HEAD" or (len(ref) in {40, 64} and all(c in "0123456789abcdefABCDEF" for c in ref)):
            return ""
        return ref

    def _main_branch(self):
        return self._git(self.runtime.workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()

    def _row(self, record, occupancy, current_id):
        present = Path(record.path).is_dir()
        state = self._merge_state(record)
        dirty = 0
        if present:
            from .agents.session_worktrees import status_counts
            try:
                counts = status_counts(record.path)
                dirty = _dirty_total(counts) + (1 if counts.get("clipped") else 0)
            except Exception:
                dirty = 0
        sessions = occupancy.get(record.child_id, [])
        base = record.base_ref.removeprefix("refs/heads/")
        if len(base) in {40, 64}:
            base = base[:12]
        return {
            "id": record.child_id, "child_id": record.child_id, "name": record.name, "kind": "worktree",
            "branch": record.branch, "base_ref": record.base_ref,
            "base_branch": base, "base_commit": record.base_commit, "present": present, "dirty": dirty,
            "merged": bool(state["merged"] and state["has_work"]), "has_work": bool(state["has_work"]),
            "unmerged_commits": int(state["unmerged_commits"]),
            "sessions": sessions[:20], "created_by": record.created_by,
            "last_activity": max([record.updated_at, *(item["last_activity"] for item in sessions)]),
            "current": record.child_id == current_id,
        }

    def list(self, session, occupancy=None):
        """Every Nexus worktree of this project plus the main checkout, as seen from ``session``.

        Bounded to ``worktrees.max_worktrees`` rows (``clipped`` counts the rest). Extra
        Git worktrees Nexus did not create are listed read-only under ``external``.
        ``occupancy`` lets a caller on the event loop pass the session index so a
        worker thread only runs Git.
        """
        from .agents.session_worktrees import status_counts
        root = self.runtime.session_root(session)
        policy = self.policy()
        occupancy = occupancy if occupancy is not None else self.occupancy(fresh=True)
        records = sorted(self.project_records(), key=lambda r: -r.updated_at)
        shown = records[:policy.max_worktrees]
        current_id = root.worktree_id if root.kind == "worktree" else ""
        rows = [self._row(record, occupancy, current_id) for record in shown]
        main_dirty = 0
        try:
            counts = status_counts(self.runtime.workspace)
            main_dirty = _dirty_total(counts) + (1 if counts.get("clipped") else 0)
        except Exception:
            pass
        main = {
            "id": "main", "child_id": "", "name": "main", "kind": "main", "branch": self._main_branch(),
            "base_ref": "", "path": str(self.runtime.workspace), "dirty": main_dirty, "present": True,
            "merged": False, "unmerged_commits": 0,
            "sessions": occupancy.get("main", [])[:20], "current": root.kind == "main",
        }
        known = {Path(r.path).resolve() for r in records} | {self.runtime.workspace.resolve()}
        external = []
        try:
            current = {}
            for line in self._git(self.runtime.workspace, "worktree", "list", "--porcelain").splitlines() + [""]:
                if not line:
                    path = current.get("worktree")
                    if path and Path(path).resolve() not in known:
                        external.append({"path": path, "name": Path(path).name,
                                         "path_display": os.path.relpath(path, self.runtime.workspace.parent),
                                         "branch": current.get("branch", "").removeprefix("refs/heads/")})
                    current = {}
                elif " " in line:
                    key, value = line.split(" ", 1)
                    current[key] = value
        except Exception:
            external = []
        return {
            "session": session, "current": "main" if root.kind == "main" else (root.path.name if root.kind == "worktree" else "external"),
            "main": main, "worktrees": [main, *rows], "clipped": max(0, len(records) - len(shown)),
            "external": external[:16], "max_worktrees": policy.max_worktrees,
            "present": self._present_count(),
        }

    def environment_facts(self, session_id):
        """Labelled facts for the model's ``<session_worktree>`` block (every Git session)."""
        if not self.is_git:
            return ()
        handle = self.handle(session_id)
        if getattr(handle, "kind", None) == "manager" or getattr(handle, "_kind", None) == "manager":
            return ()
        root = self.runtime.session_root(session_id)
        if root.kind == "external":
            return ()
        from .agents.session_worktrees import status_counts
        main_dirty = _dirty_total(status_counts(self.runtime.workspace))
        occupancy = self.occupancy()
        key = "main" if root.kind == "main" else root.worktree_id
        facts = [
            ("Current root", "main checkout" if root.kind == "main" else f"worktree {root.path.name}"),
            ("Main checkout directory", str(self.runtime.workspace)),
            ("Main checkout branch", self._main_branch() or "(detached)"),
            ("Main checkout changes", f"dirty ({main_dirty} changed files)" if main_dirty else "clean"),
            ("Other sessions in the main checkout", self._sessions_text([i for i in occupancy.get("main", []) if i["id"] != session_id])),
        ]
        if root.kind == "worktree":
            facts += [
                ("Worktree name", root.path.name),
                ("Worktree directory", str(root.path)),
                ("Worktree branch", root.branch),
                ("Base branch", self._base_branch(root) or root.base_ref),
                ("Base commit", root.base_sha),
            ]
            try:
                record = self.service.get_worktree(root.worktree_id, root=self.registry, parent_workspace=self.runtime.workspace)
                state = self._merge_state(record)
                if state["merged"] and state["has_work"]:
                    text = "merged into " + (self._base_branch(root) or "its base")
                elif state["has_work"]:
                    text = f"unmerged ({state['unmerged_commits']} commits not in {self._base_branch(root) or 'its base'})"
                else:
                    text = "no commits of its own yet"
                dirty = _dirty_total(status_counts(root.path))
                facts += [("Worktree state", text),
                          ("Worktree changes", f"dirty ({dirty} changed files)" if dirty else "clean")]
            except Exception:
                facts.append(("Worktree state", "unavailable"))
            facts.append(("Other sessions in this worktree", self._sessions_text(
                [i for i in occupancy.get(key, []) if i["id"] != session_id])))
        last = next((e for e in reversed(handle.events) if e.type == "worktree.moved"), None)
        if last is not None:
            data = last.data
            line = f"{data.get('action', 'move')} to {data.get('to_name', '?')} from {data.get('from_name', '?')}"
            if data.get("reason"):
                line += f": {redact_secrets(str(data['reason']))[:200]}"
            facts.append(("Last move", line))
        return tuple(facts)

    def status(self, session):
        """Projection for the host: current root, Git state, setup trust."""
        root = self.runtime.session_root(session)
        is_git = self.is_git
        policy = self.policy()
        result = {
            "git": is_git, "disabled_reason": "" if is_git else "Worktrees need a Git repository",
            "pending_move": session in self._moves,
            "setup": self._display_command(policy.setup), "setup_shell": policy.setup_shell, "submodules": policy.submodules,
            "kind": root.kind, "worktree_id": root.worktree_id, "branch": root.branch, "base_ref": root.base_ref,
            "path_display": root.to_data(self.runtime.workspace)["path_display"], "branches": [],
            "worktree_name": root.path.name if root.kind == "worktree" else "",
            "main_path_display": str(self.runtime.workspace),
            "base_branch": self._base_branch(root) or self._main_branch(),
        }
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
        elif is_git:
            try:
                from .agents.session_worktrees import status_counts
                self.runtime.validate_session_root(session)
                branch = self._main_branch()
                head = self._git(root.path, "rev-parse", "--verify", "HEAD^{commit}", check=False).strip()
                result.update(branch=branch, head=head, uncommitted=status_counts(root.path), commits=[])
                if head:
                    fields = self._git(root.path, "log", "-5", "--format=%H%x00%s%x00").split("\0")
                    result["commits"] = [{"sha": fields[i].strip(), "subject": redact_secrets(fields[i+1])[:200]} for i in range(0, len(fields)-1, 2)]
            except Exception as exc:
                result.update(state="unavailable", warning=redact_secrets(str(exc))[:500])
        result["setup_digest"] = self._setup_digest()
        return result

    # -- publishing a root --------------------------------------------------

    async def _publish_root(self, handle, record, *, created=True, dirty=None, extra=None):
        extra = dict(extra or {})
        root = SessionRoot(kind="worktree", path=Path(record.path), project_key=self.runtime.session_root(handle.id).project_key,
                           branch=record.branch, base_ref=record.base_ref, base_sha=record.base_commit, worktree_id=record.child_id)
        display = root.to_data(self.runtime.workspace)
        if created:
            self._event(handle, "worktree.created", {"branch": record.branch, "base_ref": record.base_ref,
                "base_sha": record.base_commit, "dirty_main": dirty or {"modified": 0, "untracked": 0},
                "path_display": display["path_display"], "worktree_id": record.child_id, "name": record.name, **extra})
        self._event(handle, "session.root_set", {**display, **extra})
        if created:
            await self._thread(handle.id, self.service.activate_session, record.child_id, root=self.registry)
        self.runtime._session_roots.pop(handle.id, None)
        self._occupancy_cache = (0.0, {})
        handle._turn_root_agents_key = None
        from .tools.builtin._shell_logs import write_session_cwd
        try:
            write_session_cwd(self.runtime.workspace, handle.id, str(root.path), home=self.runtime._home)
        except OSError:
            self.notice(handle, "Working root selected; shell cwd could not be saved")
        return root

    def _dirty(self):
        from .agents.session_worktrees import status_counts
        return status_counts(self.runtime.workspace)

    # -- turn boundaries ----------------------------------------------------

    def required(self, handle):
        """Whether the turn's tools must be built after ``prepare`` (the root may need recreating)."""
        if getattr(handle, "kind", None) == "manager" or getattr(handle, "_kind", None) == "manager":
            return False
        return self.runtime.session_root(handle.id).kind == "worktree"

    async def prepare(self, handle, lease, content):
        if getattr(handle, "_kind", None) == "manager":
            return
        root = self.runtime.session_root(handle.id)
        if root.kind != "worktree":
            return
        # The checkout may have been removed (merged and cleaned up, or by hand):
        # recreate it from its kept branch.
        record = await self._thread(handle.id, self.service.get_worktree, root.worktree_id,
                                    root=self.registry, parent_workspace=self.runtime.workspace)
        if not root.path.is_dir():
            record = await self._thread(handle.id, self.service.recreate_worktree, root.worktree_id,
                                        root=self.registry, parent_workspace=self.runtime.workspace)
            await self.setup(handle, record, reason="recreated")
        self.runtime.validate_session_root(handle.id)
        await self._turn_started(handle, root, lease.turn_id)

    async def _turn_started(self, handle, root, turn_id):
        info = await self._thread(handle.id, self.service.session_status, root.worktree_id, root=self.registry)
        seq, _ = self._turn_data(handle, turn_id)
        self._emit(handle, "worktree.turn_started", {"head": info.get("head", ""), "seq": seq, "worktree_id": root.worktree_id}, turn_id)

    async def finish(self, handle, terminal):
        try:
            root = self.runtime.session_root(handle.id)
            if root.kind == "worktree":
                await self._close_part(handle, terminal.turn, root)
                await self._return_landed(handle, root)
            pending = self._moves.pop(handle.id, None)
            if pending is not None:
                self._event(handle, "worktree.move_failed", {"request_id": pending.get("request_id"), "to": pending.get("to") or pending["action"],
                    "detail": "The turn ended before the move was applied"})
        except Exception as exc:
            self._emit(handle, "worktree.commit_skipped", {"reason": "git_error", "detail": redact_secrets(str(exc))[:500]}, terminal.turn)
            self.notice(handle, "Worktree action failed: " + str(exc))

    async def _close_part(self, handle, turn_id, root):
        """Close this turn's part in worktree ``root``: shell commits, harness commit, merge detection."""
        seq, _ = self._turn_data(handle, turn_id)
        since = next((e.data.get("head", "") for e in reversed(handle.events)
                      if e.type == "worktree.turn_started" and e.turn == turn_id and e.data.get("worktree_id", root.worktree_id) == root.worktree_id), "")
        if since:
            commits = await self._thread(handle.id, self.service.session_commits, root.worktree_id, root=self.registry, since=since)
            known = {e.data.get("sha") for e in handle.events if e.type == "worktree.committed"}
            for result in commits:
                if result.get("sha") not in known:
                    self._commit_event(handle, {**result, "kind": "committed", "by": "agent", "via": "shell"}, seq=seq, agent="root", turn_id=turn_id)
        paths = self._turn_paths(handle, turn_id, root)
        await self.commit(handle, turn_id, paths=paths)
        await self._detect_landing(handle, root, turn_id)

    async def _detect_landing(self, handle, root, turn_id):
        landing = await self._thread(handle.id, self._landing, handle, root)
        if landing is not None:
            self._emit(handle, "worktree.merged", landing, turn_id)
            self.notice(handle, f"Merged into {landing['target_branch']} @ {landing['sha'][:12]}: {landing['subject']}")

    def _turn_paths(self, handle, turn_id, root):
        """Paths the harness commit may include: ``None`` = everything (this session is alone here).

        With other sessions in the worktree only the files this session wrote in
        the turn are committed, so another session's half-finished edits are
        never swept into the commit.
        """
        try:
            others = self._others_in(handle.id, root.worktree_id)
        except Exception:
            others = [{}]
        if not others:
            return None
        display = root.to_data(self.runtime.workspace)["path_display"]
        touched = []
        for event in handle.events:
            if event.turn != turn_id:
                continue
            if (event.type == "worktree.file_written" and event.data.get("owner_session") == handle.id
                    and event.data.get("root_display") == display):
                touched.append(event.data.get("path"))
            elif event.type == "context.notice" and event.data.get("kind") == "subagent_merged":
                touched.extend(event.data.get("files", []))
        return [path for path in dict.fromkeys(touched) if isinstance(path, str) and path]

    # -- commits ------------------------------------------------------------

    async def commit(self, handle, turn_id, *, message=None, paths=None, agent="root", by="harness"):
        root = self.runtime.session_root(handle.id)
        if root.kind != "worktree":
            raise ValueError("Only a Nexus worktree can be committed; create one with worktree_create")
        if self.runtime.session_has_active_jobs(handle.id):
            payload = {"reason": "git_error", "detail": "Background shell jobs are running; finish them before committing"}
            self._event(handle, "worktree.commit_skipped", payload)
            return {"kind": "skipped", **payload}
        seq, prompt = self._turn_data(handle, turn_id)
        policy = self.policy()
        result = await self._thread(handle.id, self.service.commit_session, root.worktree_id, root=self.registry,
            message=message if message is not None else "nexus: " + (prompt.splitlines()[0] if prompt else "session changes")[:72],
            session_name=self._name(handle), turn_seq=seq, agent_id=agent, paths=paths,
            max_files=policy.commit_max_files, max_file_mb=policy.commit_max_file_mb, by=by, session_id=handle.id)
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
                self._event(handle, "worktree.identity_fallback", {})
                self.notice(handle, "Git identity is unset; session commits use Nexus <nexus@localhost>")
        elif kind == "skipped":
            self._emit(handle, "worktree.commit_skipped", payload, turn_id)
            self.notice(handle, "Worktree commit skipped: " + str(payload.get("detail", payload.get("reason", "Git error"))))

    def unpin_session_turns(self, session):
        """Delete the hidden turn refs of a deleted session (call from session deletion)."""
        from .agents.session_worktrees import delete_turn_refs
        return delete_turn_refs(self.runtime.workspace, session)

    def _checkpoint_snapshot(self, directory, session, seq, suffix, *, base_ref=""):
        """Commit object of a checkout's current files, built from a private index.

        Only the (disabled) subagent-worktree path uses it, to give a child a
        baseline that includes the parent's uncommitted edits. The main-checkout
        turn checkpoints it once served are gone (plan 3.5.1); it never touches
        the user's index or branch.
        """
        from .agents.session_worktrees import _git
        directory = Path(directory)
        common = Path(_git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        index = Path(_git(directory, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        temp = common / f"nexus-checkpoint-index-{uuid.uuid4().hex}"
        safe = ''.join(c for c in session if c.isalnum() or c in '-_')[:190]
        ref = f"refs/nexus/checkpoints/{safe}/{seq:010d}{suffix}"
        env = {"GIT_INDEX_FILE": str(temp), "GIT_AUTHOR_NAME": "Nexus", "GIT_AUTHOR_EMAIL": "nexus@localhost",
               "GIT_COMMITTER_NAME": "Nexus", "GIT_COMMITTER_EMAIL": "nexus@localhost"}
        try:
            if index.exists():
                shutil.copyfile(index, temp)
            else:
                head = _git(directory, "rev-parse", "--verify", "HEAD^{commit}", check=False).strip()
                _git(directory, "read-tree", head if head else "--empty", extra_env={"GIT_INDEX_FILE": str(temp)})
            policy = self.policy()
            status = _git(directory, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            candidates = [row[3:] for row in status.split("\0") if len(row) >= 4]
            if len(candidates) > policy.commit_max_files:
                raise OverflowError("changed file count exceeds configured limit")
            for relative in candidates:
                path = directory / relative
                if not path.is_symlink() and path.is_file() and path.stat().st_size > policy.commit_max_file_mb * 1024 * 1024:
                    raise OverflowError("a changed file exceeds configured size limit")
            _git(directory, "add", "-A", "--", ".", extra_env={"GIT_INDEX_FILE": str(temp)})
            tree = _git(directory, "write-tree", extra_env={"GIT_INDEX_FILE": str(temp)})
            if base_ref:
                base_tree = _git(directory, "rev-parse", "--verify", base_ref + "^{tree}", check=False).strip()
                if base_tree and base_tree == tree:
                    return "", "", "", []
            head = _git(directory, "rev-parse", "--verify", "HEAD^{commit}", check=False).strip()
            parents = ["-p", head] if head else []
            commit = _git(directory, "commit-tree", tree, *parents, "-m", f"nexus snapshot {session} {seq}", extra_env=env)
            _git(directory, "update-ref", ref, commit, "")
            return ref, commit, head, []
        finally:
            temp.unlink(missing_ok=True)
            Path(str(temp) + ".lock").unlink(missing_ok=True)

    # -- setup (copy / submodules / command) --------------------------------

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
        self._event(handle, "worktree.setup_trust", {"digest": digest, "decision": decision})
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
            self._event(handle, "worktree.setup_ran", {"command": self._display_command(command), "exit": None, "reason": reason,
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
        self._event(handle, "worktree.setup_ran", {"command": self._display_command(command), "exit": code,
            "duration_ms": int((time.monotonic()-started)*1000), "copied": copied[:50], "copied_clipped": max(0,len(copied)-50),
            "shared": shared[:50], "submodules": policy.submodules, "reason": reason,
            "output_tail": redact_secrets(output.decode(errors="replace"))[-8192:]})
        if code:
            if policy.setup_on_fail == "fail":
                raise ValueError(f"Worktree setup failed (exit {code}); {output.decode(errors='replace')[-500:]}")
            self.notice(handle, f"Worktree setup failed (exit {code}); continuing per setup_on_fail=warn")

    # -- file provenance (what this session wrote) ---------------------------

    def record_write(self, session, turn_id, call_id, path, data, before=None):
        root = self.runtime.session_root(session)
        path = Path(path)
        try:
            relative = path.relative_to(root.path).as_posix()
        except ValueError:
            return
        self._plain_path(root.path, relative)
        handle = self.handle(session)
        # One receipt per successful atomic write; only successful tool results
        # authorize carrying files over, and in a shared worktree they bound the
        # harness commit to this session's own paths. Bytes never enter the log,
        # only their digest.
        receipts = [e for e in handle.events if e.type == "worktree.file_written" and e.turn == turn_id]
        if len(receipts) >= 500:
            if not any(e.type == "worktree.provenance_clipped" and e.turn == turn_id for e in handle.events):
                self._emit(handle, "worktree.provenance_clipped", {"owner_session": session}, turn_id)
            return
        self._emit(handle, "worktree.file_written", {"path": relative, "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
            "deleted": data is None, "call_id": call_id, "owner_session": session, "before_oids": before,
            "after_oids": self._blob_oids(data), "root_display": root.to_data(self.runtime.workspace)["path_display"],
            "worktree_id": root.worktree_id}, turn_id)

    @staticmethod
    def _blob_oids(data):
        if data is None:
            return None
        header = f"blob {len(data)}\0".encode()
        hashes = {"sha1": hashlib.sha1(header), "sha256": hashlib.sha256(header)}
        for value in hashes.values():
            value.update(data)
        return {key: value.hexdigest() for key, value in hashes.items()}

    def _dirty_paths(self, directory):
        status = self._git(directory, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        dirty = set()
        rows = iter(status.split("\0"))
        for row in rows:
            if not row:
                continue
            dirty.add(row[3:])
            if row[0] in {"R", "C"} or row[1] in {"R", "C"}:
                dirty.add(next(rows, ""))
        return dirty

    def _owned_changes(self, handle, root):
        display = root.to_data(self.runtime.workspace)["path_display"]
        successful = {(e.turn, e.data.get("tool_use_id")) for e in handle.events if e.type == "tool.result" and not e.data.get("is_error")}
        receipts = {}
        for event in handle.events:
            if event.type == "worktree.promoted":
                receipts.clear()
            elif event.type == "worktree.provenance_clipped" and event.data.get("owner_session") == handle.id:
                raise ValueError("Can't separate session changes: file provenance exceeded 500 writes")
            elif (event.type == "worktree.file_written" and (event.turn, event.data.get("call_id")) in successful
                    and event.data.get("owner_session") == handle.id and event.data.get("root_display") == display):
                receipts.setdefault(event.data["path"], []).append(event.data)
        if len(receipts) > 500:
            raise ValueError("Carrying changes over is bounded to 500 files")
        dirty = self._dirty_paths(root.path)
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
            source = self._plain_path(root.path, relative)
            data = None
            if source.exists():
                if not source.is_file() or source.stat().st_size > self.policy().commit_max_file_mb * 1024 * 1024:
                    raise ValueError(f"Can't carry over non-file or oversized {relative}")
                with source.open("rb") as stream:
                    data = stream.read(self.policy().commit_max_file_mb * 1024 * 1024 + 1)
                total += len(data)
                if total > 128 * 1024 * 1024:
                    raise ValueError("Carrying changes over exceeds 128 MiB; commit or move large files yourself")
            digest = hashlib.sha256(data).hexdigest() if data is not None else None
            if digest != receipt.get("sha256"):
                raise ValueError(f"Can't separate this session's changes in {relative}; commit or move them yourself")
            owned[relative] = data
        return owned, sorted(dirty - owned.keys())[:500]

    # -- moves: request, validate, apply ------------------------------------

    @staticmethod
    def _check_text(value, label, limit):
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise ValueError(f"{label} must be a string of at most {limit} characters")
        return value.strip()

    async def _leave_check(self, handle, root, *, in_turn, turn_id=""):
        """Refuse to leave a worktree with changes nobody will commit."""
        if root.kind != "worktree" or not root.path.is_dir():
            return
        dirty = await self._thread(handle.id, self._dirty_paths, root.path)
        if not dirty:
            return
        if in_turn:
            paths = self._turn_paths(handle, turn_id, root)
            if paths is None:
                return  # the harness commits everything at the step boundary
            dirty = dirty - set(paths)
            if not dirty:
                return
        shown = ", ".join(sorted(dirty)[:5]) + (f" and {len(dirty) - 5} more" if len(dirty) > 5 else "")
        raise ValueError(f"Worktree {root.path.name} has uncommitted changes ({shown}). "
                         "Commit them with worktree_commit (or git), then retry the move.")

    async def _validate(self, handle, request, *, in_turn, turn_id=""):
        if not self.is_git:
            raise ValueError("Worktrees need a Git repository")
        request["reason"] = self._check_text(request.get("reason", ""), "reason", 500)
        if not request["reason"]:
            raise ValueError("A reason is required")
        if self.runtime.session_has_active_jobs(handle.id):
            raise SessionBusy("A background shell or job of this session is still running; stop or finish it before moving")
        root = self.runtime.session_root(handle.id)
        if root.kind == "external":
            raise ValueError("This session works in an external checkout; it can't move between Nexus worktrees")
        if request["action"] == "create":
            name = self._check_text(request.get("name", ""), "name", 60)
            base = self._check_text(request.get("base", ""), "base", 200)
            if base and (base.startswith("-") or any(ord(c) < 32 for c in base)):
                raise ValueError("base must be a local branch name")
            request["name"], request["base"] = name, base
            if name:
                await self._thread(handle.id, self.service.check_session_name_free, self.runtime.workspace, name, root=self.registry)
            if await self._thread(handle.id, self._present_count) >= self.policy().max_worktrees:
                raise ValueError(self._limit_message())
        else:
            to = self._check_text(request.get("to", ""), "to", 200)
            request["to"] = to
            if to == "main":
                if root.kind == "main":
                    raise ValueError("The session is already in the main checkout")
            else:
                record = await self._thread(handle.id, self._find_record, to)
                if root.kind == "worktree" and record.child_id == root.worktree_id:
                    raise ValueError(f"The session is already in worktree {record.name}")
        await self._leave_check(handle, root, in_turn=in_turn, turn_id=turn_id)

    async def request_move(self, ctx, *, action, reason, to="", name="", base=""):
        """Tool entry: validate, queue, return at once. Applied at the next step boundary."""
        handle = self.handle(ctx.session_id)
        if not handle.active or handle.active_turn_id != ctx.turn_id:
            raise ValueError("Worktree moves can only be requested during a turn")
        if handle.id in self._moves:
            raise ValueError("A worktree move is already queued for this step; wait for it to apply before requesting another")
        request = {"action": action, "reason": reason, "to": to, "name": name, "base": base}
        await self._validate(handle, request, in_turn=True, turn_id=ctx.turn_id)
        request.update(request_id=ctx.call_id, turn=ctx.turn_id)
        self._moves[handle.id] = request
        self._emit(handle, "worktree.move_requested", {"request_id": ctx.call_id, "action": action, "to": request["to"],
            "name": request["name"], "base": request["base"], "reason": redact_secrets(request["reason"])}, ctx.turn_id)
        target = "a new worktree" if action == "create" else ("the main checkout" if request["to"] == "main" else f"worktree {request['to']}")
        return (f"move: queued\ntarget: {target}\nreason: {request['reason']}\n"
                "The move happens after this step. Do not run tools that depend on the new directory in the same step; "
                "other tool calls of this step still run in the old directory.")

    async def move_session(self, session, to, *, reason="", action="move", name="", base="", continuation=""):
        """Idle-only entry (user override from the host): validate and apply now."""
        handle = self.handle(session)
        self._idle(handle)
        handle._lock.acquire(shared=False, blocking=False)
        try:
            if self.runtime.sessions._has_pending_work(session):
                raise SessionBusy("Moving a session requires a turn boundary without pending work")
            request = {"action": action, "reason": reason or "requested by the user", "to": to, "name": name, "base": base,
                       "request_id": "", "continuation": continuation[:16000] if isinstance(continuation, str) else ""}
            await self._validate(handle, request, in_turn=False)
            try:
                await self._apply(handle, request, in_turn=False, held=True)
            except BaseException as exc:
                self._event(handle, "worktree.move_failed", {"to": request["to"] or action, "detail": redact_secrets(str(exc))[:500]})
                raise
        finally:
            handle._lock.release()
        return self.status(session)

    async def promote(self, session, *, reason="", continuation="", name="", base=""):
        """Create a worktree for an idle session and move into it (UI / launch override)."""
        return await self.move_session(session, "", reason=reason or "requested by the user", action="create",
                                       name=name, base=base, continuation=continuation)

    async def detach(self, session):
        return await self.move_session(session, "main", reason="requested by the user")

    async def step_boundary(self, handle):
        """Safe point between model steps: detect merges, then apply a queued move.

        Runs after every tool call of the step finished and before the next
        model request, so compaction and queued steers never land between the
        request and the move. Returns whether the session's root changed (the
        caller then rebuilds the turn's per-root state).
        """
        if getattr(handle, "kind", None) in ("manager", "child") or getattr(handle, "_kind", None) in ("manager", "child"):
            return False
        root = self.runtime.session_root(handle.id)
        turn_id = handle.active_turn_id
        pending = self._moves.pop(handle.id, None)
        try:
            if root.kind == "worktree":
                await self._detect_landing(handle, root, turn_id)
            if pending is None:
                return False
            await self._apply(handle, pending, in_turn=True)
        except (OperationCancelled, asyncio.CancelledError):
            raise
        except Exception as exc:
            if pending is not None:
                detail = redact_secrets(str(exc))[:500]
                self._emit(handle, "worktree.move_failed", {"request_id": pending.get("request_id"),
                    "to": pending.get("to") or pending["action"], "detail": detail}, turn_id)
                self._model_notice(handle, f"The requested worktree move failed and the working directory is unchanged: {detail}", turn_id)
        return self.runtime.session_root(handle.id) != root

    def _model_notice(self, handle, text, turn_id):
        """Durable in-band notice the model reads (kept in history; facts are re-rendered every step)."""
        handle.append_message(Message(role="user", content=[Text(text)],
                                      meta=MessageMeta(turn_id=turn_id or None, notice=_NOTICE_KIND)))
        self._event(handle, "context.notice", {"kind": _NOTICE_KIND, "model_visible": True, "message": redact_secrets(text)[:2000]})

    async def _apply(self, handle, request, *, in_turn, held=False):
        action = request["action"]
        reason = request.get("reason", "")
        request_id = request.get("request_id", "")
        turn_id = handle.active_turn_id if in_turn else ""
        policy = self.policy()
        workspace = self.runtime.workspace
        if in_turn:
            handle.cancel_token.raise_if_cancelled()
        if self.runtime.session_has_active_jobs(handle.id):
            raise SessionBusy("Finish background shells and jobs before moving")
        source = self.runtime.session_root(handle.id)
        source_ok = True
        try:
            self.runtime.validate_session_root(handle.id)
        except Exception:
            if in_turn:
                raise
            source_ok = False  # a vanished root recovers while idle
        source_record = None
        left_state = {"merged": False, "unmerged_commits": 0, "has_work": False}
        left_name = source.path.name if source.kind == "worktree" else "main"
        if source.kind == "worktree" and source_ok:
            source_record = await self._thread(handle.id, self.service.get_worktree, source.worktree_id,
                                               root=self.registry, parent_workspace=workspace)
            if in_turn:
                await self._close_part(handle, turn_id, source)
            await self._leave_check(handle, source, in_turn=False)
            left_state = await self._thread(handle.id, self._merge_state, source_record)

        extra = {"reason": redact_secrets(reason)[:500], "request_id": request_id, "action": action}
        record = None
        created = False
        owned: dict = {}
        excluded: list = []
        dirty = {"modified": 0, "untracked": 0}
        if action == "move" and request["to"] == "main":
            target_root = None
        elif action == "move":
            record = await self._thread(handle.id, self._find_record, request["to"])
            if not Path(record.path).is_dir():
                if await self._thread(handle.id, self._present_count) >= policy.max_worktrees:
                    raise ValueError(self._limit_message())
                record = await self._thread(handle.id, self.service.recreate_worktree, record.child_id,
                                            root=self.registry, parent_workspace=workspace)
                await self.setup(handle, record, reason="recreated", honor_cancel=in_turn)
            target_root = True
        else:
            created = True
            target_root = True
            base = request.get("base", "")
            if not base:
                base = (self._base_branch(source) or "current") if source.kind == "worktree" else "current"
            carry = source.kind == "main" and base in {"current", self._main_branch()} and source_ok
            base_sha = await self._thread(handle.id, self._git, workspace, "rev-parse", "--verify", "HEAD^{commit}") if carry else ""
            if carry:
                dirty = await self._thread(handle.id, self._dirty)
                owned, excluded = await self._thread(handle.id, self._owned_changes, handle, source)
            name = request.get("name", "")
            try:
                record = await self._thread(handle.id, self.service.create_session, workspace, handle.id, self._name(handle),
                    root=self.registry, base=base, max_worktrees=policy.max_worktrees,
                    **({"exact_name": name} if name else {"random_words": True}))
            except Exception as exc:
                if "maximum session worktrees" in str(exc):
                    raise ValueError(self._limit_message()) from exc
                raise
        published = False
        try:
            if created:
                if carry and record.base_commit != base_sha:
                    raise ValueError("Main checkout HEAD moved while preparing the worktree; retry")
                await self.setup(handle, record, reason="created", honor_cancel=in_turn)
                if in_turn:
                    handle.cancel_token.raise_if_cancelled()
                if carry:
                    # Revalidate source hashes immediately before copying. No source
                    # file is restored, reset, staged or committed in the main checkout.
                    current, _ = await self._thread(handle.id, self._owned_changes, handle, source)
                    if current != owned:
                        raise ValueError("Session files changed while the worktree was being prepared")
                    await self._carry_over(handle, record, owned, turn_id)
            if target_root is None:
                root = self.runtime.set_session_root(handle.id, workspace, kind="main", held=True, event_extra=extra)
            else:
                root = await self._publish_root(handle, record, created=created, dirty=dirty, extra=extra)
            published = True
        finally:
            if created and not published:
                await self._thread(handle.id, self.service.abort_session, record.child_id, root=self.registry)

        to_name = "main" if root.kind == "main" else root.path.name
        self._occupancy_cache = (0.0, {})
        self._event(handle, "worktree.moved", {
            "action": action, "reason": redact_secrets(reason)[:500], "request_id": request_id,
            "from_kind": source.kind, "from_worktree_id": source.worktree_id, "from_name": left_name,
            "to_kind": root.kind, "to_worktree_id": root.worktree_id, "to_name": to_name,
            "branch": root.branch, "base_ref": root.base_ref, "base_sha": root.base_sha,
            "kept_unmerged": bool(source.kind == "worktree" and left_state["has_work"] and not left_state["merged"]),
            "unmerged_commits": left_state["unmerged_commits"] if source.kind == "worktree" else 0})
        if created and source.kind == "main":
            self._event(handle, "worktree.promoted", {"reason": redact_secrets(reason), "request_id": request_id, "files": list(owned),
                "excluded": excluded, "source_display": source.to_data(workspace)["path_display"]})
        if request.get("continuation"):
            handle.enqueue(request["continuation"], origin={"kind": "promote", "request_id": request_id})
        try:
            if in_turn:
                self._model_notice(handle, self._move_text(root, action, left_name, source, left_state, reason, owned, excluded), turn_id)
                if root.kind == "worktree":
                    await self._turn_started(handle, root, turn_id)
            elif owned or excluded:
                self.notice(handle, f"Moved to {root.branch}. Files carried over remain modified in the main checkout: {', '.join(owned) or '(none)'}. "
                                    f"Excluded other changes: {', '.join(excluded) or '(none)'}")
            if source_record is not None and source_record.child_id != root.worktree_id:
                await self._cleanup_left(handle, source_record)
        except Exception as exc:
            # The move stands (root_set is durable); the next tool call re-validates the root.
            self.notice(handle, "Worktree move applied, but follow-up failed: " + redact_secrets(str(exc))[:300])
        return root

    async def _carry_over(self, handle, record, owned, turn_id):
        for relative, data in owned.items():
            target = self._plain_path(Path(record.path), relative)
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        if not owned:
            return
        policy = self.policy()
        seq, _ = self._turn_data(handle, turn_id or "")
        result = await self._thread(handle.id, self.service.commit_session, record.child_id, root=self.registry,
            message="nexus: carry over changes from the main checkout", session_name=self._name(handle),
            turn_seq=seq, agent_id="root", paths=list(owned), max_files=policy.commit_max_files,
            max_file_mb=policy.commit_max_file_mb, by="harness", allow_pending=True, session_id=handle.id)
        if result.get("kind") not in {"committed", "unchanged"}:
            raise ValueError("Could not commit carried session changes: " + str(result.get("detail", result.get("kind"))))
        if result.get("kind") == "committed":
            self._commit_event(handle, result, seq=seq, agent="root", turn_id=turn_id)

    def _move_text(self, root, action, left_name, source, left_state, reason, owned, excluded):
        sha = (root.base_sha or "")[:7]
        if root.kind == "main":
            text = f"Working directory is now the main checkout (`{root.path}`)."
            if source.kind == "worktree":
                if left_state["has_work"] and not left_state["merged"]:
                    text += (f" You left worktree {left_name}; its branch `{source.branch}` is kept unmerged "
                             f"({left_state['unmerged_commits']} commits not in the base).")
                elif left_state["merged"]:
                    text += f" You left worktree {left_name}; its work is merged."
                else:
                    text += f" You left worktree {left_name}; it has no commits of its own."
        else:
            base = self._base_branch(root) or root.base_ref
            text = (f"Working directory is now ⎇ {root.path.name} (`{root.path}`, branch `{root.branch}`, "
                    f"base `{base} @ {sha}`).")
            if action == "create":
                text += f" Created from {left_name}."
                if owned:
                    text += f" Carried over from the main checkout: {', '.join(list(owned)[:10])}. They remain modified there."
            if source.kind == "worktree" and left_state["has_work"] and not left_state["merged"]:
                text += f" Worktree {left_name} stays unmerged (branch `{source.branch}`)."
        if reason:
            text += f" Reason: {redact_secrets(reason)[:300]}"
        return text + " Tool calls and shell commands now run there."

    async def _return_landed(self, handle, root):
        """Return only a clean, landed, idle session; never discard local work."""
        if not self.policy().remove_after_landing or self.runtime.session_has_active_jobs(handle.id):
            return
        if handle.queued_ids or handle.id in self._moves or self._others_in(handle.id, root.worktree_id):
            return
        record = await self._thread(handle.id, self.service.get_worktree, root.worktree_id,
                                    root=self.registry, parent_workspace=self.runtime.workspace)
        state = await self._thread(handle.id, self._merge_state, record)
        if not state["merged"] or not state["has_work"]:
            return
        info = await self._thread(handle.id, self.service.session_status, root.worktree_id, root=self.registry)
        if info.get("missing") or any(info.get("uncommitted", {}).values()):
            return
        from .worktree_cleanup import _ignored_paths, _operation_markers
        if await self._thread(handle.id, _ignored_paths, root.path):
            return
        if await self._thread(handle.id, _operation_markers, root.path):
            return
        if info.get("branch") != record.branch:
            return
        await self._apply(handle, {"action": "move", "to": "main",
                                  "reason": "landed"},
                          in_turn=False, held=True)
        self._model_notice(
            handle, "Working directory is now the main checkout; the worktree work is merged.",
            handle.active_turn_id,
        )

    def archive_landed(self, session):
        """Runtime-owned archive hook; reuse the turn-boundary lifecycle under its lock."""
        handle = self.handle(session)
        root = self.runtime.session_root(session)
        if root.kind != "worktree" or not self.policy().remove_after_landing:
            return
        self._idle(handle)
        handle._lock.acquire(shared=False, blocking=False)
        try:
            if self.runtime.sessions._has_pending_work(session):
                return
            asyncio.run(self._return_landed(handle, root))
        finally:
            handle._lock.release()

    async def _cleanup_left(self, handle, record):
        """Remove the checkout of a merged, clean worktree once its last session left."""
        from .worktree_cleanup import auto_cleanup
        result = await self._thread(handle.id, auto_cleanup, self, record.child_id, leaving=handle.id)
        if result.get("state") == "removed":
            self._event(handle, "worktree.removed", {"worktree_id": record.child_id, "branch": record.branch,
                "name": record.name, "reason": "merged and clean; last session left"})

    # -- merge detection -----------------------------------------------------

    def _landing(self, handle, root):
        """Payload for ``worktree.merged`` when the agent landed the worktree branch with Git; else ``None``.

        The agent runs ``git -C <main checkout> merge --ff-only <worktree branch>``;
        the host notices instead of being told (checked at every step boundary and
        at turn end). A landing means the worktree head has commits beyond the
        recorded base commit and is now an ancestor of (or equal to) the target
        branch head. ``head`` (the landed head) dedupes: one event per landed head
        per session, so a later move of the target does not repeat it. Best effort:
        any Git failure returns ``None``.
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
            landed = [e for e in handle.events if e.type == "worktree.merged" and e.data.get("session_branch") == record.branch]
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
                "clipped": len(names) > 200 or len(rows) > 100, "head": head, "worktree_id": record.child_id}

    # -- model tools ----------------------------------------------------------

    def tools(self, session):
        if not self.is_git:
            return ()
        handle = self.handle(session)
        if getattr(handle, "kind", None) == "manager" or getattr(handle, "_kind", None) == "manager":
            return ()
        from .tools.builtin.worktree import build_worktree_tools
        in_worktree = self.runtime.session_root(session).kind == "worktree"

        def check(ctx):
            if ctx.session_id != session:
                raise ValueError("Session worktree tools cannot act for another session")

        async def listing(args, ctx):
            check(ctx)
            return await self._thread(session, self.list, session, self.occupancy(fresh=True))

        async def commit(args, ctx):
            check(ctx)
            guard = self.runtime.session_path_guard(session)
            paths = args.get("paths")
            if paths is not None:
                paths = [str(guard.resolve(path, for_write=True).absolute.relative_to(ctx.workspace)) for path in paths]
            return await self.commit(self.handle(session), ctx.turn_id, message=args["message"], paths=paths,
                                     agent=ctx.agent_id, by="agent")

        async def create(args, ctx):
            check(ctx)
            return await self.request_move(ctx, action="create", reason=args["reason"],
                                           name=args.get("name", ""), base=args.get("base", ""))

        async def move(args, ctx):
            check(ctx)
            return await self.request_move(ctx, action="move", reason=args["reason"], to=args["to"])

        return build_worktree_tools(listing, commit, create, move, in_worktree=in_worktree)

    # -- recovery ---------------------------------------------------------------

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
                from .agents.session_worktrees import _is_current
                records = list((self.registry / "records").glob("*.json"))
                if len(records) > 10000:
                    raise ValueError("Worktree recovery exceeds 10,000 records")
                created = set()
                for path in records:
                    payload = json.loads(worktrees._read_metadata_bytes(path)).get("record", {})
                    if not _is_current(payload):
                        continue
                    # The creating session logged worktree.created for the branch
                    # once the checkout was published. Subagent child ids
                    # (`<parent>/sub/<n>`) are not root session ids.
                    session = payload.get("created_by", payload.get("session_id"))
                    if (not isinstance(session, str) or not is_valid_session_id(session)
                            or not self.runtime.sessions.exists(session)):
                        continue
                    handle = self.handle(session)
                    if any(e.type == "worktree.created" and e.data.get("branch") == payload.get("branch") for e in handle.events):
                        created.add(session)
                recovered = await self._thread("", self.service.recover_sessions, root=self.registry, created_ids=created, pending_ids=set())
                for record in recovered:
                    session = record.created_by
                    if session not in created or record.lifecycle != "active":
                        continue
                    handle = self.handle(session)
                    root = self.runtime.session_root(session)
                    if root.kind != "worktree":
                        created_at = max((i for i, e in enumerate(handle.events) if e.type == "worktree.created" and e.data.get("branch") == record.branch), default=-1)
                        root_at = max((i for i, e in enumerate(handle.events) if e.type == "session.root_set"), default=-1)
                        if root_at > created_at:
                            continue  # the session moved on after creating it
                        await self._publish_root(handle, record, created=False)
                        root = self.runtime.session_root(session)
                    if root.worktree_id == record.child_id and record.path.is_dir():
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
