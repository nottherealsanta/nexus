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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import msgspec

from .config import Config
from .context import ContextManager
from .context.cache import TokenCountCache
from .context.counting import RequestTokenCounter
from .core.bus import DROP_OLDEST, Bus
from .core.cancel import CancelToken
from .core.turn import TurnLimits
from .errors import OperationCancelled
from .model.provider import Provider
from .model.providers.anthropic import AnthropicProvider
from .model.request import ModelRequest, ToolSchema
from .model.router import ModelRouter
from .session import Session, SessionManager
from .tools.builtin._jobs import JobRegistry
from .tools.builtin.todo import TodoStore
from .tools.manager import ToolManager
from .tools.permissions import (
    ApprovalBroker,
    Grant,
    PathGuard,
    PermissionEngine,
    collect_grants,
)
from .tools.spec import ToolCall, ToolContext

if TYPE_CHECKING:  # pragma: no cover - typing only; the runtime imports lazily
    from .ext.manager import ExtensionManager
    from .skills.manager import SkillManager

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
        skills: Any | None = None,
        extensions: Any | None = None,
        activations: Any | None = None,
    ) -> None:
        self.manager = manager
        self._workspace = Path(workspace)
        self._session_id = session_id
        self._turn_id = turn_id
        self._config = config
        #: The narrow, manager-owned service seams a tool may reach. Injected
        #: here (never a ``Runtime``) so ``Skill``/``ReloadExtensions`` can act
        #: without the tools layer importing a concrete manager.
        self._skills = skills
        self._extensions = extensions
        self._activations = activations

    def prepare(self, tool_uses):
        calls = [ToolCall.from_tool_use(block) for block in tool_uses]
        return self.manager.prepare(calls)

    def _ctx_factory(self, call: ToolCall, spec: Any) -> ToolContext:
        return ToolContext(
            workspace=self._workspace,
            session_id=self._session_id,
            turn_id=self._turn_id,
            config=self._config,
            skills=self._skills,
            extensions=self._extensions,
            activations=self._activations,
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


class _UnattendedTurnFailure(RuntimeError):
    """A pending approval was failed by the unattended ``fail_turn`` policy.

    Raised out of ``await_decision`` so the loop's generic failure handler ends
    the turn as ``turn.failed`` (not ``turn.cancelled``). It is deliberately not
    a ``CancelledError``: a fail_turn is a policy outcome, not cancellation.
    """


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
        #: Futures the loop has already started awaiting. ``await_decision`` pops
        #: them from ``_futures``; this second map keeps them reachable so a
        #: presence-driven fallback can resolve/fail a request that is parked.
        self._inflight: dict[str, asyncio.Future] = {}

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
        if request.id in self._futures or request.id in self._inflight:
            raise RuntimeError(f"duplicate permission request {request.id!r}")
        future = self._broker.request(request)
        self._futures[request.id] = future
        self._inflight[request.id] = future

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
            if future in done:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)
                return future.result()
            if not future.done():
                future.cancel()
            self._broker.cancel(request.id)
            raise OperationCancelled(cancel.reason or "cancelled")
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            self._broker.cancel(request.id)
            raise
        finally:
            self._inflight.pop(request.id, None)

    def resolution(self, request_id: str):
        for record in reversed(self._broker.records):
            if record.get("id") == request_id:
                return record
        return None

    def resolve(self, request_id: str, decision: object) -> bool:
        return self._broker.resolve(request_id, decision)

    def fail(self, request_id: str, reason: str) -> bool:
        """Fail one pending approval so the turn ends as ``turn.failed``.

        First resolver wins: a request already resolved (or whose future is
        done) returns ``False`` and is left alone. Otherwise the in-flight
        future is completed with :class:`_UnattendedTurnFailure`, which
        ``await_decision`` re-raises and the loop converts into a failed turn.
        The broker's bookkeeping is then dropped so a later ``resolve`` cannot
        resurrect it; the broker's own ``cancel`` cannot cancel an already-done
        future.
        """
        future = self._inflight.get(request_id) or self._futures.get(request_id)
        if future is None or future.done():
            return False
        future.set_exception(_UnattendedTurnFailure(reason))
        self._broker.cancel(request_id)
        return True

    def cancel_pending(self) -> None:
        for request_id in list(self._futures) + list(self._inflight):
            self._broker.cancel(request_id)
        self._futures.clear()
        self._inflight.clear()


