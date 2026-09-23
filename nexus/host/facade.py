"""The single surface API: ``HostFacade`` over one :class:`Runtime`.

PLAN §14.4: "``host/facade.py`` is the **only** surface API. Everything a UI can
do is here; anything not here, a UI cannot do." The facade is transport-neutral:
it returns domain values (``SessionSummary``, ``ConversationView``, strings) and
the wire layer wraps them with the structs in :mod:`nexus.host.protocol`.

Responsibilities:

* **own every session operation** the PLAN §14.4 verb list names, delegating to
  the :class:`~nexus.runtime.Runtime` and its managers rather than reimplementing
  them;
* **schedule turns** through :class:`~nexus.host.supervisor.Supervisor`, so a
  global concurrency cap and fair per-session queues apply to every surface;
* **track views** through :class:`~nexus.host.presence.Presence`, giving
  first-responder semantics to permission races and derived attendance;
* **produce a view baseline** by folding the session log through the pure
  ``view/`` reducer, so a late joiner catches up from the same code every
  surface renders with.

The facade never returns a credential, environment value, or raw configuration:
errors are redacted, health reports counters only, and session projections cross
the same content contract as an export. There is deliberately no method that
hands a surface a ``Runtime``, a manager, or a tool.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import msgspec

from ..errors import ExtensionTrashError, SessionBusy
from ..events import Event
from ..ext.quarantine import sanitize_text
from ..model.message import ContentBlock
from ..model.selection import ModelSelection
from ..session.manager import SessionSummary, TrashRecord
from ..util import redact_secrets
from ..view import ConversationView, apply, initial_state
from . import protocol as p
from .doctor import mismatch_summary
from .presence import Presence
from .supervisor import Supervisor

#: Default global cap when the caller does not supply one.
DEFAULT_MAX_CONCURRENT_TURNS = 4


class HostFacade:
    """Wrap one runtime as the complete, transport-neutral surface API."""

    def __init__(
        self,
        runtime: Any,
        *,
        max_concurrent_turns: int = DEFAULT_MAX_CONCURRENT_TURNS,
        owns_runtime: bool = False,
        emit: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.presence = Presence()
        self.supervisor = Supervisor(max_concurrent=max_concurrent_turns, emit=emit)
        self._owns_runtime = bool(owns_runtime)
        self._started = time.time()
        self._closed = False
        self._managed: set[str] = set()

    @property
    def max_concurrent_turns(self) -> int:
        return self.supervisor.max_concurrent

    @property
    def closed(self) -> bool:
        return self._closed

    # -- session lifecycle -------------------------------------------------

    def list_sessions(self) -> list[SessionSummary]:
        """Every session, newest activity first (PLAN §14.4)."""
        return list(self.runtime.sessions.list())

    def open_session(
        self,
        session_id: str,
        *,
        create: bool = True,
        recover: bool = True,
    ) -> SessionSummary:
        """Open (migrating/recovering) a session and return its summary."""
        self._session(session_id, create=create, recover=recover)
        return self.runtime.sessions.summary(session_id)

    def fork(
        self,
        session_id: str,
        at_seq: int | None = None,
        *,
        new_id: str | None = None,
    ) -> SessionSummary:
        """Branch a session at ``at_seq`` into a new one."""
        child = self.runtime.sessions.fork(session_id, at_seq, new_id=new_id)
        return self.runtime.sessions.summary(child.id)

    def delete(
        self, session_id: str, *, force: bool = False, reason: str = ""
    ) -> TrashRecord:
        """Move a session's artifacts to trash (never cancelling a turn).

        Refuses any session the supervisor still holds work for -- a running
        turn or a parked submission -- and any live session with persisted
        queued input, because deleting under that work would let a durable
        submission resurrect a trashed session. Cancel explicitly (dropping the
        queue) first, then delete.
        """
        if self.supervisor.running_for(session_id) or self.supervisor.queued_for(
            session_id
        ):
            raise SessionBusy(
                f"Session {session_id!r} has scheduled work; cancel it explicitly "
                "before deleting (delete never cancels a turn)"
            )
        queued = getattr(self.runtime.sessions, "queued_depth", None)
        if callable(queued) and queued(session_id) > 0:
            raise SessionBusy(
                f"Session {session_id!r} has queued input; drop it explicitly "
                "before deleting (delete never drops a queue)"
            )
        record = self.runtime.sessions.delete(session_id, force=force, reason=reason)
        self.presence.forget(session_id)
        # The handle is gone, so the scheduler must not retain it. This is a
        # no-op if the session still has an active/queued turn (which delete
        # already refused), so forgetting can never strand live work.
        self.supervisor.forget(session_id)
        self._managed.discard(session_id)
        return record

    def restore(self, trash_id: str) -> str:
        """Restore a trashed session; return its session id."""
        return self.runtime.sessions.restore(trash_id)

    def export(self, session_id: str, *, format: str = "json") -> str:
        """Render a consistent log prefix (json/markdown/jsonl)."""
        return self.runtime.sessions.export(session_id, format=format)

    def list_trashed(self) -> list[TrashRecord]:
        return list(self.runtime.sessions.list_trashed())

    # -- turns and the input queue ----------------------------------------

    async def start_turn(self, session_id: str, content: Any) -> str:
        """Schedule one turn for ``session_id``; return its pre-assigned id."""
        handle = self._session(session_id)
        return await self.supervisor.submit(session_id, handle, content)

    async def enqueue(self, session_id: str, content: Any) -> tuple[str, str]:
        """Persist a submission and schedule its consumption at the boundary.

        Returns ``(queued_id, turn_id)``. The submission is durable through the
        session's own ``input.queued`` event; the supervisor only decides *when*
        it runs, so the global cap applies to queued work too.
        """
        handle = self._session(session_id)
        queued_id = handle.enqueue(content)
        turn_id = await self.supervisor.submit(
            session_id, handle, None, queued_id=queued_id
        )
        return queued_id, turn_id

    async def cancel(
        self, session_id: str, *, reason: str | None = None, drop_queue: bool = True
    ) -> tuple[bool, int]:
        """Cancel the active turn and (by default) drop the session queue."""
        return await self.supervisor.cancel(
            session_id, reason=reason, drop_queue=drop_queue
        )

    async def wait_idle(self, *, timeout: float | None = None) -> None:
        """Wait until every scheduled turn has finished."""
        await self.supervisor.wait_idle(timeout=timeout)

    # -- views -------------------------------------------------------------

    async def subscribe(
        self,
        session_id: str,
        from_seq: int = 0,
        *,
        follow: bool = True,
        client_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """Catch up from ``from_seq`` then follow, registering a view.

        The attachment is registered on first iteration (so the count matches the
        session's own viewer count) and released on close, which also frees any
        approval lease the view still held.
        """
        handle = self._session(session_id)
        attachment = self.presence.attach(session_id, client_id)
        try:
            async for event in handle.subscribe(from_seq, follow=follow):
                yield event
        finally:
            self.presence.detach(attachment)

    def state(self, session_id: str, from_seq: int = 0) -> tuple[ConversationView, int]:
        """Fold the session log through the reducer into a baseline view.

        ``from_seq`` is exclusive: the returned view covers every event after it,
        which is exactly what a late joiner needs before subscribing from
        ``seq + 1``. This is the same pure reducer every surface renders with.
        """
        handle = self._session(session_id, create=False, recover=False)
        view = initial_state(session_id)
        for event in handle.events:
            if event.seq > from_seq:
                view = apply(view, event)
        return view, view.last_seq

    def resolve_permission(
        self,
        session_id: str,
        request_id: str,
        decision: Any,
        *,
        client_id: str | None = None,
    ) -> bool:
        """Answer one approval; the first responder wins (PLAN §14.6)."""
        handle = self._session(session_id)
        if not self.presence.claim(session_id, request_id, client_id):
            return False
        try:
            return bool(handle.resolve_permission(request_id, decision))
        finally:
            # The request is answered (or stale) either way, so the lease has
            # served its purpose; holding it would only block a future request.
            self.presence.release(session_id, request_id, client_id)

    # -- extensions / models / agents -------------------------------------

    async def reload_extensions(self, trigger: str = "api") -> Any | None:
        """Run one serialized extension rebuild; return its report."""
        extensions = self.runtime.extensions
        if extensions is None:
            return None
        return await extensions.reload(trigger=trigger)

    def list_extensions(self) -> tuple[dict[str, Any], ...]:
        extensions = self.runtime.extensions
        return tuple(extensions.list_extensions()) if extensions is not None else ()

    def validate_extensions(self, target: str | None = None) -> dict[str, Any]:
        """Quarantine-check candidate files without swapping the manifest.

        Returns the manager's generation, an overall verdict, how many files
        were checked, and one sanitized row per candidate. A runtime without an
        extension manager validates nothing and reports that plainly.
        """
        extensions = self.runtime.extensions
        if extensions is None:
            return {
                "generation": 0,
                "valid": True,
                "checked": 0,
                "results": [],
            }
        report = extensions.validate(target=target)
        rows = list(getattr(report, "results", ()) or ())
        return {
            "generation": getattr(extensions, "generation", 0),
            "valid": bool(getattr(report, "valid", True)),
            "checked": len(rows),
            "results": [_asdict(row) for row in rows],
        }

    async def trash_extension(
        self, target: str, *, reason: str = "", force: bool = False
    ) -> Any:
        """Safely trash one managed extension file, then rebuild the manifest.

        Delegates the path scoping, atomic move/rollback, and pin-aware reload to
        the extension manager; a runtime without an extension manager (or one
        that cannot trash) is refused rather than silently no-op'd.
        """
        extensions = self.runtime.extensions
        if extensions is None:
            raise ExtensionTrashError("extension manager is disabled")
        trash = getattr(extensions, "trash", None)
        if not callable(trash):
            raise ExtensionTrashError("extension manager does not support trash")
        return await trash(target, reason=reason, force=force)

    def doctor(self, *, explain_reload: bool = False) -> dict[str, Any]:
        """A redacted health report over config, providers, registry, extensions.

        Counters and descriptors only; never a credential, endpoint userinfo, or
        environment value. ``explain_reload`` adds the hot-vs-restart boundary.

        ``registry_mismatches`` aggregates the durable catalogue defects the loop
        records when a provider rejects a claimed capability (PLAN §15.5). The
        scan is bounded and best-effort: a bounded set of session logs is read
        from a bounded tail, an inaccessible/corrupt log is skipped, and no
        session handle is opened. Only counts, provider/model/reason tallies, and
        bounded samples cross the boundary; the raw provider ``detail`` does not.
        """
        report: dict[str, Any] = {
            "workspace": str(getattr(self.runtime, "workspace", "") or ""),
            "providers": self._provider_report(),
            "registry": _status_dict(getattr(self.runtime.registry, "status", lambda: None)()),
            "registry_mismatches": mismatch_summary(
                getattr(getattr(self.runtime, "sessions", None), "directory", None)
            ),
            "sessions": len(self.list_sessions()),
        }
        mcp = self._mcp_report()
        if mcp is not None:
            report["mcp"] = mcp
        extensions = self.runtime.extensions
        if extensions is not None:
            report["extensions"] = {
                "generation": getattr(extensions, "generation", 0),
                "loaded": len(self.list_extensions()),
                "diagnostics": [_asdict(item) for item in getattr(extensions, "diagnostics", lambda: ())()],
            }
        if explain_reload:
            report["reload"] = _reload_boundary()
        return report

    def _provider_report(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in sorted(self.runtime.providers):
            provider = self.runtime.providers[name]
            rows.append({"name": name, "kind": type(provider).__name__})
        return rows

    def _mcp_report(self) -> dict[str, Any] | None:
        """Point-in-time MCP server health, redacted and counter-only.

        ``None`` when the runtime has no MCP manager (MCP disabled). Every row
        is the manager's own sanitized ``to_dict``; the facade never reaches
        into a server connection or a credential.
        """
        mcp = getattr(self.runtime, "mcp", None)
        statuses = getattr(mcp, "statuses", None)
        if mcp is None or not callable(statuses):
            return None
        try:
            servers = [_asdict(status) for status in statuses()]
        except Exception:  # noqa: BLE001 - health must never raise
            servers = []
        diagnostics = getattr(mcp, "diagnostics", None)
        rows: list[dict[str, Any]] = []
        if callable(diagnostics):
            with contextlib.suppress(Exception):
                rows = [_asdict(row) for row in (diagnostics() or ())]
        return {"servers": servers, "diagnostics": rows}

    async def refresh_models(self) -> Any | None:
        """Force a catalogue acquisition; return the registry status."""
        return await self.runtime.refresh_models()

    def list_models(
        self,
        *,
        provider: str | None = None,
        tier: str | None = None,
        selectable_only: bool = False,
        search: str | None = None,
    ) -> list[Any]:
        registry = self.runtime.registry
        if registry is None:
            return []
        return list(
            registry.list(
                provider=provider,
                tier=tier,
                selectable_only=selectable_only,
                search=search,
            )
        )

    def model_info(self, ref: str) -> dict[str, Any] | None:
        """Resolve one model reference to its descriptive row, or ``None``.

        Never raises for an unknown reference and never carries a credential or
        endpoint: only the registry's descriptive fields cross the wire.
        """
        registry = self.runtime.registry
        if registry is None:
            return None
        info = registry.get(ref)
        return _asdict(info) if info is not None else None

    def model_tiers(self) -> dict[str, Any]:
        """The tier table's ordering, default, curated map, and user overrides."""
        tiers = self.runtime.tiers
        if tiers is None:
            return {"order": [], "default": "", "builtin": {}, "overrides": {}}
        return {
            "order": list(tiers.order),
            "default": tiers.default,
            "builtin": dict(tiers.builtin),
            "overrides": dict(tiers.overrides),
        }

    def select_model(self, session: str, ref: str) -> ModelSelection:
        """Validate and persist a per-session model selection (PLAN §14.11).

        Delegates resolution to the runtime, which uses the same router/tier
        rules as a configured default and refuses an unknown provider, a
        malformed reference, or a tier with no runnable model. The selection is
        durable and applies from the session's next turn; a turn already running
        is untouched.
        """
        return self.runtime.select_session_model(session, ref)

    def _fallback_chain(self, selection: ModelSelection | None = None) -> list[str]:
        """The configured fallback refs, redacted, deduped, minus the selection.

        Only descriptive reference strings cross the wire: each is sanitized and
        secret-redacted, duplicates collapse (first occurrence wins), and a
        reference equal to the selected one is dropped -- falling back to the
        model already in use is not a fallback. The list is empty when no
        fallback is configured or the router exposes none.
        """
        router = getattr(self.runtime, "router", None)
        chain = getattr(router, "fallback", None)
        if callable(chain):  # pragma: no cover - a callable fallback seam
            chain = chain()
        excluded: set[str] = set()
        if selection is not None:
            excluded.add(selection.reference)
            excluded.add(f"{selection.provider}/{selection.model}")
        seen: set[str] = set()
        out: list[str] = []
        for ref in chain or ():
            text = redact_secrets(sanitize_text(str(ref), limit=200))
            if not text or text in seen or text in excluded:
                continue
            seen.add(text)
            out.append(text)
        return out

    def list_agents(self) -> list[dict[str, Any]]:
        """A sanitized, transport-neutral index of discovered subagents."""
        agents = self.runtime.agents
        if agents is None:
            return []
        rows: list[dict[str, Any]] = []
        for entry in agents.index:
            rows.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "source": str(entry.source),
                    "model": entry.model,
                    "read_only": bool(entry.read_only),
                }
            )
        return rows

    async def list_tools(self) -> list[dict[str, Any]]:
        """The model-facing tool catalog for the current config and manifest."""
        lister = getattr(self.runtime, "list_tools", None)
        if not callable(lister):
            return []
        return list(await lister())

    # -- health / shutdown -------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Daemon-level liveness: counters only, never a credential."""
        sessions = self.runtime.sessions
        try:
            total = len(sessions.list())
        except Exception:  # noqa: BLE001 - health must never raise
            total = 0
        return {
            "ok": not self._closed,
            "version": p.PROTOCOL_VERSION,
            "sessions": total,
            "running": self.supervisor.running,
            "queued": self.supervisor.queued,
            "max_concurrent": self.supervisor.max_concurrent,
            "viewers": self.presence.total_viewers(),
            "uptime": max(0.0, time.time() - self._started),
        }

    async def shutdown(self, reason: str = "") -> bool:
        """Stop scheduling, close every session, and (if owned) the runtime."""
        if self._closed:
            return False
        self._closed = True
        await self.supervisor.aclose()
        closer = getattr(self.runtime.sessions, "aclose_all", None)
        if callable(closer):
            await closer()
        if self._owns_runtime:
            await self.runtime.aclose()
        return True

    # -- wire dispatch -----------------------------------------------------

    async def handle(self, command: p.Command) -> p.Result:
        """Dispatch one protocol command, converting failures to a redacted error."""
        try:
            return await self._dispatch(command)
        except Exception as exc:  # noqa: BLE001 - the wire never raises a traceback
            return p.ErrorResult(
                kind=type(exc).__name__, message=redact_secrets(str(exc))
            )

    async def _dispatch(self, command: p.Command) -> p.Result:
        if isinstance(command, p.SessionList):
            return p.SessionListResult(sessions=self.list_sessions())
        if isinstance(command, p.SessionOpen):
            return p.SessionOpenResult(
                session=self.open_session(
                    command.session, create=command.create, recover=command.recover
                )
            )
        if isinstance(command, p.SessionStart):
            turn_id = await self.start_turn(
                command.session, _content(command.content, command.blocks)
            )
            return p.SessionStartResult(session=command.session, turn_id=turn_id)
        if isinstance(command, p.SessionEnqueue):
            queued_id, turn_id = await self.enqueue(
                command.session, _content(command.content, command.blocks)
            )
            return p.SessionEnqueueResult(
                session=command.session,
                queued_id=queued_id,
                depth=self.supervisor.queued_for(command.session),
                turn_id=turn_id,
            )
        if isinstance(command, p.SessionCancel):
            cancelled, dropped = await self.cancel(
                command.session, reason=command.reason or None, drop_queue=command.drop_queue
            )
            return p.SessionCancelResult(
                session=command.session, cancelled=cancelled, dropped=dropped
            )
        if isinstance(command, p.SessionSubscribe):
            return p.SessionSubscribeResult(
                session=command.session, from_seq=command.from_seq
            )
        if isinstance(command, p.SessionState):
            view, seq = self.state(command.session, command.from_seq)
            return p.SessionStateResult(
                session=command.session, seq=seq, view=view.to_dict()
            )
        if isinstance(command, p.SessionFork):
            return p.SessionForkResult(
                session=self.fork(
                    command.session, command.at_seq, new_id=command.new_id
                )
            )
        if isinstance(command, p.SessionDelete):
            record = self.delete(
                command.session, force=command.force, reason=command.reason
            )
            return p.SessionDeleteResult(
                session=command.session,
                trash_id=record.trash_id,
                delete_after=record.delete_after,
            )
        if isinstance(command, p.SessionRestore):
            return p.SessionRestoreResult(session=self.restore(command.trash_id))
        if isinstance(command, p.SessionExport):
            return p.SessionExportResult(
                session=command.session,
                format=command.format,
                content=self.export(command.session, format=command.format),
            )
        if isinstance(command, p.PermissionResolve):
            return p.PermissionResolveResult(
                session=command.session,
                request_id=command.request_id,
                resolved=self.resolve_permission(
                    command.session,
                    command.request_id,
                    command.decision,
                    client_id=command.client_id,
                ),
                client_id=command.client_id,
            )
        if isinstance(command, p.ExtensionsReload):
            return _reload_result(await self.reload_extensions(command.trigger))
        if isinstance(command, p.ExtensionsList):
            extensions = self.runtime.extensions
            return p.ExtensionsListResult(
                generation=getattr(extensions, "generation", 0),
                extensions=list(self.list_extensions()),
            )
        if isinstance(command, p.ExtensionsValidate):
            report = self.validate_extensions(command.target)
            return p.ExtensionsValidateResult(
                generation=report["generation"],
                valid=report["valid"],
                checked=report["checked"],
                results=report["results"],
            )
        if isinstance(command, p.ExtensionsTrash):
            outcome = await self.trash_extension(
                command.target, reason=command.reason, force=command.force
            )
            return _trash_result(command.target, outcome)
        if isinstance(command, p.ModelsRefresh):
            status = await self.refresh_models()
            return p.ModelsRefreshResult(status=_status_dict(status))
        if isinstance(command, p.ModelsList):
            models = self.list_models(
                provider=command.provider,
                tier=command.tier,
                selectable_only=command.selectable_only,
                search=command.search,
            )
            return p.ModelsListResult(
                count=len(models), models=[_asdict(model) for model in models]
            )
        if isinstance(command, p.ModelShow):
            model = self.model_info(command.ref)
            return p.ModelShowResult(
                ref=command.ref, found=model is not None, model=model
            )
        if isinstance(command, p.ModelTiers):
            tiers = self.model_tiers()
            return p.ModelTiersResult(
                order=tiers["order"],
                default=tiers["default"],
                builtin=tiers["builtin"],
                overrides=tiers["overrides"],
            )
        if isinstance(command, p.ModelSelect):
            selection = self.select_model(command.session, command.ref)
            return p.ModelSelectResult(
                session=command.session,
                accepted=True,
                reference=selection.reference,
                provider=selection.provider,
                model=selection.model,
                tier=selection.tier,
                tier_source=selection.tier_source,
                requested_tier=selection.requested_tier,
                clamped=bool(selection.clamped),
                fallback=self._fallback_chain(selection),
                apply_next_turn=True,
            )
        if isinstance(command, p.AgentsList):
            agents = self.runtime.agents
            return p.AgentsListResult(
                generation=getattr(agents, "generation", 0),
                agents=self.list_agents(),
            )
        if isinstance(command, p.ToolsList):
            tools = await self.list_tools()
            return p.ToolsListResult(count=len(tools), tools=tools)
        if isinstance(command, p.Doctor):
            # The report can read a bounded set of session tails (up to 64 x
            # 512 KiB) and parse their JSON. ``doctor`` stays a synchronous
            # facade method -- its direct callers are unchanged -- but the wire
            # path runs it in a worker thread so a large scan cannot stall the
            # event loop and every other session.
            report = await asyncio.to_thread(
                self.doctor, explain_reload=command.explain_reload
            )
            return p.DoctorResult(ok=not self._closed, report=report)
        if isinstance(command, p.Health):
            return p.HealthResult(**self.health())
        if isinstance(command, p.Shutdown):
            return p.ShutdownResult(stopping=await self.shutdown(command.reason))
        raise ValueError(f"unknown command {type(command).__name__}")

    # -- internals ---------------------------------------------------------

    def _session(
        self, session_id: str, *, create: bool = True, recover: bool = True
    ) -> Any:
        """Open a session and take over its queue scheduling exactly once."""
        handle = self.runtime.session(session_id, create=create, recover=recover)
        if session_id not in self._managed:
            bind = getattr(handle, "bind", None)
            if callable(bind):
                # The supervisor owns promotion now, so a finished turn must not
                # auto-consume the queue and bypass the global cap.
                bind(auto_start_queued=False)
            self._managed.add(session_id)
        return handle


