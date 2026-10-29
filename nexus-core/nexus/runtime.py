"""The daemon's state and work: projects, sessions, turns and approvals.

Everything the HTTP API does goes through `Runtime`, and everything it changes
is published on the bus:

| event | when |
| --- | --- |
| `project.created` / `project.updated` / `project.deleted` | project changes (global log entries) |
| `session.created` / `session.updated` / `session.deleted` | session changes; `deleted` is a minimal global tombstone |
| `session.status` | `{"status": "busy" | "idle"}` around each turn |
| `message.added` | a message was saved (user, assistant, tool) |
| `permission.asked` / `permission.replied` | a tool call waits for approval |
| `question.asked` / `question.replied` | the `ask` tool waits for user answers; `replied` also carries cancellation |
| `session.workspace_tools` | the workspace control tools were activated for a session |
| `session.message_tool` | `message_session` was exposed to a session by its first `session` call |
| `session.directory_changed` | a session's working directory moved (with the destination instructions) |
| `worktree.listed` / `worktree.created` / `worktree.deleted` | worktree control operations |
| `session.error` | a turn failed (provider, auth or tool-loop error) |
| everything else | the loop and stream events, unchanged (`wire_request`, `text_delta`, `tool_start`, …) |

Each session freezes its agent, rendered prompt, tool definitions and child
catalog at creation (`nexus/snapshot.py`). Turns replay that snapshot rather
than reloading files, so agent edits affect new sessions only; explicit model,
effort, agent and tool-switch changes apply to subsequent calls. The snapshot also
carries the labelled `AGENTS.md` section from the session directory. An old
session without a snapshot is reconstructed once with a visible note.

One turn runs per session at a time. Interrupting cancels the turn, and the
turns of every descendant session. A tool call left without a result gets an
"interrupted" error result, so the next request stays valid.

Session trees: an agent whose file lists `control: [session]` gets the
`session` tool, but only in a root session (children never get it, so trees
are one level deep). The tool starts a child session in the same directory,
with `launched_by: "agent"`, `parent_id` and `parent_call_id` set. With
`wait: true` the tool returns the child's answer. With `wait: false` it returns
at once, and the answer is delivered to the parent when the child finishes: it
is added before the parent's next model call if the parent is busy, or starts a
new parent turn if it is idle (`session.notified`). The first `session` call
also exposes `message_session` (once per session, persisted in the snapshot): it
sends a message to a child the caller launched. A working child receives it
before its next model call (a steer); an idle child starts a new turn on its
existing conversation. Either way its answer comes back as a notification.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from . import agents as agent_files
from . import questions as question_contract
from . import snapshot as snapshots
from . import workspace as workspace_control
from . import worktrees
from .bash_jobs import NOTICE_CHARS, Job, Jobs
from .bus import Bus
from .codec import message_to_json, to_json
from .continuation import ContextInvalid, Reconciliation, predecessor, reconcile
from .kernels import Kernels
from .llmhub.auth import AuthError, CredentialError
from .llmhub.hub import provider_for
from .llmhub.types import (
    Done,
    Message,
    PartDone,
    ProviderError,
    Reasoning,
    ReasoningDelta,
    Text,
    TextDelta,
    ToolCall,
    ToolCallStarted,
    ToolResult,
    ToolSpec,
    Usage,
)
from .loop import (
    MAX_STEPS,
    NO_USER_MESSAGE,
    Agent,
    MessageAdded,
    NoUserMessage,
    RetryExhausted,
    RetryScheduled,
    RetryStarted,
    StepStart,
    ToolEnd,
    ToolStart,
    TurnEnd,
)
from .observations import FileGuard, FileObservation
from .questions import PendingQuestion, QuestionError
from .recovery import Recovery, open_approvals
from .retention import Retention
from .retry import RetryPolicy
from .store import Project, Session, Store, StoredMessage, new_id
from .tools import BASH_JOB_TOOLS, DEFAULT_TOOLS, Tool, ToolContext, ToolError
from .tools import bash as bash_tool
from .tools.base import clip

AUTO_APPROVED = {"read", "bash_output", "bash_kill", "session", agent_files.MESSAGE_TOOL, "code", "ask", "edit", "write",
                 *workspace_control.CONTROL_NAMES}
CHILD_RESULT_CHARS = 30_000
TITLE_CHARS = 60
MAX_RESOLVED_QUESTIONS = 256  # tombstones so a late or duplicate reply gets 409, not a second event


class Busy(Exception):
    pass


class Conflict(Exception):
    def __init__(self, message: str, *, code: str | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class RevisionConflict(Conflict):
    """A mutation expected an older resource revision and changed nothing."""

    def __init__(self, message: str, *, current: int, resource: str) -> None:
        super().__init__(message, code="revision_conflict",
                         payload={"revision": current, "current_revision": current, "resource": resource})
        self.current = current
        self.resource = resource


class NotFound(Exception):
    pass


def project_json(p: Project) -> dict[str, Any]:
    return {"id": p.id, "path": p.path, "name": p.name, "created": p.created, "updated": p.updated,
            "revision": p.revision}


def message_json(m: StoredMessage) -> dict[str, Any]:
    return {"id": m.id, "session_id": m.session_id, "seq": m.seq, "created": m.created, **message_to_json(m.message), **({"delivery": m.delivery} if m.delivery else {})}


class ReasoningClock:
    """Thinking time of one provider request.

    A reasoning part runs from its first delta to its `PartDone`, or to the
    first delta of another part when the dialect sends `PartDone` late (chat).
    """

    def __init__(self) -> None:
        self.open: float | None = None
        self.total: float | None = None

    def delta(self) -> None:
        if self.open is None:
            self.open = time.monotonic()

    def close(self) -> None:
        if self.open is not None:
            self.total = (self.total or 0.0) + time.monotonic() - self.open
            self.open = None

    def seconds(self) -> float | None:
        """Total so far, closing an open part; `None` when no reasoning streamed."""
        self.close()
        return round(self.total, 3) if self.total is not None else None


class Runtime:
    def __init__(self, store: Store, bus: Bus, default_model: str) -> None:
        self.kernels = Kernels()
        self.jobs = Jobs()
        self.store = store
        from .inputs import Inputs
        self.inputs = Inputs(store)
        self.bus = bus
        self.default_model = default_model
        self.turns: dict[str, asyncio.Task[None]] = {}
        self.approvals: dict[str, tuple[str, asyncio.Future[bool], dict[str, Any]]] = {}  # id -> (session, future, request)
        self.questions: dict[str, PendingQuestion] = {}  # qst id -> pending question request
        self.resolved_questions: dict[str, str] = {}  # qst id -> "answered" | "cancelled" (bounded tombstones)
        self.inboxes: dict[str, list[str]] = {}  # session id -> notices waiting for its next model call
        self.notify: dict[str, str] = {}  # child id -> parent id, for children started with wait: false
        self.errors: dict[str, str] = {}  # session id -> how its last turn failed
        self._parents: dict[str, str | None] = {}  # session id -> parent id (never changes)
        self.pending_reads: dict[str, list[FileObservation]] = {}
        self.removing_worktrees: set[Path] = set()
        self.closing_sessions: set[str] = set()
        self.pending_directories: dict[str, dict[str, Any]] = {}  # session id -> a validated, not-yet-applied move
        self.file_lock = asyncio.Lock()  # serializes Nexus file mutations for this daemon
        self.worktree_lock = asyncio.Lock()  # serializes create/delete so they cannot conflict
        self._mutation_locks: dict[str, tuple[asyncio.Lock, int]] = {}  # same idempotency key shares one acceptance
        self.retry_policy = RetryPolicy.from_env()
        self.retention = Retention(self)
        self.mutation_lifetime = self.retention.settings.mutation_lifetime
        bus.attach(store)  # every published event is durable before any subscriber sees it
        self.recovered: list[dict[str, Any]] = []

    def recover(self) -> list[dict[str, Any]]:
        """Reconcile turns a crash left unfinished. Runs before the daemon serves requests."""
        # Never resume queued work during startup; any orphaned steer is held.
        pending = self.inputs.list()
        with self.store.db:
            for item in pending:
                self.store.db.execute("update pending_input set turn_id = null where id = ?", (item["id"],))
                if item["mode"] == "steer":
                    self.store.db.execute("update pending_input set mode = 'queue' where id = ?", (item["id"],))
                self.store.db.execute("insert or ignore into input_hold values (?)", (item["session_id"],))
        self.recovered = Recovery(self).run()
        for row in self.store.db.execute("select * from pending_input where state = 'delivered' and event_id is null").fetchall():
            sid, iid = row["session_id"], row["id"]
            if not self.store.has_event("input.delivered", {"id": iid}, session_id=sid):
                item = self.inputs._json(row)
                event = self.bus.publish("input.delivered", {**item, "message_id": row["message_id"], "turn_id": row["turn_id"]}, session_id=sid)
                event_id = event.get("id", -1)
            else:
                event_id = -1
            with self.store.db:
                self.store.db.execute("update pending_input set event_id = ? where id = ?", (event_id, iid))
        self.store.resolve_pending_mutations(lifetime=self.mutation_lifetime)
        return self.recovered

    async def start_maintenance(self) -> None:
        """Prune expired diagnostics in a background task; the first pass is synchronous."""
        self.retention.run_once()
        self.retention.start()

    # -- idempotent mutations --------------------------------------------------

    @asynccontextmanager
    async def mutation_lock(self, key: str) -> AsyncIterator[None]:
        # Count owners and waiters. An unlocked lock can still have a waiter
        # scheduled to acquire it; eviction based on locked() alone races that
        # handoff and can create two independent locks for the same key.
        lock, users = self._mutation_locks.get(key, (asyncio.Lock(), 0))
        self._mutation_locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, remaining = self._mutation_locks[key]
            if remaining == 1:
                self._mutation_locks.pop(key)
            else:
                self._mutation_locks[key] = (lock, remaining - 1)

    def replay_mutation(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        """The stored acceptance for a key, or None; a different payload is a conflict."""
        entry = self.store.mutation(key)
        if entry is None:
            return None
        if entry["state"] == "resolved" and entry["expires"] is not None and entry["expires"] < time.time():
            self.store.delete_mutation(key)  # the deduplication guarantee has expired
            return None
        if entry["fingerprint"] != fingerprint:
            raise Conflict(
                f"idempotency key {key!r} was already used for {entry['operation']} {entry['target']}; "
                "reusing a key for a different request is refused",
                code="idempotency_key_reused",
                payload={"key": key, "operation": entry["operation"], "target": entry["target"]},
            )
        if entry["state"] == "pending":
            raise Conflict(
                f"keyed operation {entry['operation']} {entry['target']} has no recorded outcome; "
                "inspect the target before retrying with a new key",
                code="mutation_outcome_unknown",
                payload={"key": key, "operation": entry["operation"], "target": entry["target"]},
            )
        return entry

    def record_mutation(self, key: str, fingerprint: str, operation: str, target: str, *,
                        status: int | None, body: Any, turn_id: str | None = None,
                        resource: str | None = None) -> dict[str, Any]:
        return self.store.record_mutation(
            key, fingerprint, operation, target, status=status, body=body, turn_id=turn_id,
            resource=resource, lifetime=self.mutation_lifetime)

    def begin_external_mutation(self, key: str, fingerprint: str, operation: str, target: str,
                                *, resource: str | None = None) -> dict[str, Any]:
        return self.store.begin_mutation(key, fingerprint, operation, target, resource=resource)

    def finish_external_mutation(self, key: str, *, status: int, body: Any,
                                 resource: str | None = None) -> dict[str, Any]:
        return self.store.finish_mutation(key, status=status, body=body, resource=resource,
                                          lifetime=self.mutation_lifetime)

    def discard_mutation(self, key: str) -> None:
        self.store.discard_mutation(key)

    # -- projects ------------------------------------------------------------

    def open_project(self, path: str, name: str | None = None) -> Project:
        self._check_removing_worktree(Path(path).expanduser())
        project, created = self.store.project_for_path(path, name)
        if created:
            self.bus.publish("project.created", project_json(project))
        return project

    def project(self, project_id: str) -> Project:
        project = self.store.project(project_id)
        if project is None:
            raise NotFound(f"no project {project_id}")
        return project

    def rename_project(self, project_id: str, name: str, *, expected_revision: int | None = None) -> Project:
        self.project(project_id)
        project = self.store.update_project(project_id, name=name, expected_revision=expected_revision)
        if project is None:
            latest = self.project(project_id)
            raise RevisionConflict(
                f"project {project_id} is at revision {latest.revision}, not {expected_revision}; read it again",
                current=latest.revision, resource=project_id)
        self.bus.publish("project.updated", project_json(project))
        return project

    async def delete_project(self, project_id: str) -> None:
        sessions = self.store.sessions(project_id)
        if any(self.inputs.list(session.id) for session in sessions):
            raise Conflict("project has pending inputs; remove them before deleting it")
        closing_ids = {session.id for session in sessions}
        self.closing_sessions.update(closing_ids)
        try:
            for session in sessions:
                await self.interrupt(session.id, reason="the session's project was deleted")
            ids = {session.id for session in sessions}
            self.close_questions(ids, "the session's project was deleted")
            for sid in ids:
                self.pending_directories.pop(sid, None)
            await self.jobs.cancel_for(ids)
            sessions = self.store.sessions(project_id)  # anything created while awaiting is deleted too
            if any(self.inputs.list(session.id) for session in sessions):
                raise Conflict("project has pending inputs; remove them before deleting it")
            if not self.store.delete_project(project_id):
                raise NotFound(f"no project {project_id}")
            self._publish_tombstones(sessions)
            self.bus.publish("project.deleted", {"id": project_id})
        finally:
            self.closing_sessions.difference_update(closing_ids)

    # -- sessions ------------------------------------------------------------

    def session_json(self, s: Session) -> dict[str, Any]:
        """`model`/`effort` are what the next turn uses; `*_source` says where they came from."""
        snapshot_info: dict[str, Any] | None = None
        state: dict[str, Any] = {}
        starting = s.directory
        try:
            snapshot = self.snapshot_for(s)
            agent = snapshot["agent"]
            model, model_source = self.resolve_model(s, agent)
            effort = s.effort or agent["effort"]
            effort_source = "session" if s.effort else ("agent" if agent["effort"] else "provider")
            tools = self.effective_tool_names(snapshot, s)
            snapshot_info = snapshots.summary(snapshot)
            starting = snapshot.get("directory", s.directory)
            state = snapshot.get("workspace") or {}
        except ValueError as exc:
            snapshot_info = {"error": str(exc)}
            tools = []
            model, model_source = s.model or self.default_model, "session" if s.model else "default"
            effort, effort_source = s.effort, "session" if s.effort else "provider"
        pending = self.pending_directories.get(s.id)
        return {
            "id": s.id, "project_id": s.project_id, "directory": s.directory, "title": s.title, "agent": s.agent,
            "parent_id": s.parent_id, "parent_call_id": s.parent_call_id, "launched_by": s.launched_by,
            "children": [c.id for c in self.store.children(s.id)],
            "model": model, "model_source": model_source, "effort": effort, "effort_source": effort_source,
            "tools": tools, "disabled_tools": sorted(s.disabled()), "snapshot": snapshot_info,
            "workspace": {"entry": bool(state.get("entry")), "active": bool(state.get("activated"))},
            "starting_directory": starting,
            "directory_changed": str(Path(s.directory).resolve()) != starting,
            "directory_change": ({"from": pending["from"], "to": pending["to"], "status": "pending",
                                  "source": pending["source"]} if pending else None),
            "permission": s.permission, "status": "busy" if s.id in self.turns else "idle",
            "archived": bool(s.archived), "revision": s.revision,
            "created": s.created, "updated": s.updated,
        }

    def agents(self, project_path: str | None) -> agent_files.AgentSet:
        return agent_files.load(project_path)

    def resolve_model(self, session: Session, agent: dict[str, Any]) -> tuple[str, str]:
        if session.model:
            return session.model, "session"
        if agent["model"]:
            return agent["model"], "agent"
        return self.default_model, "default"

    # -- session snapshots -----------------------------------------------------

    def _registry(self) -> dict[str, Tool]:
        """Every task tool this build can execute and describe, by name."""
        return {tool.spec.name: tool for tool in (*DEFAULT_TOOLS, *BASH_JOB_TOOLS)}

    @staticmethod
    def granted_tool_names(spec: agent_files.AgentDef) -> list[str]:
        """The task tools an agent grants. bash's background companions are added when it is available."""
        return list(spec.tools) if spec.tools is not None else [t.spec.name for t in DEFAULT_TOOLS]

    @staticmethod
    def enabled_tool_names(granted: list[str], control: list[str], disabled: set[str], child: bool) -> list[str]:
        names = [n for n in granted if n not in disabled]
        if "bash" in names:
            # Background jobs need their reader/killer; grant them with bash unless switched off individually.
            at = names.index("bash") + 1
            for tool in BASH_JOB_TOOLS:
                name = tool.spec.name
                if name not in names and name not in disabled:
                    names.insert(at, name)
                    at += 1
        if not child and "session" in control and "session" not in disabled:
            names.append("session")
        return names

    @staticmethod
    def enabled_names(granted: list[str], control: list[str], disabled: set[str], child: bool,
                      workspace: dict[str, Any] | None, messaging: dict[str, Any] | None = None) -> list[str]:
        """Every tool the next request offers: task tools, granted control tools and workspace controls."""
        names = Runtime.enabled_tool_names(granted, control, disabled, child)
        state = workspace or {}
        if state.get("entry") and workspace_control.EXPOSE not in disabled:
            names.append(workspace_control.EXPOSE)
        if state.get("activated"):
            for name in workspace_control.WORKSPACE_TOOLS:
                if name not in disabled and name not in names:
                    names.append(name)
        # Exposed by the first `session` call; it needs `session` itself to be available.
        if (messaging or {}).get("activated") and "session" in names and agent_files.MESSAGE_TOOL not in disabled:
            names.append(agent_files.MESSAGE_TOOL)
        return names

    @staticmethod
    def effective_tool_names(snapshot: dict[str, Any], session: Session) -> list[str]:
        """Enabled tools in request order, from the snapshot's grants and the session's switches."""
        return Runtime.enabled_names(
            snapshot["granted_tools"], snapshot["control"], session.disabled(), session.parent_id is not None,
            snapshot.get("workspace"), snapshot.get("messaging"))

    def _session_spec(self, child_agents: list[dict[str, Any]]) -> ToolSpec:
        return ToolSpec(
            name="session",
            description=(
                "Launch a subagent: start a child session that runs one of the agents listed in the system prompt's "
                "tools section on a complete prompt. wait=true (default) returns its answer; wait=false returns at "
                "once and the answer arrives as a message when it finishes. The first successful call also exposes "
                "the `message_session` tool, which sends a further message to a child you launched: a steer while it "
                "works, or a follow-up after it has answered."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": [a["name"] for a in child_agents], "description": "Which agent runs the child"},
                    "prompt": {"type": "string", "description": "The complete task for the child"},
                    "wait": {"type": "boolean", "description": "Block until the child answers (default true)"},
                    "title": {"type": "string", "description": "Short title for the child session"},
                },
                "required": ["agent", "prompt"],
            },
        )

    def _session_guidance(self, child_agents: list[dict[str, Any]]) -> str:
        return (
            "This is how subagents are launched: another agent works on a prompt in this directory, in a child session "
            "the user can open. It does not see this conversation, so give it a complete prompt. `wait=true` returns "
            "its answer; `wait=false` lets you continue, and the answer arrives as a message when it finishes. Your "
            "first call also exposes `message_session`, to steer a working child or send a follow-up to a finished "
            "one. Child agents:\n"
            + "\n".join(f"  - `{a['name']}`: {a['description']}" for a in child_agents)
        )

    @staticmethod
    def _message_definition() -> dict[str, Any]:
        return {
            "name": agent_files.MESSAGE_TOOL,
            "description": (
                "Send a message to a child session you launched with `session`. If the child is working, the message "
                "reaches it before its next model call (a steer). If it is idle, the message starts a new turn on its "
                "existing conversation. Returns at once; the child's answer arrives as a message when it finishes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Id of a child session this session launched"},
                    "message": {"type": "string", "description": "What to tell the child"},
                },
                "required": ["session", "message"],
                "additionalProperties": False,
            },
            "guidance": (
                "Send a message to a child you launched. While it works, it is a steer the child sees before its next "
                "model call; once it has answered, it starts a new turn on the same conversation. The reply arrives "
                "as a message; the call itself returns at once."
            ),
        }

    def _definition(self, name: str, registry: dict[str, Tool], child_agents: list[dict[str, Any]]) -> dict[str, Any]:
        if name == "session":
            return {
                "name": "session",
                "description": self._session_spec(child_agents).description,
                "parameters": self._session_spec(child_agents).parameters,
                "guidance": self._session_guidance(child_agents),
            }
        if name == agent_files.MESSAGE_TOOL:
            return self._message_definition()
        if name in workspace_control.CONTROL_NAMES:
            return workspace_control.definition(name)
        tool = registry[name]
        return snapshots.tool_record(tool)

    def _build_snapshot(self, *, directory: str, spec: agent_files.AgentDef, found: agent_files.AgentSet,
                        disabled: set[str], child: bool, origin: str = "created",
                        instruction_file: dict[str, Any] | None = None,
                        current_instruction_file: dict[str, Any] | None = None,
                        notice: str | None = None, workspace: dict[str, Any] | None = None,
                        directory_changes: list[dict[str, Any]] | None = None,
                        messaging: dict[str, Any] | None = None) -> dict[str, Any]:
        """Freeze one session's agent, prompt, tool schemas and child catalog."""
        registry = self._registry()
        granted = self.granted_tool_names(spec)
        unknown = [n for n in granted if n not in registry]
        if unknown:
            raise ValueError(
                f"agent {spec.name} lists unknown tools: {', '.join(unknown)}; available: {', '.join(registry)}"
            )
        child_agents = [snapshots.agent_summary(a) for a in found.allowed(child=True)]
        state = dict(workspace) if workspace is not None else {"entry": True, "activated": False}
        names = self.enabled_names(granted, list(spec.control), disabled, child, state, messaging)
        definitions = {n: self._definition(n, registry, child_agents) for n in names}
        if instruction_file is None:
            instruction_file = snapshots.read_instruction_file(directory)
        agent_prompt = agent_files.render_prompt(
            spec, [(n, definitions[n]["guidance"]) for n in names], Path(directory))
        return {
            "version": snapshots.SNAPSHOT_VERSION,
            "origin": origin,
            "created": time.time(),
            "notice": notice,
            "directory": str(Path(directory).resolve()),
            "agent": snapshots.agent_record(spec),
            "granted_tools": granted,
            "control": list(spec.control),
            "tools": names,
            "tool_definitions": definitions,
            "workspace": state,
            "messaging": {"activated": bool((messaging or {}).get("activated"))},
            "directory_changes": list(directory_changes or []),
            "child_agents": child_agents,
            "instruction_file": instruction_file,
            **({"current_instruction_file": current_instruction_file} if current_instruction_file else {}),
            "agent_prompt": agent_prompt,
            "system_prompt": snapshots.compose_prompt(agent_prompt, instruction_file, notice=notice),
        }

    def snapshot_for(self, session: Session) -> dict[str, Any]:
        """The stored snapshot, reconstructing and storing one once for an old session."""
        stored = self.store.snapshot(session.id)
        if stored is not None:
            return stored
        found = self.agents(self.project(session.project_id).path)
        spec = found.get(session.agent, child=session.parent_id is not None)
        rebuilt = self._build_snapshot(
            directory=session.directory, spec=spec, found=found, disabled=session.disabled(),
            child=session.parent_id is not None, origin="reconstructed", notice=snapshots.RECONSTRUCTED_NOTE,
            # Older sessions do not silently gain the on-demand workspace entry point.
            workspace={"entry": False, "activated": False},
        )
        self.store.set_snapshot(session.id, rebuilt)
        self.bus.publish("session.snapshot", snapshots.summary(rebuilt), session_id=session.id)
        return rebuilt

    def snapshot_json(self, session_id: str) -> dict[str, Any]:
        """The full stored snapshot: prompt, tool schemas, guidance and every source path."""
        return self.snapshot_for(self.session(session_id))

    def _sync_tool_definitions(self, snapshot: dict[str, Any], session: Session) -> None:
        """Keep definitions for exactly the tools an explicit switch leaves available.

        A switched-off tool's schema and guidance disappear from the stored snapshot
        as well as the next request. Re-enabling it adds its definition again.
        """
        registry = self._registry()
        names = self.effective_tool_names(snapshot, session)
        available = set(names)
        for name in list(snapshot["tool_definitions"]):
            if name not in available:
                del snapshot["tool_definitions"][name]
        for name in names:
            if name not in snapshot["tool_definitions"]:
                snapshot["tool_definitions"][name] = self._definition(name, registry, snapshot["child_agents"])
        snapshot["tools"] = names

    def session(self, session_id: str) -> Session:
        session = self.store.session(session_id)
        if session is None:
            raise NotFound(f"no session {session_id}")
        return session

    def parent_of(self, session_id: str) -> str | None:
        if session_id not in self._parents:
            session = self.store.session(session_id)
            self._parents[session_id] = session.parent_id if session else None
        return self._parents[session_id]

    def in_tree(self, session_id: str | None, root_id: str) -> bool:
        """Whether `session_id` is `root_id` or one of its descendants."""
        while session_id is not None:
            if session_id == root_id:
                return True
            session_id = self.parent_of(session_id)
        return False

    def tree(self, session_id: str) -> dict[str, Any]:
        """The whole tree containing `session_id`, from its root, as nested session JSON."""
        root = self.session(session_id)
        while root.parent_id:
            root = self.session(root.parent_id)

        def node(s: Session) -> dict[str, Any]:
            return self.session_json(s) | {"children": [node(c) for c in self.store.children(s.id)]}
        return node(root)

    def create_session(self, project: Project, *, title: str | None, model: str | None, permission: str | None,
                       effort: str | None = None, agent: str | None = None, parent: Session | None = None,
                       parent_call_id: str | None = None, launched_by: str = "user",
                       disabled_tools: list[str] | None = None) -> Session:
        if permission not in (None, "ask", "allow"):
            raise ValueError("permission must be 'ask' or 'allow'")
        if parent is not None:
            # A caller may hold an immutable Session fetched before archiving.
            parent = self.session(parent.id)
        if parent is not None and parent.archived:
            root = self._root(parent)
            raise Conflict(
                f"session {parent.id} is archived; unarchive its root tree (session {root.id}) "
                "before creating a child session"
            )
        if parent is None:
            from .config import ConfigFile
            defaults = ConfigFile().read()["values"]
            settings = defaults["defaults"]
            if model is None and settings["model"]:
                try:
                    provider_for(settings["model"])
                except ValueError:
                    pass
                else:
                    model = settings["model"]
            if permission is None:
                permission = settings["permission"]
            if effort is None:
                effort = settings["effort"] or None
            if disabled_tools is None:
                try:
                    disabled_tools = self.check_tool_names(defaults["tools"]["disabled"])
                except ValueError:
                    pass
            if agent is None and settings["agent"]:
                available = self.agents(project.path).allowed(child=False)
                if settings["agent"] in [item.name for item in available]:
                    agent = settings["agent"]
        if disabled_tools is None and parent is not None:
            disabled_tools = sorted(parent.disabled())
        if disabled_tools is not None:
            self.check_tool_names(disabled_tools)
        if model:
            provider_for(model)  # validates the provider name
        found = self.agents(project.path)
        if not agent:
            # `build` unless the user's agent files make it child-only; then the first agent allowed here.
            fitting = [a.name for a in found.allowed(child=parent is not None)]
            agent = agent_files.DEFAULT_AGENT if agent_files.DEFAULT_AGENT in fitting else (fitting or [""])[0]
        spec = found.get(agent, child=parent is not None)  # the agent's mode must fit
        directory = parent.directory if parent else project.path
        self._check_removing_worktree(directory)
        snapshot = self._build_snapshot(
            directory=directory, spec=spec, found=found, disabled=set(disabled_tools or []),
            child=parent is not None,
        )
        session = self.store.create_session(project, title=title or "New session", agent=agent, model=model or "",
                                            permission=permission or "allow", effort=effort or None, parent=parent,
                                            parent_call_id=parent_call_id, launched_by=launched_by,
                                            disabled_tools=disabled_tools, snapshot=snapshot)
        self.bus.publish("session.created", self.session_json(session), session_id=session.id)
        return session

    @staticmethod
    def _tool_notice(snapshot: dict[str, Any], names: list[str], before: set[str]) -> str:
        gained = [n for n in names if n not in before]
        lost = sorted(before - set(names))
        lines = [
            "[Nexus notice: tool availability changed]",
            ("This is forward guidance for subsequent calls. The session's starting system prompt is frozen and "
             "is not rewritten; where it describes tools that are now switched off, this notice is authoritative."),
        ]
        if gained:
            lines.append("Now available:")
            for name in gained:
                guidance = snapshot["tool_definitions"].get(name, {}).get("guidance", "")
                lines.append(f"- `{name}`: {guidance}" if guidance else f"- `{name}`")
        if lost:
            lines.append("No longer available, including inside `code`: " + ", ".join(f"`{n}`" for n in lost) + ".")
        lines.append("Current tools: " + (", ".join(f"`{n}`" for n in names) if names else "none") + ".")
        return "\n".join(lines)

    @staticmethod
    def _agent_notice(before: str, after: str) -> str:
        return (
            "[Nexus notice: agent changed]\n"
            f"The session now runs agent `{after}` instead of `{before}`. Its starting system prompt and tool "
            "definitions were replaced with the new agent's snapshot; earlier messages and the loaded instructions "
            "file remain in the conversation."
        )

    def update_session(self, session_id: str, **fields: Any) -> Session:
        current = self.session(session_id)
        expected_revision = fields.pop("expected_revision", None)
        if fields.get("permission") not in (None, "ask", "allow"):
            raise ValueError("permission must be 'ask' or 'allow'")
        if fields.get("model"):
            provider_for(fields["model"])
        if fields.get("disabled_tools") is not None:
            self.check_tool_names(fields["disabled_tools"])
        if fields.get("agent") == "":
            raise ValueError("agent must not be empty")
        if expected_revision is not None and expected_revision != current.revision:
            raise RevisionConflict(
                f"session {session_id} is at revision {current.revision}, not {expected_revision}; read it again",
                current=current.revision, resource=session_id)
        new_agent: agent_files.AgentDef | None = None
        found: agent_files.AgentSet | None = None
        if fields.get("agent"):
            found = self.agents(self.project(current.project_id).path)
            candidate = found.get(fields["agent"], child=current.parent_id is not None)
            if candidate.name != current.agent:
                new_agent = candidate
        before_disabled = current.disabled()
        new_disabled = fields.get("disabled_tools")
        tools_changed = new_disabled is not None and set(new_disabled) != before_disabled
        before_snapshot = self.snapshot_for(current) if tools_changed and new_agent is None else None
        before_enabled = set(self.effective_tool_names(before_snapshot, current)) if before_snapshot else set()
        if before_snapshot is not None and new_disabled is not None:
            target = self.enabled_names(
                before_snapshot["granted_tools"], before_snapshot["control"], set(new_disabled),
                current.parent_id is not None, before_snapshot.get("workspace"), before_snapshot.get("messaging"))
            registry = self._registry()
            unknown_exec = [n for n in target if n not in registry and n not in agent_files.NEXUS_CONTROL_TOOLS
                            and n != "code"]
            if unknown_exec:
                raise ValueError(
                    f"session snapshot needs tools this Nexus build cannot execute: {', '.join(unknown_exec)}; "
                    "start a new session or switch the agent"
                )
        rebuilt: dict[str, Any] | None = None
        if new_agent is not None and found is not None:
            previous = self.store.snapshot(session_id)
            previous_workspace = previous.get("workspace") if previous else None
            rebuilt = self._build_snapshot(
                directory=current.directory, spec=new_agent, found=found,
                disabled=set(new_disabled) if new_disabled is not None else current.disabled(),
                child=current.parent_id is not None, origin="agent-switch",
                instruction_file=previous["instruction_file"] if previous else None,
                current_instruction_file=previous.get("current_instruction_file") if previous else None,
                # Workspace activation and directory moves are session state, not agent configuration.
                # A session without stored workspace state is old: do not silently add the entry point.
                workspace=previous_workspace if previous_workspace is not None
                else {"entry": False, "activated": False},
                directory_changes=(previous.get("directory_changes") if previous else None),
                messaging=previous.get("messaging") if previous else None,
            )
        session = self.store.update_session(session_id, expected_revision=expected_revision, **fields)
        if session is None:
            latest = self.session(session_id)
            raise RevisionConflict(
                f"session {session_id} is at revision {latest.revision}, not {expected_revision}; read it again",
                current=latest.revision, resource=session_id)
        notice: str | None = None
        if rebuilt is not None:
            self.store.set_snapshot(session_id, rebuilt)
            notice = self._agent_notice(current.agent, session.agent)
        elif tools_changed:
            snapshot = before_snapshot or self.snapshot_for(session)
            self._sync_tool_definitions(snapshot, session)
            self.store.set_snapshot(session_id, snapshot)
            notice = self._tool_notice(snapshot, self.effective_tool_names(snapshot, session), before_enabled)
        if notice is not None:
            if session_id in self.turns:
                # Add at the next model boundary, after the active call's results.
                self.inboxes.setdefault(session_id, []).append(notice)
            else:
                stored = self.store.append_message(session_id, Message("user", (Text(notice),)))
                self._publish_message(session_id, stored)
        self.bus.publish("session.updated", self.session_json(session), session_id=session_id)
        return session

    # -- workspace controls ----------------------------------------------------

    def _forward_notice(self, session_id: str, text: str) -> None:
        """Add forward context now when idle, or before the next request when a call is running."""
        if session_id in self.turns:
            self.inboxes.setdefault(session_id, []).append(text)
            return
        stored = self.store.append_message(session_id, Message("user", (Text(text),)))
        self._publish_message(session_id, stored)

    def _publish_message(self, session_id: str, stored: StoredMessage) -> None:
        """Announce a saved message once; keep its durable event id for recovery."""
        event = self.bus.publish("message.added", message_json(stored), session_id=session_id)
        if event.get("id") is not None:
            self.store.link_message_event(stored.id, event["id"])

    def _notify(self, session_id: str, text: str) -> None:
        """Deliver an automatic notice without waking an archived session.

        Busy: before the next model call. Idle: start a turn (the existing
        behavior). Archived: save the model-visible notice as context but do not
        call the provider; it is present when the user later unarchives and
        resumes.
        """
        session = self.store.session(session_id)
        if session is None:
            return
        if session.archived:
            stored = self.store.append_message(session_id, Message("user", (Text(text),)))
            self._publish_message(session_id, stored)
        elif session_id in self.turns:
            self.inboxes.setdefault(session_id, []).append(text)
        else:
            self.prompt(session_id, text)

    def activate_workspace_tools(self, session_id: str, *, source: str = "tool") -> str:
        """Make `change_session_directory` and `worktree` available; idempotent per session."""
        session = self.session(session_id)
        snapshot = self.snapshot_for(session)
        state = dict(snapshot.get("workspace") or {"entry": False, "activated": False})
        if state.get("activated"):
            return workspace_control.already_active_text(self.effective_tool_names(snapshot, session))
        state["activated"] = True
        snapshot["workspace"] = state
        self._sync_tool_definitions(snapshot, session)
        self.store.set_snapshot(session_id, snapshot)
        text = workspace_control.activation_text(self.effective_tool_names(snapshot, session))
        self.bus.publish(
            "session.workspace_tools",
            {"active": True, "entry": bool(state.get("entry")), "tools": list(workspace_control.WORKSPACE_TOOLS)},
            session_id=session_id,
        )
        if source != "tool":  # a tool's own result is the forward guidance
            self._forward_notice(session_id, text)
        self.bus.publish("session.updated", self.session_json(self.session(session_id)), session_id=session_id)
        return text

    def _check_removing_worktree(self, directory: str | Path) -> None:
        target = Path(directory).resolve()
        if any(target.is_relative_to(root) for root in self.removing_worktrees):
            raise ValueError(f"{target} is inside a worktree being deleted; wait for deletion to finish")

    async def _describe_directory_change(self, session: Session, directory: Any, source: str) -> dict[str, Any] | None:
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("directory must be a non-empty string")
        target = Path(directory).expanduser()
        if not target.is_absolute():  # relative to where the session works now
            target = Path(session.directory) / target
        target = target.resolve()
        self._check_removing_worktree(target)
        if not target.is_dir():
            raise ValueError(f"{target} is not an existing directory")
        previous = str(Path(session.directory).resolve())
        if str(target) == previous:
            return None
        git = await worktrees.git_state(target)
        record = snapshots.read_instruction_file(target)
        notice = workspace_control.directory_notice(
            previous=previous, current=str(target), git=git,
            instruction_section=snapshots.instruction_section(record), source=source,
        )
        return {
            "from": previous, "to": str(target), "branch": (git or {}).get("branch"),
            "repository": (git or {}).get("repository"), "worktree": (git or {}).get("worktree"),
            "instruction_file": record, "notice": notice, "source": source,
        }

    async def change_directory(self, session_id: str, directory: Any, *, source: str = "tool") -> dict[str, Any]:
        """Validate a move, then apply it now or at the next tool boundary.

        A running tool call keeps the directory it started with (`ToolContext` is
        frozen). The next `refresh` boundary applies a queued move, so later direct
        and nested calls resolve from the new directory.
        """
        if source not in ("tool", "api"):
            raise ValueError("source must be 'tool' or 'api'")
        session = self.session(session_id)
        change = await self._describe_directory_change(session, directory, source)
        if change is None:
            return {"status": "unchanged", "directory": session.directory,
                    "notice": f"This session already works in {session.directory}; no change was made."}
        self._check_removing_worktree(change["to"])
        # Validation awaited Git: another move may have landed in the meantime.
        change["from"] = self.session(session_id).directory
        summary = {key: change[key] for key in ("from", "to", "branch")}
        if session_id in self.turns:
            if session_id in self.pending_directories:
                raise Conflict("a directory change is already pending for this session")
            self.pending_directories[session_id] = change
            if source == "api":  # make the pending state visible immediately
                self.bus.publish("session.updated", self.session_json(self.session(session_id)),
                                 session_id=session_id)
            return {"status": "pending", **summary, "notice": change["notice"]}
        self._apply_directory_change(session_id, change)
        return {"status": "applied", **summary, "notice": change["notice"]}

    def _apply_pending_directory(self, session_id: str) -> None:
        change = self.pending_directories.pop(session_id, None)
        if change is not None and self.store.session(session_id) is not None:
            self._apply_directory_change(session_id, change)

    def _apply_directory_change(self, session_id: str, change: dict[str, Any]) -> None:
        """Persist one validated move and publish its control event.

        The frozen prompt's directory and instruction file stay as they were; the
        destination's instruction file is recorded as the current one and, for a
        client-driven move, announced as forward context.
        """
        session = self.store.session(session_id)
        if session is None:
            return
        if not Path(change["to"]).is_dir():
            # The destination vanished while a running call delayed the move.
            self._forward_notice(session_id, (
                "[Nexus notice: directory change not applied]\n"
                f"The destination {change['to']} no longer exists; the session remains in {change['from']}."
            ))
            self.bus.publish("session.updated", self.session_json(self.session(session_id)),
                             session_id=session_id)
            return
        self._check_removing_worktree(change["to"])
        original_notice = change["notice"]
        change["from"] = session.directory
        change["instruction_file"] = snapshots.read_instruction_file(change["to"])
        change["notice"] = workspace_control.directory_notice(
            previous=session.directory, current=change["to"],
            git={key: change.get(key) for key in ("branch", "repository", "worktree")}
            if change.get("repository") else None,
            instruction_section=snapshots.instruction_section(change["instruction_file"]), source=change["source"],
        )
        snapshot = self.snapshot_for(session)  # before the move, so an old session freezes the old directory
        self.store.update_session(session_id, directory=change["to"])
        session = self.session(session_id)
        snapshot["workspace"] = dict(snapshot.get("workspace") or {"entry": False, "activated": False})
        record = change["instruction_file"]
        snapshot["current_instruction_file"] = record
        changes = list(snapshot.get("directory_changes") or [])
        changes.append({
            "from": change["from"], "to": change["to"], "at": time.time(), "source": change["source"],
            "instructions": {key: record[key] for key in ("path", "status", "bytes", "sha256")},
        })
        snapshot["directory_changes"] = changes[-20:]
        self.store.set_snapshot(session_id, snapshot)
        self.bus.publish("session.directory_changed", {
            "from": change["from"], "to": change["to"], "branch": change["branch"],
            "repository": change.get("repository"), "worktree": change.get("worktree"),
            "source": change["source"], "project_id": session.project_id,
            "frozen_directory": snapshot.get("directory"),
            "instructions": {key: record[key] for key in ("path", "status", "bytes", "sha256")},
        }, session_id=session_id)
        if change["source"] == "api" or change["notice"] != original_notice:
            self._forward_notice(session_id, change["notice"])
        elif session_id not in self.turns:
            # A tool move applied at turn teardown may have no saved result.
            announced = any(isinstance(part, ToolResult) and part.output == change["notice"]
                            for stored in self.store.messages(session_id) for part in stored.message.parts)
            if not announced:
                self._forward_notice(session_id, change["notice"])
        self.bus.publish("session.updated", self.session_json(self.session(session_id)), session_id=session_id)

    async def list_worktrees(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        try:
            data = await worktrees.listing(session.directory)
        except worktrees.WorktreeError as exc:
            raise ToolError(str(exc)) from exc
        data = data | {"session_directory": session.directory}
        self.bus.publish("worktree.listed", data, session_id=session_id)
        return data

    async def list_project_worktrees(self, project_id: str) -> dict[str, Any]:
        """Worktrees of a project's own checkout, for a client with no session yet."""
        project = self.project(project_id)
        try:
            data = await worktrees.listing(project.path)
        except worktrees.WorktreeError as exc:
            raise ToolError(str(exc)) from exc
        return data | {"project_directory": project.path}

    async def create_worktree(self, session_id: str, path: Any, branch: Any = None, base: Any = None) -> dict[str, Any]:
        """Create or reuse a worktree; never changes the session directory."""
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string")
        for name, value in (("branch", branch), ("base", base)):
            if value is not None and not isinstance(value, str):
                raise ToolError(f"{name} must be a string")
        async with self.worktree_lock:  # a create and a delete cannot interleave
            session = self.session(session_id)
            try:
                data = await worktrees.create(session.directory, path, branch, base)
            except worktrees.WorktreeError as exc:
                raise ToolError(str(exc)) from exc
            data["session_directory"] = session.directory
            self.bus.publish("worktree.created", data, session_id=session_id)
            return data

    async def delete_worktree(self, session_id: str, path: Any) -> dict[str, Any]:
        """Remove one clean worktree that no session or pending move is using."""
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path must be a non-empty string")
        async with self.worktree_lock:
            session = self.session(session_id)
            try:
                listing = await worktrees.listing(session.directory)
                target = worktrees.resolve_target(Path(listing["main"]), path)
            except worktrees.WorktreeError as exc:
                raise ToolError(str(exc)) from exc
            entry = next((item for item in listing["worktrees"] if Path(item["path"]).resolve() == target), None)
            if entry is None:
                raise ToolError(f"{target} is not a registered worktree of {listing['main']}")
            if entry["main"]:
                raise ToolError(f"refusing to delete the main checkout {target}")
            self.removing_worktrees.add(target)
            try:
                users = sorted(item.id for item in self.store.sessions()
                               if Path(item.directory).resolve().is_relative_to(target))
                if users:
                    raise ToolError(
                        f"refusing to delete worktree {target}: it is the directory of session(s) "
                        f"{', '.join(users)}; change those sessions' directories first")
                projects = sorted(item.id for item in self.store.projects() if Path(item.path).resolve().is_relative_to(target))
                if projects:
                    raise ToolError(
                        f"refusing to delete worktree {target}: it is the path of project(s) "
                        f"{', '.join(projects)}; remove those projects first")
                pending = sorted(sid for sid, change in self.pending_directories.items()
                                 if Path(change["to"]).resolve().is_relative_to(target))
                if pending:
                    raise ToolError(
                        f"refusing to delete worktree {target}: a directory change for session(s) "
                        f"{', '.join(pending)} is pending")
                jobs = sorted(job.id for job in self.jobs.list()
                              if job.finished is None and Path(job.directory).resolve().is_relative_to(target))
                if jobs:
                    raise ToolError(f"refusing to delete worktree {target}: running background job(s) {', '.join(jobs)}")
                try:
                    data = await worktrees.remove(session.directory, path)
                except worktrees.WorktreeError as exc:
                    raise ToolError(str(exc)) from exc
                data["session_directory"] = session.directory
                self.bus.publish("worktree.deleted", data, session_id=session_id)
                return data
            finally:
                self.removing_worktrees.discard(target)

    async def _worktree_tool(self, session_id: str, args: dict[str, Any]) -> str:
        action = args.get("action")
        if action == "list":
            data = await self.list_worktrees(session_id)
        elif action in ("create", "delete"):
            path = args.get("path")
            if not isinstance(path, str) or not path.strip():
                raise ToolError(f"path is required for {action}")
            if action == "create":
                data = await self.create_worktree(session_id, path, args.get("branch"), args.get("base"))
            else:
                data = await self.delete_worktree(session_id, path)
        else:
            raise ToolError("action must be one of: list, create, delete")
        return clip(workspace_control.worktree_text(action, data))

    async def delete_session(self, session_id: str, *, drop_inputs: bool = False) -> None:
        ids = [session_id, *(child.id for child in self.store.descendants(session_id))]
        pending = [item for sid in ids for item in self.inputs.list(sid)]
        if pending and not drop_inputs:
            raise Conflict("session tree has pending inputs; remove them or set drop_inputs")
        self.closing_sessions.update(ids)
        try:
            await self.interrupt(session_id, reason="the session was deleted")  # also interrupts descendants
            descendants = self.store.descendants(session_id)
            ids = {session_id, *(child.id for child in descendants)}
            pending = [item for sid in ids for item in self.inputs.list(sid)]
            if pending and not drop_inputs:
                raise Conflict("session tree has pending inputs; remove them or set drop_inputs")
            for item in pending:
                removed = self.inputs.remove(item["session_id"], item["id"])
                self.bus.publish("input.removed", removed, session_id=item["session_id"])
            self.close_questions(ids, "the session was deleted")
            for sid in ids:
                self.pending_directories.pop(sid, None)  # a queued move must not apply to a deleted session
            await self.jobs.cancel_for(ids)  # a background command must not outlive the session that started it
            if not self.store.delete_session(session_id):
                raise NotFound(f"no session {session_id}")
            self._publish_tombstones(descendants, session_id)
        finally:
            self.closing_sessions.difference_update(ids)

    def _publish_tombstones(self, sessions: list[Session], root_id: str | None = None) -> None:
        """Publish one minimal, global deletion record per deleted session.

        The session rows (and their event logs) are already gone. The tombstones
        carry only the id, survive the deletion, and are global so a filtered
        stream cannot miss them for lack of a session row. The root goes first:
        a client filtering its tree can drop the whole subtree at once.
        """
        ordered = sorted(sessions, key=lambda s: (s.created, s.id))
        ids = ([root_id] if root_id is not None else []) + [s.id for s in ordered if s.id != root_id]
        for sid in ids:
            self.bus.publish("session.deleted", {"id": sid})

    def archive_session(self, session_id: str, *, archived: bool, drop_inputs: bool = False) -> Session:
        """Archive or unarchive one whole root tree, atomically and idempotently.

        Nothing is interrupted: a running turn (or a pending question/approval)
        anywhere in the tree refuses the operation. Data, directories, worktrees,
        snapshots and background jobs are untouched; archive state is one column.
        """
        session = self.session(session_id)
        if session.parent_id is not None:
            root = self._root(session)
            raise ValueError(
                f"session {session_id} is a child session; archive and unarchive operate on a whole tree: "
                f"use its root session {root.id}"
            )
        ids = self.store.tree_ids(session_id)
        busy = next((sid for sid in ids
                     if sid in self.turns or self.pending_approvals(sid) or self.pending_questions(sid)), None)
        if busy is not None:
            raise Conflict(
                f"cannot {'archive' if archived else 'unarchive'} session tree {session_id}: session {busy} "
                "is running a turn or waiting for the user; interrupt it first"
            )
        pending = [item for sid in ids for item in self.inputs.list(sid)] if archived else []
        if pending and not drop_inputs:
            raise Conflict("session tree has pending inputs; remove them or set drop_inputs")
        for item in pending:
            removed = self.inputs.remove(item["session_id"], item["id"])
            self.bus.publish("input.removed", removed, session_id=item["session_id"])
        ids, changed = self.store.set_tree_archived(session_id, archived)
        if changed:
            self.bus.publish(
                "session.archived" if archived else "session.unarchived",
                {"root": session_id, "sessions": ids, "archived": archived},
                session_id=session_id,
            )
        return self.session(session_id)

    def context(self, session_id: str) -> dict[str, Any]:
        """The request body the next turn would send, with no new user message."""
        agent = self._agent(self.session(session_id))
        return agent.provider.body(agent.request())

    # -- turns ---------------------------------------------------------------

    @staticmethod
    def check_tool_names(names: Any) -> list[str]:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("disabled_tools must be a list of tool names")
        known = ([t.spec.name for t in DEFAULT_TOOLS + BASH_JOB_TOOLS] + list(agent_files.CONTROL_TOOLS)
                 + [agent_files.MESSAGE_TOOL] + list(workspace_control.CONTROL_NAMES))
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(f"unknown tools: {', '.join(unknown)}; known: {', '.join(known)}")
        return names

    def _root(self, session: Session) -> Session:
        root = session
        while root.parent_id:
            root = self.session(root.parent_id)
        return root

    async def _start_job(self, session: Session, root: Session, args: dict[str, Any], ctx: ToolContext) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        timeout = bash_tool.timeout_value(args.get("timeout"))
        job = await self.jobs.start(session_id=session.id, root=root.id, command=command,
                                    directory=ctx.workspace, timeout=timeout, on_finish=self._job_finished)
        self.bus.publish("bash.started", job.json(), session_id=session.id)
        return bash_tool.started(job)

    def _job_finished(self, job: Job) -> None:
        data = job.json() | {"output": clip(job.output, 30_000)}
        self.bus.publish("bash.finished", data, session_id=job.session_id)
        if self.store.session(job.session_id) is None:
            return
        self._notify(job.session_id, self._job_notice(job))

    @staticmethod
    def _job_notice(job: Job) -> str:
        head = f"→ Background Shell Done: {clip(repr(job.command), 500)}\njob {job.id} · {job.status}"
        if job.exit_code is not None:
            head += f" · exit code {job.exit_code}"
        if job.dropped:
            head += f" · {job.dropped} chars dropped"
        output = job.output[-NOTICE_CHARS:]
        if len(job.output) > len(output):
            output = f"[… earlier output omitted …]\n{output}"
        return f"{head}\n{output or '(no output)'}\nUse bash_output(job_id=\"{job.id}\") for the retained output."

    def _refresh_call(self, active: Agent, session_id: str) -> None:
        """Rebind explicit changes at dispatch boundaries, never within a running call."""
        self._apply_pending_directory(session_id)  # a queued move lands between calls, not inside one
        updated = self._agent(self.session(session_id), refresh=False)
        for name in ("provider", "model", "workspace", "tools", "instructions", "reasoning_effort", "approve",
                     "files", "cwd_changed"):
            setattr(active, name, getattr(updated, name))

    def _agent(self, session: Session, *, refresh: bool = True) -> Agent:
        snapshot = self.snapshot_for(session)
        spec = snapshot["agent"]
        provider, model = provider_for(self.resolve_model(session, spec)[0])
        names = self.effective_tool_names(snapshot, session)
        definitions = snapshot["tool_definitions"]
        missing = [n for n in names if n not in definitions]
        if missing:
            raise ValueError(
                f"session snapshot is missing definitions for: {', '.join(missing)}; "
                "switch the agent or clear the tool switch that re-enabled them"
            )
        registry = self._registry()
        missing_exec = [n for n in names if n not in registry and n not in agent_files.NEXUS_CONTROL_TOOLS
                        and n != "code"]
        if missing_exec:
            raise ValueError(
                f"session snapshot needs tools this Nexus build cannot execute: {', '.join(missing_exec)}; "
                "start a new session or switch the agent"
            )
        workspace = Path(session.directory)
        starting = snapshot.get("directory", session.directory)
        cwd_changed = str(workspace.resolve()) != starting
        root = self._root(session)
        files = FileGuard(self.store, session.id, self.file_lock, self.pending_reads.setdefault(session.id, []))

        async def run_bash(args: dict[str, Any], ctx: ToolContext) -> str:
            background = args.get("background", False)
            if not isinstance(background, bool):
                raise ToolError("background must be a boolean")
            if not background:
                return await bash_tool.TOOL.run(args, ctx)
            return await self._start_job(session, root, args, ctx)

        async def run_bash_output(args: dict[str, Any], ctx: ToolContext) -> str:
            job_id = args.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ToolError("job_id must be a non-empty string")
            return bash_tool.report(self.jobs.get(job_id, root=root.id), args)

        async def run_bash_kill(args: dict[str, Any], ctx: ToolContext) -> str:
            job_id = args.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ToolError("job_id must be a non-empty string")
            return bash_tool.killed(await self.jobs.kill(job_id, root=root.id))

        async def run_session(args: dict[str, Any], ctx: ToolContext) -> str:
            return await self._launch(session.id, args, ctx.call_id)

        async def run_message(args: dict[str, Any], ctx: ToolContext) -> str:
            return self._message_child(session.id, args)

        async def run_ask(args: dict[str, Any], ctx: ToolContext) -> str:
            try:
                questions = question_contract.normalize_request(args)
            except QuestionError as exc:
                raise ToolError(str(exc)) from exc
            return await self.ask_user(session.id, ctx.call_id, questions)

        async def run_expose(args: dict[str, Any], ctx: ToolContext) -> str:
            try:
                return self.activate_workspace_tools(session.id, source="tool")
            except NotFound as exc:  # the session was deleted while the call was queued
                raise ToolError(str(exc)) from exc

        async def run_change_directory(args: dict[str, Any], ctx: ToolContext) -> str:
            try:
                result = await self.change_directory(session.id, args.get("directory"), source="tool")
            except (NotFound, Conflict) as exc:
                raise ToolError(str(exc)) from exc
            return result["notice"]

        async def run_worktree(args: dict[str, Any], ctx: ToolContext) -> str:
            try:
                return await self._worktree_tool(session.id, args)
            except NotFound as exc:
                raise ToolError(str(exc)) from exc

        bound = {"bash": run_bash, "bash_output": run_bash_output, "bash_kill": run_bash_kill,
                 "session": run_session, agent_files.MESSAGE_TOOL: run_message, "ask": run_ask,
                 workspace_control.EXPOSE: run_expose,
                 workspace_control.CHANGE_DIRECTORY: run_change_directory,
                 workspace_control.WORKTREE: run_worktree}

        def stored_tool(name: str) -> Tool:
            definition = definitions[name]
            runner = bound.get(name) or (None if name == "code" else registry[name].run)
            return Tool(ToolSpec(name, definition["description"], definition["parameters"]), runner,  # type: ignore[arg-type]
                        guidance=definition["guidance"])

        tools = [stored_tool(name) for name in names if name != "code"]
        if "code" in names:
            task_tools = [t for t in tools if t.spec.name not in agent_files.NEXUS_CONTROL_TOOLS]
            code_definition = definitions["code"]

            async def run_code(args: dict[str, Any], ctx: ToolContext) -> str:
                return await self.kernels.run(
                    args, root=root.id, caller=session.id, ctx=ctx, tools=task_tools,
                    approve=lambda call, a: self._approve(session.id, call, a),
                    publish=lambda kind, data: self.bus.publish(kind, data, session_id=session.id),
                )

            tools.insert(names.index("code"), Tool(
                ToolSpec("code", code_definition["description"], code_definition["parameters"]),
                run_code, guidance=code_definition["guidance"]))
        return Agent(
            provider, model=model, workspace=workspace,
            tools=tuple(tools),
            instructions=snapshot["system_prompt"],
            reasoning_effort=session.effort or spec["effort"], max_steps=spec["steps"] or MAX_STEPS,
            messages=[m.message for m in self.store.messages(session.id)],
            conversation_id=session.id, approve=lambda call, args: self._approve(session.id, call, args),
            inbox=lambda: self.inboxes.pop(session.id, []),
            steering=lambda: [(item["id"], self.inputs.message(item)) for item in self.inputs.list(session.id, "steer")],
            refresh=(lambda active: self._refresh_call(active, session.id)) if refresh else None,
            files=files,
            cwd_changed=cwd_changed,
            retry_policy=self.retry_policy,
        )

    # -- child sessions --------------------------------------------------------

    def _session_tool(self, parent: Session) -> Tool:
        # The live view, used by inspection. Turns use the session snapshot instead.
        allowed = self.agents(self.project(parent.project_id).path).allowed(child=True)
        records = [snapshots.agent_summary(a) for a in allowed]

        async def run(args: dict[str, Any], ctx: ToolContext) -> str:
            return await self._launch(parent.id, args, ctx.call_id)

        return Tool(self._session_spec(records), run, guidance=self._session_guidance(records))

    async def _launch(self, parent_id: str, args: dict[str, Any], call_id: str | None) -> str:
        parent = self.session(parent_id)
        if parent.parent_id is not None:
            raise ToolError("child sessions cannot start sessions")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError("prompt must be a non-empty string")
        agent = args.get("agent")
        if not isinstance(agent, str) or not agent:
            raise ToolError("agent must be a non-empty string; choose one of the child agents in the tool schema")
        try:
            child = self.create_session(
                self.project(parent.project_id), title=args.get("title") or None, model=None, permission=parent.permission,
                agent=agent, parent=parent, parent_call_id=call_id, launched_by="agent",
                disabled_tools=sorted(parent.disabled()),
            )
        except (ValueError, Conflict) as exc:
            raise ToolError(str(exc)) from exc
        exposed = self._expose_messaging(parent.id)
        wait = args.get("wait", True) is not False
        if not wait:
            self.notify[child.id] = parent.id
        self.prompt(child.id, prompt)
        if not wait:
            return (f"Started child session {child.id} (agent {child.agent}). Its answer will arrive as a message when it "
                    "finishes; continue with other work meanwhile." + exposed)
        task = self.turns[child.id]
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.interrupt(child.id)
            raise
        return self._child_report(child.id) + exposed

    def _expose_messaging(self, session_id: str) -> str:
        """Expose `message_session` on the first `session` call; once per session, persisted in the snapshot.

        Returns text for that call's result (empty when already exposed or switched off). The
        tool's schema reaches the next request through the snapshot, so a tool result is the
        only forward guidance needed.
        """
        session = self.session(session_id)
        snapshot = self.snapshot_for(session)
        if (snapshot.get("messaging") or {}).get("activated"):
            return ""
        snapshot["messaging"] = {"activated": True}
        self._sync_tool_definitions(snapshot, session)
        self.store.set_snapshot(session_id, snapshot)
        self.bus.publish("session.message_tool", {"active": True, "tools": [agent_files.MESSAGE_TOOL]},
                         session_id=session_id)
        self.bus.publish("session.updated", self.session_json(self.session(session_id)), session_id=session_id)
        if agent_files.MESSAGE_TOOL not in snapshot["tools"]:
            return ""  # switched off: nothing to announce, and enabling it later gets the usual notice
        definition = snapshot["tool_definitions"][agent_files.MESSAGE_TOOL]
        return (
            "\n\n[Nexus notice: `message_session` is now available]\n"
            f"{definition['description']}\nGuidance: {definition['guidance']}"
        )

    def _message_child(self, parent_id: str, args: dict[str, Any]) -> str:
        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ToolError("message must be a non-empty string")
        child_id = args.get("session")
        if not isinstance(child_id, str) or not child_id:
            raise ToolError("session must be the id of a child session you launched")
        child = self.store.session(child_id)
        if child is None or child.parent_id != parent_id:
            raise ToolError(f"{child_id} is not a child session of this session; message only children you launched")
        # The answer comes back as a notification, as for `wait: false`.
        if child_id in self.turns:
            self.notify[child_id] = parent_id
            self.inboxes.setdefault(child_id, []).append(message)
            return (f"Message queued for child session {child_id} (agent {child.agent}), which is working; it sees it "
                    "before its next model call. Its answer will arrive as a message when it finishes.")
        try:
            self._check_turn_start(child)
        except (Conflict, Busy) as exc:
            raise ToolError(str(exc)) from exc
        self.notify[child_id] = parent_id
        self.prompt(child_id, message)
        return (f"Child session {child_id} (agent {child.agent}) was idle; started a new turn with your message. Its "
                "answer will arrive as a message when it finishes.")

    def _child_report(self, child_id: str) -> str:
        child = self.session(child_id)
        if child_id in self.errors:
            return f"Child session {child_id} (agent {child.agent}) failed: {self.errors[child_id]}"
        answer = ""
        for stored in reversed(self.store.messages(child_id)):
            if stored.message.role == "assistant":
                answer = "".join(p.text for p in stored.message.parts if isinstance(p, Text))
                if answer:
                    break
        if len(answer) > CHILD_RESULT_CHARS:
            answer = answer[:CHILD_RESULT_CHARS] + f"\n[… answer clipped at {CHILD_RESULT_CHARS} of {len(answer)} characters]"
        return f"Child session {child_id} (agent {child.agent}) finished.\n\n{answer or '(no text answer)'}"

    def _deliver(self, child_id: str) -> None:
        """A wait: false child finished: hand its report to the parent."""
        parent_id = self.notify.pop(child_id, None)
        if parent_id is None or self.store.session(parent_id) is None:
            return
        notice = f"[Notification: child session finished]\n{self._child_report(child_id)}"
        self.bus.publish("session.notified", {"from": child_id, "text": notice}, session_id=parent_id)
        self._notify(parent_id, notice)

    def _check_turn_start(self, session: Session) -> None:
        if session.id in self.closing_sessions:
            raise Conflict("session is being deleted")
        if session.archived:
            root = self._root(session)
            raise Conflict(
                f"session {session.id} is archived; unarchive its root tree (session {root.id}) before starting a turn "
                f"(nexus session unarchive {root.id} or POST /api/session/{root.id}/unarchive)"
            )
        if session.id in self.turns:
            raise Busy(f"session {session.id} is already running a turn")

    def _begin_turn(self, session: Session, *, user_text: str | Message | NoUserMessage, origin: str,
                    continuation: dict[str, Any] | None = None,
                    mutation: dict[str, Any] | None = None, input_id: str | None = None) -> dict[str, Any]:
        """Record one turn durably, announce it, and start its task.

        Prompting and manual continuation share this path; the only difference is
        the explicit `NO_USER_MESSAGE` marker and the provenance carried in the
        durable `turn.data`. A keyed request is written in the same transaction as
        the turn, so a crash leaves either both or neither.
        """
        session_id = session.id
        self.errors.pop(session_id, None)
        data: dict[str, Any] = {"origin": origin}
        if continuation is not None:
            data["continuation"] = continuation
        turn = self.store.start_turn(session_id, data=data, mutation=mutation, input_id=input_id)  # durable before the task exists
        status: dict[str, Any] = {"status": "busy", "turn_id": turn["id"], "origin": origin}
        if continuation is not None:
            status["continuation"] = continuation
            if continuation.get("reconciliation"):
                status["reconciled"] = continuation["reconciliation"]
        # Published here, not in the task, so a session is never seen idle between being prompted and running.
        self.bus.publish("session.status", status, session_id=session_id)
        self.turns[session_id] = asyncio.create_task(
            self._run(session_id, user_text, turn["id"], origin=origin, continuation=continuation, input_id=input_id))
        return turn

    def prompt(self, session_id: str, text: str, *, images: list[dict[str, Any]] | None = None, mutation: dict[str, Any] | None = None, mode: str = "send", typed: bool = False) -> dict[str, Any]:
        session = self.session(session_id)
        if mode not in ("send", "steer", "queue"):
            raise ValueError("mode must be send, steer or queue")
        if session_id in self.closing_sessions:
            raise Conflict("session is being deleted")
        busy = session_id in self.turns
        if mode == "send" or not busy:
            self._check_turn_start(session)
        elif session.archived:
            raise Conflict("cannot add input to an archived session")
        from .images import image_parts

        parts = image_parts(images)
        if not text.strip() and not parts:
            raise ValueError("text or images must not be empty")
        if busy or mode != "send":
            labelled = tuple(part for index, image in enumerate(parts) for part in (Text(f"[Image {chr(ord('A') + index)}]"), image))
            message = Message("user", ((Text(text),) if text else ()) + labelled)
            item = self.inputs.add(session_id, mode, message_to_json(message)["parts"], mutation)
            self.bus.publish("input.added", item, session_id=session_id)
            if not busy:
                self.inputs.hold(session_id, False)
                self._start_input(session_id, item)
            return {"input_id": item["id"], "mode": mode, "state": "pending"}
        if session.title == "New session":
            self.update_session(session_id, title=" ".join(text.split())[:TITLE_CHARS] or "Image")
        labelled_parts = tuple(part for index, image in enumerate(parts) for part in (
            Text(f"[Image {chr(ord('A') + index)}]"), image,
        ))
        turn = self._begin_turn(session, user_text=Message("user", (Text(text), *labelled_parts)) if parts else text, origin="prompt", mutation=mutation)
        if typed:
            self.store.record_prompt(session_id, Message("user", (Text(text), *labelled_parts)))
        return {"status": "busy", "turn_id": turn["id"], "origin": "prompt"}

    def _start_input(self, session_id: str, item: dict[str, Any]) -> dict[str, Any]:
        session = self.session(session_id)
        self._check_turn_start(session)
        turn = self._begin_turn(session, user_text=self.inputs.message(item), origin="prompt", input_id=item["id"])
        return {"status": "busy", "turn_id": turn["id"], "input_id": item["id"], "mode": item["mode"], "state": "pending"}

    def release_input(self, session_id: str) -> dict[str, Any]:
        session = self.session(session_id)
        self._check_turn_start(session)
        items = self.inputs.list(session_id)
        if not items:
            raise ValueError("no queued input")
        self.inputs.hold(session_id, False)
        self.bus.publish("input.held", {"session_id": session_id, "held": False}, session_id=session_id)
        return self._start_input(session_id, items[0])

    def continue_session(self, session_id: str, *, mutation: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a normal turn from the stored context, with no new user message.

        This is an explicit client action. It refuses an empty conversation, an
        archived tree, a running turn and a session a crash left unreconciled; it
        never calls startup recovery for other sessions.
        """
        session = self.session(session_id)
        self._check_turn_start(session)
        unfinished = next((turn for turn in self.store.turns_for_session(session_id)
                           if turn["status"] == "running" or turn["recovery"] == "pending"), None)
        if unfinished is not None:
            raise Conflict(
                f"session {session_id} has unfinished durable turn {unfinished['id']}; restart the daemon so "
                "startup recovery reconciles it before continuing"
            )
        if not self.store.messages(session_id):
            raise ValueError(
                f"session {session_id} has no stored messages to continue; send a prompt first "
                f"(POST /api/session/{session_id}/prompt or `nexus run --session {session_id} \"…\"`)"
            )
        link: dict[str, Any] = {"predecessor": predecessor(self.store, session_id)}
        try:
            reconciliation: Reconciliation | None = reconcile(self.store, session_id)
        except ContextInvalid as exc:
            raise ValueError(str(exc)) from exc
        if reconciliation is not None:
            # The repair committed in one transaction; announce both stored messages once.
            self._publish_message(session_id, reconciliation.tool_message)
            self._publish_message(session_id, reconciliation.notice)
            link["reconciliation"] = reconciliation.json()
        else:
            link["reconciliation"] = None
        response = {"status": "busy", "origin": "continuation", "continuation": link,
                    "reconciled": link.get("reconciliation")}
        if mutation is not None:
            mutation = {**mutation, "body": {**response, "idempotency_key": mutation["key"]}}
        turn = self._begin_turn(session, user_text=NO_USER_MESSAGE, origin="continuation",
                                continuation=link, mutation=mutation)
        return {**response, "turn_id": turn["id"]}

    async def _run(self, session_id: str, user_text: str | Message | NoUserMessage, turn_id: str, *,
                   origin: str = "prompt", continuation: dict[str, Any] | None = None, input_id: str | None = None) -> None:
        started: set[str] = set()
        finished: dict[str, ToolResult] = {}
        active_request: str | None = None
        active_attempt: str | None = None
        request_parts: list[dict[str, Any]] = []
        clock = ReasoningClock()
        step = 0
        closed = False
        interrupted = False
        try:
            agent = self._agent(self.session(session_id))
            async for event in agent.turn(user_text):
                if isinstance(event, MessageAdded):
                    if event.message.role == "assistant":
                        started.clear()
                        finished.clear()
                        stored = self.store.save_assistant_message(
                            session_id, event.message, turn_id=turn_id, request_id=active_request)
                        active_request = None
                        active_attempt = None
                        request_parts = []
                        clock = ReasoningClock()
                    elif event.message.role == "tool":
                        stored = self.store.save_tool_message(session_id, event.message, turn_id=turn_id)
                    elif event.input_id or input_id:
                        iid = event.input_id or input_id
                        item = self.inputs.get(session_id, iid)
                        stored = self.inputs.deliver(session_id, iid, turn_id)
                        agent.messages[-1] = stored.message
                        published = self.bus.publish("input.delivered", {**item, "state": "delivered", "message_id": stored.id, "turn_id": turn_id}, session_id=session_id)
                        with self.store.db:
                            self.store.db.execute("update pending_input set event_id = ? where id = ?", (published.get("id", -1), iid))
                        input_id = None
                    else:
                        stored = self.store.append_message(session_id, event.message)
                    self._commit_file_reads(session_id, event.message)
                    self._publish_message(session_id, stored)
                else:
                    if isinstance(event, StepStart):
                        step = event.step
                        # `refresh` has re-bound the agent: record what this step runs with.
                        active_request = self.store.start_request(
                            turn_id, session_id, event.step, model=self._model_ref(session_id),
                            effort=agent.reasoning_effort)["id"]
                        active_attempt = f"{active_request}:1"
                        self.store.start_attempt(turn_id, session_id, active_request, active_attempt, 1)
                        self.store.mark_attempt_started(active_attempt)
                        request_parts = []
                        clock = ReasoningClock()
                    elif isinstance(event, ReasoningDelta):
                        clock.delta()
                    elif isinstance(event, (TextDelta, ToolCallStarted)):
                        clock.close()
                    elif isinstance(event, PartDone):
                        if isinstance(event.part, Reasoning):
                            clock.close()
                        request_parts.append(to_json(event.part))
                    elif isinstance(event, Usage):
                        if active_request is not None:
                            self.store.record_usage(active_request, active_attempt, event)
                    elif isinstance(event, Done):
                        if active_request is not None:
                            self.store.finish_request(
                                active_request, stop=event.stop, response_id=event.response_id,
                                parts=request_parts, reasoning_seconds=clock.seconds())
                        if active_attempt is not None:
                            self.store.finish_attempt(active_attempt, "completed")
                    elif isinstance(event, TurnEnd):
                        self.store.mark_turn_ended(turn_id, event.stop, event.steps)
                    elif isinstance(event, ToolStart):
                        started.add(event.call.id)
                        self.store.mark_tool_started(turn_id, session_id, event.call)
                    elif isinstance(event, ToolEnd):
                        finished[event.call.id] = event.result
                        self.store.mark_tool_finished(
                            turn_id, session_id, event.call, event.result,
                            self._pending_observation_data(session_id, event.call.id))
                    elif isinstance(event, RetryScheduled):
                        if active_attempt is not None:
                            self.store.finish_attempt(active_attempt, "failed", error=event.reason)
                        active_attempt = event.attempt_id
                        self.store.start_attempt(
                            turn_id, session_id, active_request, event.attempt_id, event.next_attempt,
                            delay=event.delay, error=event.reason)
                    elif isinstance(event, RetryStarted):
                        self.store.mark_attempt_started(event.attempt_id)
                    elif isinstance(event, RetryExhausted):
                        # The failed attempt is closed by the ProviderError handler;
                        # `event.attempt_id` is only an event correlation id.
                        pass
                    data = to_json(event)
                    self.bus.publish(data["type"], data, session_id=session_id)
            self.store.finish_turn(turn_id, "completed", steps=step)
            closed = True
        except asyncio.CancelledError:
            interrupted = True
            if active_request is not None:
                self.store.interrupt_request(active_request, reason="interrupted by the user",
                                             reasoning_seconds=clock.seconds())
            if active_attempt is not None:
                self.store.finish_attempt(active_attempt, "interrupted")
            self._close_dangling_calls(
                session_id, started, finished, turn_id,
                pending_approvals=self._pending_approval_calls(session_id))
            error = {"kind": "interrupted", "message": "interrupted by the user"}
            self._fail(session_id, error)
            self.store.finish_turn(turn_id, "interrupted", steps=step, error=error)
            closed = True
        except ProviderError as exc:
            error = {"kind": "provider", "message": str(exc), "status": exc.status, "body": exc.body}
            if active_request is not None:
                self.store.fail_request(active_request, error, reasoning_seconds=clock.seconds())
            if active_attempt is not None:
                self.store.finish_attempt(active_attempt, "failed", error=str(exc))
            self._fail(session_id, error)
            self.store.finish_turn(turn_id, "failed", steps=step, error=error)
            closed = True
        except (AuthError, CredentialError, ValueError) as exc:
            error = {"kind": "auth" if not isinstance(exc, ValueError) else "request", "message": str(exc)}
            if active_request is not None:
                self.store.fail_request(active_request, error, reasoning_seconds=clock.seconds())
            if active_attempt is not None:
                self.store.finish_attempt(active_attempt, "failed", error=str(exc))
            self._fail(session_id, error)
            self.store.finish_turn(turn_id, "failed", steps=step, error=error)
            closed = True
        finally:
            if not closed:  # an unexpected failure still closes the durable turn visibly
                if active_request is not None:
                    self.store.interrupt_request(active_request, reason="the turn ended without a recorded outcome",
                                                 reasoning_seconds=clock.seconds())
                if active_attempt is not None:
                    self.store.finish_attempt(active_attempt, "failed", error="the turn ended without a recorded outcome")
                self.store.finish_turn(
                    turn_id, "interrupted", steps=step,
                    error={"kind": "internal", "message": "the turn ended without a recorded outcome"})
            # Before idle, so a client has the closed turn when it sees the session idle.
            # A clean close's bookkeeping (`recovery` back to none) lands below; a crash
            # before it leaves the turn to startup recovery, which publishes again.
            self.publish_turn_summary(session_id, turn_id, recovery="none" if closed else None)
            self.pending_reads.pop(session_id, None)
            self.turns.pop(session_id, None)
            # A reserved input whose first message did not commit remains pending.
            if input_id:
                with self.store.db:
                    self.store.db.execute("update pending_input set turn_id = null where id = ? and state = 'pending'", (input_id,))
            # Steers that arrived at the terminal boundary did not reach the model.
            for item in self.inputs.list(session_id, "steer"):
                with self.store.db:
                    self.store.db.execute("update pending_input set mode = 'queue' where id = ?", (item["id"],))
                self.bus.publish("input.updated", self.inputs.get(session_id, item["id"]), session_id=session_id)
            if (interrupted or not closed) and self.inputs.list(session_id):
                self.inputs.hold(session_id, True)
                if self.inputs.list(session_id):
                    self.bus.publish("input.held", {"session_id": session_id, "held": True}, session_id=session_id)
            # A move queued during the turn lands here if no later call boundary did.
            self._apply_pending_directory(session_id)
            for approval_id, (sid, future, _) in list(self.approvals.items()):
                if sid == session_id:
                    future.cancel()
                    self.approvals.pop(approval_id, None)
                    # Make a vanished wait explicit, so clients and retention do not
                    # treat it as still pending forever.
                    self.bus.publish("permission.replied", {
                        "id": approval_id, "session_id": session_id, "allow": False,
                        "outcome": "cancelled", "reason": "the turn ended before this approval was answered",
                    }, session_id=session_id)
            self.close_questions({session_id}, "the turn ended without an answer")
            leftover = self.inboxes.pop(session_id, [])
            watcher = self.notify.get(session_id)  # a message sent as the turn ended starts another turn
            self._deliver(session_id)  # before idle, so the tree is never all idle while a parent is about to wake
            queued = self.inputs.list(session_id, "queue") if not self.inputs.held(session_id) else []
            if queued and self.store.session(session_id) is not None:
                if leftover:
                    self.inboxes.setdefault(session_id, []).extend(leftover)
                self._start_input(session_id, queued[0])
            elif leftover and self.store.session(session_id) is not None:
                if watcher is not None:
                    self.notify[session_id] = watcher
                self.prompt(session_id, "\n\n".join(leftover))  # notices that arrived as the turn ended
            else:
                idle: dict[str, Any] = {"status": "idle", "turn_id": turn_id, "origin": origin}
                if continuation is not None:
                    idle["continuation"] = continuation
                    if continuation.get("reconciliation"):
                        idle["reconciled"] = continuation["reconciliation"]
                self.bus.publish("session.status", idle, session_id=session_id)
            if closed:
                self.store.set_turn_recovery(turn_id, "none")
            self.store.resolve_mutations_for_turn(turn_id, lifetime=self.mutation_lifetime)

    def publish_turn_summary(self, session_id: str, turn_id: str, *, recovery: str | None = None) -> None:
        """Announce a closed turn's summary: the same entry `GET /api/session/{id}/turn` returns.

        `recovery` is the state the caller is about to record, when it publishes
        before that last bookkeeping write.
        """
        if self.store.session(session_id) is None:
            return
        summary = self.store.turn_summary(turn_id)
        if summary is not None:
            if recovery is not None:
                summary["recovery"] = recovery
            self.bus.publish("turn.summary", summary, session_id=session_id)

    def _model_ref(self, session_id: str) -> str | None:
        """The `provider/model` ref the session runs with now, as `_agent` resolves it."""
        session = self.store.session(session_id)
        if session is None:
            return None
        return self.resolve_model(session, self.snapshot_for(session)["agent"])[0]

    def _fail(self, session_id: str, error: dict[str, Any]) -> None:
        self.errors[session_id] = error["message"]
        self.bus.publish("session.error", error, session_id=session_id)

    def _pending_observation_data(self, session_id: str, call_id: str) -> list[dict[str, Any]]:
        """Observations from one read call, kept with its result for crash recovery."""
        return [observation.json() | {"path": observation.path}
                for observation in self.pending_reads.get(session_id, [])
                if observation.call_id == call_id]

    def _commit_file_reads(self, session_id: str, message: Message) -> None:
        if message.role != "tool":
            return
        calls = {p.call_id for p in message.parts if isinstance(p, ToolResult) and not p.is_error}
        guard = FileGuard(self.store, session_id, self.file_lock, self.pending_reads.get(session_id, []))
        guard.commit_reads(calls)

    def _pending_approval_calls(self, session_id: str) -> set[str]:
        """Calls still waiting for a user decision when a turn was cancelled.

        The in-memory wait is unwound before this runs, so the durable
        `permission.asked`/`permission.replied` records are the evidence.
        """
        messages = self.store.messages(session_id)
        lifecycle = self.store.wait_lifecycle(session_id, since=messages[-1].created if messages else None)
        return {call_id for call_id, request in open_approvals(lifecycle).items()
                if not request.get("replied") or request.get("outcome") == "cancelled"}

    def _close_dangling_calls(self, session_id: str, started: set[str],
                              finished: dict[str, ToolResult], turn_id: str, *,
                              pending_approvals: set[str] | None = None) -> None:
        messages = self.store.messages(session_id)
        if not messages or messages[-1].message.role != "assistant":
            return
        calls = [p for p in messages[-1].message.parts if isinstance(p, ToolCall)]
        if not calls:
            return
        waiting = pending_approvals or set()
        results: list[ToolResult] = []
        for call in calls:
            if call.id in finished:
                results.append(finished[call.id])
            elif call.id in waiting:
                results.append(ToolResult(
                    call.id,
                    "interrupted by the user while this call was waiting for approval; it did not run",
                    is_error=True,
                ))
            elif call.id in started:
                results.append(ToolResult(
                    call.id,
                    "interrupted by the user while this tool was running; its result is unknown and it may have "
                    "changed the workspace",
                    is_error=True,
                ))
            else:
                results.append(ToolResult(call.id, "interrupted by the user before this tool ran", is_error=True))
        message = Message("tool", tuple(results))
        stored = self.store.save_tool_message(session_id, message, turn_id=turn_id)
        self._commit_file_reads(session_id, message)
        self._publish_message(session_id, stored)

    async def interrupt(self, session_id: str, *, reason: str = "the turn was interrupted") -> bool:
        stopped = False
        descendants = self.store.descendants(session_id)
        # Closing a pending question before cancelling its turn publishes one cancellation outcome,
        # so a reply racing with the interrupt cannot also resolve it.
        self.close_questions({session_id, *(child.id for child in descendants)}, reason)
        for child in descendants:
            self.notify.pop(child.id, None)  # an interrupted parent is not woken by its children
            stopped = await self._cancel(child.id) or stopped
        return await self._cancel(session_id) or stopped

    async def _cancel(self, session_id: str) -> bool:
        task = self.turns.get(session_id)
        if task is None:
            return False
        # A newly created task must enter its try/finally before cancellation;
        # otherwise a reserved queued input and durable turn would be stranded.
        await asyncio.sleep(0)
        task = self.turns.get(session_id, task)
        if task.done():
            return True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    # -- approvals -------------------------------------------------------------

    async def _approve(self, session_id: str, call: ToolCall, args: dict[str, Any]) -> bool:
        if call.name in AUTO_APPROVED or self.session(session_id).permission == "allow":
            return True
        approval_id = new_id("per")
        request = {"id": approval_id, "session_id": session_id, "tool": call.name, "call_id": call.id, "args": args}
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approvals[approval_id] = (session_id, future, request)
        self.bus.publish("permission.asked", request, session_id=session_id)
        try:
            return await future
        finally:
            removed = self.approvals.pop(approval_id, None)
            answered = future.done() and not future.cancelled()
            if not future.done():
                future.cancel()
            if removed is not None and not answered:
                self.bus.publish("permission.replied", {
                    "id": approval_id, "session_id": session_id, "allow": False,
                    "outcome": "cancelled", "reason": "the turn ended before this approval was answered",
                }, session_id=session_id)

    def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return [request for sid, _, request in self.approvals.values() if sid == session_id]

    def reply(self, session_id: str, approval_id: str, allow: bool) -> None:
        entry = self.approvals.get(approval_id)
        if entry is None or entry[0] != session_id:
            raise NotFound(f"no pending permission {approval_id}")
        if entry[1].done():
            raise Conflict(f"permission {approval_id} was already resolved")
        self.bus.publish("permission.replied", {"id": approval_id, "session_id": session_id,
                                                "outcome": "allowed" if allow else "denied", "allow": allow},
                         session_id=session_id)
        if not entry[1].done():
            entry[1].set_result(allow)

    # -- questions -------------------------------------------------------------

    async def ask_user(self, session_id: str, call_id: str | None,
                       questions: list[dict[str, Any]]) -> str:
        """Publish `question.asked`, wait for a validated answer, and return the model-visible result.

        No timeout of its own: the wait ends with an answer or with cancellation
        (an interrupted turn, a deleted session/project, or a code cell ending).
        """
        request = PendingQuestion(
            id=new_id("qst"), session_id=session_id, call_id=call_id, questions=questions,
            created=question_contract.now(), future=asyncio.get_running_loop().create_future(),
        )
        self.questions[request.id] = request
        self.bus.publish("question.asked", question_contract.asked_event(request), session_id=session_id)
        try:
            answers = await request.future
        except asyncio.CancelledError:
            self._close_question(request, "the turn or code cell was interrupted")
            raise
        # The validated question/answer limits bound this structured result. Generic
        # text clipping would corrupt JSON and silently discard a valid answer.
        return question_contract.result_text(questions, answers)

    def pending_questions(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Pending question requests, for one session or every session."""
        return [
            request.json()
            for request in self.questions.values()
            if session_id is None or request.session_id == session_id
        ]

    def reply_question(self, session_id: str, question_id: str, answers: Any) -> None:
        """Validate and resolve one question request exactly once."""
        request = self.questions.get(question_id)
        if request is None:
            if question_id in self.resolved_questions:
                raise Conflict(
                    f"question {question_id} is already {self.resolved_questions[question_id]}; "
                    "a question request resolves once"
                )
            raise NotFound(f"no pending question {question_id}")
        if request.session_id != session_id:
            raise NotFound(f"no pending question {question_id}")
        if request.future.cancelled():
            self._close_question(request, "the turn or code cell was interrupted")
            raise Conflict(f"question {question_id} is already cancelled")
        validated = question_contract.validate_answers(request.questions, answers)
        request.closed = True
        self.questions.pop(question_id, None)
        self._remember_question(question_id, "answered")
        self.bus.publish(
            "question.replied",
            question_contract.replied_event(request, outcome="answered", answers=validated),
            session_id=session_id,
        )
        if not request.future.done():
            request.future.set_result(validated)

    def _close_question(self, request: PendingQuestion, reason: str) -> None:
        if request.closed:
            return
        request.closed = True
        self.questions.pop(request.id, None)
        self._remember_question(request.id, "cancelled")
        self.bus.publish(
            "question.replied",
            question_contract.replied_event(request, outcome="cancelled", reason=reason),
            session_id=request.session_id,
        )
        if not request.future.done():
            request.future.cancel()

    def close_questions(self, session_ids: set[str] | None, reason: str) -> None:
        """Close pending questions (all of them when `session_ids` is None) with an explicit outcome."""
        for request in list(self.questions.values()):
            if session_ids is None or request.session_id in session_ids:
                self._close_question(request, reason)

    def _remember_question(self, question_id: str, outcome: str) -> None:
        self.resolved_questions[question_id] = outcome
        while len(self.resolved_questions) > MAX_RESOLVED_QUESTIONS:
            self.resolved_questions.pop(next(iter(self.resolved_questions)))

    async def shutdown(self) -> None:
        for session_id in list(self.turns):
            await self.interrupt(session_id, reason="the daemon is shutting down")
        self.close_questions(None, "the daemon is shutting down")
        await self.retention.stop()
        await self.jobs.shutdown()
        await self.kernels.shutdown()
