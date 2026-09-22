"""Runtime composition root (plan section 2.1, layer L4).

``Runtime`` wires the pieces together — configuration, providers, the model
router, the context manager, the session manager, and the native tool
infrastructure — without changing the pre-existing ``Agent``/CLI path. It is
additive: nothing imports it by default, and the CLI keeps using
:class:`nexus.agent.Agent`.

Design rules:

* **Injectable seams.** Tests (and later phases) can pass a ``providers`` map, a
  ``router``/resolver, a ``context`` assembler, a ``sessions`` manager, a
  ``ToolManager``/``PermissionEngine``, or a full ``tool_factory``. When a
  dependency is injected, the runtime does not own or replace it.
* **Owned lifecycle.** Providers the runtime builds from configuration are owned
  and closed by :meth:`aclose`; injected providers are the caller's to close. The
  runtime also owns the shell ``JobRegistry`` and ``TodoStore`` it creates (they
  are shared across per-turn tool snapshots and closed on :meth:`aclose`).
* **Frozen per turn.** One config load feeds the context snapshot and the tool
  snapshot, so catalog, schemas, path guard, permission policy, and result limits
  cannot change mid-turn; the next turn reloads.
* **Secrets resolve at use only.** Provider credential references such as
  ``${env:ANTHROPIC_API_KEY}`` are stored as opaque strings and handed to the
  adapter, which resolves them at request time. The runtime never reads, logs,
  or persists a secret value.

The concrete tool adapters (:class:`_ToolDispatcherAdapter`,
:class:`_PermissionGateAdapter`) live here so ``nexus.core.loop`` stays
protocol-only. It deliberately does not replace the legacy agent or implement
fallback routing.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from .config import Config
from .context import ContextManager
from .core.cancel import CancelToken
from .core.turn import TurnLimits
from .errors import OperationCancelled
from .model.provider import Provider
from .model.providers.anthropic import AnthropicProvider
from .model.request import ToolSchema
from .model.router import ModelRouter
from .session import Session, SessionManager
from .tools.builtin._jobs import JobRegistry
from .tools.builtin.todo import TodoStore
from .tools.manager import ToolManager
from .tools.permissions import (
    ApprovalBroker,
    Grant,
    PermissionEngine,
    collect_grants,
)
from .tools.spec import ToolCall, ToolContext

__all__ = ["Runtime", "ToolTurn"]


def _grants_from_events(events: Any) -> tuple[Grant, ...]:
    """Reconstruct session-scoped ``*_ALWAYS`` grants from persisted events.

    ``ONCE`` decisions carry no ``grant`` and therefore never replay. Unknown or
    malformed records are skipped rather than failing the turn.
    """
    records: list[dict[str, Any]] = []
    for event in events or ():
        if getattr(event, "type", None) != "permission.resolved":
            continue
        data = getattr(event, "data", None)
        if isinstance(data, Mapping):
            records.append(dict(data))
    try:
        return collect_grants(records)
    except Exception:  # noqa: BLE001 - a corrupt audit line must not break a turn
        return ()


class _ToolDispatcherAdapter:
    """Adapts :class:`~nexus.tools.manager.ToolManager` to the loop protocol."""

    def __init__(
        self,
        manager: ToolManager,
        *,
        workspace: Path,
        session_id: str,
        turn_id: str,
        config: Config,
    ) -> None:
        self.manager = manager
        self._workspace = Path(workspace)
        self._session_id = session_id
        self._turn_id = turn_id
        self._config = config

    def prepare(self, tool_uses):
        calls = [ToolCall.from_tool_use(block) for block in tool_uses]
        return self.manager.prepare(calls)

    def _ctx_factory(self, call: ToolCall, spec: Any) -> ToolContext:
        return ToolContext(
            workspace=self._workspace,
            session_id=self._session_id,
            turn_id=self._turn_id,
            config=self._config,
        )

    async def dispatch(
        self,
        prepared,
        /,
        *,
        emit: Callable[[str, dict[str, Any] | None], object],
        cancel: CancelToken,
        parallel_allowed: bool = True,
    ):
        async def _emit(event) -> None:
            outcome = emit(event.type, event.data)
            if inspect.isawaitable(outcome):
                await outcome

        results = await self.manager.dispatch(
            prepared,
            ctx_factory=self._ctx_factory,
            parallel_allowed=parallel_allowed,
            emit=_emit,
            cancel=cancel,
        )
        return prepared.to_ir_results(results)


class _PermissionGateAdapter:
    """Binds a frozen engine, grants, attended flag, and approval broker."""

    def __init__(
        self,
        engine: PermissionEngine,
        *,
        grants: tuple[Grant, ...] = (),
        attended: bool = True,
        broker: ApprovalBroker | None = None,
    ) -> None:
        self.engine = engine
        self._grants = tuple(grants)
        self._attended = bool(attended)
        self._broker = broker if broker is not None else ApprovalBroker()
        self._futures: dict[str, asyncio.Future] = {}

    def plan(self, prepared):
        return self.engine.plan(
            prepared.calls(),
            prepared.spec_map(),
            grants=self._grants,
            attended=self._attended,
        )

    def request_for(self, evaluation):
        return self.engine.request_for(evaluation)

    def open(self, request) -> None:
        if request.id in self._futures:
            raise RuntimeError(f"duplicate permission request {request.id!r}")
        self._futures[request.id] = self._broker.request(request)

    async def await_decision(self, request, /, *, cancel: CancelToken):
        future = self._futures.pop(request.id, None)
        if future is None:
            raise RuntimeError(
                f"permission request {request.id!r} was never opened"
            )
        cancel_task = asyncio.ensure_future(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {future, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            self._broker.cancel(request.id)
            raise
        if future in done:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            return future.result()
        if not future.done():
            future.cancel()
        self._broker.cancel(request.id)
        raise OperationCancelled(cancel.reason or "cancelled")

    def resolution(self, request_id: str):
        for record in reversed(self._broker.records):
            if record.get("id") == request_id:
                return record
        return None

    def resolve(self, request_id: str, decision: object) -> bool:
        return self._broker.resolve(request_id, decision)

    def cancel_pending(self) -> None:
        for request_id in list(self._futures):
            self._broker.cancel(request_id)
        self._futures.clear()


@dataclass(frozen=True)
class ToolTurn:
    """The frozen per-turn tool environment handed to the loop.

    ``schemas`` is what :class:`~nexus.context.manager.ContextManager` freezes
    into the request; ``dispatcher``/``gate`` are the loop-facing adapters. The
    manager/engine are immutable snapshots built from one config load.
    """

    manager: ToolManager
    engine: PermissionEngine
    dispatcher: _ToolDispatcherAdapter
    gate: _PermissionGateAdapter
    schemas: tuple[ToolSchema, ...]


class Runtime:
    """Owns the Phase 1 managers and hands out sendable sessions."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        home: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        config: Config | None = None,
        config_loader: Callable[[], Config] | None = None,
        providers: Mapping[str, Provider] | None = None,
        router: Any | None = None,
        context: Any | None = None,
        sessions: SessionManager | None = None,
        session_dir: str | Path | None = None,
        limits: TurnLimits | Callable[[], TurnLimits] | None = None,
        http_transport: Any | None = None,
        client: Any | None = None,
        tools: ToolManager | None = None,
        owns_tools: bool | None = None,
        permissions: PermissionEngine | None = None,
        tool_factory: Callable[..., ToolTurn] | None = None,
        job_registry: Any | None = None,
        todo_store: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._home = Path(home) if home is not None else None
        self._environ = environ
        self._config = config
        self._config_loader = config_loader
        self._http_transport = http_transport
        self._client = client
        self._closed = False

        # Tool infrastructure. A caller may inject a ready ToolManager, a
        # PermissionEngine, or a full per-turn factory; otherwise the runtime
        # builds a fresh ToolManager snapshot per turn while sharing one owned
        # JobRegistry/TodoStore across turns (so shell jobs and todos survive
        # between turns without a mutable runtime-global manager).
        self._tools = tools
        self._permissions = permissions
        self._tool_factory = tool_factory
        self._job_registry = job_registry
        self._todo_store = todo_store
        self._owns_job_registry = False
        #: Explicit ownership. An injected manager is only closed when the
        #: caller says so; a manager the runtime builds is always owned. Built
        #: per-turn managers carry no independent resources (the runtime injects
        #: the shared job registry/todo store), so only the most recent is kept.
        self._owns_tools = (tools is None) if owns_tools is None else bool(owns_tools)
        self._owned_tools: list[ToolManager] = []
        if tools is not None and self._owns_tools:
            self._owned_tools.append(tools)
        if tools is None and tool_factory is None:
            if self._job_registry is None:
                self._job_registry = JobRegistry()
                self._owns_job_registry = True
            if self._todo_store is None:
                self._todo_store = TodoStore()

        # Load the effective config at most once during construction: the
        # provider set and the router must agree on one snapshot. Per-turn
        # reloading is unaffected (ContextManager keeps its own loader).
        initial: Config | None = None
        if providers is not None:
            self._providers: dict[str, Provider] = dict(providers)
            # Injected providers are owned by the caller, never closed here.
            self._owned_providers: list[Provider] = []
        else:
            initial = self._load_config()
            self._providers = self._build_providers(initial)
            self._owned_providers = list(self._providers.values())

        if router is not None:
            self._router = router
        else:
            if initial is None:
                initial = self._load_config()
            self._router = self._build_router(initial)
        self._context = (
            context
            if context is not None
            else ContextManager(self.workspace, config_loader=self._load_config)
        )
        if sessions is not None:
            self._sessions = sessions
        else:
            directory = (
                Path(session_dir)
                if session_dir is not None
                else self.workspace / ".nexus" / "sessions"
            )
            self._sessions = SessionManager(
                directory,
                assemble=self._context,
                provider_for=self._router,
                limits=limits if limits is not None else self._limits_from_config,
                tools=self._make_tool_turn,
            )

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, workspace: str | Path, **kwargs: Any) -> Runtime:
        """Convenience constructor; open a runtime rooted at ``workspace``."""
        return cls(workspace, **kwargs)

    # -- introspection -----------------------------------------------------

    @property
    def providers(self) -> dict[str, Provider]:
        return dict(self._providers)

    @property
    def router(self) -> Any:
        return self._router

    @property
    def context(self) -> Any:
        return self._context

    @property
    def sessions(self) -> SessionManager:
        return self._sessions

    @property
    def tools(self) -> ToolManager | None:
        """The injected ``ToolManager``, or ``None`` when built per turn."""
        return self._tools

    @property
    def job_registry(self) -> Any | None:
        """The runtime-owned shell job registry (shared across turns)."""
        return self._job_registry

    @property
    def todo_store(self) -> Any | None:
        """The runtime-owned todo store (keyed by session id)."""
        return self._todo_store

    @property
    def closed(self) -> bool:
        return self._closed

    # -- sessions ----------------------------------------------------------

    def session(
        self, session_id: str, *, create: bool = True, recover: bool = True
    ) -> Session:
        """Open (and migrate, and recover) a session ready for ``send``."""
        return self._sessions.open(session_id, create=create, recover=recover)

    # -- configuration -----------------------------------------------------

    def _load_config(self) -> Config:
        if self._config_loader is not None:
            return self._config_loader()
        if self._config is not None:
            return self._config
        kwargs: dict[str, Any] = {}
        if self._home is not None:
            kwargs["home"] = self._home
        if self._environ is not None:
            kwargs["environ"] = self._environ
        return Config.load(self.workspace, **kwargs)

    def _limits_from_config(self) -> TurnLimits:
        config = self._load_config()
        v2 = getattr(config, "v2", None)
        if v2 is None:
            return TurnLimits()
        return TurnLimits(
            max_iterations=v2.agent.max_iterations,
            max_seconds=v2.agent.max_turn_seconds,
        )

    # -- tools -------------------------------------------------------------

    def _make_tool_turn(
        self,
        *,
        config: Config | None,
        session: Session,
        turn_id: str,
        attended: bool,
    ) -> ToolTurn | None:
        """Build the frozen per-turn tool environment from one config snapshot.

        Unknown profiles fail closed here, before the session appends anything.
        An injected ``tool_factory`` fully overrides this path (tests).
        """
        if self._tool_factory is not None:
            return self._tool_factory(
                config=config,
                session=session,
                turn_id=turn_id,
                attended=attended,
            )
        if config is None:
            config = self._load_config()
        manager = self._tools
        if manager is None:
            manager = ToolManager(
                config,
                workspace=self.workspace,
                job_registry=self._job_registry,
                todo_store=self._todo_store,
            )
            if self._owns_tools:
                self._owned_tools = [manager]
        engine = self._permissions
        if engine is None:
            permissions = getattr(getattr(config, "v2", None), "permissions", None)
            if permissions is None:
                engine = PermissionEngine(workspace=self.workspace, home=self._home)
            else:
                engine = PermissionEngine.from_config(
                    permissions, workspace=self.workspace, home=self._home
                )
        grants = _grants_from_events(session.events)
        gate = _PermissionGateAdapter(
            engine, grants=grants, attended=attended
        )
        dispatcher = _ToolDispatcherAdapter(
            manager,
            workspace=self.workspace,
            session_id=session.id,
            turn_id=turn_id,
            config=config,
        )
        return ToolTurn(
            manager=manager,
            engine=engine,
            dispatcher=dispatcher,
            gate=gate,
            schemas=tuple(manager.schemas()),
        )

    # -- providers / router ------------------------------------------------

    def _build_providers(self, config: Config) -> dict[str, Provider]:
        """Construct the adapters the Phase 1 runtime knows how to own."""
        providers: dict[str, Provider] = {}
        v2 = getattr(config, "v2", None)
        sections = getattr(v2, "providers", None) or {}
        model_ref = getattr(config, "model", None) or ""
        if "anthropic" in sections or model_ref.startswith("anthropic/"):
            providers["anthropic"] = AnthropicProvider.from_config(
                config,
                http_transport=self._http_transport,
                client=self._client,
                environ=self._environ,
            )
        return providers

    def _build_router(self, config: Config) -> ModelRouter:
        aliases: dict[str, str] = {}
        v2 = getattr(config, "v2", None)
        model_section = getattr(v2, "model", None)
        if model_section is not None:
            if model_section.default:
                aliases.setdefault("default", model_section.default)
            if model_section.fast:
                aliases["fast"] = model_section.fast
            if model_section.plan:
                aliases["plan"] = model_section.plan
        return ModelRouter(
            self._providers,
            aliases=aliases,
            default=getattr(config, "model", None),
        )

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close providers this runtime owns. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for provider in self._owned_providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
        self._owned_providers = []
        for manager in self._owned_tools:
            aclose = getattr(manager, "aclose", None)
            if aclose is not None:
                await aclose()
        self._owned_tools = []
        if self._owns_job_registry and self._job_registry is not None:
            aclose = getattr(self._job_registry, "aclose", None)
            if aclose is not None:
                await aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.aclose()
        return False