def _content(content: str, blocks: list[dict[str, Any]]) -> Any:
    """Coerce a wire payload into session content (text or typed blocks)."""
    if blocks:
        return msgspec.convert(blocks, type=list[ContentBlock])
    return content


def _asdict(value: Any) -> dict[str, Any]:
    if isinstance(value, msgspec.Struct):
        return msgspec.structs.asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return dict(value)


def _status_dict(status: Any) -> dict[str, Any] | None:
    if status is None:
        return None
    return _asdict(status)


def _reload_result(report: Any) -> p.ExtensionsReloadResult:
    if report is None:
        return p.ExtensionsReloadResult()
    modules = getattr(getattr(report, "diff", None), "modules", None)
    return p.ExtensionsReloadResult(
        generation=getattr(report, "generation", 0),
        previous_generation=getattr(report, "previous_generation", 0),
        changed=bool(getattr(report, "changed", False)),
        loaded=list(getattr(modules, "added", ()) or ()),
        unloaded=list(getattr(modules, "removed", ()) or ()),
        failed=[_asdict(item) for item in getattr(report, "failed", ()) or ()],
    )


def _trash_result(target: str, outcome: Any) -> p.ExtensionsTrashResult:
    record = getattr(outcome, "record", None)
    report = getattr(outcome, "report", None)
    if record is None:
        raise ExtensionTrashError("extension trash returned no record")
    return p.ExtensionsTrashResult(
        target=sanitize_text(target, limit=400),
        trash_id=getattr(record, "trash_id", ""),
        source_path=sanitize_text(getattr(record, "source_path", ""), limit=400),
        relative_path=sanitize_text(getattr(record, "relative_path", ""), limit=400),
        origin=getattr(record, "origin", ""),
        names=list(getattr(record, "modules", ()) or ()),
        tools=list(getattr(record, "tools", ()) or ()),
        sha256=getattr(record, "sha256", ""),
        module_generation=getattr(record, "generation", 0),
        trashed_at=getattr(record, "trashed_at", 0.0),
        delete_after=getattr(record, "delete_after", 0.0),
        reason=sanitize_text(getattr(record, "reason", ""), limit=300),
        changed=bool(getattr(report, "changed", False)),
        generation=getattr(report, "generation", 0),
        previous_generation=getattr(report, "previous_generation", 0),
    )


def _reload_boundary() -> dict[str, Any]:
    """The hot-vs-restart boundary, stated plainly (PLAN section 6.6)."""
    return {
        "hot": [
            ".nexus/tools/*.py",
            ".nexus/providers/*.py",
            ".nexus/hooks/*.py",
            ".nexus/skills/**/SKILL.md",
            ".nexus/agents/*.md",
            ".nexus/mcp.json",
            "nexus.toml",
            "SOUL.md",
            "MEMORY.md",
        ],
        "restart_only": [
            "nexus/core/**",
            "nexus/model/message.py",
            "nexus/runtime.py",
            "the Manifest shape itself",
            "new pip installs",
        ],
        "note": (
            "Hot extensions swap at the next loop iteration in the same turn. "
            "Core source, the manifest shape, and new imports need a daemon "
            "restart; use `nexus daemon stop` and the next command auto-starts."
        ),
    }


__all__ = ["DEFAULT_MAX_CONCURRENT_TURNS", "HostFacade"]