class _ContextCoordinator:
    """Injects frozen per-turn provider capabilities and token counting.

    Sits between the session and the context manager. It resolves the configured
    model through the runtime's router **before** assembly so the context manager
    receives the right capability descriptor (budget ceiling, prompt caching) and
    a request-aware counter — without the context layer importing a concrete
    provider, router, or runtime.
    """

    def __init__(
        self,
        context: Any,
        resolver: Any,
        *,
        token_cache: TokenCountCache | None = None,
    ) -> None:
        self._context = context
        self._resolver = resolver
        self._token_cache = token_cache

    def effective_config(self) -> Config | None:
        fn = getattr(self._context, "effective_config", None)
        if callable(fn):
            return fn()
        return None

    def turn_limits(self) -> TurnLimits | None:
        fn = getattr(self._context, "turn_limits", None)
        if callable(fn):
            return fn()
        return None

    def _resolve(self, provider_name: str | None, model: str | None) -> Any:
        resolve = getattr(self._resolver, "resolve", None)
        if resolve is None:
            return None
        try:
            request = ModelRequest(
                messages=[], provider=provider_name, model=model
            )
            return resolve(request)
        except Exception:  # noqa: BLE001 - unresolved model keeps base defaults
            return None

    def for_turn(self) -> Any:
        manager = self._context
        for_turn = getattr(manager, "for_turn", None)
        if not callable(for_turn):
            return manager
        config = self.effective_config()
        provider_name: str | None = None
        model: str | None = None
        reference = getattr(manager, "model_reference", None)
        if config is not None and callable(reference):
            try:
                provider_name, model = reference(config)
            except Exception:  # noqa: BLE001
                provider_name, model = None, None
        capabilities = None
        request_counter = None
        if config is not None and self._resolver is not None:
            resolved = self._resolve(provider_name, model)
            if resolved is not None:
                capabilities = resolved.capabilities
                provider_name = resolved.provider.name
                model = resolved.model
                request_counter = RequestTokenCounter(
                    resolved.provider,
                    cache=self._token_cache,
                    provider_name=provider_name,
                )
        return for_turn(
            capabilities=capabilities,
            request_counter=request_counter,
            model=model,
            provider=provider_name,
        )

    def assemble(self, session: Any) -> Any:
        for_turn = getattr(self._context, "for_turn", None)
        if callable(for_turn):
            return self.for_turn().assemble(session)
        return self._context.assemble(session)

    def for_iteration(
        self,
        *,
        config: Config | None,
        system_files: Any = None,
        skills_index: Any = None,
    ) -> Any:
        """Build one iteration's assembler from a pinned manifest generation.

        Resolves capabilities and the request counter from the manifest config
        (so a model/sampling reload takes effect next iteration) and hands the
        context manager the frozen config, system-file snapshot, and skills index
        so it never rereads a live file. Resolution is strict: a config naming a
        provider the router cannot resolve raises, and the loop turns that into a
        visible failed turn rather than silently falling back.
        """
        manager = self._context
        for_iteration = getattr(manager, "for_iteration", None)
        if not callable(for_iteration):
            return manager

        provider_name: str | None = None
        model: str | None = None
        reference = getattr(manager, "model_reference", None)
        if config is not None and callable(reference):
            provider_name, model = reference(config)

        capabilities = None
        request_counter = None
        if config is not None and self._resolver is not None and (
            provider_name is not None or model is not None
        ):
            resolve = getattr(self._resolver, "resolve", None)
            if resolve is not None:
                request = ModelRequest(
                    messages=[], provider=provider_name, model=model
                )
                resolved = resolve(request)  # strict: unknown provider raises
                capabilities = resolved.capabilities
                provider_name = resolved.provider.name
                model = resolved.model
                request_counter = RequestTokenCounter(
                    resolved.provider,
                    cache=self._token_cache,
                    provider_name=provider_name,
                )
        return for_iteration(
            config=config,
            system_files=system_files,
            skills_index=skills_index,
            capabilities=capabilities,
            request_counter=request_counter,
            model=model,
            provider=provider_name,
        )


@dataclass(frozen=True)
class _IterationEnv:
    """One iteration's environment, built from a single pinned generation."""

    assembler: Any
    dispatcher: Any
    gate: Any
    manager: Any

    def assemble(self, session: Any) -> Any:
        assemble = getattr(self.assembler, "assemble", None)
        if callable(assemble):
            return assemble(session)
        return self.assembler(session)  # type: ignore[operator]


