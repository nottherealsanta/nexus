"""Host-side delivery of subagent reports to the root session.

Contract (plans/done/NONBLOCKING_SUBAGENTS_PLAN.md sections 3.1, 3.2, 3.8):

* A finished background child run is delivered to its root as an ordinary root
  input: ``mode="steer"``, ``origin={"kind": "subagent_report", ...}``, admitted
  once through ``Session.admit_submission`` (idempotent on the request id). A
  running root consumes it at its next iteration boundary; an idle root is woken by
  the supervisor, which starts a turn that consumes the queue head. There is no
  second delivery path and no ``agent.reported`` event.
* :class:`ReportDelivery` is the facade's side of the pool's seams
  (``AgentPool.report_sink``, ``root_active``, ``report_backlog``). The pool
  decides *when* (coalescing, bounds, the wake cap); this module decides *how*.
* **Reconcile on open.** When the daemon first opens a root session, a background
  run that ended (``agent.completed``) with no ``input.queued`` report covering it
  (a daemon that died between the two) is delivered. Statuses that mean the work
  was cut off (``interrupted``, ``cancelled``) are queued **without a wake**, so a
  restart never starts model calls on its own. The rebuilt text is the child's
  last assistant message plus the recorded error, clipped, with a ``context_read``
  pointer.
* A human message resets the pool's consecutive-wake counter and releases held
  reports (:meth:`ReportDelivery.human_input`).
* :meth:`ReportDelivery.activity` answers the one-shot wait: what still keeps a
  session busy after its turn ended.

Everything is bounded: reports per reconcile (``MAX_RECONCILED``), rebuilt text
(``RECONCILED_TEXT_CHARS``). Errors never cross the boundary unredacted.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..agents.pool import (
    REPORT_ORIGIN_KIND,
    AgentReport,
    ReportBatch,
    ReportLimits,
    take_batch,
)
from ..session.session import is_live_agent
from ..session.submissions import submission_digest
from ..util import redact_secrets
from . import protocol as p

if TYPE_CHECKING:
    from .facade import HostFacade

__all__ = ["MAX_RECONCILED", "RECONCILED_TEXT_CHARS", "ReportDelivery", "WAKING_STATUSES"]

#: Most missed reports one open will rebuild (older ones are skipped, never partial).
MAX_RECONCILED = 64
#: Clip for the child's last message in a rebuilt report.
RECONCILED_TEXT_CHARS = 8000
#: Run statuses that wake an idle root when reconciled; the rest queue silently.
WAKING_STATUSES = frozenset({"completed", "failed", "needs_attention", "refused"})
_SKIP_KINDS = frozenset({"manager", "child"})


class ReportDelivery:
    """The facade's report seams for one runtime."""

    def __init__(self, facade: HostFacade) -> None:
        self._facade = facade
        self._tasks: set[asyncio.Task] = set()

    # -- wiring ---------------------------------------------------------------

    def install(self) -> None:
        pool = getattr(self._facade.runtime, "agent_pool", None)
        if pool is None:
            return
        pool.report_sink = self.deliver
        pool.root_active = self.root_active
        pool.report_backlog = self.backlog

    async def aclose(self) -> None:
        tasks, self._tasks = list(self._tasks), set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- the pool's sink and probes ------------------------------------------------

    def _handle(self, session_id: str) -> Any:
        return self._facade._session(session_id, create=False, recover=False)

    def root_active(self, session_id: str) -> bool:
        return bool(getattr(self._handle(session_id), "active", False))

    def backlog(self, session_id: str) -> int:
        return int(getattr(self._handle(session_id), "queued_report_count", 0))

    async def deliver(self, batch: ReportBatch) -> bool:
        """Admit ``batch`` as one ``steer`` input; wake an idle root. ``True`` if it woke it.

        Idempotent on the batch's request id: a repeated delivery admits nothing
        and wakes nothing. No title is derived from a report.
        """
        session_id = batch.root_session
        handle = self._handle(session_id)
        digest = submission_digest("subagent_report", list(batch.keys), "steer")
        queued_id, turn_id, fresh = handle.admit_submission(
            batch.render(), request_id=batch.request_id(), digest=digest,
            mode="steer", origin=batch.origin(),
        )
        if not fresh:
            return False
        was_active = bool(getattr(handle, "active", False))
        if batch.wake:
            await self._facade.supervisor.submit(
                session_id, handle, None, queued_id=queued_id, turn_id=turn_id
            )
        return batch.wake and not was_active

    def human_input(self, session_id: str, origin: Mapping[str, Any] | None = None) -> None:
        """A non-report input arrived: reset the wake counter and release held reports."""
        if origin and origin.get("kind") == REPORT_ORIGIN_KIND:
            return
        pool = getattr(self._facade.runtime, "agent_pool", None)
        root = pool.get(session_id) if pool is not None else None
        if root is not None:
            root.resume_reports()

    # -- one-shot quiescence -------------------------------------------------------

    def activity(self, session_id: str) -> p.SessionActivityResult:
        handle = self._handle(session_id)
        pool = getattr(self._facade.runtime, "agent_pool", None)
        root = pool.get(session_id) if pool is not None else None
        return p.SessionActivityResult(
            session=session_id,
            turn_active=bool(getattr(handle, "active", False)),
            queued=int(getattr(handle, "queue_depth", 0)),
            running_agents=len(root.running()) if root is not None else 0,
            reports_pending=root.report_pending if root is not None else 0,
            wait_seconds=self._oneshot_wait_seconds(),
        )

    def _agents_section(self) -> Any:
        loader = getattr(self._facade.runtime, "_load_config", None)
        try:
            config = loader() if callable(loader) else None
        except Exception:  # noqa: BLE001 - unreadable config means defaults
            config = None
        return getattr(getattr(config, "v2", None), "agents", None)

    def _oneshot_wait_seconds(self) -> int:
        value = getattr(self._agents_section(), "oneshot_wait_seconds", 600)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 600

    # -- reconcile on open -----------------------------------------------------------

    def reconcile(self, session_id: str, handle: Any) -> None:
        """Schedule delivery of background runs that ended without a delivered report."""
        if getattr(handle, "kind", None) in _SKIP_KINDS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            missing = self._missing(session_id, handle)
        except Exception:  # noqa: BLE001 - a bad log never blocks an open
            return
        if not missing:
            return
        task = loop.create_task(self._deliver_missing(session_id, missing))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _missing(self, session_id: str, handle: Any) -> list[AgentReport]:
        background: dict[str, Mapping[str, Any]] = {}
        completed: list[tuple[str, int, Mapping[str, Any]]] = []
        covered: set[str] = set()
        #: ``agent:run`` of continuations started without waiting (their report is pushed).
        resumed_background: set[str] = set()
        for event in handle.read().events():
            data = event.data
            if not isinstance(data, Mapping):
                continue
            if event.type == "agent.spawned":
                agent = _agent_id(data)
                if agent and data.get("background") is True:
                    background[agent] = data
            elif event.type == "agent.resumed":
                agent = _agent_id(data)
                run = data.get("run")
                if agent and data.get("background") is True and isinstance(run, int) and not isinstance(run, bool):
                    resumed_background.add(f"{agent}:{run}")
            elif event.type == "agent.completed":
                agent = _agent_id(data)
                run = data.get("run")
                run = run if isinstance(run, int) and not isinstance(run, bool) and run > 0 else 1
                if agent:
                    completed.append((agent, run, data))
            elif event.type == "input.queued":
                origin = data.get("origin")
                if isinstance(origin, Mapping) and origin.get("kind") == REPORT_ORIGIN_KIND:
                    agents = origin.get("agents")
                    if isinstance(agents, list):
                        covered.update(item for item in agents if isinstance(item, str))
                    if origin.get("agent") is not None and origin.get("run") is not None:
                        covered.add(f"{origin['agent']}:{origin['run']}")
        pool = getattr(self._facade.runtime, "agent_pool", None)
        root = pool.get(session_id) if pool is not None else None
        held = set(root.pending_report_keys()) if root is not None else set()
        reports: list[AgentReport] = []
        for agent, run, data in completed:
            key = f"{agent}:{run}"
            # Run 1 is background when the spawn was; a continuation only when sent without wait.
            if agent not in background or key in covered or key in held:
                continue
            if run > 1 and key not in resumed_background:
                continue
            if is_live_agent(session_id, agent):
                continue
            reports.append(self._rebuild(session_id, agent, run, data, background[agent]))
        return reports[-MAX_RECONCILED:]

    def _rebuild(
        self, root_session: str, agent: str, run: int, data: Mapping[str, Any], spawned: Mapping[str, Any]
    ) -> AgentReport:
        meta = data.get("agent") if isinstance(data.get("agent"), Mapping) else data
        spawn_meta = spawned.get("agent") if isinstance(spawned.get("agent"), Mapping) else spawned
        child_session = str(meta.get("session") or spawn_meta.get("session") or "")
        status = str(data.get("status") or "completed")
        lines = [
            "[This report was rebuilt from the session log after a restart or a "
            "missed delivery; the original wording is not available.]"
        ]
        error = data.get("error")
        if error:
            lines.append(f"error: {redact_secrets(str(error))[:500]}")
        text = self._final_text(root_session, agent)
        if text:
            lines.append(text)
        return AgentReport(
            agent_id=agent, run=run, status=status,
            role=str(meta.get("type") or spawn_meta.get("type") or ""),
            model=data.get("resolved_model") or spawn_meta.get("model") or None,
            tier=meta.get("tier") or spawn_meta.get("tier") or None,
            session_id=child_session, body="\n".join(lines),
        )

    def _final_text(self, session_id: str, agent_id: str) -> str:
        """The child's last assistant message, read from the root's reduced view."""
        try:
            view, _seq = self._facade.state(session_id)
            agent = view.agents.get(agent_id)
            if agent is None:
                return ""
            for turn in reversed(agent.body.turns):
                for message in reversed(turn.messages):
                    text = str(getattr(message, "text", "") or "").strip()
                    if text and getattr(message, "role", "assistant") == "assistant":
                        return text[:RECONCILED_TEXT_CHARS]
        except Exception:  # noqa: BLE001 - best effort; the context_read pointer still works
            return ""
        return ""

    async def _deliver_missing(self, session_id: str, reports: list[AgentReport]) -> None:
        limits = ReportLimits.from_section(self._agents_section())
        for wake in (True, False):
            group = [r for r in reports if (r.status in WAKING_STATUSES) is wake]
            while group:
                count = take_batch(group, limits, room=limits.max_pending_reports)
                batch = ReportBatch(
                    session_id, tuple(group[:count]), wake=wake, max_chars=limits.max_report_chars,
                )
                del group[:count]
                with contextlib.suppress(Exception):
                    await self.deliver(batch)


def _agent_id(data: Mapping[str, Any]) -> str:
    meta = data.get("agent")
    value = meta.get("id") if isinstance(meta, Mapping) else data.get("id")
    return value if isinstance(value, str) else ""
