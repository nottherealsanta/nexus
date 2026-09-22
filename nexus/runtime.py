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
import contextlib
import hashlib
import inspect
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import msgspec

from .agents import SubagentOutcome, SubagentRunner, SubagentUsage
from .config import Config
from .context import ContextManager
from .context.cache import TokenCountCache
from .context.counting import RequestTokenCounter
from .core.bus import DROP_OLDEST, Bus
from .core.cancel import CancelToken
from .core.loop import run_turn
from .core.turn import TurnLimits
from .errors import ConfigError, OperationCancelled
from .events import Event
from .hooks import HookEvent, HookInvocation, HookManager, HookOutcome
from .model.message import Text
from .model.provider import Provider
from .model.providers.anthropic import AnthropicProvider
from .model.registry import ModelRegistry
from .model.request import ModelRequest, ToolSchema
from .model.router import ModelRouter
from .model.tiers import DEFAULT_TIER, TierTable
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
from .util import redact_url_userinfo

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
        subagents: Any | None = None,
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
        #: The ``Task`` subagent service for this iteration. Injected into every
        #: ``ToolContext`` so the builtin never imports ``nexus.agents``.
        self._subagents = subagents

    def prepare(self, tool_uses):
        calls = [ToolCall.from_tool_use(block) for block in tool_uses]
        return self.manager.prepare(calls)

    def bundle_for(self, name: object) -> str | None:
        """The bundle of a named tool, for hook matchers (``Bundle:fs``)."""
        tool = self.manager.get(name) if name is not None else None
        spec = getattr(tool, "spec", None)
        bundle = getattr(spec, "bundle", None)
        return bundle if isinstance(bundle, str) else None

    def preview(self, tool_uses):
        """Preview canonical ``(bundle, key)`` for each call; executes nothing."""
        calls = [ToolCall.from_tool_use(block) for block in tool_uses]
        return self.manager.preview(calls)

    def _ctx_factory(self, call: ToolCall, spec: Any) -> ToolContext:
        return ToolContext(
            workspace=self._workspace,
            session_id=self._session_id,
            turn_id=self._turn_id,
            config=self._config,
            skills=self._skills,
            extensions=self._extensions,
            activations=self._activations,
            subagents=self._subagents,
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
        mcp_index: Any = None,
        pre_compact: Any = None,
        turn_id: str | None = None,
        iteration: int = 0,
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
            mcp_index=mcp_index,
            capabilities=capabilities,
            request_counter=request_counter,
            model=model,
            provider=provider_name,
            pre_compact=pre_compact,
            turn_id=turn_id,
            iteration=iteration,
        )