class _ManifestEnvironmentFactory:
    """Build a loop iteration's environment from a pinned manifest generation.

    Called by the loop once per iteration, synchronously after pinning. Every
    artifact — context assembler, tool dispatcher, permission gate — comes from
    that one immutable snapshot, so a reload that commits mid-iteration is not
    visible until the next pin. The permission engine, approval broker, path
    guard, attended flag, and unattended policy are the turn-frozen security
    baseline and are shared across iterations; only the catalog/config/system
    files/skills refresh.
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        session: Session,
        turn_id: str,
        gate: _PermissionGateAdapter,
        path_guard: PathGuard,
        permissions: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._turn_id = turn_id
        #: The turn-scoped gate (engine + grants + attended + broker). Reused for
        #: every iteration, so a reload cannot detach a pending approval.
        self._gate = gate
        self._path_guard = path_guard
        #: The turn-start ``[permissions]`` section. A reload may refresh model,
        #: sampling, profile, and context, but the absolute deny rules, write
        #: roots, and read denyroots stay exactly as they were at turn start.
        self._turn_permissions = permissions

    # The loop may call either form; keep both for protocol flexibility.
    def __call__(self, session: Any, lease: Any, iteration: int) -> _IterationEnv:
        return self.for_iteration(session, lease, iteration)

    def for_iteration(
        self, session: Any, lease: Any, iteration: int
    ) -> _IterationEnv:
        runtime = self._runtime
        manifest = lease.manifest
        config = getattr(manifest, "config", None)
        if config is None:
            config = runtime._load_config()
        config = self._secure_config(config)
        system_files = getattr(manifest, "system_files", None)
        skills = getattr(manifest, "skills", None)
        skills_index = (
            tuple(skills.values()) if isinstance(skills, Mapping) else ()
        )

        assembler = runtime._assembler.for_iteration(
            config=config,
            system_files=system_files,
            skills_index=skills_index,
        )
        activation = self._activation_for(session)
        skill_tools = self._skill_tools_for(manifest, activation)
        catalog = (
            tuple(manifest.tools.values()) + skill_tools
            if isinstance(getattr(manifest, "tools", None), Mapping)
            else skill_tools
        )
        manager = runtime._build_iteration_manager(
            config,
            manifest,
            restrict=self._restrict_for(activation, skill_tools),
            catalog=catalog,
            path_guard=self._path_guard,
        )
        # Freeze this iteration's schemas into the iteration's context snapshot
        # so the model request advertises exactly the catalog the dispatcher can
        # execute (same generation, same selection).
        freeze = getattr(assembler, "freeze_tools", None)
        if callable(freeze):
            freeze(tuple(manager.schemas()))
        dispatcher = _ToolDispatcherAdapter(
            manager,
            workspace=runtime.workspace,
            session_id=getattr(session, "id", self._session.id),
            turn_id=self._turn_id,
            config=config,
            skills=runtime._skills,
            extensions=runtime._extensions,
            activations=runtime._activations,
        )
        return _IterationEnv(
            assembler=assembler,
            dispatcher=dispatcher,
            gate=self._gate,
            manager=manager,
        )

    def _secure_config(self, config: Config) -> Config:
        """Pin the turn-start permission section onto a refreshed config.

        Model, sampling, profile, context, and tool limits may come from a
        reloaded manifest, but ``[permissions]`` (absolute deny rules, write
        roots, read denyroots, unattended policy) is replaced with the value
        frozen at turn start, so a mid-turn reload can never loosen it. The
        builtin tools build their own :class:`PathGuard` from ``ctx.config``, so
        this is the guard that makes the turn-start roots authoritative for
        them too.
        """
        permissions = self._turn_permissions
        if permissions is None:
            return config
        v2 = getattr(config, "v2", None)
        if v2 is None or getattr(v2, "permissions", None) is permissions:
            return config
        try:
            secured_v2 = msgspec.structs.replace(v2, permissions=permissions)
            return msgspec.structs.replace(config, v2=secured_v2)
        except Exception:  # noqa: BLE001 - a merge failure keeps the config
            return config

    def _activation_for(self, session: Any) -> Any | None:
        """The session/turn-local activation overlay, if one is recorded.

        A ``Skill`` call records an immutable overlay keyed by session and turn;
        only the *next* iteration reads it. A later ``Skill`` call in the same
        turn replaces the earlier one (the log's documented semantics).
        """
        from .tools.builtin.skill import _turn_number

        log = self._runtime._activations
        session_id = getattr(session, "id", self._session.id)
        return log.get(session_id, _turn_number(self._turn_id))

    def _skill_tools_for(
        self, manifest: Any, activation: Any | None
    ) -> tuple[Any, ...]:
        """The active skill's bundled tools from the *pinned* generation.

        The association lives on the manifest (``skill_tools``), so reading it
        here cannot observe a concurrent reload: the whole iteration is built
        from the one pinned snapshot.
        """
        if activation is None:
            return ()
        skill_name = getattr(activation, "skill", None)
        if not isinstance(skill_name, str) or not skill_name:
            return ()
        skill_tools = getattr(manifest, "skill_tools", None)
        if not isinstance(skill_tools, Mapping):
            return ()
        entry = skill_tools.get(skill_name)
        tools = getattr(entry, "tools", None) if entry is not None else None
        if not isinstance(tools, (tuple, list)):
            return ()
        return tuple(tool for tool in tools if callable(getattr(tool, "run", None)))

    def _restrict_for(
        self, activation: Any | None, skill_tools: Sequence[Any]
    ) -> tuple[str, ...] | None:
        """The tool-name narrowing this iteration applies, or ``None``.

        A skill that declared nothing does not narrow: the profile-selected
        catalog (including its bundled tools) is exposed unchanged. A skill that
        declared ``allowed-tools``/``bundles`` narrows to exactly the declared
        set, *plus* any of its own bundled tools whose bundle it declared -- so a
        declaration can select a bundled tool but can never widen authority.
        """
        if activation is None:
            return None
        allowed = tuple(getattr(activation, "allowed_tools", ()) or ())
        bundles = tuple(getattr(activation, "bundles", ()) or ())
        if not allowed and not bundles:
            return None
        restrict = set(getattr(activation, "declared", ()) or ())
        for tool in skill_tools:
            spec = getattr(tool, "spec", None)
            bundle = getattr(spec, "bundle", None)
            name = getattr(tool, "name", None)
            if isinstance(name, str) and bundle in bundles:
                restrict.add(name)
        return tuple(sorted(restrict))


@dataclass(frozen=True)
class ToolTurn:
    """The per-turn tool environment handed to the loop.

    ``schemas`` is the first iteration's model-facing catalog; ``dispatcher`` and
    ``gate`` are the loop-facing adapters for the *static* (no-manifest) path.
    When the runtime owns an extension manifest, ``manifest_ref`` and
    ``environment_for`` are set and the loop rebuilds the assembler/dispatcher
    from one pinned generation per iteration; ``manager``/``engine``/``gate``
    remain the turn-frozen security baseline and the first iteration's catalog.
    """

    manager: ToolManager
    engine: PermissionEngine
    dispatcher: _ToolDispatcherAdapter
    gate: _PermissionGateAdapter
    schemas: tuple[ToolSchema, ...]
    #: The live manifest handle and per-iteration environment factory. ``None``
    #: on the static path (no extension integration): existing behaviour.
    manifest_ref: Any | None = None
    environment_for: Any | None = None


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
        extensions: ExtensionManager | None = None,
        owns_extensions: bool | None = None,
        skills: SkillManager | None = None,
        extension_sink: Any | None = None,
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
        #: Request-aware token-count cache rooted in the workspace (best-effort;
        #: a missing/unwritable cache is a silent miss, never a turn failure).
        self._token_cache = TokenCountCache(
            self.workspace / ".nexus" / "cache" / "tokens"
        )
        self._assembler = _ContextCoordinator(
            self._context, self._router, token_cache=self._token_cache
        )

        # Extension world. The runtime owns the manager, its atomic manifest
        # ref, the event bus its reload events are published on, the skill
        # manager, and the session/turn-local activation log. An injected
        # manager is the caller's to close. The manifest is bootstrapped lazily
        # (see ``ensure_started``) so construction stays synchronous, and the
        # construction-time config snapshot is reused so no second load happens.
        from .skills.manager import SkillManager

        self._skills = (
            skills
            if skills is not None
            else SkillManager.for_workspace(self.workspace, home=self._home)
        )
        self._extension_events = Bus(maxsize=256, policy=DROP_OLDEST)
        if extensions is not None:
            self._extensions: ExtensionManager | None = extensions
            self._owns_extensions = (
                False if owns_extensions is None else bool(owns_extensions)
            )
        else:
            from .ext.manager import ExtensionManager
            from .tools.builtin import BUILTIN_TOOLS

            self._extensions = ExtensionManager(
                self.workspace,
                home=self._home,
                config=initial,
                config_loader=self._load_config,
                builtin_tools=BUILTIN_TOOLS,
                skills=self._skills,
                sink=(
                    extension_sink
                    if extension_sink is not None
                    else self._extension_events
                ),
            )
            self._owns_extensions = True
        # The manager is authoritative for the skill set its manifest carries;
        # adopt it so the ``Skill`` service seam and the manifest always agree.
        if self._extensions is not None:
            self._skills = self._extensions.skills
        from .tools.builtin.skill import SkillActivationLog

        #: Session/turn-local ``SkillActivation`` overlays. Shared, but keyed by
        #: (session, turn), so one session's activation can never leak into
        #: another's environment.
        self._activations = SkillActivationLog()
        self._extensions_started = False
        self._extensions_lock: asyncio.Lock | None = None

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
                assemble=self._assembler,
                provider_for=self._router,
                limits=limits if limits is not None else self._limits_from_config,
                tools=self._make_tool_turn,
                snapshot_every=self._snapshot_every,
                unattended_decision=self._unattended_decision,
                ensure_ready=self.ensure_started,
                turn_cleanup=self._clear_activation,
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
    def extensions(self) -> ExtensionManager | None:
        """The runtime-owned extension manager (or an injected one)."""
        return self._extensions

    @property
    def skills(self) -> SkillManager:
        """The runtime-owned skill manager."""
        return self._skills

    @property
    def manifest_ref(self) -> Any | None:
        """The atomic manifest handle the loop pins per iteration."""
        return None if self._extensions is None else self._extensions.ref

    @property
    def manifest(self) -> Any | None:
        """The current immutable manifest generation, or ``None``."""
        return None if self._extensions is None else self._extensions.manifest

    @property
    def extension_events(self) -> Bus:
        """The bus extension reload events are published on."""
        return self._extension_events

    @property
    def closed(self) -> bool:
        return self._closed

    # -- extensions --------------------------------------------------------

    async def ensure_started(self) -> None:
        """Refresh the manifest at a turn boundary and start the watcher once.

        Called by a session before it prepares each turn. A serialized rebuild
        runs every turn, so an on-disk config/``SOUL.md``/``MEMORY.md``/skill or
        external-tool change is visible from the *next* turn — while a reload
        that commits mid-turn stays invisible to the iteration already running
        (the loop pins one generation per iteration). The rebuild reuses
        unchanged modules and no-ops its swap when nothing changed, so it is
        cheap. The first call also lazily starts the directory watcher (a no-op
        when watching is disabled). A failed rebuild keeps the previous manifest
        and is recorded on the manager's diagnostics; it never fails a turn.
        """
        if self._extensions is None:
            return
        if self._extensions_lock is None:
            self._extensions_lock = asyncio.Lock()
        async with self._extensions_lock:
            await self._extensions.reload(trigger="turn")
            if not self._extensions_started:
                self._extensions.start(sink=self._extension_events)
                self._extensions_started = True

    def _clear_activation(self, session_id: str, turn_id: str) -> None:
        """Drop one finished turn's skill activation.

        Turns are serialized per session, so clearing every activation for the
        session when its turn retires is exact: no other live turn can be using
        one, and the finished turn's overlay can never leak forward.
        """
        self._activations.clear(session_id)

    # -- sessions ----------------------------------------------------------

    def session(
        self, session_id: str, *, create: bool = True, recover: bool = True
    ) -> Session:
        """Open (and migrate, and recover) a session ready for ``send``."""
        return self._sessions.open(session_id, create=create, recover=recover)

    async def close_session_jobs(self, session_id: str) -> bool:
        """Terminate and reap the shell jobs owned by one session.

        The runtime shares a single :class:`~nexus.tools.builtin._jobs.JobRegistry`
        across concurrent sessions, partitioned by ``session_id``. This releases
        one session's jobs without touching any other session. Returns ``True``
        when the session had a partition (even an empty one). A runtime whose
        registry is injected without per-session release support returns
        ``False``.
        """
        registry = self._job_registry
        if registry is None:
            return False
        aclose_session = getattr(registry, "aclose_session", None)
        if aclose_session is None:
            return False
        return bool(await aclose_session(session_id))

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

    def _snapshot_every(self) -> int | None:
        """Completed-turn snapshot cadence from the effective config.

        Re-evaluated per completed turn (the session holds this callable), so a
        config edit between turns takes effect without recreating the session.
        A non-positive value disables automatic snapshots.
        """
        config = self._load_config()
        v2 = getattr(config, "v2", None)
        section = getattr(v2, "session", None)
        value = getattr(section, "snapshot_every", None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        return None

    def _unattended_decision(self) -> str:
        """Derived-presence fallback policy from ``permissions.on_unattended``.

        Passed to the session manager as a callable and re-read on every viewer
        drop (and every mid-turn approval that arrives with zero viewers), so a
        config edit between turns takes effect. The session maps the vocabulary
        to a one-shot ``deny``/``allow`` or a ``fail_turn``. An absent or
        unreadable config fails closed to ``deny``.
        """
        try:
            config = self._load_config()
        except Exception:  # noqa: BLE001 - a bad config must not wedge approvals
            return "deny"
        v2 = getattr(config, "v2", None)
        permissions = getattr(v2, "permissions", None)
        value = getattr(permissions, "on_unattended", None)
        if value in ("deny", "allow", "fail_turn"):
            return value
        return "deny"

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
            skills=self._skills,
            extensions=self._extensions,
            activations=self._activations,
        )
        # Per-iteration environment path: only when the runtime (not a caller)
        # owns the tool catalog and an extension world exists. An injected
        # ``ToolManager``/``tool_factory`` preserves the static behaviour.
        manifest_ref = None
        environment_for = None
        if self._tools is None and self._extensions is not None:
            manifest_ref = self._extensions.ref
            environment_for = _ManifestEnvironmentFactory(
                self,
                session=session,
                turn_id=turn_id,
                gate=gate,
                # The turn-frozen path guard: a config reload cannot loosen the
                # write roots / read denyroots mid-turn.
                path_guard=manager.path_guard,
                permissions=getattr(getattr(config, "v2", None), "permissions", None),
            )
        return ToolTurn(
            manager=manager,
            engine=engine,
            dispatcher=dispatcher,
            gate=gate,
            schemas=tuple(manager.schemas()),
            manifest_ref=manifest_ref,
            environment_for=environment_for,
        )

    def _build_iteration_manager(
        self,
        config: Config,
        manifest: Any,
        *,
        restrict: tuple[str, ...] | None = None,
        catalog: Sequence[Any] | None = None,
        path_guard: PathGuard | None = None,
    ) -> ToolManager:
        """Build one iteration's catalog from a pinned manifest generation.

        The catalog defaults to the manifest's registered tools (builtins plus
        external tools) and the profile selects among them exactly as before.
        When a skill is active for this session/turn the caller passes a
        ``catalog`` that also carries that skill's bundled tools, so they are
        selectable *only* for this iteration; ``restrict`` applies the skill's
        declared-tool narrowing. ``path_guard`` is the turn-frozen guard so
        security roots never refresh mid-turn.

        The empty catalog is passed through as an empty catalog: ``None`` means
        "derive from the manifest" and a genuinely empty manifest yields zero
        tools, never the built-in fallback. ``ToolManager`` already treats
        ``tools=None`` as "all builtins" and ``tools=()`` as "none", so the
        runtime must never collapse an empty tuple back to ``None``.
        """
        if catalog is None:
            tools = getattr(manifest, "tools", None)
            catalog = tuple(tools.values()) if isinstance(tools, Mapping) else ()
        catalog = tuple(catalog)
        return ToolManager(
            config,
            workspace=self.workspace,
            tools=catalog,
            restrict=restrict,
            job_registry=self._job_registry,
            todo_store=self._todo_store,
            path_guard=path_guard,
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
        if self._owns_extensions and self._extensions is not None:
            # The extension manager is the sole owner of the modules it loaded:
            # its ``aclose`` retires the live generation through the ref and
            # releases every module and staged copy after the last lease drains.
            # The runtime must not second-guess that with a direct release.
            await self._extensions.aclose()
        await self._extension_events.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.aclose()
        return False
