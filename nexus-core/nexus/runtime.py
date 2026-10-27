"""The daemon's state and work: projects, sessions, turns and approvals.

Everything the HTTP API does goes through `Runtime`, and everything it changes
is published on the bus:

| event | when |
| --- | --- |
| `project.created` / `project.updated` / `project.deleted` | project changes |
| `session.created` / `session.updated` / `session.deleted` | session changes |
| `session.status` | `{"status": "busy" | "idle"}` around each turn |
| `message.added` | a message was saved (user, assistant, tool) |
| `permission.asked` / `permission.replied` | a tool call waits for approval |
| `session.error` | a turn failed (provider, auth or tool-loop error) |
| everything else | the loop and stream events, unchanged (`wire_request`, `text_delta`, `tool_start`, …) |

Each turn resolves the session's agent (`nexus/agents.py`, reloaded every turn
so edits to agent files apply at once): its prompt plus the environment block
becomes the instructions, its `tools` limit the tool list, and its `model`,
`effort` and `steps` apply unless the session sets them.

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
new parent turn if it is idle (`session.notified`).
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from . import agents as agent_files
from .bash_jobs import NOTICE_CHARS, Job, Jobs
from .bus import Bus
from .codec import message_to_json, to_json
from .kernels import Kernels
from .llmhub.auth import AuthError, CredentialError
from .llmhub.hub import provider_for
from .llmhub.types import Message, ProviderError, Text, ToolCall, ToolResult, ToolSpec
from .loop import MAX_STEPS, Agent, MessageAdded
from .store import Project, Session, Store, StoredMessage, new_id
from .tools import BASH_JOB_TOOLS, DEFAULT_TOOLS, Tool, ToolContext, ToolError
from .tools import bash as bash_tool
from .tools.base import clip
from .tools.code import SPEC as CODE_SPEC

AUTO_APPROVED = {"read", "bash_output", "bash_kill", "session", "code"}
CHILD_RESULT_CHARS = 30_000
TITLE_CHARS = 60


class Busy(Exception):
    pass


class NotFound(Exception):
    pass


def project_json(p: Project) -> dict[str, Any]:
    return {"id": p.id, "path": p.path, "name": p.name, "created": p.created, "updated": p.updated}


def message_json(m: StoredMessage) -> dict[str, Any]:
    return {"id": m.id, "session_id": m.session_id, "seq": m.seq, "created": m.created, **message_to_json(m.message)}


class Runtime:
    def __init__(self, store: Store, bus: Bus, default_model: str) -> None:
        self.kernels = Kernels()
        self.jobs = Jobs()
        self.store = store
        self.bus = bus
        self.default_model = default_model
        self.turns: dict[str, asyncio.Task[None]] = {}
        self.approvals: dict[str, tuple[str, asyncio.Future[bool], dict[str, Any]]] = {}  # id -> (session, future, request)
        self.inboxes: dict[str, list[str]] = {}  # session id -> notices waiting for its next model call
        self.notify: dict[str, str] = {}  # child id -> parent id, for children started with wait: false
        self.errors: dict[str, str] = {}  # session id -> how its last turn failed
        self._parents: dict[str, str | None] = {}  # session id -> parent id (never changes)
        bus.sink = self._record

    def _record(self, event: dict[str, Any]) -> None:
        """Store every event that belongs to a session (the durable event log)."""
        if event["session_id"] is None or event["type"] == "session.deleted":
            return
        try:
            self.store.append_event(event)
        except sqlite3.IntegrityError:
            pass  # the session was deleted while the event was in flight

    # -- projects ------------------------------------------------------------

    def open_project(self, path: str, name: str | None = None) -> Project:
        project, created = self.store.project_for_path(path, name)
        if created:
            self.bus.publish("project.created", project_json(project))
        return project

    def project(self, project_id: str) -> Project:
        project = self.store.project(project_id)
        if project is None:
            raise NotFound(f"no project {project_id}")
        return project

    def rename_project(self, project_id: str, name: str) -> Project:
        self.project(project_id)
        project = self.store.update_project(project_id, name=name)
        self.bus.publish("project.updated", project_json(project))  # type: ignore[arg-type]
        return project  # type: ignore[return-value]

    async def delete_project(self, project_id: str) -> None:
        sessions = self.store.sessions(project_id)
        for session in sessions:
            await self.interrupt(session.id)
        await self.jobs.cancel_for({session.id for session in sessions})
        if not self.store.delete_project(project_id):
            raise NotFound(f"no project {project_id}")
        self.bus.publish("project.deleted", {"id": project_id})

    # -- sessions ------------------------------------------------------------

    def session_json(self, s: Session) -> dict[str, Any]:
        """`model`/`effort` are what the next turn uses; `*_source` says where they came from."""
        try:
            agent = self.agent_def(s)
            model, model_source = self.resolve_model(s, agent)
            effort = s.effort or agent.effort
            effort_source = "session" if s.effort else ("agent" if agent.effort else "provider")
            tools = self.tool_names(s, agent)
        except ValueError:
            tools = []
            model, model_source = s.model or self.default_model, "session" if s.model else "default"
            effort, effort_source = s.effort, "session" if s.effort else "provider"
        return {
            "id": s.id, "project_id": s.project_id, "directory": s.directory, "title": s.title, "agent": s.agent,
            "parent_id": s.parent_id, "parent_call_id": s.parent_call_id, "launched_by": s.launched_by,
            "children": [c.id for c in self.store.children(s.id)],
            "model": model, "model_source": model_source, "effort": effort, "effort_source": effort_source,
            "tools": tools, "disabled_tools": sorted(s.disabled()),
            "permission": s.permission, "status": "busy" if s.id in self.turns else "idle",
            "created": s.created, "updated": s.updated,
        }

    def agents(self, project_path: str | None) -> agent_files.AgentSet:
        return agent_files.load(project_path)

    def agent_def(self, session: Session) -> agent_files.AgentDef:
        return self.agents(self.project(session.project_id).path).get(session.agent)

    def resolve_model(self, session: Session, agent: agent_files.AgentDef) -> tuple[str, str]:
        if session.model:
            return session.model, "session"
        if agent.model:
            return agent.model, "agent"
        return self.default_model, "default"

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
        found.get(agent, child=parent is not None)  # the agent's mode must fit
        session = self.store.create_session(project, title=title or "New session", agent=agent, model=model or "",
                                            permission=permission or "ask", effort=effort or None, parent=parent,
                                            parent_call_id=parent_call_id, launched_by=launched_by,
                                            disabled_tools=disabled_tools)
        self.bus.publish("session.created", self.session_json(session), session_id=session.id)
        return session

    def update_session(self, session_id: str, **fields: Any) -> Session:
        self.session(session_id)
        if fields.get("permission") not in (None, "ask", "allow"):
            raise ValueError("permission must be 'ask' or 'allow'")
        if fields.get("model"):
            provider_for(fields["model"])
        if fields.get("disabled_tools") is not None:
            self.check_tool_names(fields["disabled_tools"])
        if fields.get("agent") == "":
            raise ValueError("agent must not be empty")
        if fields.get("agent"):
            current = self.session(session_id)
            self.agents(self.project(current.project_id).path).get(fields["agent"], child=current.parent_id is not None)
        session = self.store.update_session(session_id, **fields)
        self.bus.publish("session.updated", self.session_json(session), session_id=session_id)  # type: ignore[arg-type]
        return session  # type: ignore[return-value]

    async def delete_session(self, session_id: str) -> None:
        await self.interrupt(session_id)  # also interrupts descendants, which the delete cascades to
        ids = {session_id, *(child.id for child in self.store.descendants(session_id))}
        await self.jobs.cancel_for(ids)  # a background command must not outlive the session that started it
        if not self.store.delete_session(session_id):
            raise NotFound(f"no session {session_id}")
        self.bus.publish("session.deleted", {"id": session_id}, session_id=session_id)

    def context(self, session_id: str) -> dict[str, Any]:
        """The request body the next turn would send, with no new user message."""
        agent = self._agent(self.session(session_id))
        return agent.provider.body(agent.request())

    # -- turns ---------------------------------------------------------------

    @staticmethod
    def _has_session_tool(session: Session, spec: agent_files.AgentDef) -> bool:
        return "session" in spec.control and session.parent_id is None and "session" not in session.disabled()

    def _task_tool_names(self, session: Session, spec: agent_files.AgentDef) -> list[str]:
        """The task tools this session offers, before the session control tool."""
        names = list(spec.tools) if spec.tools is not None else [t.spec.name for t in DEFAULT_TOOLS]
        names = [n for n in names if n not in session.disabled()]
        if "bash" in names:
            # Background jobs need their reader/killer; grant them with bash unless switched off individually.
            at = names.index("bash") + 1
            for tool in BASH_JOB_TOOLS:
                name = tool.spec.name
                if name not in names and name not in session.disabled():
                    names.insert(at, name)
                    at += 1
        return names

    def tool_names(self, session: Session, spec: agent_files.AgentDef) -> list[str]:
        """The tools the session's next turn offers, in order."""
        names = self._task_tool_names(session, spec)
        return names + (["session"] if self._has_session_tool(session, spec) else [])

    @staticmethod
    def check_tool_names(names: Any) -> list[str]:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ValueError("disabled_tools must be a list of tool names")
        known = [t.spec.name for t in DEFAULT_TOOLS + BASH_JOB_TOOLS] + list(agent_files.CONTROL_TOOLS)
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
        notice = self._job_notice(job)
        if job.session_id in self.turns:
            self.inboxes.setdefault(job.session_id, []).append(notice)
        else:
            self.prompt(job.session_id, notice)

    @staticmethod
    def _job_notice(job: Job) -> str:
        head = f"[Notification: background bash {job.status}]\njob {job.id} · {clip(repr(job.command), 500)}"
        if job.exit_code is not None:
            head += f" · exit code {job.exit_code}"
        if job.dropped:
            head += f" · {job.dropped} chars dropped"
        output = job.output[-NOTICE_CHARS:]
        if len(job.output) > len(output):
            output = f"[… earlier output omitted …]\n{output}"
        return f"{head}\n{output or '(no output)'}\nUse bash_output(job_id=\"{job.id}\") for the retained output."

    def _agent(self, session: Session) -> Agent:
        spec = self.agent_def(session)
        provider, model = provider_for(self.resolve_model(session, spec)[0])
        available = {tool.spec.name: tool for tool in (*DEFAULT_TOOLS, *BASH_JOB_TOOLS)}
        names = self._task_tool_names(session, spec)
        unknown = [n for n in names if n not in available]
        if unknown:
            raise ValueError(f"agent {spec.name} lists unknown tools: {', '.join(unknown)}; available: {', '.join(available)}")
        workspace = Path(session.directory)
        root = self._root(session)

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

        bound = {"bash": run_bash, "bash_output": run_bash_output, "bash_kill": run_bash_kill}

        def bind(tool: Tool) -> Tool:
            runner = bound.get(tool.spec.name)
            return Tool(tool.spec, runner, tool.guidance) if runner else tool

        tools = [bind(available[n]) for n in names]
        if "code" in names:
            task_tools = [t for t in tools if t.spec.name != "code"]

            async def run_code(args: dict[str, Any], ctx: ToolContext) -> str:
                return await self.kernels.run(
                    args, root=root.id, caller=session.id, ctx=ctx, tools=task_tools,
                    approve=lambda call, a: self._approve(session.id, call, a),
                    publish=lambda kind, data: self.bus.publish(kind, data, session_id=session.id),
                )

            tools = [Tool(CODE_SPEC, run_code, available["code"].guidance) if t.spec.name == "code" else t for t in tools]
        if self._has_session_tool(session, spec):
            tools.append(self._session_tool(session))
        return Agent(
            provider, model=model, workspace=workspace,
            tools=tuple(tools),
            instructions=agent_files.render_prompt(spec, [(t.spec.name, t.guidance) for t in tools], workspace),
            reasoning_effort=session.effort or spec.effort, max_steps=spec.steps or MAX_STEPS,
            messages=[m.message for m in self.store.messages(session.id)],
            conversation_id=session.id, approve=lambda call, args: self._approve(session.id, call, args),
            inbox=lambda: self.inboxes.pop(session.id, []),
        )

    # -- child sessions --------------------------------------------------------

    def _session_tool(self, parent: Session) -> Tool:
        # Built from the agents loaded for this project each turn (builtin, global, project), filtered by mode,
        # so a new agent file, or a mode change such as `build` made child-only, shows up here by itself.
        allowed = self.agents(self.project(parent.project_id).path).allowed(child=True)

        async def run(args: dict[str, Any], ctx: ToolContext) -> str:
            return await self._launch(parent.id, args, ctx.call_id)

        return Tool(ToolSpec(
            name="session",
            description=(
                "Start a child session running one of the agents listed in the system prompt's tools section, on a "
                "complete prompt. wait=true (default) returns its answer; wait=false returns at once and the answer "
                "arrives as a message when it finishes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": [a.name for a in allowed], "description": "Which agent runs the child"},
                    "prompt": {"type": "string", "description": "The complete task for the child"},
                    "wait": {"type": "boolean", "description": "Block until the child answers (default true)"},
                    "title": {"type": "string", "description": "Short title for the child session"},
                },
                "required": ["agent", "prompt"],
            },
        ), run, guidance=(
            "Start a child session: another agent works on a prompt in this directory, in a session the user can open. "
            "It does not see this conversation, so give it a complete prompt. `wait=true` returns its answer; "
            "`wait=false` lets you continue, and the answer arrives as a message when it finishes. Child agents:\n"
            + "\n".join(f"  - `{a.name}`: {a.description}" for a in allowed)
        ))

    async def _launch(self, parent_id: str, args: dict[str, Any], call_id: str | None) -> str:
        parent = self.session(parent_id)
        if parent.parent_id is not None:
            raise ToolError("child sessions cannot start sessions")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ToolError("prompt must be a non-empty string")
        try:
            child = self.create_session(
                self.project(parent.project_id), title=args.get("title") or None, model=None, permission=parent.permission,
                agent=str(args.get("agent") or ""), parent=parent, parent_call_id=call_id, launched_by="agent", disabled_tools=sorted(parent.disabled()),
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        wait = args.get("wait", True) is not False
        if not wait:
            self.notify[child.id] = parent.id
        self.prompt(child.id, prompt)
        if not wait:
            return (f"Started child session {child.id} (agent {child.agent}). Its answer will arrive as a message when it "
                    "finishes; continue with other work meanwhile.")
        task = self.turns[child.id]
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.interrupt(child.id)
            raise
        return self._child_report(child.id)

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
        if parent_id in self.turns:
            self.inboxes.setdefault(parent_id, []).append(notice)
        else:
            self.prompt(parent_id, notice)

    def prompt(self, session_id: str, text: str) -> None:
        session = self.session(session_id)
        if session_id in self.turns:
            raise Busy(f"session {session_id} is already running a turn")
        if not text.strip():
            raise ValueError("text must not be empty")
        if session.title == "New session":
            self.update_session(session_id, title=" ".join(text.split())[:TITLE_CHARS])
        self.errors.pop(session_id, None)
        # Published here, not in the task, so a session is never seen idle between being prompted and running.
        self.bus.publish("session.status", {"status": "busy"}, session_id=session_id)
        self.turns[session_id] = asyncio.create_task(self._run(session_id, text))

    async def _run(self, session_id: str, text: str) -> None:
        try:
            agent = self._agent(self.session(session_id))
            async for event in agent.turn(text):
                if isinstance(event, MessageAdded):
                    stored = self.store.append_message(session_id, event.message)
                    self.bus.publish("message.added", message_json(stored), session_id=session_id)
                else:
                    data = to_json(event)
                    self.bus.publish(data["type"], data, session_id=session_id)
        except asyncio.CancelledError:
            self._close_dangling_calls(session_id)
            self._fail(session_id, {"kind": "interrupted", "message": "interrupted by the user"})
        except ProviderError as exc:
            self._fail(session_id, {"kind": "provider", "message": str(exc), "status": exc.status, "body": exc.body})
        except (AuthError, CredentialError, ValueError) as exc:
            self._fail(session_id, {"kind": "auth" if not isinstance(exc, ValueError) else "request", "message": str(exc)})
        finally:
            self.turns.pop(session_id, None)
            for approval_id, (sid, future, _) in list(self.approvals.items()):
                if sid == session_id:
                    future.cancel()
                    self.approvals.pop(approval_id, None)
            leftover = self.inboxes.pop(session_id, [])
            self._deliver(session_id)  # before idle, so the tree is never all idle while a parent is about to wake
            if leftover and self.store.session(session_id) is not None:
                self.prompt(session_id, "\n\n".join(leftover))  # notices that arrived as the turn ended
            else:
                self.bus.publish("session.status", {"status": "idle"}, session_id=session_id)

    def _fail(self, session_id: str, error: dict[str, Any]) -> None:
        self.errors[session_id] = error["message"]
        self.bus.publish("session.error", error, session_id=session_id)

    def _close_dangling_calls(self, session_id: str) -> None:
        messages = self.store.messages(session_id)
        if not messages or messages[-1].message.role != "assistant":
            return
        calls = [p for p in messages[-1].message.parts if isinstance(p, ToolCall)]
        if calls:
            results = tuple(ToolResult(c.id, "interrupted by the user before this tool ran", is_error=True) for c in calls)
            stored = self.store.append_message(session_id, Message("tool", results))
            self.bus.publish("message.added", message_json(stored), session_id=session_id)

    async def interrupt(self, session_id: str) -> bool:
        stopped = False
        for child in self.store.descendants(session_id):
            self.notify.pop(child.id, None)  # an interrupted parent is not woken by its children
            stopped = await self._cancel(child.id) or stopped
        return await self._cancel(session_id) or stopped

    async def _cancel(self, session_id: str) -> bool:
        task = self.turns.get(session_id)
        if task is None:
            return False
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
            self.approvals.pop(approval_id, None)

    def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        return [request for sid, _, request in self.approvals.values() if sid == session_id]

    def reply(self, session_id: str, approval_id: str, allow: bool) -> None:
        entry = self.approvals.get(approval_id)
        if entry is None or entry[0] != session_id:
            raise NotFound(f"no pending permission {approval_id}")
        if not entry[1].done():
            entry[1].set_result(allow)
        self.bus.publish("permission.replied", {"id": approval_id, "allow": allow}, session_id=session_id)

    async def shutdown(self) -> None:
        for session_id in list(self.turns):
            await self.interrupt(session_id)
        await self.jobs.shutdown()
        await self.kernels.shutdown()