@dataclass(frozen=True)
class _IterationEnv:
    """One iteration's environment, built from a single pinned generation."""

    assembler: Any
    dispatcher: Any
    gate: Any
    manager: Any
    hooks: Any | None = None

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
        budget: Any | None = None,
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
        #: The turn-scoped subagent tree budget, shared across every iteration and
        #: every child/grandchild, so token/cost spend accumulates over the turn
        #: instead of resetting each iteration.
        self._budget = budget

    # The loop may call either form; keep both for protocol flexibility.
    def __call__(self, session: Any, lease: Any, iteration: int) -> _IterationEnv:
        return self.for_iteration(session, lease, iteration)

    def for_iteration(
        self, session: Any, lease: Any, iteration: int, *, cancel: Any = None
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

        # The pinned generation's hooks, so a concurrent reload cannot change
        # what this iteration enforces.
        hooks_service = None
        if runtime._hooks is not None:
            hooks_service = _HookService(
                runtime._hooks,
                getattr(manifest, "hooks", None),
                workspace=runtime.workspace,
            )
        session_id = getattr(session, "id", self._session.id)
        pre_compact = runtime._pre_compact_gate(
            hooks_service, session_id, self._turn_id, iteration, cancel
        )
        assembler = runtime._assembler.for_iteration(
            config=config,
            system_files=system_files,
            skills_index=skills_index,
            # The pinned generation's MCP view: connected servers and their
            # resource roots only, folded by the context layer into its
            # untrusted ``mcp_index`` part.
            mcp_index=getattr(manifest, "mcp", None),
            pre_compact=pre_compact,
            turn_id=self._turn_id,
            iteration=iteration,
        )
        activation = self._activation_for(session)
        skill_tools = self._skill_tools_for(manifest, activation)
        base_catalog = (
            tuple(manifest.tools.values()) + skill_tools
            if isinstance(getattr(manifest, "tools", None), Mapping)
            else skill_tools
        )
        restrict = self._restrict_for(activation, skill_tools)
        runner = self._subagent_runner(
            runtime, config, base_catalog, session_id, restrict, hooks_service
        )
        catalog = base_catalog
        if runner is not None:
            from .tools.builtin.task import build_task_tool

            catalog = (*base_catalog, build_task_tool(runner))
        manager = runtime._build_iteration_manager(
            config,
            manifest,
            restrict=restrict,
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
            session_id=session_id,
            turn_id=self._turn_id,
            config=config,
            skills=runtime._skills,
            extensions=runtime._extensions,
            activations=runtime._activations,
            subagents=runner,
        )
        return _IterationEnv(
            assembler=assembler,
            dispatcher=dispatcher,
            gate=self._gate,
            manager=manager,
            hooks=hooks_service,
        )

    def _subagent_runner(
        self,
        runtime: Runtime,
        config: Config,
        base_catalog: Sequence[Any],
        session_id: str,
        restrict: tuple[str, ...] | None,
        hooks: Any | None = None,
    ) -> Any | None:
        if runtime._agents is None:
            return None
        # The parent's authority is exactly the tools this iteration advertises.
        # Build a provisional manager (with a static Task) so the selection
        # includes Task when the profile allows it, then rebuild with the Task
        # tool bound to the runner.
        from .tools.builtin.task import build_task_tool

        provisional = runtime._build_iteration_manager(
            config,
            runtime.manifest,
            restrict=restrict,
            catalog=(*base_catalog, build_task_tool(None)),
            path_guard=self._path_guard,
        )
        parent_tools = list(provisional.names)
        authority = _ChildAuthority(
            engine=self._gate.engine, path_guard=self._path_guard
        )
        return runtime._make_subagent_runner(
            session_id=session_id,
            parent_tools=parent_tools,
            permissions=authority,
            grants=tuple(getattr(self._gate, "_grants", ())),
            config=config,
            catalog=base_catalog,
            hooks=hooks,
            budget=self._budget,
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
    #: The turn's lifecycle-hook runner (pinned manifest's hooks). ``None`` keeps
    #: hook integration off entirely.
    hooks: Any | None = None


#: MCP lifecycle events that change the manifest's tool set or server index.
#: Each one schedules one coalesced rebuild; the rebuild itself is idempotent
#: and emits nothing further, so the callback cannot loop.
_MCP_REBUILD_EVENTS = frozenset(
    {"mcp.connected", "mcp.disconnected", "mcp.failed", "mcp.tools_changed"}
)


class _MCPEventSink:
    """The MCP manager's event sink: publish to the bus, rebuild on change.

    The manager calls ``publish(event)`` for every ``mcp.*`` lifecycle event.
    Publishing keeps those events visible on the runtime's extension bus, and a
    tool-affecting change asks the runtime to rebuild the manifest so the next
    iteration sees the server's current tools — the "manager change callback"
    without the MCP layer importing the runtime or the extension layer.
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def publish(self, event: Event) -> int:
        self._runtime._on_mcp_event(event)
        return 0


# ---------------------------------------------------------------------------
# Lifecycle hooks (plan section 5.7)
# ---------------------------------------------------------------------------


class _HookService:
    """Runs exactly the hooks of one pinned manifest generation.

    The loop calls :meth:`lifecycle` and the two tool hooks; this adapter maps
    them onto the runtime-owned :class:`~nexus.hooks.manager.HookManager` but
    passes the *pinned* specs explicitly, so a reload that commits mid-iteration
    cannot change what the running iteration enforces. It returns the real
    :class:`~nexus.hooks.model.HookOutcome` (or an allow when the event has no
    hooks), and the loop persists the decisions as ``hook.fired``/
    ``hook.blocked``.
    """

    def __init__(self, manager: Any, specs_by_event: Any, *, workspace: Path) -> None:
        self._manager = manager
        self._specs = {
            str(event): tuple(specs)
            for event, specs in (specs_by_event or {}).items()
            if specs
        }
        self._workspace = workspace

    @property
    def enabled(self) -> bool:
        return bool(self._specs) and self._manager is not None

    def has_event(self, event: str) -> bool:
        """Whether this pinned generation declares any hook for ``event``."""
        return bool(self._specs.get(event)) and self._manager is not None

    async def _run(
        self, event: str, invocation: HookInvocation, cancel: Any
    ) -> Any:
        specs = self._specs.get(event, ())
        if not specs or self._manager is None:
            return HookOutcome.allow(event)
        return await self._manager.run(
            HookEvent.coerce(event), invocation, cancel=cancel, specs=specs
        )

    async def lifecycle(
        self,
        event: str,
        *,
        session_id: str,
        turn_id: str,
        data: Mapping[str, Any] | None = None,
        cancel: Any = None,
    ) -> Any:
        name = HookEvent.coerce(event)
        invocation = HookInvocation(
            event=name.value,
            session_id=session_id,
            turn_id=turn_id,
            data=dict(data or {}),
        )
        return await self._run(name.value, invocation, cancel)

    async def pre_tool_use(
        self,
        *,
        tool: str,
        key: str | None = None,
        bundle: str | None = None,
        tool_input: Mapping[str, Any],
        session_id: str,
        turn_id: str,
        cancel: Any = None,
    ) -> Any:
        invocation = HookInvocation(
            event=HookEvent.PRE_TOOL_USE.value,
            tool=tool,
            key=key,
            bundle=bundle,
            tool_input=dict(tool_input or {}),
            session_id=session_id,
            turn_id=turn_id,
        )
        return await self._run(HookEvent.PRE_TOOL_USE.value, invocation, cancel)

    async def post_tool_use(
        self,
        *,
        tool: str,
        key: str | None = None,
        bundle: str | None = None,
        tool_input: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        session_id: str,
        turn_id: str,
        cancel: Any = None,
    ) -> Any:
        invocation = HookInvocation(
            event=HookEvent.POST_TOOL_USE.value,
            tool=tool,
            key=key,
            bundle=bundle,
            tool_input=dict(tool_input or {}),
            session_id=session_id,
            turn_id=turn_id,
            data={"result": dict(result or {})},
        )
        return await self._run(HookEvent.POST_TOOL_USE.value, invocation, cancel)

    async def user_prompt_submit(
        self,
        *,
        content: Any,
        session_id: str,
        turn_id: str,
        cancel: Any = None,
    ) -> Any:
        """Run ``UserPromptSubmit`` with the prompt as the modifiable input.

        The input is ``{"content": [...]}`` (JSON-safe block views); a hook's
        ``modify`` returns the replacement mapping, which the session parses back
        into content blocks before anything is persisted.
        """
        invocation = HookInvocation(
            event=HookEvent.USER_PROMPT_SUBMIT.value,
            tool_input={"content": list(content or [])},
            session_id=session_id,
            turn_id=turn_id,
        )
        return await self._run(HookEvent.USER_PROMPT_SUBMIT.value, invocation, cancel)


# ---------------------------------------------------------------------------
# Subagents (plan sections 5.6, 15.6-15.8)
# ---------------------------------------------------------------------------


#: The turn-frozen authority a child inherits: the parent's permission engine and
#: path guard, passed through unchanged (never broadened).
@dataclass(frozen=True)
class _ChildAuthority:
    engine: Any
    path_guard: Any


def _child_session_id(logical: str) -> str:
    """Map a logical ``<parent>/sub/<n>`` id to a valid, collision-resistant id.

    Sanitizing ``/`` to ``_`` could map two distinct logical ids onto one disk
    id (``a/b/1`` and ``a_b_1``); a short deterministic hash of the logical id is
    appended so the mapping is injective in practice while staying stable across
    reopens. The 80-char session-id limit is preserved.
    """
    raw = str(logical or "subagent")
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    safe = (safe or "subagent")[: 80 - len(digest) - 1]
    return f"{safe}_{digest}"


class _ChildSessionFacade:
    """The runner's ``SessionFacade``: logical ids, real (valid) child logs.

    The plan's `<parent>/sub/<n>` id is kept for agent metadata and the event
    tree; a sanized id of the same shape is used for the on-disk session so the
    child is a real, replayable session rather than an in-memory fake.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._manager = SessionManager(self._directory)

    @property
    def manager(self) -> SessionManager:
        return self._manager

    def child_id(self, parent_id: str, index: int) -> str:
        return f"{parent_id}/sub/{int(index)}"

    async def aclose(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            self._manager.evict(_child_session_id(session_id))


#: Child events that are internal to the child's own turn/session and must not
#: reach the parent log: a relayed child ``turn.completed`` would look like the
#: parent turn ending, and a relayed ``permission.requested`` would be tracked as
#: a parent pending approval. The child's own log keeps them; the parent tree is
#: reconstructed from ``agent.spawned``/``agent.completed`` plus the child's
#: content/tool/model events carrying ``agent`` metadata.
_CHILD_RELAY_SUPPRESS = frozenset(
    {
        "turn.started",
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "session.opened",
        "session.closed",
        "context.assembled",
        "context.compacted",
        "context.degraded",
        "permission.requested",
        "permission.resolved",
        "input.queued",
        "input.consumed",
        "input.dropped",
        "presence.joined",
        "presence.left",
        "presence.changed",
    }
)


class _ChildEventSink:
    """Persist a child turn's events to its own log and relay them upward.

    Relaying through the runner's ``spec.emit`` is what puts an ``agent`` field
    on every child event and makes the whole tree replay from the parent log.
    Child turn/session lifecycle events are persisted to the child log but *not*
    relayed, so they cannot masquerade as the parent's own lifecycle.
    """

    def __init__(self, session: Any, relay: Any) -> None:
        self._session = session
        self._relay = relay

    async def emit(self, event: Event) -> Event:
        with contextlib.suppress(Exception):
            self._session.append_event(event)
        if self._relay is None or event.type in _CHILD_RELAY_SUPPRESS:
            return event
        with contextlib.suppress(Exception):
            outcome = self._relay(event.type, dict(event.data))
            if inspect.isawaitable(outcome):
                await outcome
        return event


class _ChildRuntime:
    """One child run: a restricted nested turn over the parent's providers.

    It is deliberately not a second :class:`Runtime`: the child borrows the
    parent's router and agent manager, gets the tools the runner already
    intersected into ``spec.tools``, inherits the parent's permission engine and
    grants, and can spawn its own children through the runner's shared budget.
    """

    def __init__(self, runtime: Runtime, spec: Any, runner: Any) -> None:
        self._runtime = runtime
        self._spec = spec
        self._runner = runner

    async def run(self) -> SubagentOutcome:
        runtime = self._runtime
        spec = self._spec
        facade = runtime._ensure_child_sessions()
        real_id = _child_session_id(spec.session_id)
        session = facade.manager.open(real_id, create=True, recover=False)
        config = runtime._child_config(spec)
        assembler = runtime._build_child_assembler(spec, config)
        manager = runtime._build_child_tool_manager(spec, config, self._runner)
        engine = runtime._child_permission_engine(spec, config)
        gate = _PermissionGateAdapter(
            engine, grants=tuple(spec.grants), attended=False
        )
        dispatcher = _ToolDispatcherAdapter(
            manager,
            workspace=runtime.workspace,
            session_id=real_id,
            turn_id="",
            config=config,
            subagents=self._runner,
        )
        max_iterations = int(spec.max_iterations or 60)
        limits = TurnLimits(max_iterations=max_iterations, max_seconds=1800.0)
        lease = session.begin_turn(limits=limits)
        sink = _ChildEventSink(session, spec.emit)
        try:
            outcome = await run_turn(
                session=session,
                user_input=spec.prompt,
                assemble=assembler,
                provider_for=runtime._router,
                emit=sink,
                tools=dispatcher,
                gate=gate,
                lease=lease,
                persist_user_message=True,
                hooks=spec.hooks,
            )
        finally:
            with contextlib.suppress(Exception):
                lease.release()
        cost = runtime._child_cost(session, outcome)
        return runtime._child_outcome(spec, session, outcome, cost=cost)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


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
        registry: ModelRegistry | None = None,
        tiers: TierTable | None = None,
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
        mcp: Any | None = None,
        owns_mcp: bool | None = None,
        mcp_client_factory: Any | None = None,
        agents: Any | None = None,
        owns_agents: bool | None = None,
        hooks: Any | None = None,
        owns_hooks: bool | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._home = Path(home) if home is not None else None
        self._environ = environ
        self._config = config
        self._config_loader = config_loader
        self._http_transport = http_transport
        self._client = client
        self._mcp_client_factory = mcp_client_factory
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

        if initial is None:
            initial = self._load_config()

        # Model registry and tier table (plan sections 15.3-15.4). The runtime
        # owns both; an injected registry/tier table is the caller's. A registry
        # is only built when ``[models]`` is explicitly configured, so a plain
        # config keeps the Phase 1 router and never touches the network.
        self._tiers: TierTable = (
            tiers if tiers is not None else self._build_tiers(initial)
        )
        self._registry: ModelRegistry | None = (
            registry if registry is not None else self._build_registry(initial)
        )
        self._registry_loaded = False
        #: Serializes concurrent turn boundaries onto one acquisition and is the
        #: flag's guard: ``_registry_loaded`` is set only after a successful
        #: load, so a failed load is retried rather than remembered as done.
        self._registry_lock: asyncio.Lock | None = None

        if router is not None:
            self._router = router
        else:
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

        # MCP world. The runtime owns the server manager (or adopts an injected
        # one), wires its lifecycle events onto the extension bus, and asks the
        # extension manager to fold its immutable snapshot into each manifest
        # generation. Construction does not connect anything: ``ensure_started``
        # reconciles ``.nexus/mcp.json`` and lazily connects enabled servers at
        # the turn boundary, so a dead or hung server can only remove its own
        # tools, never fail construction or a turn.
        self._mcp_rebuild_pending = False
        self._mcp_rebuild_dirty = False
        self._mcp_rebuild_task: asyncio.Task | None = None
        if mcp is not None:
            self._mcp: Any | None = mcp
            self._owns_mcp = False if owns_mcp is None else bool(owns_mcp)
        else:
            self._mcp = self._build_mcp_manager(initial)
            self._owns_mcp = self._mcp is not None

        # Agents and hooks (plan sections 5.6-5.7, 15.6-15.8). The runtime owns
        # both managers; the extension manager folds their discovered sets into
        # the same pinned generation as tools, so one reload advances the whole
        # world together. Agent construction seeds the workspace roles once (if
        # enabled); hook construction is inert until the first rebuild.
        self._agents = self._build_agent_manager(initial, agents, owns_agents)
        self._hooks = self._build_hook_manager(initial, hooks, owns_hooks)
        self._child_sessions: _ChildSessionFacade | None = None

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
                mcp=self._mcp,
                agents=self._agents,
                hooks=self._hooks,
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
            self._owns_sessions = False
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
                hooks=self._session_hooks,
            )
            self._owns_sessions = True

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
    def registry(self) -> ModelRegistry | None:
        """The runtime-owned model registry, or ``None`` when ``[models]`` is off."""
        return self._registry

    @property
    def tiers(self) -> TierTable:
        """The runtime-owned tier table (always present; built-ins by default)."""
        return self._tiers

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
    def agents(self) -> Any | None:
        """The runtime-owned agent-definition manager (or an injected one)."""
        return self._agents

    @property
    def hooks(self) -> Any | None:
        """The runtime-owned lifecycle-hook manager (or an injected one)."""
        return self._hooks

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
        await self._ensure_registry()
        if self._extensions is None:
            return
        if self._extensions_lock is None:
            self._extensions_lock = asyncio.Lock()
        async with self._extensions_lock:
            await self._extensions.reload(trigger="turn")
            if not self._extensions_started:
                self._extensions.start(sink=self._extension_events)
                self._extensions_started = True

    async def _ensure_registry(self, *, force: bool = False) -> None:
        """Populate the model registry once, publishing its lifecycle events.

        Called at the turn boundary (before model resolution) so tier names
        resolve against a loaded catalogue. Acquisition is cache-first and
        TTL-bound; ``offline`` and an already-installed injected registry never
        touch the network. A load failure degrades to the status the registry
        already reports (stale cache, snapshot, or empty) and is never fatal.
        """
        registry = self._registry
        if registry is None:
            return
        if self._registry_loaded and not force:
            return
        if self._registry_lock is None:
            self._registry_lock = asyncio.Lock()
        async with self._registry_lock:
            # Re-check under the lock: a second turn boundary that waited here
            # must not repeat the acquisition the first one just completed.
            if self._registry_loaded and not force:
                return
            try:
                status = await registry.load(force=force)
            except Exception as exc:  # noqa: BLE001 - registry I/O must not fail a turn
                self._publish_registry_event(
                    "registry.failed",
                    {
                        "error": redact_url_userinfo(
                            f"{type(exc).__name__}: {exc}"
                        )
                    },
                )
                # The flag stays unset so the next turn boundary retries; a
                # transient fetch failure must not permanently disable tiers.
                return
            self._registry_loaded = True
        data = {
            "source": status.source,
            "stale": status.stale,
            "models": status.model_count,
            "providers": status.provider_count,
        }
        if status.error:
            self._publish_registry_event(
                "registry.failed",
                {**data, "error": redact_url_userinfo(status.error)},
            )
        if status.stale:
            self._publish_registry_event("registry.stale", data)
        else:
            self._publish_registry_event("registry.refreshed", data)

    async def refresh_models(self) -> Any:
        """Force a catalogue refresh, publishing ``registry.*`` events.

        Returns the resulting :class:`~nexus.model.registry.RegistryStatus`, or
        ``None`` when no registry is configured.
        """
        if self._registry is None:
            return None
        await self._ensure_registry(force=True)
        return self._registry.status()

    def _publish_registry_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Publish a registry lifecycle event on the runtime event bus."""
        try:
            self._extension_events.publish(Event(type=event_type, data=dict(data)))
        except Exception:  # noqa: BLE001 - a closed bus must not fail a turn
            return

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

    async def aclose_session(self, session_id: str) -> bool:
        """Close one session: fire ``SessionEnd``, then release its shell jobs.

        The ``SessionEnd`` hook runs against the pinned manifest and its events
        are persisted before the session bus closes. Idempotent; a session with
        no live handle returns ``False``.
        """
        closer = getattr(self._sessions, "aclose_session", None)
        closed = False
        if callable(closer):
            closed = bool(await closer(session_id))
        else:  # pragma: no cover - injected managers without the seam
            handle = getattr(self._sessions, "_handles", {}).pop(session_id, None)
            if handle is not None:
                with contextlib.suppress(Exception):
                    await handle.aclose()
                closed = True
        await self.close_session_jobs(session_id)
        return closed

    async def close_session(self, session_id: str) -> bool:
        """Alias for :meth:`aclose_session` (explicit session close)."""
        return await self.aclose_session(session_id)

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
        hooks_service = None
        current_manifest = self.manifest
        if self._hooks is not None:
            hooks_service = _HookService(
                self._hooks,
                getattr(current_manifest, "hooks", None),
                workspace=self.workspace,
            )
        runner = None
        if self._agents is not None and self._tools is None:
            authority = _ChildAuthority(engine=engine, path_guard=manager.path_guard)
            runner = self._make_subagent_runner(
                session_id=session.id,
                parent_tools=manager.names,
                permissions=authority,
                grants=grants,
                config=config,
                catalog=manager.tools,
                hooks=hooks_service,
                budget=self._new_subagent_budget(config),
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
            subagents=runner,
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
                budget=self._new_subagent_budget(config),
            )
        return ToolTurn(
            manager=manager,
            engine=engine,
            dispatcher=dispatcher,
            gate=gate,
            schemas=tuple(manager.schemas()),
            manifest_ref=manifest_ref,
            environment_for=environment_for,
            hooks=hooks_service,
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

    def _build_tiers(self, config: Config) -> TierTable:
        """Build the tier table from ``[models.tiers]`` (plan section 15.4)."""
        v2 = getattr(config, "v2", None)
        models = getattr(v2, "models", None)
        overrides = getattr(models, "tiers", None) if models is not None else None
        default = getattr(models, "default", None) if models is not None else None
        try:
            table = TierTable(overrides=overrides or None, default=DEFAULT_TIER)
            if isinstance(default, str) and default in table.order:
                table = TierTable(overrides=overrides or None, default=default)
            return table
        except ConfigError:
            raise
        except ValueError as exc:  # pragma: no cover - defensive
            raise ConfigError(f"invalid models.tiers: {exc}") from exc

    def _build_registry(self, config: Config) -> ModelRegistry | None:
        """Build the registry only when ``[models]`` is explicitly configured.

        A plain config keeps the Phase 1 router, so the default suite (and any
        run without a model catalogue) never touches the network. An explicit
        ``[models]`` block -- a default, tiers, a URL/TTL, or ``offline`` --
        opts in.
        """
        v2 = getattr(config, "v2", None)
        if v2 is None:
            return None
        models = getattr(v2, "models", None)
        if models is None or not v2.models_configured():
            return None
        sections = getattr(v2, "providers", None) or {}
        providers_cfg = {name: section for name, section in sections.items()}
        cache_path = self.workspace / ".nexus" / "cache" / "models.dev.json"
        return ModelRegistry(
            providers=providers_cfg,
            env=self._environ,
            cache_path=cache_path,
            catalogue_url=models.catalogue_url,
            ttl_days=models.refresh_ttl_days,
            offline=models.offline,
            tier_table=self._tiers,
        )

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
        default_ref = getattr(config, "model", None)
        if v2 is not None:
            if default_ref:
                aliases.setdefault("default", default_ref)
            fast = v2.model_fast()
            plan = v2.model_plan()
            if fast:
                aliases["fast"] = fast
            if plan:
                aliases["plan"] = plan
        return ModelRouter(
            self._providers,
            aliases=aliases,
            default=default_ref,
            registry=self._registry,
            tiers=self._tiers,
        )

    # -- agents / hooks ----------------------------------------------------

    @staticmethod
    def _agents_section(config: Config | None) -> Any | None:
        v2 = getattr(config, "v2", None)
        return getattr(v2, "agents", None)

    @staticmethod
    def _hooks_section(config: Config | None) -> Any | None:
        v2 = getattr(config, "v2", None)
        return getattr(v2, "hooks", None)

    def _build_agent_manager(
        self, config: Config | None, injected: Any, owns: bool | None
    ) -> Any | None:
        """Own the agent-definition manager, seeding the workspace roles once."""
        if injected is not None:
            self._owns_agents = False if owns is None else bool(owns)
            return injected
        self._owns_agents = True
        section = self._agents_section(config)
        if section is None or not getattr(section, "enabled", True):
            return None
        try:
            from .agents import AgentManager
            from .tools.builtin import BUILTIN_TOOLS
            from .tools.bundles import BUNDLES

            known_bundles = {
                name: bundle.tools for name, bundle in BUNDLES.items()
            }
            known_tools = {tool.name for tool in BUILTIN_TOOLS}
            # ``Task`` is injected by the runtime per iteration, not a static
            # builtin; include it so a role may declare it without a false
            # ``unknown_tool`` diagnostic.
            known_tools.add("Task")
            manager = AgentManager.for_workspace(
                self.workspace,
                home=self._home,
                seed=bool(getattr(section, "seed_roles", True)),
                known_tools=known_tools,
                known_bundles=known_bundles,
            )
            return manager
        except Exception:  # noqa: BLE001 - a broken agents dir must not stop boot
            return None

    def _build_hook_manager(
        self, config: Config | None, injected: Any, owns: bool | None
    ) -> Any | None:
        """Own the lifecycle-hook manager (inert until the first rebuild)."""
        if injected is not None:
            self._owns_hooks = False if owns is None else bool(owns)
            return injected
        self._owns_hooks = True
        section = self._hooks_section(config)
        if section is None or not getattr(section, "enabled", True):
            return None
        try:
            return HookManager(self.workspace, home=self._home, config=config)
        except Exception:  # noqa: BLE001 - a broken hooks dir must not stop boot
            return None

    def _session_hooks(self) -> Any | None:
        """The current manifest's hook runner for session-level lifecycle events.

        Called by a :class:`~nexus.session.session.Session` before a prompt is
        persisted and once at close, so ``SessionStart``/``UserPromptSubmit``/
        ``SessionEnd`` run against the pinned set and are persisted through the
        session's own sink.
        """
        if self._hooks is None:
            return None
        manifest = self.manifest
        specs = getattr(manifest, "hooks", None)
        if not specs:
            return None
        return _HookService(self._hooks, specs, workspace=self.workspace)

    def _pre_compact_gate(
        self,
        hooks_service: Any | None,
        session_id: str,
        turn_id: str,
        iteration: int = 0,
        cancel: Any = None,
    ) -> Any | None:
        """Build the async ``PreCompact`` gate for one iteration, or ``None``.

        Runs exactly the pinned generation's PreCompact hooks and translates a
        block/modify into a typed :class:`~nexus.context.manager.PreCompactDecision`.
        A modify is accepted only through the four typed compaction options;
        anything else is disallowed (no options applied). ``cancel`` is the
        parent turn's token, so a command hook is cancelled promptly instead of
        outliving the turn.
        """
        if hooks_service is None or not hooks_service.has_event("PreCompact"):
            return None
        from .context.manager import CompactionOptions, PreCompactDecision

        async def gate(request: Any) -> Any:
            request_turn = getattr(request, "turn_id", None) or turn_id
            outcome = await hooks_service.lifecycle(
                "PreCompact",
                session_id=session_id,
                turn_id=request_turn or "",
                data=request.to_dict(),
                cancel=cancel,
            )
            payload = outcome.to_dict() if hasattr(outcome, "to_dict") else {}
            if getattr(outcome, "blocked", False):
                return PreCompactDecision(
                    blocked=True,
                    reason=str(
                        getattr(outcome, "reason", "")
                        or "blocked by PreCompact hook"
                    ),
                    outcome=payload,
                )
            options = None
            if getattr(outcome, "modified", False):
                options = CompactionOptions.from_mapping(
                    getattr(outcome, "modified_input", None)
                )
            return PreCompactDecision(
                blocked=False, options=options, outcome=payload
            )

        return gate

    # -- subagents ---------------------------------------------------------

    def _ensure_child_sessions(self) -> _ChildSessionFacade:
        if self._child_sessions is None:
            directory = getattr(self._sessions, "directory", None) or (
                self.workspace / ".nexus" / "sessions"
            )
            self._child_sessions = _ChildSessionFacade(Path(directory) / "agents")
        return self._child_sessions

    def _bundle_map(self, catalog: Sequence[Any] | None = None) -> dict[str, list[str]]:
        from .tools.bundles import BUNDLES

        mapping: dict[str, list[str]] = {
            name: list(bundle.tools) for name, bundle in BUNDLES.items()
        }
        for tool in catalog or ():
            spec = getattr(tool, "spec", tool)
            bundle = getattr(spec, "bundle", None)
            name = getattr(spec, "name", None)
            if not isinstance(bundle, str) or not isinstance(name, str):
                continue
            mapping.setdefault(bundle, [])
            if name not in mapping[bundle]:
                mapping[bundle].append(name)
        return mapping

    @staticmethod
    def _mutating_names(catalog: Sequence[Any] | None = None) -> tuple[str, ...]:
        names: set[str] = set()
        for tool in catalog or ():
            spec = getattr(tool, "spec", tool)
            if getattr(spec, "mutates", False):
                name = getattr(spec, "name", None)
                if isinstance(name, str):
                    names.add(name)
        return tuple(sorted(names))

    def _new_subagent_budget(self, config: Config | None) -> Any | None:
        """Build the turn-scoped subagent tree budget, or ``None``.

        One budget per turn (shared across iterations, children, and
        grandchildren) so aggregate token/cost spend accumulates over the turn
        rather than resetting each iteration.
        """
        if self._agents is None:
            return None
        effective = config if isinstance(config, Config) else self._load_config()
        section = self._agents_section(effective)
        if section is None or not getattr(section, "enabled", True):
            return None
        try:
            from .agents import SubagentBudget

            return SubagentBudget(
                max_concurrent=int(getattr(section, "max_concurrent", 4)),
                max_depth=int(getattr(section, "max_depth", 3)),
                max_fanout=getattr(section, "max_fanout", 16),
                token_budget=getattr(section, "token_budget", None),
                cost_budget=getattr(section, "cost_budget", None),
            )
        except Exception:  # noqa: BLE001 - an invalid config disables the budget
            return None

    def _make_subagent_runner(
        self,
        *,
        session_id: str,
        parent_tools: Sequence[str],
        parent_depth: int = 0,
        parent_session: str | None = None,
        parent_tier: str | None = None,
        budget: Any | None = None,
        permissions: Any | None = None,
        grants: Sequence[Any] = (),
        config: Config | None = None,
        catalog: Sequence[Any] | None = None,
        event_sink: Any | None = None,
        hooks: Any | None = None,
    ) -> Any | None:
        """Build a bounded subagent runner for one turn/iteration, or ``None``.

        The runner is the ``Task`` service: it computes authority, clamps the
        tier, enforces the shared tree budget, and drives children through the
        real child runtime. It is built fresh per iteration because its tool
        ceiling is that iteration's catalog.
        """
        if self._agents is None:
            return None
        effective = config if isinstance(config, Config) else self._load_config()
        section = self._agents_section(effective)
        if section is None or not getattr(section, "enabled", True):
            return None
        max_tier = str(getattr(section, "max_tier", "medium"))
        if self._tiers.rank(max_tier) is None:
            max_tier = self._tiers.default
        try:
            return SubagentRunner(
                agents=self._agents,
                runtime_factory=self._build_child_runtime,
                workspace=self.workspace,
                parent_session=parent_session or session_id,
                tiers=self._tiers,
                sessions=self._ensure_child_sessions(),
                parent_tools=tuple(parent_tools or ()),
                parent_tier=parent_tier or self._tiers.default,
                parent_depth=parent_depth,
                permissions=permissions,
                grants=tuple(grants),
                budget=budget,
                max_tier=max_tier,
                max_concurrent=int(getattr(section, "max_concurrent", 4)),
                max_depth=int(getattr(section, "max_depth", 3)),
                max_fanout=getattr(section, "max_fanout", 16),
                token_budget=getattr(section, "token_budget", None),
                cost_budget=getattr(section, "cost_budget", None),
                default_type=str(getattr(section, "default_type", "general")),
                config=effective,
                event_sink=event_sink,
                bundle_map=self._bundle_map(catalog),
                mutating_tools=self._mutating_names(catalog),
                hooks=hooks,
            )
        except Exception:  # noqa: BLE001 - an invalid agents config disables Task
            return None

    def _build_child_runtime(self, spec: Any) -> _ChildRuntime:
        """The ``RuntimeFactory``: build a nested, restricted child run."""
        config = spec.config if isinstance(spec.config, Config) else self._load_config()
        child_runner = self._make_subagent_runner(
            session_id=spec.session_id,
            parent_tools=spec.tools,
            parent_depth=spec.depth,
            parent_session=spec.session_id,
            parent_tier=spec.tier,
            budget=spec.budget,
            permissions=spec.permissions,
            grants=spec.grants,
            config=config,
            hooks=spec.hooks,
        )
        return _ChildRuntime(self, spec, child_runner)

    def _child_config(self, spec: Any) -> Config:
        base = spec.config if isinstance(spec.config, Config) else self._load_config()
        reference = spec.model
        # Without a model registry a tier name cannot resolve to a concrete
        # model, so an inherited/clamped tier falls back to the parent's model.
        if reference in self._tiers.order and self._registry is None:
            reference = getattr(base, "model", None)
        if not reference:
            reference = getattr(base, "model", None)
        import dataclasses

        v2 = getattr(base, "v2", None)
        if v2 is not None:
            with contextlib.suppress(Exception):
                child_v2 = dataclasses.replace(
                    v2,
                    model=dataclasses.replace(v2.model, default=None),
                    models=dataclasses.replace(v2.models, default=None),
                )
                return dataclasses.replace(base, model=reference, v2=child_v2)
        return dataclasses.replace(base, model=reference)

    def _build_child_assembler(self, spec: Any, config: Config) -> Any:
        manager = self._assembler
        if not hasattr(manager, "for_iteration"):
            return manager
        # The agent body is the child's SOUL; MEMORY is deliberately empty.
        return manager.for_iteration(
            config=config,
            system_files={"soul": spec.system_prompt, "memory": ""},
            skills_index=(),
            mcp_index={},
        )

    def _build_child_tool_manager(
        self, spec: Any, config: Config, runner: Any
    ) -> ToolManager:
        manifest = self.manifest
        catalog_map: dict[str, Any] = (
            dict(manifest.tools) if manifest is not None else {}
        )
        if runner is not None:
            from .tools.builtin.task import build_task_tool

            catalog_map["Task"] = build_task_tool(runner)
        catalog = [catalog_map[name] for name in spec.tools if name in catalog_map]
        authority = (
            spec.permissions if isinstance(spec.permissions, _ChildAuthority) else None
        )
        return ToolManager(
            config,
            workspace=self.workspace,
            tools=catalog,
            path_guard=authority.path_guard if authority is not None else None,
            job_registry=self._job_registry,
            todo_store=self._todo_store,
        )

    def _child_permission_engine(self, spec: Any, config: Config) -> PermissionEngine:
        authority = spec.permissions
        if isinstance(authority, _ChildAuthority):
            return authority.engine
        permissions = getattr(getattr(config, "v2", None), "permissions", None)
        if permissions is not None:
            return PermissionEngine.from_config(
                permissions, workspace=self.workspace, home=self._home
            )
        return PermissionEngine(workspace=self.workspace, home=self._home)

    @staticmethod
    def _child_outcome(
        spec: Any, session: Any, outcome: Any, *, cost: float | None = None
    ) -> SubagentOutcome:
        text = ""
        for message in reversed(session.messages):
            if getattr(message, "role", None) != "assistant":
                continue
            chunk = "".join(
                block.text
                for block in message.content
                if isinstance(block, Text)
            )
            if chunk.strip():
                text = chunk
                break
        ok = bool(getattr(outcome, "ok", False))
        phase = getattr(outcome, "phase", "failed")
        if ok:
            status = "completed"
        elif phase == "cancelled":
            status = "cancelled"
        else:
            status = "failed"
        usage = getattr(outcome, "usage", None)
        return SubagentOutcome(
            agent=spec.agent,
            session_id=spec.session_id,
            status=status,
            text=text,
            is_error=not ok,
            usage=SubagentUsage(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
                cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
                reasoning_tokens=int(getattr(usage, "reasoning_tokens", 0) or 0),
                cost_usd=cost,
            ),
            iterations=int(getattr(outcome, "iterations", 0) or 0),
            stop_reason=getattr(outcome, "stop_reason", None),
            dropped_tools=tuple(spec.dropped_tools),
            clamped=bool(spec.clamped),
            tier=spec.tier,
            requested_tier=spec.requested_tier,
            error=getattr(outcome, "error", None),
        )

    def _child_cost(self, session: Any, outcome: Any) -> float | None:
        """Price a child turn from the registry's ``ModelInfo`` cost, or ``None``.

        The child's concrete provider/model comes from its ``model.started``
        event; pricing is ``$``/Mtok from ``models.dev``. Unknown pricing (no
        registry, no catalogue entry, or no cost data — 421 catalogue models are
        local/open-weight) returns ``None``, so the aggregate ``spent_cost``
        counts only priced models: a ``cost_budget`` is therefore a bound over
        priced models only, and ``token_budget`` is the universal bound. This is
        documented rather than guessed.
        """
        registry = self._registry
        if registry is None:
            return None
        provider = model = None
        for event in reversed(list(getattr(session, "events", ()) or ())):
            if getattr(event, "type", None) != "model.started":
                continue
            data = getattr(event, "data", None) or {}
            candidate_provider = data.get("provider")
            candidate_model = data.get("model")
            if isinstance(candidate_provider, str) and isinstance(candidate_model, str):
                provider, model = candidate_provider, candidate_model
                break
        if not provider or not model:
            return None
        model_cost = getattr(registry, "model_cost", None)
        if callable(model_cost):
            pricing = model_cost(provider, model)
        else:  # pragma: no cover - duck-typed registry without the seam
            get = getattr(registry, "get", None)
            info = get(f"{provider}/{model}") if callable(get) else None
            pricing = getattr(info, "cost", None)
        if pricing is None:
            return None
        usage = getattr(outcome, "usage", None)
        if usage is None:
            return None
        input_rate = float(getattr(pricing, "input", 0.0) or 0.0)
        output_rate = float(getattr(pricing, "output", 0.0) or 0.0)
        total = (
            int(getattr(usage, "input_tokens", 0) or 0) * input_rate
            + int(getattr(usage, "output_tokens", 0) or 0) * output_rate
            + int(getattr(usage, "cache_read_tokens", 0) or 0)
            * float(getattr(pricing, "cache_read", 0.0) or 0.0)
            + int(getattr(usage, "cache_write_tokens", 0) or 0)
            * float(getattr(pricing, "cache_write", 0.0) or 0.0)
            # Reasoning is priced at the output rate; where a provider already
            # folds reasoning into output this over-counts, the conservative
            # direction for a budget.
            + int(getattr(usage, "reasoning_tokens", 0) or 0) * output_rate
        ) / 1_000_000
        return total

    # -- MCP ---------------------------------------------------------------

    @property
    def mcp(self) -> Any | None:
        """The runtime-owned (or injected) MCP manager."""
        return self._mcp

    def _build_mcp_manager(self, config: Config) -> Any:
        """Construct the MCP server manager from the effective config.

        The manager is cheap and does no I/O here: it parses nothing until a
        reconcile and connects nothing until ``ensure_started``. A caller may
        inject ``mcp_client_factory`` to choose a client backend (for example
        the deterministic native transports in tests).
        """
        from .mcp.manager import MCPManager

        v2 = getattr(config, "v2", None)
        mcp_config = getattr(v2, "mcp", None)
        enabled = bool(getattr(mcp_config, "enabled", True))
        restart_max = getattr(mcp_config, "restart_max", 5)
        return MCPManager(
            None,
            enabled=enabled,
            cache_dir=self.workspace / ".nexus" / "cache" / "mcp",
            workspace=self.workspace,
            environ=self._environ,
            client_factory=self._mcp_client_factory,
            sink=_MCPEventSink(self),
            restart_max=restart_max,
        )

    def _on_mcp_event(self, event: Event) -> None:
        """Publish an MCP event and, for tool-affecting ones, schedule a rebuild."""
        try:
            self._extension_events.publish(event)
        except Exception:  # noqa: BLE001, S110 - a closed bus must not break MCP
            pass
        if getattr(event, "type", None) in _MCP_REBUILD_EVENTS:
            self._schedule_mcp_rebuild()

    def _schedule_mcp_rebuild(self) -> None:
        """Coalesce change notifications into one background manifest rebuild.

        Safe to call from any MCP coroutine: it only schedules on a running loop,
        does nothing after close, and suppresses a second task while one is
        pending. The rebuild reconciles definitions, connects lazily (a no-op
        when already connected), and swaps the manifest only if the aggregate
        actually changed — so it cannot loop back into another change.
        """
        if self._extensions is None or self._extensions.closed:
            return
        if self._mcp_rebuild_pending:
            # A rebuild is already in flight. Mark it dirty so it runs once more
            # *after* it reads the snapshot; otherwise a change that lands during
            # the build (a `list_changed` refresh, a late connect) would be lost.
            self._mcp_rebuild_dirty = True
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._mcp_rebuild_pending = True
        self._mcp_rebuild_task = loop.create_task(self._run_mcp_rebuild())

    async def _run_mcp_rebuild(self) -> None:
        try:
            while not self._closed:
                self._mcp_rebuild_dirty = False
                extensions = self._extensions
                if extensions is None or extensions.closed:
                    return
                await asyncio.sleep(0)
                await extensions.reload(trigger="mcp", sink=self._extension_events)
                if not self._mcp_rebuild_dirty:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background rebuild must not raise
            return
        finally:
            self._mcp_rebuild_pending = False
            if self._mcp_rebuild_dirty and not self._closed:
                # Re-arm for a change that arrived in the final window.
                self._schedule_mcp_rebuild()

    async def _aclose_mcp(self) -> None:
        task, self._mcp_rebuild_task = self._mcp_rebuild_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._owns_mcp and self._mcp is not None:
            aclose = getattr(self._mcp, "aclose", None)
            if aclose is not None:
                await aclose()

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
        # Close sessions first, while the manifest and hook manager are still
        # live, so every ``SessionEnd`` runs against the pinned set and its events
        # are persisted before the session buses close.
        if self._owns_sessions and self._sessions is not None:
            aclose_all = getattr(self._sessions, "aclose_all", None)
            if callable(aclose_all):
                with contextlib.suppress(Exception):
                    await aclose_all()
            else:  # pragma: no cover - injected managers without the seam
                for handle in self._sessions.close_all():
                    with contextlib.suppress(Exception):
                        await handle.aclose()
        if self._owns_extensions and self._extensions is not None:
            # The extension manager is the sole owner of the modules it loaded:
            # its ``aclose`` retires the live generation through the ref and
            # releases every module and staged copy after the last lease drains.
            # The runtime must not second-guess that with a direct release.
            await self._extensions.aclose()
        # Retire the hook modules and any open child-session handles the runtime
        # owns. The runner closes each child it spawns; this is the outer net for
        # a handle left open by a cancelled turn.
        if self._owns_hooks and self._hooks is not None:
            aclose = getattr(self._hooks, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        if self._child_sessions is not None:
            with contextlib.suppress(Exception):
                await self._child_sessions.manager.aclose_all()
        # Close MCP after the extension world so no scheduled change callback can
        # rebuild against a half-closed ref; the task is cancelled first.
        await self._aclose_mcp()
        await self._extension_events.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.aclose()
        return False
