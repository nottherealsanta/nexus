"""Runtime composition root (plan section 2.1, layer L4).

``Runtime`` wires the pieces together — configuration, providers, the model
router, the context manager, the session manager, and the tool infrastructure.
It is the object a workspace daemon owns; surfaces reach it only through
``nexus.host`` and never import it directly.

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
import copy
import hashlib
import inspect
import re
import subprocess
import threading
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import httpx
import msgspec
import os

from .agents import SubagentOutcome, SubagentRunner, SubagentUsage
from .agents.model import AgentError, AgentNotFoundError
from .agents.runner import WORKTREE_CHILD_TOOLS, files_changed
from .config import Config
from .config.paths import (
    nexus_home,
    project_agents_dir,
    project_key,
    project_state_dir,
    state_db_path,
)
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
from .model.http import DEFAULT_TIMEOUT
from .model.message import Document, Image, Text, Thinking, ToolResult, ToolUse
from .model.provider import Provider
from .model.providers.anthropic import AnthropicProvider
from .model.providers.gemini import GeminiProvider
from .model.providers.ollama import OllamaProvider
from .model.providers.openai import OpenAIProvider
from .model.providers.opencode import OpenCodeProvider
from .model.registry import (
    ADAPTER_ANTHROPIC,
    ADAPTER_CLAUDE_AGENT,
    ADAPTER_GEMINI,
    ADAPTER_OLLAMA,
    ADAPTER_OPENAI,
    ADAPTER_OPENCODE,
    OPENAI_COMPATIBLE,
    ModelRegistry,
)
from .model.request import REASONING_EFFORT_ORDER, ModelRequest, ToolSchema
from .model.router import ModelRouter
from .model.selection import ModelSelection
from .model.tiers import DEFAULT_TIER, TierTable
from .net import OutboundHTTPService, SafeOutboundHTTPService
from .net.local_search import LocalSearchHTTPService
from .session import Session, SessionManager
from .session.agent_selection import AgentSelection
from .session.db import StateDatabase
from .tools.builtin._jobs import JobRegistry
from .tools.builtin.question import ANSWER_WINDOW_S
from .tools.builtin.todo import TodoStore
from .tools.manager import ToolManager
from .tools.permissions import (
    ApprovalBroker,
    Decision,
    Grant,
    Outcome,
    PathGuard,
    PermissionEngine,
    collect_grants,
)
from .tools.questions import QuestionBroker
from .tools.spec import ToolCall, ToolContext
from .util import redact_secrets, redact_url_userinfo

if TYPE_CHECKING:  # pragma: no cover - typing only; the runtime imports lazily
    from .ext.manager import ExtensionManager
    from .skills.manager import SkillManager

__all__ = ["Runtime", "ToolTurn"]


#: Provider section names that name a shipped adapter directly. ``codex`` is
#: here so a Codex model reference (``codex/gpt-5-codex``) routes to the OpenAI
#: adapter's Responses dialect.
_CORE_ADAPTERS: dict[str, str] = {
    "anthropic": ADAPTER_ANTHROPIC,
    "openai": ADAPTER_OPENAI,
    "codex": ADAPTER_OPENAI,
    "google": ADAPTER_GEMINI,
    "gemini": ADAPTER_GEMINI,
    "ollama": ADAPTER_OLLAMA,
    "opencode": ADAPTER_OPENCODE,
    "claude-agent": ADAPTER_CLAUDE_AGENT,
}

#: ``kind`` spellings that mean "any OpenAI-compatible endpoint".
_OPENAI_COMPATIBLE_KINDS = frozenset(
    {OPENAI_COMPATIBLE, "openai-compatible", "openai_compatible", "compatible"}
)

#: ``kind`` spellings for the OpenCode ACP subprocess agent surface.
_OPENCODE_AGENT_KINDS = frozenset(
    {ADAPTER_OPENCODE, "opencode-agent", "opencode_agent", "acp"}
)

#: ``kind`` spellings for the Gemini adapter. ``google`` is the catalogue id, so
#: it must select the same adapter as ``gemini`` rather than fall through to the
#: OpenAI-compatible default.
_GEMINI_KINDS = frozenset({ADAPTER_GEMINI, "google"})


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
        agent_id: str = "root",
        skills: Any | None = None,
        extensions: Any | None = None,
        activations: Any | None = None,
        subagents: Any | None = None,
        outbound_http: OutboundHTTPService | None = None,
        local_search_http: LocalSearchHTTPService | None = None,
        questions: Any | None = None,
    ) -> None:
        self.manager = manager
        self._workspace = Path(workspace)
        self._session_id = session_id
        self._turn_id = turn_id
        self._config = config
        self._agent_id = agent_id
        #: The narrow, manager-owned service seams a tool may reach. Injected
        #: here (never a ``Runtime``) so ``Skill``/``ReloadExtensions`` can act
        #: without the tools layer importing a concrete manager.
        self._skills = skills
        self._extensions = extensions
        self._activations = activations
        #: The ``Task`` subagent service for this iteration. Injected into every
        #: ``ToolContext`` so the builtin never imports ``nexus.agents``.
        self._subagents = subagents
        self._outbound_http = outbound_http
        self._local_search_http = local_search_http
        self._questions = questions

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
            agent_id=self._agent_id,
            skills=self._skills,
            extensions=self._extensions,
            activations=self._activations,
            subagents=self._subagents,
            outbound_http=self._outbound_http,
            local_search_http=self._local_search_http,
            questions=self._questions,
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


class _OperatorWatch:
    """Cancel source for a question: the turn's token, or losing every viewer.

    Presence can drop to zero while a question is open; polling the root
    session's ``attended`` flag lets the waiter end instead of parking the turn
    until the answer window closes.
    """

    _POLL_S = 1.0

    def __init__(self, token: Any | None, session: Any | None) -> None:
        self._token = token
        self._session = session
        self.reason: str | None = None

    async def wait(self) -> None:
        while True:
            token_wait = getattr(self._token, "wait", None)
            try:
                if callable(token_wait):
                    await asyncio.wait_for(token_wait(), self._POLL_S)
                    self.reason = getattr(self._token, "reason", None) or "cancelled"
                    return
                await asyncio.sleep(self._POLL_S)
            except TimeoutError:
                pass
            if self._session is not None and not getattr(self._session, "attended", True):
                self.reason = "the operator disconnected"
                return


class _QuestionService:
    """Binds the runtime's :class:`QuestionBroker` to one root session."""

    def __init__(self, broker: QuestionBroker, root_session_id: str, session: Any | None) -> None:
        self._broker = broker
        self._root = root_session_id
        self._session = session

    @property
    def attended(self) -> bool:
        return bool(getattr(self._session, "attended", False))

    async def ask(self, *, cancel: Any = None, **request: Any) -> str:
        return await self._broker.request(
            root_session_id=self._root,
            cancel=_OperatorWatch(cancel, self._session),
            **request,
        )


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
        manager: ToolManager | None = None,
        grants: tuple[Grant, ...] = (),
        attended: bool = True,
        broker: ApprovalBroker | None = None,
    ) -> None:
        self.engine = engine
        self._manager = manager
        self._grants = tuple(grants)
        self._attended = bool(attended)
        self._broker = broker if broker is not None else ApprovalBroker()
        self._futures: dict[str, asyncio.Future] = {}
        #: Futures the loop has already started awaiting. ``await_decision`` pops
        #: them from ``_futures``; this second map keeps them reachable so a
        #: presence-driven fallback can resolve/fail a request that is parked.
        self._inflight: dict[str, asyncio.Future] = {}
        self._resolved_decisions: dict[str, tuple[Any, Decision]] = {}
        self._approval_evaluations: dict[str, Any] = {}
        self._issued_plans: dict[int, tuple[Any, Any]] = {}
        #: One unforgeable evidence scope per gate/turn. Managers can be shared
        #: across sessions, so cleanup must revoke only this gate's capabilities.
        self._evidence_scope = object()

    def plan(self, prepared):
        plan = self.engine.plan(
            prepared.calls(),
            prepared.spec_map(),
            grants=self._grants,
            attended=self._attended,
        )
        evaluations = []
        for evaluation in plan.evaluations:
            if (
                evaluation.call.name == "subagent"
                and evaluation.call.input.get("worktree") is True
                and evaluation.outcome is not Outcome.DENY
            ):
                if self._attended:
                    evaluation = replace(
                        evaluation,
                        outcome=Outcome.ASK,
                        decision=None,
                        code="worktree_approval_required",
                        reason="Creating an isolated worktree requires explicit approval",
                    )
                else:
                    evaluation = replace(
                        evaluation,
                        outcome=Outcome.DENY,
                        decision=Decision.DENY_ONCE,
                        code="worktree_approval_required",
                        reason=(
                            "Worktree creation requires attended approval; "
                            "unattended child calls are denied"
                        ),
                    )
            evaluations.append(evaluation)
        if len(evaluations) != len(plan.evaluations) or any(
            updated is not original
            for updated, original in zip(evaluations, plan.evaluations)
        ):
            plan = replace(plan, evaluations=tuple(evaluations))
        self._issued_plans[id(plan)] = (plan, prepared)
        return plan

    def bind_manager(self, manager: ToolManager) -> None:
        """Bind multi-target evidence to the manager executing this iteration.

        Manifest-backed catalogs are rebuilt per iteration, and static agent
        setup may also rebuild its final catalog. The permission baseline stays
        turn-frozen, but evidence must be issued by the exact manager that will
        consume it. A manager switch is only valid between approval batches.
        """
        if self._manager is manager:
            return
        if self._futures or self._inflight:
            raise RuntimeError("cannot replace a manager with pending approvals")
        revoke = getattr(self._manager, "_revoke_multi_target_authorizations", None)
        if callable(revoke):
            revoke(self._evidence_scope)
        self._manager = manager
        self._issued_plans.clear()
        self._resolved_decisions.clear()
        self._approval_evaluations.clear()

    def request_for(self, evaluation):
        request = self.engine.request_for(evaluation)
        self._approval_evaluations[request.id] = evaluation
        return request

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
                decision = future.result()
                evaluation = self._approval_evaluations.get(request.id)
                if getattr(evaluation, "target_evaluations", ()):
                    self._resolved_decisions[request.id] = (
                        request, Decision.from_value(decision)
                    )
                else:
                    self._approval_evaluations.pop(request.id, None)
                return decision
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

    def authorize(self, prepared, plan, decisions):
        """Attach manager-owned evidence after the loop's complete approval pass."""
        issued = self._issued_plans.pop(id(plan), None)
        if (
            self._manager is None
            or issued is None
            or issued[0] is not plan
            or tuple(item.call for item in issued[1].entries)
            != tuple(item.call for item in prepared.entries)
        ):
            return prepared
        supplied = list(decisions)
        evaluations = tuple(plan.evaluations)
        authorized = prepared
        for evaluation in evaluations:
            if not getattr(evaluation, "target_evaluations", ()):
                continue
            if getattr(evaluation.outcome, "value", None) not in {"allow", "ask"}:
                continue
            asks = tuple(
                item
                for item in evaluation.target_evaluations
                if getattr(item.outcome, "value", None) == "ask"
            )
            if asks:
                resolved = next(
                    (
                        (call_id, raw)
                        for call_id, raw in supplied
                        if call_id == evaluation.call.id
                    ),
                    None,
                )
                if resolved is None:
                    continue
                decision = Decision.from_value(resolved[1])
                # Prove the decision came through this gate's real ApprovalBroker
                # and was resolved for this exact request/evaluation.
                request_id = next(
                    (
                        key
                        for key, requested in self._approval_evaluations.items()
                        if requested is evaluation
                    ),
                    None,
                )
                if request_id is None:
                    continue
                resolved = self._resolved_decisions.pop(request_id, None)
                self._approval_evaluations.pop(request_id, None)
                if resolved is None:
                    continue
                request, broker_decision = resolved
                if broker_decision != decision:
                    continue
                record = self.resolution(request.id)
                if (
                    record is None
                    or record.get("id") != request.id
                    or record.get("call_id") != evaluation.call.id
                    or record.get("tool") != evaluation.call.name
                ):
                    continue
                if not decision.allows:
                    continue
            else:
                decision = evaluation.decision or Decision.ALLOW_ONCE
            authorized = self._manager._authorize_multi_target(
                authorized,
                evaluation,
                decision,
                authority=self._manager._multi_target_authority,
                scope=self._evidence_scope,
            )
        return authorized

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
        revoke = getattr(self._manager, "_revoke_multi_target_authorizations", None)
        if callable(revoke):
            revoke(self._evidence_scope)
        self._issued_plans.clear()
        self._resolved_decisions.clear()
        self._approval_evaluations.clear()


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
        agents: Any | None = None,
        tiers: Any | None = None,
        registry: Any | None = None,
        runtime: Any | None = None,
    ) -> None:
        self._context = context
        self._resolver = resolver
        self._token_cache = token_cache
        self._agents = agents
        self._tiers = tiers
        self._registry = registry
        #: Marker read by :meth:`Session._assemble_for_turn`: this coordinator
        #: accepts a ``session`` so it can freeze that session's model override.
        self.session_aware = True

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

    def _route_reference(
        self,
        config: Any | None,
        *,
        agent_definition: Any | None = None,
        model_selection: ModelSelection | None = None,
        apply_agent: bool = True,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Choose the requested route before resolving it through the router.

        Session selections always take precedence. Otherwise the selected root
        agent may override the configured route; tier references are interpreted
        from the agent's own model field so a configured tier cannot mask a
        concrete agent default. The final two values are the configured route,
        retained for a safe fallback if an agent route has gone stale.
        """
        configured_provider: str | None = None
        configured_model: str | None = None
        reference = getattr(self._context, "model_reference", None)
        if config is not None and callable(reference):
            try:
                configured_provider, configured_model = reference(config)
            except ConfigError:  # unresolved config may use router default
                pass

        if model_selection is not None:
            return (
                model_selection.provider,
                model_selection.model,
                configured_provider,
                configured_model,
            )

        provider_name, model = configured_provider, configured_model
        if agent_definition is not None and apply_agent:
            agent_model = getattr(agent_definition, "model", None)
            agent_provider = getattr(agent_definition, "provider", None)
            if isinstance(agent_model, str) and agent_model not in ("inherit", ""):
                if agent_model in getattr(self._tiers, "order", ()):
                    if self._registry is not None:
                        # A tier is a router reference, not a provider/model id.
                        provider_name, model = None, agent_model
                    elif isinstance(agent_provider, str) and agent_provider:
                        # Without a registry the tier is not resolvable; retain
                        # the configured model while honoring the agent adapter.
                        provider_name = agent_provider
                elif "/" in agent_model:
                    provider_name, _, model = agent_model.partition("/")
                else:
                    provider_name, model = agent_provider, agent_model
            elif isinstance(agent_provider, str) and agent_provider:
                provider_name = agent_provider

        return (
            provider_name,
            model,
            configured_provider,
            configured_model,
        )

    def _root_route(
        self,
        session: Any | None,
        *,
        config: Any | None = None,
        agent_definition: Any | None = None,
        model_selection: ModelSelection | None = None,
    ) -> tuple[str | None, str | None, Any | None]:
        """Resolve the next root-turn route without contacting a provider.

        Route precedence is shared with iteration setup by ``_route_reference``.
        An explicit session selection wins; otherwise the selected root agent
        can override the workspace route. A stale agent default falls back to
        the configured route, while a stale explicit session selection falls
        back directly to the configured route.
        """
        if config is None:
            config = self.effective_config()
        session_selection = getattr(session, "model_selection", None)
        selection = model_selection if model_selection is not None else session_selection
        provider_name, model, configured_provider, configured_model = (
            self._route_reference(
                config,
                agent_definition=agent_definition,
                model_selection=selection,
            )
        )
        resolved = self._resolve(provider_name, model)
        if resolved is None and selection is not None:
            provider_name, model, configured_provider, configured_model = (
                self._route_reference(
                    config,
                    agent_definition=agent_definition,
                    apply_agent=False,
                )
            )
            resolved = self._resolve(provider_name, model)
        if resolved is None and (provider_name, model) != (
            configured_provider,
            configured_model,
        ):
            provider_name, model = configured_provider, configured_model
            resolved = self._resolve(provider_name, model)
        if resolved is not None:
            return provider_name or resolved.provider.name, resolved.model, resolved
        # When no router/config exists, an explicit route can still be
        # described from its durable selection fields.
        return provider_name, model, None

    def root_route_metadata(self, session: Any) -> dict[str, str | None]:
        """Return the effective provider/model for the session's next root turn.

        This query only performs local route resolution; it never calls a
        provider or performs network I/O.
        """
        config = self.effective_config()
        agent_definition = self._root_agent_definition(session, config)
        provider, model, _resolved = self._root_route(
            session, config=config, agent_definition=agent_definition
        )
        return {"provider": provider, "model": model}

    def _configured_agent_exists(self, name: object) -> bool:
        """Whether ``agent.name`` resolves to a root agent (legacy aliases included)."""
        try:
            return self._agents is not None and self._agents.resolve(name, context="root") is not None
        except AgentError:
            return False

    def _root_agent_definition(self, session: Any, config: Any | None) -> Any | None:
        if self._agents is None:
            return None
        if config is not None:
            try:
                self._agents.refresh()
            except (AgentError, OSError):  # metadata remains descriptive
                pass
        selection = getattr(session, "agent_selection", None)
        configured_name = getattr(
            getattr(getattr(config, "v2", None), "agent", None), "name", "build"
        )
        name = getattr(selection, "name", None) or configured_name
        try:
            return self._agents.resolve(name, context="root")
        except AgentNotFoundError:  # unavailable agent has no route default
            return None

    @staticmethod
    def _supported_efforts(
        resolved: Any, registry: Any | None, *, provider_name: str | None = None
    ) -> tuple[str, ...]:
        """Return exact efforts for a route that can apply them.

        ScriptedProvider is intentionally supported as an offline test adapter,
        but it still needs explicit per-model metadata. Capabilities alone never
        imply levels, and unknown models have no advertised effort choices.
        """
        provider = getattr(resolved, "provider", None)
        capabilities = getattr(resolved, "capabilities", None)
        if not bool(getattr(capabilities, "thinking", False)):
            return ()
        scripted = getattr(provider, "name", None) == "scripted"
        if not scripted and getattr(provider, "_api", None) != "responses":
            return ()
        if registry is None:
            return ()
        get = getattr(registry, "get", None)
        routed_provider = provider_name or getattr(provider, "name", "")
        info = (
            get(f"{routed_provider}/{getattr(resolved, 'model', '')}")
            if callable(get)
            else None
        )
        levels = getattr(info, "reasoning_efforts", ())
        if not isinstance(levels, (tuple, list)):
            return ()
        offered = set(levels)
        return tuple(level for level in REASONING_EFFORT_ORDER if level in offered)

    def _effort_metadata(
        self,
        session: Any,
        agent_definition: Any | None,
        resolved: Any,
        *,
        provider_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve root-session effort precedence for one concrete route."""
        supported = self._supported_efforts(
            resolved, self._registry, provider_name=provider_name
        )
        selection = getattr(session, "reasoning_effort_selection", None)
        stored = getattr(selection, "effort", None)
        agent_default = getattr(agent_definition, "reasoning_effort", None)
        if stored is not None:
            effective = stored if stored in supported else None
            source = "session" if effective is not None else None
        elif getattr(session, "model_selection", None) is not None:
            effective, source = None, None
        elif agent_default is not None and agent_default in supported:
            effective, source = agent_default, "agent"
        else:
            effective, source = None, None
        return {
            "supported_levels": supported,
            "stored_override": stored,
            "effective_effort": effective,
            "source": source,
        }

    def _effective_effort(
        self,
        session: Any | None,
        agent_definition: Any | None,
        resolved: Any,
        *,
        provider_name: str | None = None,
    ) -> str | None:
        supported = self._supported_efforts(
            resolved, self._registry, provider_name=provider_name
        )
        selection = getattr(session, "reasoning_effort_selection", None)
        stored = getattr(selection, "effort", None)
        if stored is not None:
            return stored if stored in supported else None
        if getattr(session, "model_selection", None) is not None:
            return None
        default = getattr(agent_definition, "reasoning_effort", None)
        return default if default in supported else None

    def root_reasoning_effort_metadata(self, session: Any) -> dict[str, Any]:
        """Return the root session's exact reasoning-effort choices.

        Resolves the session's current model selection first, then the selected
        root agent's route/default, then the workspace route. Unknown model
        metadata yields no choices; a stored unsupported override is reported
        as dormant and is never replaced by the agent default.
        """
        config = self.effective_config()
        agent_definition = getattr(session, "_turn_agent_definition", None)
        if agent_definition is None:
            agent_definition = self._root_agent_definition(session, config)
        provider_name, _model, resolved = self._root_route(
            session, config=config, agent_definition=agent_definition
        )
        return self._effort_metadata(
            session, agent_definition, resolved, provider_name=provider_name
        )

    def for_turn(
        self,
        *,
        session: Any | None = None,
        model_selection: ModelSelection | None = None,
        agent_definition: Any | None = None,
    ) -> Any:
        """Freeze one turn's assembler, honoring a session model override.

        The override (a session's last durable ``model.selected``) is resolved
        once, here at turn start, and captured into the snapshot; the running
        turn never re-reads it. If the override can no longer resolve (the
        provider was removed from config), the configured default is used
        instead, so a stale selection cannot wedge a turn.
        """
        manager = self._context
        for_turn = getattr(manager, "for_turn", None)
        if not callable(for_turn):
            return manager
        session_model_selection = getattr(session, "model_selection", None)
        config = self.effective_config()
        selection = getattr(session, "agent_selection", None) if session is not None else None
        configured_name = getattr(
            getattr(getattr(config, "v2", None), "agent", None), "name", "build"
        )
        if self._agents is not None:
            self._agents.refresh()
        if agent_definition is None and self._agents is not None:
            name = getattr(selection, "name", None)
            if name is None:
                name = configured_name
            try:
                agent_definition = self._agents.resolve(name, context="root")
            except Exception as exc:
                raise ConfigError(f"cannot prepare root agent {name!r}: {exc}") from exc
        if selection is not None and self._agents is not None:
            try:
                self._agents.resolve(selection.name, context="root")
            except Exception as exc:
                raise ConfigError(
                    f"cannot prepare selected root agent {selection.name!r}: {exc}"
                ) from exc
        provider_name, model, resolved = self._root_route(
            session,
            config=config,
            agent_definition=agent_definition,
            model_selection=model_selection,
        )
        capabilities = getattr(resolved, "capabilities", None)
        request_counter = None
        selected_agent_effort = None
        explicit_session_model = session_model_selection is not None
        if resolved is not None:
            request_counter = RequestTokenCounter(
                resolved.provider,
                cache=self._token_cache,
                provider_name=provider_name,
            )
            selected_agent_effort = self._effective_effort(
                session, agent_definition, resolved, provider_name=provider_name
            )
        assembler = for_turn(
            capabilities=capabilities,
            request_counter=request_counter,
            model=model,
            provider=provider_name,
            reasoning_effort=selected_agent_effort,
        )
        if session is not None:
            # Captured by the manifest factory for every iteration of this root
            # turn. Subsequent session selection changes are next-turn only.
            session._turn_reasoning_effort = selected_agent_effort
        if hasattr(assembler, "_reasoning_effort"):
            assembler._reasoning_effort = selected_agent_effort
        if hasattr(assembler, "_agent_effort_supported"):
            assembler._agent_effort_supported = selected_agent_effort is not None
        explicit_session_model = getattr(session, "model_selection", None) is not None
        if explicit_session_model:
            assembler.agent_definition = None
        if agent_definition is not None:
            selected_agent_effort = getattr(
                assembler, "_reasoning_effort", selected_agent_effort
            )
            append_prompt = getattr(assembler, "append_agent_prompt", None)
            if callable(append_prompt):
                append_prompt(agent_definition.load_body())
            if not explicit_session_model:
                assembler.agent_definition = agent_definition
                assembler._requested_agent_effort = selected_agent_effort
                if agent_definition.provider and agent_definition.model and "/" not in agent_definition.model:
                    assembler._agent_provider_default = agent_definition.provider
                assembler._agent_effort_supported = selected_agent_effort is not None
            assembler.agent_selection_source = (
                "session" if getattr(session, "agent_selection", None) is not None else
                "config" if self._configured_agent_exists(configured_name) else
                "default"
            )
            assembler._context_agent_definition = agent_definition
            assembler.agent_selection_source = (
                "session" if getattr(session, "agent_selection", None) is not None else
                "config" if self._configured_agent_exists(configured_name) else
                "default"
            )
            if session is not None:
                session._turn_agent_definition = agent_definition
        if explicit_session_model:
            assembler.agent_definition = None
            assembler._requested_agent_effort = None
            assembler._agent_effort_supported = False
        return assembler

    def assemble(self, session: Any) -> Any:
        for_turn = getattr(self._context, "for_turn", None)
        if callable(for_turn):
            return self.for_turn(session=session).assemble(session)
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
        model_selection: ModelSelection | None = None,
        agent_definition: Any | None = None,
        reasoning_effort: str | None = None,
        selected_agent_effort: str | None = None,
    ) -> Any:
        """Build one iteration's assembler from a pinned manifest generation.

        Resolves capabilities and the request counter from the manifest config
        (so a model/sampling reload takes effect next iteration) and hands the
        context manager the frozen config, system-file snapshot, and skills index
        so it never rereads a live file. Resolution is strict: a config naming a
        provider the router cannot resolve raises, and the loop turns that into a
        visible failed turn rather than silently falling back.

        ``model_selection`` is the session override **frozen at turn start** by
        the caller; it is never read live here, so a selection made mid-turn
        cannot change a running turn's model between iterations.
        """
        manager = self._context
        for_iteration = getattr(manager, "for_iteration", None)
        if not callable(for_iteration):
            return manager
        provider_name: str | None = None
        model: str | None = None
        capabilities = None
        request_counter = None
        selected_agent_effort = reasoning_effort
        (
            provider_name,
            model,
            configured_provider,
            configured_model,
        ) = self._route_reference(
            config,
            agent_definition=agent_definition,
            model_selection=model_selection,
        )

        if config is not None and self._resolver is not None and (
            provider_name is not None or model is not None
        ):
            resolve = getattr(self._resolver, "resolve", None)
            if resolve is not None:
                routed_provider_name = provider_name
                request = ModelRequest(messages=[], provider=provider_name, model=model)
                try:
                    resolved = resolve(request)
                except Exception:
                    if (provider_name, model) == (
                        configured_provider, configured_model
                    ):
                        raise
                    # A selected session route falls back to config (never to
                    # the selected agent); a stale agent default falls back to
                    # config as well. Precedence itself remains centralized.
                    provider_name, model = configured_provider, configured_model
                    routed_provider_name = provider_name
                    request = ModelRequest(
                        messages=[], provider=provider_name, model=model
                    )
                    resolved = resolve(request)
                capabilities = resolved.capabilities
                provider_name = resolved.provider.name
                model = resolved.model
                request_counter = RequestTokenCounter(
                    resolved.provider,
                    cache=self._token_cache,
                    provider_name=provider_name,
                )
                supported = self._supported_efforts(
                    resolved,
                    self._registry,
                    provider_name=routed_provider_name,
                )
                selected_agent_effort = (
                    reasoning_effort if reasoning_effort in supported else None
                )
        assembler = for_iteration(
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
            agent_definition=agent_definition,
            reasoning_effort=selected_agent_effort,
        )
        provider_default = getattr(agent_definition, "provider", None)
        if (
            provider_default
            and getattr(agent_definition, "model", None)
            and "/" not in agent_definition.model
            and provider_name == "openai"
            and assembler._env is not None
        ):
            from dataclasses import replace

            assembler._env = replace(assembler._env, provider=provider_default)
        return assembler


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
        model_selection: ModelSelection | None = None,
        agent_definition: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self._session = session
        self._turn_id = turn_id
        #: The session's model override, frozen at turn start (``None`` for the
        #: configured default). Reused for every iteration and never re-read, so
        #: a selection made mid-turn cannot change the running turn.
        self._model_selection = model_selection
        #: Effort is frozen with the root assembler and reused by manifest
        #: iterations even if the durable session record changes mid-turn.
        self._reasoning_effort = getattr(session, "_turn_reasoning_effort", None)
        if model_selection is not None and getattr(
            getattr(session, "reasoning_effort_selection", None), "effort", None
        ) is None:
            self._reasoning_effort = None
        self._agent_definition = agent_definition
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
            model_selection=self._model_selection,
            agent_definition=self._agent_definition,
            reasoning_effort=getattr(self, "_reasoning_effort", None),
        )
        if self._agent_definition is not None:
            assembler._agent_effort_supported = getattr(
                assembler, "_reasoning_effort", None
            ) is not None
            assembler.agent_definition = self._agent_definition
        activation = self._activation_for(session)
        skill_tools = self._skill_tools_for(manifest, activation)
        base_catalog = (
            tuple(manifest.tools.values()) + skill_tools
            if isinstance(getattr(manifest, "tools", None), Mapping)
            else skill_tools
        )
        base_catalog = runtime._filter_web_catalog(config, base_catalog)
        restrict = self._restrict_for(activation, skill_tools)
        runner = self._subagent_runner(
            runtime,
            config,
            base_catalog,
            session_id,
            restrict,
            hooks_service,
            agent_definition=self._agent_definition,
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
        if self._agent_definition is not None:
            from .tools.bundles import BUNDLES, profile_tools

            manager_names = manager.names
            profile = (
                profile_tools(self._agent_definition.profile)
                if self._agent_definition.profile
                else manager_names
            )
            selected = runtime._agents.select_tools(
                self._agent_definition,
                available=manager_names,
                profile=profile,
                bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                mutating=tuple(
                    spec.name
                    for spec in manager.specs
                    if getattr(spec, "mutates", False)
                ),
            )
            manager = runtime._build_iteration_manager(
                config,
                manifest,
                restrict=tuple(name for name in manager_names if name in selected.selected),
                catalog=catalog,
                path_guard=self._path_guard,
            )
        if self._gate is not None:
            self._gate.bind_manager(manager)
        manager._agent_selection_source = getattr(
            self._session, "_turn_agent_selection_source", "default"
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
            outbound_http=runtime._outbound_http_service,
            local_search_http=getattr(runtime, "_local_search_http_service", None),
            questions=(
                runtime._question_service(session_id)
                if hasattr(runtime, "_question_service")
                else None
            ),
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
        agent_definition: Any | None = None,
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
        if agent_definition is not None:
            from .tools.bundles import BUNDLES, profile_tools

            root_profile = (
                profile_tools(agent_definition.profile)
                if agent_definition.profile
                else provisional.names
            )
            root_selection = runtime._agents.select_tools(
                agent_definition,
                available=provisional.names,
                profile=root_profile,
                bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                mutating=tuple(
                    spec.name
                    for spec in provisional.specs
                    if getattr(spec, "mutates", False)
                ),
            )
            parent_tools = [
                name for name in parent_tools if name in root_selection.selected
            ]
        authority = _ChildAuthority(
            engine=self._gate.engine, path_guard=self._path_guard
        )
        # The session's selected tier is the parent tier, so a child can never
        # exceed the model the session is actually running (section 15.8).
        parent_tier = (
            self._model_selection.tier if self._model_selection is not None else None
        )
        parent_provider = (
            self._model_selection.provider
            if self._model_selection is not None
            else None
        )
        parent_model = (
            self._model_selection.model
            if self._model_selection is not None
            else None
        )
        if parent_tier is None:
            if self._model_selection is None:
                parent_provider, parent_model = runtime._assembler._context.model_reference(config)
            if self._agent_definition is not None:
                agent_model = getattr(self._agent_definition, "model", None)
                agent_provider = getattr(self._agent_definition, "provider", None)
                if self._model_selection is None and agent_model == "inherit":
                    agent_model = None
                if (
                    self._model_selection is None
                    and agent_model
                    and "/" not in agent_model
                    and agent_provider
                ):
                    agent_model = f"{agent_provider}/{agent_model}"
                if self._model_selection is None and agent_model:
                    parent_model = agent_model
                    parent_provider = None
                elif self._model_selection is None and agent_provider:
                    parent_provider = agent_provider
            if parent_provider and parent_model and "/" not in parent_model:
                parent_model = f"{parent_provider}/{parent_model}"
                parent_provider = None
            reference = (
                parent_model
                if parent_model
                else f"{parent_provider}/{parent_model}"
                if parent_provider and parent_model
                else None
            )
            if reference:
                info = runtime._registry.get(reference) if runtime._registry else None
                parent_tier = runtime._tiers.resolve(reference, info=info).tier
        effective_parent_model = parent_model
        if parent_provider and parent_model and "/" not in parent_model:
            effective_parent_model = f"{parent_provider}/{parent_model}"
        return runtime._make_subagent_runner(
            session_id=session_id,
            parent_tools=parent_tools,
            parent_tier=parent_tier,
            parent_model=effective_parent_model,
            agent_definition=self._agent_definition,
            root_turn_id=self._turn_id,
            permissions=authority,
            grants=tuple(getattr(self._gate, "_grants", ())),
            config=config,
            catalog=base_catalog,
            hooks=hooks,
            budget=self._budget,
            runtime_supports_workspace=runtime._supports_child_workspace(),
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

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._allocation_lock = threading.Lock()
        self._allocated: dict[str, int] = {}

    @property
    def manager(self) -> SessionManager:
        return self._manager

    def child_id(self, parent_id: str, index: int) -> str:
        return f"{parent_id}/sub/{int(index)}"

    def allocate_child_id(self, parent_id: str) -> tuple[int, str]:
        # Tree-local, parent-session scoped monotonic allocation prevents
        # concurrent Task calls in separate iterations/runners colliding.
        with self._allocation_lock:
            index = self._allocated.get(parent_id, 0) + 1
            while self._manager.store.exists(
                _child_session_id(self.child_id(parent_id, index))
            ):
                index += 1
            self._allocated[parent_id] = index
            return index, self.child_id(parent_id, index)

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
        "input.started",
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
        if event.type == "todo.updated":
            self._session.append_event(event)
        else:
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
        workspace = Path(getattr(spec, "workspace", runtime.workspace)).resolve()
        facade = runtime._ensure_child_sessions()
        real_id = _child_session_id(spec.session_id)
        session = facade.manager.open(real_id, create=True, recover=False)
        try:
            runtime._restore_todos(session)
            config = (
                spec.config
                if isinstance(getattr(spec, "config", None), Config)
                else runtime._child_config(spec)
            )
            path_guard_builder = getattr(runtime, "_child_path_guard", None)
            real_path_scope = (
                callable(path_guard_builder)
                and hasattr(runtime, "_child_workspace_config")
            )
            if real_path_scope:
                path_guard = path_guard_builder(spec, workspace)
                config = runtime._child_workspace_config(config, path_guard)
            else:  # lightweight runtime fakes use their prebuilt manager guard
                path_guard = None
            assembler_builder = runtime._build_child_assembler
            if real_path_scope:
                assembler = assembler_builder(spec, config, workspace=workspace)
                manager = runtime._build_child_tool_manager(
                    spec, config, self._runner, workspace=workspace,
                    path_guard=path_guard,
                )
            else:
                assembler = assembler_builder(spec, config)
                manager = runtime._build_child_tool_manager(
                    spec, config, self._runner
                )
            freeze_tools = getattr(assembler, "freeze_tools", None)
            if callable(freeze_tools):
                freeze_tools(tuple(manager.schemas()))
            engine_builder = getattr(runtime, "_child_permission_engine", None)
            engine = (
                engine_builder(spec, config, workspace=workspace, path_guard=path_guard)
                if real_path_scope and callable(engine_builder)
                else runtime._child_permission_engine(spec, config)
            )
            gate = _PermissionGateAdapter(
                engine, manager=manager, grants=tuple(spec.grants), attended=False
            )
            dispatcher = _ToolDispatcherAdapter(
                manager,
                workspace=workspace,
                session_id=real_id,
                turn_id="",
                config=config,
                agent_id=spec.agent_id,
                subagents=self._runner,
                outbound_http=getattr(runtime, "_outbound_http_service", None),
                local_search_http=getattr(runtime, "_local_search_http_service", None),
                questions=(
                    runtime._question_service(spec.session_id)
                    if hasattr(runtime, "_question_service")
                    else None
                ),
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
                    relay_transcript=True,
                )
            finally:
                with contextlib.suppress(Exception):
                    lease.release()
            cost = runtime._child_cost(session, outcome)
            return runtime._child_outcome(spec, session, outcome, cost=cost)
        finally:
            store = runtime._effective_todo_store()
            clear_session = getattr(store, "clear_session", None)
            if callable(clear_session):
                clear_session(real_id)

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
        outbound_http_service: OutboundHTTPService | None = None,
        owns_outbound_http_service: bool | None = None,
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
        codex_auth_factory: Callable[..., Any] | None = None,
        copilot_auth_factory: Callable[..., Any] | None = None,
        api_key_auth_factory: Callable[..., Any] | None = None,
        voice_engine_factory: Callable[..., Any] | None = None,
        voice_store: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._home = Path(home) if home is not None else None
        self._environ = environ
        self._config = config
        self._config_loader = config_loader
        self._http_transport = http_transport
        self._client = client
        #: One HTTP client owned by the runtime and shared by every adapter it
        #: constructs; ``None`` until the first provider build that needs it.
        self._shared_client: Any | None = None
        self._owns_shared_client = False
        self._mcp_client_factory = mcp_client_factory
        self._closed = False
        self._codex_auth_factory = codex_auth_factory
        #: Keychain-backed sign-in seams (tests inject in-memory stores).
        self._copilot_auth_factory = copilot_auth_factory
        self._api_key_auth_factory = api_key_auth_factory

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
        #: One broker for every root session's operator questions. Answers are
        #: routed by (root session, question id), never by turn or agent alone.
        self._questions = QuestionBroker(timeout_s=ANSWER_WINDOW_S)
        self._owns_job_registry = False
        #: Explicit ownership. An injected manager is only closed when the
        #: caller says so; a manager the runtime builds is always owned. Keep
        #: managers with private stores strongly until shutdown; snapshots that
        #: borrow shared runtime stores are tracked weakly so iteration rebuilds
        #: do not accumulate for the runtime's lifetime.
        self._owns_tools = (tools is None) if owns_tools is None else bool(owns_tools)
        self._owned_tools: list[ToolManager] = []
        self._tracked_tools: weakref.WeakSet[ToolManager] = weakref.WeakSet()
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
            # One adapter may be registered under two keys (Gemini is ``google``
            # and ``gemini``); close each owned provider once.
            self._owned_providers = []
            for provider in self._providers.values():
                if not any(provider is owned for owned in self._owned_providers):
                    self._owned_providers.append(provider)

        if initial is None:
            initial = self._load_config()

        from .config.schema import VoiceSection
        from .voice.manager import VoiceManager
        from .voice.store import ModelStore
        voice_config = initial.v2.voice if initial.v2 else VoiceSection()
        voice_environ = os.environ if environ is None else environ
        if voice_environ.get("NEXUS_VOICE", "").strip().lower() == "off":
            voice_config = msgspec.structs.replace(voice_config, enabled=False)
        if voice_engine_factory is None:
            from .voice.engine import KestrelEngine

            def voice_engine_factory(path):
                return KestrelEngine(path, device=self.voice.config.device)

            voice_engine_factory.available = KestrelEngine.available
        self.voice = VoiceManager(
            voice_config, engine_factory=voice_engine_factory,
            store=voice_store if voice_store is not None else ModelStore(nexus_home(self._home), revision=voice_config.revision),
        )

        self._outbound_http_service = (
            outbound_http_service
            if outbound_http_service is not None
            else SafeOutboundHTTPService()
        )
        self._owns_outbound_http_service = (
            outbound_http_service is None
            if owns_outbound_http_service is None
            else bool(owns_outbound_http_service)
        )
        self._local_search_http_service = LocalSearchHTTPService()

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
        #: Only routes built here from config can be rebuilt by ``reload_model_routes``.
        self._owns_routes = providers is None and router is None and registry is None and tiers is None
        self._context = (
            context
            if context is not None
            else ContextManager(self.workspace, config_loader=self._load_config)
        )
        #: Request-aware token-count cache rooted in the workspace (best-effort;
        #: a missing/unwritable cache is a silent miss, never a turn failure).
        self._token_cache = TokenCountCache(
            project_state_dir(self.workspace, self._home) / "cache" / "tokens"
        )
        self._assembler = _ContextCoordinator(
            self._context,
            self._router,
            token_cache=self._token_cache,
            tiers=self._tiers,
            registry=self._registry,
            runtime=self,
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
        self._assembler._agents = self._agents
        self._hooks = self._build_hook_manager(initial, hooks, owns_hooks)
        self._child_sessions: _ChildSessionFacade | None = None
        from .agents.worktrees import WorktreeService

        # One canonical registry per active parent checkout. Child runtimes
        # borrow this service/root even when their workspace is itself a
        # worktree, so nested spawns never create an arbitrary nested registry.
        self._worktree_service = WorktreeService(
            runtime_ownership=self._worktree_runtime_ownership
        )
        self._worktree_root = self.workspace.parent / f".nexus-worktrees-{self.workspace.name}"
        self._worktree_roots: dict[Path, Path] = {self.workspace: self._worktree_root}

        if extensions is not None:
            self._extensions: ExtensionManager | None = extensions
            self._owns_extensions = (
                False if owns_extensions is None else bool(owns_extensions)
            )
        else:
            from .ext.manager import ExtensionManager
            from .tools.builtin import BUILTIN_TOOLS, META_TOOLS_OPT_IN, OPT_IN_TOOLS

            self._extensions = ExtensionManager(
                self.workspace,
                home=self._home,
                config=initial,
                config_loader=self._load_config,
                builtin_tools=BUILTIN_TOOLS + OPT_IN_TOOLS + META_TOOLS_OPT_IN,
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

        #: The shared state database (STATE_PLAN §4), or ``None`` in the
        #: ``session_dir=``/``sessions=`` test-compatibility paths that never
        #: touch it. Reused by :meth:`_ensure_child_sessions` so the parent and
        #: its child (``namespace="agents"``) sessions share one connection pool.
        self._state_db: StateDatabase | None = None
        if sessions is not None:
            self._sessions = sessions
            self._owns_sessions = False
        elif session_dir is not None:
            # Test-compatibility path: an explicit directory keeps opening a
            # private, per-directory database (SessionManager's own fallback).
            self._sessions = SessionManager(
                Path(session_dir),
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
        else:
            self._state_db = StateDatabase(state_db_path(self._home))
            self._sessions = SessionManager(
                db=self._state_db,
                project=self.workspace,
                namespace="main",
                lock_dir=nexus_home(self._home) / "locks" / "sessions" / project_key(self.workspace),
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

    def root_reasoning_effort_metadata(self, session: Any) -> dict[str, Any]:
        """Resolve current root-session effort choices for a facade.

        The returned mapping contains ``supported_levels`` (a tuple),
        ``stored_override`` (including dormant unsupported values),
        ``effective_effort``, and ``source`` (``"session"``, ``"agent"``, or
        ``None``). This is descriptive only; persistence remains a session API.
        """
        return self._assembler.root_reasoning_effort_metadata(session)

    def root_route_metadata(self, session: Any) -> dict[str, str | None]:
        """Resolve the provider/model effective for the session's next root turn."""
        return self._assembler.root_route_metadata(session)

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
        """The runtime-owned todo store (keyed by session and agent id)."""
        return self._todo_store

    @property
    def questions(self) -> QuestionBroker:
        """The operator-question broker the host answers through."""
        return self._questions

    def _question_service(self, session_id: str) -> _QuestionService:
        """Bind questions to the root of ``session_id`` (``<root>/sub/<n>...``)."""
        root_id = str(session_id).split("/sub/", 1)[0]
        live = getattr(self._sessions, "_live_handle", None)
        root = live(root_id) if callable(live) else None
        return _QuestionService(self._questions, root_id, root)

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
            except ConfigError:
                # A catalogue lookup can validate configured model metadata.
                # Keep an invalid explicit override visible instead of treating
                # it like an optional catalogue acquisition failure.
                raise
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

    async def list_tools(self) -> list[dict[str, Any]]:
        """The model-facing tool catalog for the current config and manifest.

        Read-only: it never opens a session, starts a turn, or mutates the
        manifest. It refreshes the manifest once (the same cheap, serialized
        rebuild a turn boundary does) so a just-added external tool is visible,
        then reports each selected tool's name, description, bundle, whether it
        mutates, and its JSON input schema. Descriptions are advisory data, never
        authority.
        """
        await self.ensure_started()
        config = self._load_config()
        if self._tools is not None:
            manager = self._tools
        elif self.extensions is not None:
            manager = self._build_iteration_manager(config, self.manifest)
        else:
            manager = ToolManager(
                config,
                workspace=self.workspace,
                tools=self._filter_web_catalog(
                    config, ToolManager._builtin_catalog()
                ),
                job_registry=self._job_registry,
                todo_store=self._effective_todo_store(),
            )
            self._track_tool_manager(manager)
        specs = {spec.name: spec for spec in manager.specs}
        rows: list[dict[str, Any]] = []
        for schema in manager.schemas():
            spec = specs.get(schema.name)
            rows.append(
                {
                    "name": schema.name,
                    "description": schema.description,
                    "bundle": str(getattr(spec, "bundle", "") or ""),
                    "mutates": bool(getattr(spec, "mutates", False)),
                    "input_schema": dict(schema.input_schema),
                }
            )
        web_available, unavailable = self._web_tool_availability(config)
        from .tools.bundles import profile_tools

        selected_profile = (
            frozenset(manager.names)
            if self._tools is not None and manager is self._tools
            else profile_tools(manager.profile)
        )
        web_names = {"webfetch", "websearch"}
        rows = [
            row
            for row in rows
            if row["name"] not in web_names or row["name"] in web_available
        ]
        for name, reason in unavailable.items():
            if name in selected_profile:
                rows.append(
                    {
                        "name": name,
                        "description": "",
                        "bundle": "web",
                        "mutates": False,
                        "availability": "unavailable",
                        "reason": reason,
                    }
                )
        return rows

    async def inspect_context(self, session: Any) -> dict[str, Any]:
        """Assemble next-turn context from persisted conversation history.

        One manifest lease pins system files, skills/MCP indexes, tools, and
        schemas to the same generation used for assembly. The empty session view
        intentionally omits draft input. This inspection does not run
        prompt-submit/compaction hooks, persist summaries, or call a provider.
        """
        await self.ensure_started()
        if getattr(session, "active", False):
            raise ConfigError("context preview is unavailable while the session is active")
        class _PreviewSession:
            """Read-only subset of Session used during local preparation."""

            id = getattr(session, "id", "")
            messages = tuple(getattr(session, "messages", ()))
            model_selection = getattr(session, "model_selection", None)
            agent_selection = getattr(session, "agent_selection", None)
            reasoning_effort_selection = getattr(
                session, "reasoning_effort_selection", None
            )
            attended = bool(getattr(session, "attended", False))
            # Only the durable selection fields are relevant to standing
            # context. Do not replay real-session events into preview setup.
            events: tuple[Any, ...] = ()

            def latest_summary(self):
                getter = getattr(session, "latest_summary", None)
                return getter() if callable(getter) else None

            def summary_for(self, input_digest: str, strategy: str):
                getter = getattr(session, "summary_for", None)
                return getter(input_digest, strategy) if callable(getter) else None

            def message_seqs(self):
                getter = getattr(session, "message_seqs", None)
                return getter() if callable(getter) else ()

        preview_session = _PreviewSession()
        assembler = self._assembler.for_turn(session=preview_session)
        tool_turn = self._make_tool_turn(
            config=assembler.effective_config(),
            session=preview_session,
            turn_id="context-preview",
            attended=preview_session.attended,
        )
        lease = None
        try:
            if tool_turn is not None and tool_turn.manifest_ref is not None:
                lease = tool_turn.manifest_ref.pin()
                if tool_turn.environment_for is not None:
                    from .tools.builtin.skill import _turn_number

                    iteration = tool_turn.environment_for.for_iteration(
                        preview_session,
                        lease,
                        _turn_number("context-preview"),
                    )
                    assembler = iteration.assembler
                    # A preview must not invoke lifecycle hooks or their
                    # PreCompact callbacks while computing its budget view.
                    if hasattr(assembler, "_pre_compact"):
                        assembler._pre_compact = None
            elif tool_turn is not None:
                freeze = getattr(assembler, "freeze_tools", None)
                if callable(freeze):
                    freeze(tuple(tool_turn.schemas))

            # Exact request counting may call a provider's count API. Inspection
            # is local-only, so retain estimates and skip exact-count refinement.
            frozen_env = getattr(assembler, "_env", None)
            if frozen_env is not None and hasattr(frozen_env, "request_counter"):
                assembler._env = replace(
                    frozen_env,
                    counter=None,
                    counter_async=False,
                    request_counter=None,
                    summarizer=None,
                    summarizer_async=False,
                )

            request = assembler.assemble(preview_session)
            if inspect.isawaitable(request):
                request = await request
            parts = getattr(assembler, "last_included_parts", {})
            budget = getattr(assembler, "last_budget", {})
            budget_parts = {
                row.get("name"): row
                for row in budget.get("parts", ())
                if isinstance(row, Mapping)
            }
            tools_supported = bool(
                getattr(getattr(frozen_env, "capabilities", None), "tools", True)
            )
            generation = getattr(lease, "generation", None)
            manifest = getattr(lease, "manifest", None)
            agent = getattr(
                assembler,
                "_context_agent_definition",
                getattr(assembler, "agent_definition", None),
            )
            system_files: dict[str, Any] = {}
            frozen_files = getattr(manifest, "system_files", None)
            getter = getattr(frozen_files, "get", None)
            context_config = getattr(frozen_env, "config", None)
            for key, attribute in (("soul", "instructions_file"), ("memory", "memory_file"), ("agents", "agents_file")):
                filename = getattr(context_config, attribute, None)
                entry = getter(key) if callable(getter) else None
                loaded = entry is not None
                if manifest is None and filename:
                    from .config.paths import resolve_within

                    loaded = resolve_within(self.workspace, filename).is_file()
                system_files[key] = {
                    "configured": bool(filename),
                    "loaded": loaded,
                    "included": key in parts,
                    "included_nonempty": key in parts and bool(parts.get(key, "").strip()),
                    "truncated": bool(budget_parts.get(key, {}).get("truncated", False)),
                    "source": (
                        Path(entry.path).name
                        if entry is not None and getattr(entry, "path", None)
                        else Path(filename).name if filename else None
                    ),
                }
            agent_info = {
                "name": getattr(agent, "name", None),
                "source": getattr(assembler, "agent_selection_source", "default"),
                "instructions_included": bool(
                    agent is not None
                    and "soul" in parts
                ),
            }
            skills_snapshot = (
                tuple(getattr(manifest, "skills", {}).values())
                if isinstance(getattr(manifest, "skills", None), Mapping)
                else ()
            )
            tool_specs = {
                spec.name: spec for spec in tool_turn.manager.specs
            } if tool_turn is not None else {}
            mcp_servers = []
            if self._mcp is not None:
                for status in self._mcp.statuses()[:64]:
                    snapshot = self._mcp.server_snapshot(status.name)
                    names = list(snapshot.tool_names())[:256] if snapshot else []
                    mcp_servers.append({
                        "name": status.name,
                        "status": "connected" if status.connected else "disabled" if not status.enabled else "failed",
                        "tool_count": status.tool_count,
                        "tools": names,
                    })
            return {
                "manifest_generation": generation,
                "agent": agent_info,
                "system_files": system_files,
                "system_text": redact_secrets((request.system or "")[:1_000_000]) or None,
                "redacted_for_display": True,
                "included_parts": [
                    {"name": name, "text": redact_secrets(text[:1_000_000])}
                    for name, text in list(parts.items())[:32]
                ],
                "skills_index": [
                    {
                        "name": getattr(entry, "name", ""),
                        "description": (
                            entry.sanitized_description()
                            if callable(getattr(entry, "sanitized_description", None))
                            else getattr(entry, "description", "")
                        ),
                        "included": bool(
                            any(
                                line.startswith(f"{getattr(entry, 'name', '')}:")
                                for line in parts.get("skills_index", "").splitlines()
                            )
                        ),
                        "scope": str(getattr(getattr(entry, "provenance", None), "tier", "")),
                        "origin": str(getattr(getattr(entry, "provenance", None), "relpath", "")),
                    }
                    for entry in skills_snapshot[:512]
                ],
                "mcp_index": parts.get("mcp_index", ""),
                "mcp_servers": mcp_servers,
                "tools": [
                    {
                        "name": schema.name,
                        "description": schema.description,
                        "input_schema": dict(schema.input_schema),
                        "group": (
                            f"mcp:{schema.name.split('__', 2)[1]}" if schema.name.startswith("mcp__")
                            else getattr(tool_specs.get(schema.name), "group", "") or schema.name
                        ),
                        "bundle": getattr(tool_specs.get(schema.name), "bundle", ""),
                    }
                    for schema in request.tools[:512]
                ] if tools_supported else [],
                "tools_supported": tools_supported,
                "model": request.model,
                "provider": request.provider,
                "budget": dict(budget),
                "messages": [
                    {
                        "role": message.role,
                        "blocks": [
                            self._context_block(block)
                            for block in message.content[:256]
                        ],
                    }
                    for message in request.messages[-256:]
                ],
                "history_included": bool(request.messages),
                "request_context": dict(request.metadata.get("context", {})),
                "params": {
                    "temperature": request.params.temperature,
                    "max_output_tokens": request.params.max_output_tokens,
                    "thinking_budget": request.params.thinking_budget,
                    "reasoning_effort": request.params.reasoning_effort,
                },
                "omitted": [
                    "draft input (not provided)",
                    *(
                        [f"{len(request.messages) - 256} earlier request messages omitted from display"]
                        if len(request.messages) > 256
                        else []
                    ),
                    "provider-specific request transformation and send-time changes",
                    "provider token counting and send-time exact-count refinement",
                    "new durable summaries (read-only inspection drops history that cannot be reused from an existing summary)",
                    "display bounds: at most 256 messages; text/schema strings are clipped at 16,384 characters",
                ],
            }
        finally:
            if lease is not None:
                lease.release()

    @staticmethod
    def _context_block(block: Any) -> dict[str, Any]:
        """Project one provider-request content block without binary payloads."""
        if isinstance(block, Text):
            return {"type": "text", "text": block.text}
        if isinstance(block, Thinking):
            return {"type": "thinking", "text": block.text}
        if isinstance(block, ToolUse):
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        if isinstance(block, ToolResult):
            return {
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "is_error": block.is_error,
                "content": [
                    {"type": "text", "text": item.text}
                    if isinstance(item, Text)
                    else {"type": "image", "text": "[image omitted]"}
                    for item in block.content
                ],
            }
        if isinstance(block, Image):
            return {"type": "image", "media_type": block.media_type, "text": "[image omitted]"}
        if isinstance(block, Document):
            return {
                "type": "document",
                "media_type": block.media_type,
                "title": block.title or "",
                "text": "[document payload omitted]",
            }
        return {"type": type(block).__name__}

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
        session = self._sessions.open(session_id, create=create, recover=recover)
        self._restore_todos(session)
        return session

    def _restore_todos(self, session: Any) -> None:
        """Replay durable todo state for one actual session log, if available."""
        store = self._effective_todo_store()
        replay = getattr(store, "replay", None)
        if callable(replay):
            replay(session)

    def _effective_todo_store(self) -> Any | None:
        """Return the shared store, borrowing it from injected tools if needed.

        Borrowing the store does not transfer ownership of the injected manager
        or its services to this runtime or to managers rebuilt for a turn.
        """
        if self._todo_store is not None:
            return self._todo_store
        if self._tools is not None:
            return getattr(self._tools, "todo_store", None)
        return None

    # -- per-session model selection ---------------------------------------

    def select_session_model(
        self, session_id: str, reference: str, *, create: bool = True
    ) -> ModelSelection:
        """Validate ``reference`` and persist it as this session's model.

        The reference is a tier name, ``"provider/model"``, or a bare id, and is
        resolved through the same router the turn loop uses, so a selection can
        only name a provider/model the harness can actually stream. The durable
        ``model.selected`` event makes the choice survive reopen and replay. A
        selection never touches a turn already in flight (the turn's model was
        frozen at turn start); it applies to the next turn.
        """
        selection = self._validate_model_selection(reference)
        handle = self._sessions.open(session_id, create=create, recover=True)
        handle.select_model(selection)
        return selection

    def select_session_agent(
        self, session_id: str, name: str | None, *, create: bool = True
    ) -> tuple[str, str]:
        """Validate and persist a root-agent selection for its next turn."""
        if name is not None:
            if self._agents is None:
                raise ConfigError("agent definitions are disabled")
            self._agents.refresh()
            definition = self._agents.resolve(name, context="root")
        else:
            definition = None
        handle = self._sessions.open(session_id, create=create, recover=True)
        if name is None:
            handle.reset_agent()
            return self.effective_session_agent(handle)
        handle.select_agent(AgentSelection(name=definition.name))
        return definition.name, "session"

    def effective_session_agent(self, handle: Session) -> tuple[str, str]:
        selection = getattr(handle, "agent_selection", None)
        if selection is not None:
            return selection.name or "build", "session"
        return self.default_root_agent(), "config"

    def default_root_agent(self) -> str:
        """The root agent a session without a selection runs: ``[agent] name``.

        New sessions start with it (``build`` unless configured otherwise).
        Read from disk on each call, so a Settings change applies at once.
        """
        config = self._load_config()
        name = getattr(getattr(getattr(config, "v2", None), "agent", None), "name", "build")
        if self._agents is not None:
            try:
                name = self._agents.resolve(name, context="root").name
            except Exception:  # noqa: BLE001, S110 - an unresolvable name is still reported
                pass
        return str(name or "build")

    def _validate_model_selection(self, reference: str) -> ModelSelection:
        """Resolve and validate one model/tier reference, or raise ``ConfigError``.

        Tiers need a registry to name a concrete model; without ``[models]`` a
        tier name is refused rather than silently reinterpreted as a model id.
        Resolution is otherwise delegated to the router so the selection obeys
        exactly the same provider, alias, and tier rules as a configured default.
        """
        if not isinstance(reference, str) or not reference.strip():
            raise ConfigError("model reference must be a nonempty string")
        ref = reference.strip()
        if ref in self._tiers.order and self._registry is None:
            raise ConfigError(
                f"tier {ref!r} cannot be selected without a model registry; "
                "configure [models] with a catalogue or use a provider/model id"
            )
        info = (
            self._registry.get(ref) if self._registry is not None else None
        )
        # The router is authoritative for what is runnable: it raises for an
        # unknown provider, a malformed reference, or a tier with no runnable
        # model.
        resolved = self._router.resolve(ModelRequest(messages=[], model=ref))
        resolution = self._tiers.resolve(ref, info=info)
        return ModelSelection(
            reference=ref,
            provider=resolved.provider.name,
            model=resolved.model,
            tier=resolution.tier,
            tier_source=resolution.source,
            requested_tier=ref if ref in self._tiers.order else "",
            clamped=bool(resolution.clamped),
        )

    def candidate_supported_efforts(
        self, provider: str, model: str
    ) -> tuple[str, ...]:
        """Return efforts supported by this candidate's effective runtime route.

        This is descriptive only: it resolves the same concrete provider/model
        that model selection would validate, then applies the exact adapter,
        capability, and registry checks used by root-session effort metadata.
        An unresolvable candidate or route that cannot apply effort has no
        choices. It never selects a model or changes session effort state.
        """
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
        ):
            return ()
        try:
            selection = self._validate_model_selection(f"{provider}/{model}")
            # Custom tiers/aliases can shadow a catalogue coordinate. Do not
            # advertise choices for a different model that selecting this row
            # would actually resolve to.
            if (selection.provider, selection.model) != (provider, model):
                return ()
            resolved = self._router.resolve(
                ModelRequest(
                    messages=[],
                    provider=selection.provider,
                    model=selection.model,
                )
            )
        except Exception:  # noqa: BLE001 - candidate metadata must not block listing
            return ()
        return self._assembler._supported_efforts(
            resolved,
            self._registry,
            provider_name=selection.provider,
        )

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

    def auto_archive_days(self) -> int:
        """Return the validated archive inactivity threshold from config."""
        config = self._load_config()
        v2 = getattr(config, "v2", None)
        section = getattr(v2, "sessions", None)
        value = getattr(section, "auto_archive_days", 2)
        return value if type(value) is int and 0 <= value <= 3650 else 14

    def update_check_enabled(self) -> bool:
        """Whether ``[updates] check`` allows the daily release lookup."""
        section = getattr(getattr(self._load_config(), "v2", None), "updates", None)
        return getattr(section, "check", True) is not False

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
        # Runtime.sessions is public, so callers can bypass Runtime.session().
        # Reconcile again at the actual turn boundary; TodoStore.replay only
        # applies log revisions newer than memory and is therefore safe during
        # an active runtime.
        self._restore_todos(session)
        if self._tool_factory is not None:
            return self._tool_factory(
                config=config,
                session=session,
                turn_id=turn_id,
                attended=attended,
            )
        if config is None:
            config = self._load_config()
        # Freeze the session's model override here, at turn start, and hand it to
        # both environment paths. The running turn therefore cannot observe a
        # selection made after this point.
        model_selection = getattr(session, "model_selection", None)
        agent_definition = getattr(session, "_turn_agent_definition", None)
        parent_tier = (
            model_selection.tier if model_selection is not None else None
        )
        parent_provider = (
            model_selection.provider if model_selection is not None else None
        )
        parent_model = model_selection.model if model_selection is not None else None
        if parent_tier is None:
            if agent_definition is not None:
                parent_model = getattr(agent_definition, "model", None)
                parent_provider = getattr(agent_definition, "provider", None)
                if parent_model == "inherit":
                    parent_model = None
                if parent_model and "/" not in parent_model and parent_provider:
                    parent_model = f"{parent_provider}/{parent_model}"
            if not parent_model:
                reference = getattr(self._assembler._context, "model_reference", None)
                if callable(reference):
                    configured_provider, configured_model = reference(config)
                    if configured_model:
                        parent_model = configured_model
                        if agent_definition is None or not agent_definition.provider:
                            parent_provider = configured_provider
                        else:
                            parent_provider = agent_definition.provider
            else:
                parent_provider = None
            if parent_provider and parent_model and "/" not in parent_model:
                parent_model = f"{parent_provider}/{parent_model}"
                parent_provider = None
            if parent_model:
                parent_info = (
                    self._registry.get(parent_model)
                    if self._registry is not None
                    else None
                )
                parent_tier = self._tiers.resolve(
                    parent_model, info=parent_info
                ).tier
        manager = self._tools
        if manager is not None:
            filtered = self._filter_web_catalog(
                config, manager.tools, add_available=False
            )
            if len(filtered) != len(manager.tools):
                manager = ToolManager(
                    config,
                    workspace=self.workspace,
                    tools=filtered,
                    job_registry=self._job_registry,
                    todo_store=self._effective_todo_store(),
                    path_guard=manager.path_guard,
                )
                self._track_tool_manager(manager)
        if manager is None:
            catalog = self._filter_web_catalog(config, ToolManager._builtin_catalog())
            if self._agents is not None:
                from .tools.builtin.task import build_task_tool

                catalog = (
                    tuple(tool for tool in catalog if tool.bundle != "meta")
                    + (build_task_tool(None),)
                )
            manager = ToolManager(
                config,
                workspace=self.workspace,
                tools=catalog,
                job_registry=self._job_registry,
                todo_store=self._effective_todo_store(),
            )
            self._track_tool_manager(manager)
        if (
            agent_definition is not None
            and "subagent" not in manager.names
            and self._agents is not None
        ):
            from .tools.builtin.task import build_task_tool

            original_names = manager.names
            manager = ToolManager(
                config,
                workspace=self.workspace,
                tools=(*manager.tools, build_task_tool(None)),
                restrict=(*original_names, "subagent"),
                job_registry=self._job_registry,
                todo_store=self._effective_todo_store(),
                path_guard=manager.path_guard,
            )
            self._track_tool_manager(manager)
        if agent_definition is not None:
            from .tools.bundles import BUNDLES, profile_tools

            profile = (
                profile_tools(agent_definition.profile)
                if agent_definition.profile
                else manager.names
            )
            selected = self._agents.select_tools(
                agent_definition,
                available=manager.names,
                profile=profile,
                bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                mutating=tuple(
                    spec.name for spec in manager.specs if getattr(spec, "mutates", False)
                ),
            )
            if self._extensions is None:
                manager = ToolManager(
                    config,
                    workspace=self.workspace,
                    tools=manager.tools,
                    restrict=tuple(name for name in manager.names if name in selected.selected),
                    job_registry=self._job_registry,
                    todo_store=self._effective_todo_store(),
                    path_guard=manager.path_guard,
                )
                self._track_tool_manager(manager)
            else:
                manager = self._build_iteration_manager(
                    config,
                    self.manifest,
                    catalog=self._filter_web_catalog(
                        config, manager.tools, add_available=False
                    ),
                    restrict=tuple(name for name in manager.names if name in selected.selected),
                    path_guard=manager.path_guard,
                )
        if agent_definition is not None and getattr(agent_definition, "write_roots", ()):
            settings_guard = self._settings_agent_guard(
                manager.path_guard, agent_definition.write_roots
            )
            manager = self._build_iteration_manager(
                config,
                self.manifest,
                catalog=manager.tools,
                restrict=manager.names,
                path_guard=settings_guard,
            )
        engine = self._permissions
        if engine is None:
            permissions = getattr(getattr(config, "v2", None), "permissions", None)
            if permissions is None:
                engine = PermissionEngine(workspace=self.workspace, home=self._home)
            else:
                engine = PermissionEngine.from_config(
                    permissions, workspace=self.workspace, home=self._home
                )
        if (
            agent_definition is not None
            and getattr(agent_definition, "write_roots", ())
            and getattr(getattr(getattr(config, "v2", None), "settings", None), "confirm_edits", False)
        ):
            engine.require_confirmation_for(
                spec.name for spec in manager.specs if getattr(spec, "mutates", False)
            )
        grants = _grants_from_events(session.events)
        gate = _PermissionGateAdapter(
            engine, manager=manager, grants=grants, attended=attended
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
            static_runner = self._make_subagent_runner(
                session_id=session.id,
                parent_tools=manager.names,
                parent_tier=parent_tier,
                parent_model=(
                    f"{parent_provider}/{parent_model}"
                    if parent_provider and parent_model
                    else parent_model
                ),
                agent_definition=agent_definition,
                root_turn_id=turn_id,
                permissions=authority,
                grants=grants,
                config=config,
                catalog=manager.tools,
                hooks=hooks_service,
                budget=self._new_subagent_budget(config),
                runtime_supports_workspace=self._supports_child_workspace(),
            )
            if static_runner is not None and "subagent" in manager.names:
                from .tools.builtin.task import build_task_tool

                manager = ToolManager(
                    config,
                    workspace=self.workspace,
                    tools=tuple(
                        build_task_tool(static_runner) if tool.name == "subagent" else tool
                        for tool in manager.tools
                    ),
                    restrict=manager.names,
                    job_registry=self._job_registry,
                    todo_store=self._effective_todo_store(),
                    path_guard=manager.path_guard,
                )
                self._track_tool_manager(manager)
            runner = self._make_subagent_runner(
                session_id=session.id,
                parent_tools=manager.names,
                parent_tier=parent_tier,
                parent_model=(
                    f"{parent_provider}/{parent_model}"
                    if parent_provider and parent_model
                    else parent_model
                ),
                agent_definition=agent_definition,
                root_turn_id=turn_id,
                permissions=authority,
                grants=grants,
                config=config,
                catalog=manager.tools,
                hooks=hooks_service,
                budget=self._new_subagent_budget(config),
                runtime_supports_workspace=self._supports_child_workspace(),
            )
        gate.bind_manager(manager)
        # Track the manager actually returned for this turn, after all catalog
        # rebuilds. Intermediate snapshots above share runtime-owned services
        # and own no independent resources. An unchanged injected manager stays
        # caller-owned unless it was explicitly registered as owned at init.
        dispatcher = _ToolDispatcherAdapter(
            manager,
            workspace=self.workspace,
            session_id=session.id,
            turn_id=turn_id,
            config=config,
            agent_id="root",
            skills=self._skills,
            extensions=self._extensions,
            activations=self._activations,
            subagents=runner,
            outbound_http=self._outbound_http_service,
            local_search_http=self._local_search_http_service,
            questions=self._question_service(session.id),
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
                model_selection=model_selection,
                agent_definition=agent_definition,
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

    def _settings_agent_guard(
        self, current: PathGuard, allowed_scopes: Sequence[str]
    ) -> PathGuard:
        """Narrow a Settings agent's write roots without adding authority."""
        home = self._home
        roots = {
            "global": nexus_home(home).resolve(),
            "project": project_agents_dir(self.workspace).resolve(),
        }
        requested = [roots[name] for name in allowed_scopes if name in roots]
        intersections: list[Path] = []
        for scope_root in requested:
            for existing in current.write_roots:
                if scope_root.is_relative_to(existing):
                    intersections.append(scope_root)
                elif existing.is_relative_to(scope_root):
                    intersections.append(existing)
        intersections = list(dict.fromkeys(intersections))
        return PathGuard(
            self.workspace,
            write_roots=[str(root) for root in intersections],
            read_denyroots=[str(root) for root in current.read_denyroots],
            home=home,
            _allow_empty_write_roots=True,
            settings_scopes=[str(root) for root in roots.values()],
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
        Available web tools are runtime-owned capabilities and are added
        independently of the manifest; their config/service availability is
        checked before they can be advertised or executed.
        When a skill is active for this session/turn the caller passes a
        ``catalog`` that also carries that skill's bundled tools, so they are
        selectable *only* for this iteration; ``restrict`` applies the skill's
        declared-tool narrowing. ``path_guard`` is the turn-frozen guard so
        security roots never refresh mid-turn.

        ``None`` means "derive from the manifest" and a genuinely empty
        manifest never falls back to the full builtin catalog. Runtime-owned,
        available web tools may still be added, as above. ``ToolManager`` treats
        ``tools=None`` as "all builtins" and ``tools=()`` as "none", so the
        runtime must never collapse an empty tuple back to ``None``.
        """
        if catalog is None:
            tools = getattr(manifest, "tools", None)
            catalog = tuple(tools.values()) if isinstance(tools, Mapping) else ()
        catalog = tuple(catalog)
        catalog = self._filter_web_catalog(config, catalog)
        manager = ToolManager(
            config,
            workspace=self.workspace,
            tools=catalog,
            restrict=restrict,
            job_registry=self._job_registry,
            todo_store=self._effective_todo_store(),
            path_guard=path_guard or PathGuard(
                self.workspace,
                write_roots=tuple(config.v2.permissions.write_roots) if config.v2 else ("./",),
                read_denyroots=tuple(config.v2.permissions.read_denyroots) if config.v2 else (),
                home=self._home,
            ),
        )
        self._track_tool_manager(manager)
        return manager

    def _web_tool_availability(
        self, config: Config
    ) -> tuple[frozenset[str], dict[str, str]]:
        """Return executable web tool names and host-visible unavailable reasons."""
        from .config.schema import WebSection
        from .tools.builtin import webfetch, websearch

        v2 = getattr(config, "v2", None)
        tools_section = getattr(v2, "tools", None)
        web = getattr(tools_section, "web", None)
        if not isinstance(web, WebSection):
            web = WebSection()
        available: set[str] = set()
        unavailable: dict[str, str] = {}

        if not web.fetch_enabled:
            unavailable[webfetch.SPEC.name] = "Web fetching is disabled by tools.web.fetch_enabled."
        elif self._outbound_http_service is None:
            unavailable[webfetch.SPEC.name] = "Outbound HTTP service is not configured."
        else:
            available.add(webfetch.SPEC.name)

        if not web.searxng_instances and web.local_search_enabled:
            available.add(websearch.SPEC.name)
        elif not web.searxng_instances:
            unavailable[websearch.SPEC.name] = (
                "Local search is disabled and no HTTPS SearXNG instance is configured."
            )
        elif self._outbound_http_service is None:
            unavailable[websearch.SPEC.name] = "Outbound HTTP service is not configured."
        else:
            def origin(value: str) -> tuple[str, str, int] | None:
                try:
                    result = websearch._origin(value)
                except (UnicodeError, ValueError):
                    return None
                if result is None or result[0] != "https":
                    return None
                return result

            allowed = {key for value in web.allowed_origins if (key := origin(value)) is not None}
            instances = {key for value in web.searxng_instances if (key := origin(value)) is not None}
            if not instances:
                unavailable[websearch.SPEC.name] = (
                    "No valid HTTPS SearXNG instance is configured."
                )
            elif not instances.intersection(allowed):
                unavailable[websearch.SPEC.name] = (
                    "No configured SearXNG instance origin is present in tools.web.allowed_origins."
                )
            else:
                available.add(websearch.SPEC.name)
        return frozenset(available), unavailable

    def _filter_web_catalog(
        self,
        config: Config,
        catalog: Sequence[Any],
        *,
        add_available: bool = True,
    ) -> tuple[Any, ...]:
        from .tools.builtin import WEB_TOOLS, webfetch, websearch

        available, _unavailable = self._web_tool_availability(config)
        web_names = {webfetch.SPEC.name, websearch.SPEC.name}
        retained = tuple(
            tool
            for tool in catalog
            if tool.name not in web_names or tool.name in available
        )
        registered = {tool.name for tool in retained}
        additions = (
            tuple(
                tool for tool in WEB_TOOLS
                if tool.name in available and tool.name not in registered
            )
            if add_available
            else ()
        )
        return retained + additions

    def _track_tool_manager(self, manager: ToolManager) -> None:
        """Track manager shutdown without retaining non-owning snapshots.

        ToolManager closes only stores it created itself. Runtime-created
        iteration snapshots are normally passed the runtime's shared stores,
        so the runtime can close any still-live manager weakly while the
        registry/store remain the actual owned resources. A rebuilt manager
        that owns private stores must stay strongly reachable until shutdown.
        """
        if manager is self._tools:
            return
        if manager._owns_job_registry or manager._owns_todo_store:
            if not any(manager is owned for owned in self._owned_tools):
                self._owned_tools.append(manager)
        else:
            self._tracked_tools.add(manager)

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
        provider_aliases = {"codex": "openai"} if (codex := sections.get("codex")) and not self._is_legacy_codex_section(codex) else {}
        provider_aliases.update({name: "anthropic" for name, section in sections.items()
                                 if self._adapter_kind(name, section) == ADAPTER_CLAUDE_AGENT})
        # Not project-specific: one shared catalogue cache under the home root
        # (STATE_PLAN §5.4).
        cache_path = nexus_home(self._home) / "cache" / "models.dev.json"
        return ModelRegistry(
            providers=providers_cfg,
            env=self._environ,
            cache_path=cache_path,
            catalogue_url=models.catalogue_url,
            ttl_days=models.refresh_ttl_days,
            offline=models.offline,
            tier_table=self._tiers,
            provider_aliases=provider_aliases,
            reasoning_effort_overrides=models.reasoning_efforts or None,
        )

    def _provider_transport_kwargs(self) -> dict[str, Any]:
        """Transport injection shared by every adapter the runtime builds.

        One client is owned by the runtime and shared across adapters; providers
        never close it (``owns_transport=False``), so connection reuse is real
        and a close is not duplicated. An injected ``client`` stays the caller's.
        An injected ``http_transport`` (the offline test seam) keeps per-provider
        ownership so each adapter closes its own client, matching the reference
        adapter's existing contract.
        """
        if self._http_transport is not None:
            return {"http_transport": self._http_transport}
        if self._client is not None:
            return {"client": self._client, "owns_transport": False}
        if self._shared_client is None:
            # httpx's own default is a 5 s read timeout, which aborts a
            # reasoning model mid-stream; use the provider transport default.
            self._shared_client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
            self._owns_shared_client = True
        return {"client": self._shared_client, "owns_transport": False}

    @staticmethod
    def _is_legacy_codex_section(section: Any) -> bool:
        """Whether a ``[providers.codex]`` section is the legacy CLI route.

        Legacy v1 config translates into ``[providers.codex]`` with only an
        ``executable``/``timeout_seconds``; that is the Codex subprocess, not an
        OpenAI model provider, so it must not be claimed by the OpenAI adapter.
        """
        if section is None:
            return False
        return bool(getattr(section, "executable", None)) and not getattr(section, "auth", None) and not getattr(
            section, "base_url", None
        )

    @classmethod
    def _adapter_kind(cls, name: str, section: Any) -> str | None:
        """The adapter a configured provider name/``kind`` selects, or ``None``.

        Explicit ``kind`` wins, then the direct-name table, then the plan's
        OpenAI-compatible fallback for any vendor with a configured endpoint
        (section 15.3). A legacy ``codex`` section is never a model provider.
        """
        if name == "codex" and cls._is_legacy_codex_section(section):
            return None
        kind = getattr(section, "kind", None)
        if isinstance(kind, str) and kind.strip():
            normalized = kind.strip().lower()
            if normalized in _OPENCODE_AGENT_KINDS:
                return ADAPTER_OPENCODE
            if normalized in _OPENAI_COMPATIBLE_KINDS:
                return OPENAI_COMPATIBLE
            if normalized in _GEMINI_KINDS:
                return ADAPTER_GEMINI
            if normalized in _CORE_ADAPTERS.values():
                return normalized
        mapped = _CORE_ADAPTERS.get(name)
        if mapped is not None:
            return mapped
        if getattr(section, "base_url", None) or getattr(section, "auth", None) == "github_copilot":
            return OPENAI_COMPATIBLE
        return None

    def _construct_provider(
        self,
        name: str,
        kind: str,
        section: Any,
        model_ref: str,
    ) -> Provider | None:
        """Build one adapter from its config section, or ``None`` if unmappable.

        An OpenAI-compatible vendor must carry an explicit ``base_url``: without
        one the adapter would silently default to ``api.openai.com`` and send the
        vendor's model id (and, if configured, its key) to the wrong endpoint, so
        that is a hard :class:`~nexus.errors.ConfigError` rather than a fallback.
        """
        api_key = getattr(section, "api_key", None)
        base_url = getattr(section, "base_url", None) or None
        api = getattr(section, "api", None)
        auth = getattr(section, "auth", None)
        if auth == "github_copilot":
            from .auth.copilot import DEFAULT_BASE_URL

            if (name != "github-copilot" or kind not in (ADAPTER_OPENAI, OPENAI_COMPATIBLE)
                    or base_url not in (None, DEFAULT_BASE_URL) or api not in (None, "chat")):
                raise ConfigError("github_copilot requires providers.github-copilot with the official chat endpoint")
            base_url = DEFAULT_BASE_URL
        if kind == OPENAI_COMPATIBLE and not base_url:
            raise ConfigError(
                f"providers.{name}: kind 'openai_compatible' requires a base_url "
                "(Nexus will not default an OpenAI-compatible vendor to the "
                "official OpenAI endpoint)"
            )
        if kind == ADAPTER_CLAUDE_AGENT:
            from .model.providers.claude_agent import ClaudeAgentProvider

            if any(getattr(section, field, None) is not None for field in
                   ("api_key", "base_url", "api", "auth", "command", "args", "env", "inherit_env", "permission_policy", "profile")):
                raise ConfigError("claude-agent uses Claude Code subscription login; only executable and timeout_seconds are supported")
            provider = ClaudeAgentProvider(workspace=self.workspace, model=self._model_for(name, model_ref),
                                           executable=getattr(section, "executable", None),
                                           timeout_seconds=getattr(section, "timeout_seconds", None), environ=self._environ)
            provider.name = name
            return provider
        if kind == ADAPTER_OPENCODE:
            return self._construct_opencode(name, section, model_ref)
        model = self._model_for(name, model_ref)
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "environ": self._environ,
            "capability_source": self._capability_source_for(name),
            **self._provider_transport_kwargs(),
        }
        if kind == ADAPTER_ANTHROPIC:
            return AnthropicProvider(**kwargs)
        if kind == ADAPTER_GEMINI:
            return GeminiProvider(**kwargs)
        if kind == ADAPTER_OLLAMA:
            return OllamaProvider(api=api, **kwargs)
        if kind in (ADAPTER_OPENAI, OPENAI_COMPATIBLE):
            if not api:
                api = "chat" if kind == OPENAI_COMPATIBLE else "responses"
            if auth == "chatgpt_oauth":
                if name != "codex": raise ConfigError("chatgpt_oauth is supported only by providers.codex")
                from .auth.codex import (
                    CODEX_BASE_URL,
                    ChatGPTOAuthHeaders,
                    CodexOAuthManager,
                )
                factory = self._codex_auth_factory or CodexOAuthManager
                manager = factory(profile=getattr(section, "profile", None) or "default")
                # The private endpoint requires transient Responses without an output limit.
                kwargs.update(base_url=CODEX_BASE_URL, api_key=None, auth_headers=ChatGPTOAuthHeaders(manager), default_max_tokens=None)
            elif auth == "github_copilot":
                from .auth.copilot import CopilotAuthManager, CopilotHeaders
                from .model.providers.openai import EndpointFallback
                factory = self._copilot_auth_factory or CopilotAuthManager
                manager = factory(profile=getattr(section, "profile", None) or "default")
                kwargs.update(api_key=None, auth_headers=CopilotHeaders(manager), api_selector=EndpointFallback(api or "chat"))
            elif auth == "keychain":
                from .auth.api_key import StoredKeyAuth
                factory = self._api_key_auth_factory or StoredKeyAuth
                kwargs.update(api_key=None, auth_headers=factory(name, profile=getattr(section, "profile", None) or "default"))
            provider = OpenAIProvider(api=api, **kwargs)
            # The adapter class is ``openai`` for every OpenAI-compatible wire,
            # but the router key is the configured vendor id. Relabel so the
            # provider identity the context layer round-trips (``provider.name``)
            # matches the registered key; otherwise ``acme/...`` would resolve
            # once and then fail on the second hop as ``openai/...``.
            if name != "openai":
                provider.name = name
            return provider
        return None

    def _construct_opencode(
        self, name: str, section: Any, model_ref: str
    ) -> Provider:
        """Build the OpenCode ACP subprocess agent (an agent surface, not HTTP).

        The model id is opaque and ignored by the agent; ``command``/``args``
        select the binary, ``permission_policy`` decides how ACP permission
        prompts are answered, and ``inherit_env``/``env`` extend the explicit
        credential allowlist. No credential store is read.
        """
        provider = OpenCodeProvider(
            command=getattr(section, "command", None),
            args=getattr(section, "args", None),
            workspace=self.workspace,
            model=self._model_for(name, model_ref),
            timeout_seconds=getattr(section, "timeout_seconds", None),
            environ=self._environ,
            inherit_env=getattr(section, "inherit_env", None) or (),
            env=getattr(section, "env", None),
            permission_policy=getattr(section, "permission_policy", None) or "deny",
        )
        return provider

    def _capability_source_for(self, name: str) -> Any:
        """A registry-backed capability source for one adapter.

        The adapter's own ``capabilities(model)`` must agree with the router's
        registry-overlaid view, or it will build a request with tool schemas the
        loop already dropped (or omit schemas the loop offered). This closure
        resolves ``name/model`` through the registry, so both paths read the same
        authoritative descriptor. A miss (an uncatalogued model, or one resolved
        before the registry loads) returns ``None`` and the adapter falls back.
        """
        def source(model: str) -> Any | None:
            registry = self._registry
            if registry is None or not model:
                return None
            getter = getattr(registry, "get", None)
            if getter is None:
                return None
            info = getter(f"{name}/{model}")
            if info is None:
                info = getter(model)
            if info is None or not hasattr(info, "capabilities"):
                return None
            try:
                return info.capabilities()
            except Exception:  # noqa: BLE001 - a bad catalogue entry must not break
                return None

        return source


    @staticmethod
    def _model_for(name: str, model_ref: str) -> str | None:
        """The configured default model when the reference names this provider."""
        head, separator, tail = model_ref.partition("/")
        if separator and tail and head == name:
            return tail
        return None

    @staticmethod
    def _provider_keys_for(
        name: str, kind: str, sections: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """The registry keys an adapter answers to.

        Gemini is the catalogue id ``google``, so the two catalogue names alias
        **only** when the section is actually named ``google`` or ``gemini``. A
        custom name (``[providers.myvertex] kind = "google"``) answers to its own
        name only, so two Google-kind sections can never hijack each other. An
        alias is not added when the other catalogue name is itself a configured
        section, because that section owns it.
        """
        if kind == ADAPTER_GEMINI:
            if name == "google":
                keys = ["google"]
                if "gemini" not in sections:
                    keys.append("gemini")
                return tuple(keys)
            if name == "gemini":
                keys = ["gemini"]
                if "google" not in sections:
                    keys.append("google")
                return tuple(keys)
            return (name,)
        if name == "codex":
            return ("codex",)
        return (name,)

    def _build_providers(self, config: Config) -> dict[str, Provider]:
        """Construct every adapter the configuration asks the runtime to own.

        Provider construction is config-driven (plan section 8):
        ``[providers.<name>]`` selects the adapter by direct name or ``kind``, and
        any unmapped vendor with a ``base_url`` is served by the OpenAI-compatible
        adapter -- so a new OpenAI-compatible vendor needs only a ``nexus.toml``
        block. Credentials stay references (``${env:VAR}``) resolved at request
        time; this method never reads a secret value. The one shared HTTP client
        is owned by the runtime.

        Every configured section builds its **own** adapter instance, keyed by
        its own name (plus the ``google``/``gemini`` catalogue alias when it is
        one of those). A bare core-adapter reference with no section builds a
        default instance, but only when no configured section already owns that
        key -- so a configured section is never replaced by a default.
        """
        providers: dict[str, Provider] = {}
        v2 = getattr(config, "v2", None)
        sections = getattr(v2, "providers", None) or {}
        model_ref = getattr(config, "model", None) or ""
        if not isinstance(model_ref, str):
            model_ref = ""
        fallback = self._config_fallback(v2)

        # (1) One instance per configured section, under its own name(s).
        for name, section in sections.items():
            kind = self._adapter_kind(name, section)
            if kind is None:
                continue
            provider = self._construct_provider(name, kind, section, model_ref)
            if provider is None:
                continue
            for key in self._provider_keys_for(name, kind, sections):
                providers.setdefault(key, provider)

        # (2) A core adapter the default/fallback references but no section (or
        # alias) configured, so a bare ``ollama/...``/``google/...`` still
        # resolves. An already-registered key is never overwritten.
        for reference in (model_ref, *fallback):
            head = reference.partition("/")[0] if "/" in reference else ""
            if head not in _CORE_ADAPTERS or head in providers:
                continue
            kind = _CORE_ADAPTERS[head]
            provider = self._construct_provider(head, kind, None, model_ref)
            if provider is None:
                continue
            for key in self._provider_keys_for(head, kind, sections):
                providers.setdefault(key, provider)

        # File-loaded providers (``.nexus/providers/*.py``). A configured
        # provider always wins a same-named file, and a quarantined file is a
        # diagnostic, never a boot failure (plan section 8).
        file_result = self._load_provider_files(config)
        for name, provider in file_result.providers.items():
            if name not in providers:
                providers[name] = provider
        self._provider_file_diagnostics = tuple(
            diagnostic.to_dict() for diagnostic in file_result.diagnostics
        )
        from .devtools import dev_enabled

        if dev_enabled(self._environ):  # MOCK_PLAN §3.1: scenarios only in dev mode
            from .devtools.mock.provider import MOCK_PROVIDER, MockProvider

            providers.setdefault(MOCK_PROVIDER, MockProvider())
        return providers

    def _load_provider_files(self, config: Config) -> Any:
        """Scan ``.nexus/providers/`` for hot-loaded adapters (quarantined)."""
        from .model.providers.discovery import (
            FileProviderLoader,
            ProviderFileContext,
            ProviderFileResult,
        )

        loader = FileProviderLoader()
        if not loader.directories(self.workspace, self._home):
            return ProviderFileResult()
        context = ProviderFileContext(
            config=config,
            workspace=self.workspace,
            home=self._home,
            transport=self._provider_transport_kwargs(),
        )
        try:
            return loader.load(self.workspace, self._home, context=context)
        except Exception:  # noqa: BLE001 - discovery must never stop boot
            return ProviderFileResult()

    def _provider_diagnostics(self) -> tuple[dict[str, Any], ...]:
        """Diagnostics from the most recent provider-file scan."""
        return getattr(self, "_provider_file_diagnostics", ())

    @staticmethod
    def _config_fallback(v2: Any) -> list[str]:
        """The configured provider-fallback references, in order.

        Only non-empty strings are returned: a caller-supplied config object
        (not the validated structs) must never make the provider builder or the
        router index a malformed entry.
        """
        if v2 is None:
            return []
        getter = getattr(v2, "model_fallback", None)
        if not callable(getter):
            return []
        try:
            raw = list(getter())
        except Exception:  # noqa: BLE001 - a bad config must not break boot
            return []
        return [
            ref for ref in raw if isinstance(ref, str) and ref.strip()
        ]

    def _build_router(self, config: Config) -> ModelRouter:
        aliases: dict[str, str] = {}
        v2 = getattr(config, "v2", None)
        default_ref = getattr(config, "model", None)
        fallback: list[str] = []
        if v2 is not None:
            if default_ref:
                aliases.setdefault("default", default_ref)
            fast = v2.model_fast()
            plan = v2.model_plan()
            if fast:
                aliases["fast"] = fast
            if plan:
                aliases["plan"] = plan
            fallback = self._config_fallback(v2)
        return ModelRouter(
            self._providers,
            aliases=aliases,
            default=default_ref,
            fallback=fallback,
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
        # A workspace with no v2 config still uses the packaged Build agent.
        # Only an explicit v2 [agents] disable suppresses agent definitions.
        if section is not None and not getattr(section, "enabled", True):
            return None
        try:
            from .agents import AgentManager
            from .tools.builtin import (
                BUILTIN_TOOLS,
                META_TOOLS_OPT_IN,
                OPT_IN_TOOLS,
                WEB_TOOLS,
            )
            from .tools.bundles import BUNDLES

            known_bundles = {
                name: bundle.tools for name, bundle in BUNDLES.items()
            }
            known_tools = {
                tool.name
                for tool in BUILTIN_TOOLS + OPT_IN_TOOLS + META_TOOLS_OPT_IN + WEB_TOOLS
            }
            # ``Task`` is injected by the runtime per iteration, not a static
            # builtin; include it so a role may declare it without a false
            # ``unknown_tool`` diagnostic.
            known_tools.add("subagent")
            manager = AgentManager.for_workspace(
                self.workspace,
                home=self._home,
                seed=bool(getattr(section, "seed_roles", False)),
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

    def _supports_child_workspace(self) -> bool:
        """Whether this runtime can scope all child execution to a workspace."""
        context = getattr(self._assembler, "_context", None)
        return bool(
            context is not None
            and hasattr(context, "workspace")
            and callable(getattr(context, "for_iteration", None))
            and callable(getattr(self, "_build_child_tool_manager", None))
            and callable(getattr(self, "_child_path_guard", None))
        )

    def _ensure_child_sessions(self) -> _ChildSessionFacade:
        if self._child_sessions is None:
            if self._state_db is not None:
                manager = SessionManager(
                    db=self._state_db,
                    project=self.workspace,
                    namespace="agents",
                    lock_dir=nexus_home(self._home) / "locks" / "sessions" / project_key(self.workspace),
                )
            else:
                # Test-compatibility path (an injected ``sessions=``/``session_dir=``
                # runtime): a private, per-directory database beside the parent's.
                directory = getattr(self._sessions, "directory", None) or (
                    self.workspace / ".nexus" / "sessions"
                )
                manager = SessionManager(Path(directory) / "agents")
            self._child_sessions = _ChildSessionFacade(manager)
        return self._child_sessions

    def agent_request(self, child_session: str) -> dict[str, Any] | None:
        """The latest request snapshot a child recorded in its own log.

        The child loop attaches it to its first ``context.assembled`` of each
        turn (already redacted and bounded), plus the task prompt the child was
        given (its first user message); ``None`` until the child has sent.
        """
        store = self._ensure_child_sessions().manager.store
        real_id = _child_session_id(child_session)
        if not child_session or not store.exists(real_id):
            return None
        latest: dict[str, Any] | None = None
        prompt: str | None = None
        for record in store.records(real_id):
            message = getattr(record, "message", None)
            if prompt is None and getattr(message, "role", None) == "user":
                prompt = "".join(
                    block.text for block in message.content if isinstance(block, Text)
                )[:20_000]
            event = getattr(record, "event", None)
            if getattr(event, "type", None) != "context.assembled":
                continue
            request = event.data.get("request") if isinstance(event.data, Mapping) else None
            if isinstance(request, Mapping):
                latest = {
                    **dict(request),
                    "provider": event.data.get("provider"),
                    "model": event.data.get("model"),
                }
        if latest is not None and prompt:
            latest["prompt"] = prompt
        return latest

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
        parent_agent_id: str | None = None,
        parent_tier: str | None = None,
        root_turn_id: str = "",
        budget: Any | None = None,
        permissions: Any | None = None,
        grants: Sequence[Any] = (),
        config: Config | None = None,
        catalog: Sequence[Any] | None = None,
        profile_for: Callable[[str], Any] | None = None,
        event_sink: Any | None = None,
        hooks: Any | None = None,
        agent_definition: Any | None = None,
        parent_model: str | None = None,
        workspace: Path | None = None,
        runtime_supports_workspace: bool = False,
        worktree_service: Any | None = None,
        worktree_root: Path | None = None,
        worktree_scope: bool = False,
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
        if worktree_service is not None and worktree_service is not self._worktree_service:
            raise ValueError("subagent runners must use the Runtime-owned WorktreeService")
        expected_worktree_root = self._owned_worktree_root_for(
            workspace or self.workspace
        )
        if worktree_root is not None and Path(worktree_root) not in {
            self._worktree_root,
            expected_worktree_root,
        }:
            raise ValueError("subagent runners must use the Runtime-owned worktree root")
        if not parent_tier:
            agent_reference = parent_model
            if agent_definition is not None:
                agent_model = getattr(agent_definition, "model", None)
                agent_provider = getattr(agent_definition, "provider", None)
                if parent_model is None and agent_model != "inherit":
                    agent_reference = agent_model
                if (
                    agent_reference
                    and "/" not in agent_reference
                    and agent_provider
                    and agent_reference not in self._tiers.order
                ):
                    agent_reference = f"{agent_provider}/{agent_reference}"
            if not agent_reference:
                agent_reference = parent_model
            if not agent_reference:
                reference = getattr(self._assembler._context, "model_reference", None)
                if callable(reference):
                    provider_name, model = reference(effective)
                    if provider_name and model:
                        agent_reference = f"{provider_name}/{model}"
                    else:
                        agent_reference = model
            if agent_reference:
                info = (
                    self._registry.get(agent_reference)
                    if self._registry is not None
                    else None
                )
                parent_tier = self._tiers.resolve(
                    agent_reference, info=info
                ).tier
        parent_provider = getattr(agent_definition, "provider", None)
        if parent_model and "/" in parent_model:
            parent_provider, _, parent_model = parent_model.partition("/")
        if parent_model is None and agent_definition is not None:
            parent_model = getattr(agent_definition, "model", None)
            parent_provider = getattr(agent_definition, "provider", None)
            if parent_model == "inherit":
                parent_model = None
            if parent_model and "/" not in parent_model and parent_provider:
                parent_model = f"{parent_provider}/{parent_model}"
        if parent_model is None:
            reference = getattr(self._assembler._context, "model_reference", None)
            if callable(reference):
                configured_provider, parent_model = reference(effective)
                parent_provider = (
                    getattr(agent_definition, "provider", None)
                    if agent_definition is not None
                    else None
                ) or configured_provider
        if parent_provider and parent_model and "/" not in parent_model:
            parent_model = f"{parent_provider}/{parent_model}"
            parent_provider = None
        from .tools.bundles import profile_tools

        max_tier = str(getattr(section, "max_tier", "medium"))
        if self._tiers.rank(max_tier) is None:
            max_tier = self._tiers.default
        try:
            return SubagentRunner(
                agents=self._agents,
                runtime_factory=self._build_child_runtime,
                workspace=workspace or self.workspace,
                parent_session=parent_session or session_id,
                parent_agent_id=parent_agent_id,
                root_turn_id=root_turn_id,
                tiers=self._tiers,
                sessions=self._ensure_child_sessions(),
                parent_tools=tuple(parent_tools or ()),
                parent_tier=parent_tier or self._tiers.default,
                parent_model=parent_model,
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
                default_type=str(getattr(section, "default_type", "task")),
                profile_for=profile_for or profile_tools,
                config=effective,
                event_sink=event_sink,
                bundle_map=self._bundle_map(catalog),
                mutating_tools=self._mutating_names(catalog),
                hooks=hooks,
                worktree_service=self._worktree_service,
                worktree_root=expected_worktree_root,
                worktree_root_for=self._owned_worktree_root_for,
                runtime_supports_workspace=runtime_supports_workspace,
                worktree_scope=worktree_scope,
            )
        except Exception:  # noqa: BLE001 - an invalid agents config disables Task
            return None

    def _worktree_root_for(self, workspace: str | Path) -> Path:
        """Resolve one stable, Runtime-owned registry root per parent checkout."""
        checkout = Path(workspace).expanduser().resolve()
        root = self._worktree_roots.get(checkout)
        if root is None:
            identity = hashlib.sha256(str(checkout).encode("utf-8")).hexdigest()[:16]
            root = checkout.parent / f".nexus-worktrees-{checkout.name}-{identity}"
            self._worktree_roots[checkout] = root
        return root

    def _owned_worktree_root_for(self, workspace: str | Path) -> Path:
        """Resolve a root only when the checkout is a runtime-owned worktree."""
        checkout = Path(workspace).expanduser().resolve()
        if checkout == self.workspace:
            return self._worktree_root
        owned_roots = (
            root / "worktrees"
            for root in tuple(self._worktree_roots.values())
        )
        if any(checkout.is_relative_to(root) for root in owned_roots):
            return self._worktree_root_for(checkout)
        raise ValueError("nested worktree parent is outside Runtime-owned worktrees")

    def inspect_worktree(self, child_id: str) -> Any:
        """Inspect a child from one of this runtime's owned registry roots."""
        from .agents.worktrees import WorktreeError

        missing: WorktreeError | None = None
        for root in tuple(self._worktree_roots.values()):
            if not root.exists():
                continue
            try:
                return self._worktree_service.inspect(child_id, root=root)
            except WorktreeError as exc:
                if "no owned worktree record" not in str(exc):
                    raise
                missing = exc
        if missing is not None:
            raise missing
        raise WorktreeError(f"no owned worktree record for child {child_id!r}")

    def _worktree_runtime_ownership(self, child_id: str) -> bool | None:
        """Report whether a child session still has a live runtime handle."""
        sessions = self._child_sessions
        if sessions is None:
            return False
        try:
            return sessions.manager._live_handle(_child_session_id(child_id)) is not None
        except Exception:  # noqa: BLE001 - unknown ownership must fail closed
            return None

    def _owned_worktree_record(self, child_id: str) -> tuple[Path, Any]:
        """Resolve an authenticated child record only across registered roots."""
        from .agents.worktrees import WorktreeError

        matches = []
        for root in tuple(self._worktree_roots.values()):
            if not root.exists():
                continue
            try:
                matches.append((root, self._worktree_service.get(child_id, root=root)))
            except WorktreeError as exc:
                if "no owned worktree record" not in str(exc):
                    raise
        if len(matches) != 1:
            raise WorktreeError(
                "worktree ownership is missing or ambiguous for this runtime"
            )
        return matches[0]

    def worktree_confirmation_state(self, child_id: str) -> dict[str, Any]:
        """Capture state used to bind one short-lived host confirmation."""
        import hashlib
        import json

        root, record = self._owned_worktree_record(child_id)
        from .agents import worktrees
        from .agents.worktrees import WorktreeError

        parent = record.parent_workspace.resolve(strict=True)
        child = record.path.resolve(strict=True)
        git_dir = Path(worktrees._git(parent, "rev-parse", "--absolute-git-dir"))
        index_path = git_dir / "index"

        def snapshot(checkout: Path, *, ignored: bool = False) -> dict[str, str]:
            args = [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ]
            if ignored:
                args.append("--ignored=traditional")
            result = subprocess.run(
                args,
                cwd=checkout,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                env=worktrees._git_environment(),
            )
            if result.returncode:
                raise WorktreeError("could not snapshot owned worktree Git status")
            status = result.stdout
            return {
                "head": worktrees._git(checkout, "rev-parse", "--verify", "HEAD^{commit}"),
                "status": hashlib.sha256(status).hexdigest(),
                "dirty": bool(status),
            }

        record_state = {
            name: str(value) if isinstance(value, Path) else value
            for name, value in vars(record).items()
        }
        state = {
            "service_root": str(root.resolve(strict=True)),
            "record": record_state,
            "parent": {
                **snapshot(parent),
                "index": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            },
            "child": snapshot(child, ignored=True),
        }
        # Validate serialization here rather than silently producing an unstable token.
        json.dumps(state, sort_keys=True, separators=(",", ":"))
        return state

    def acknowledge_worktree(self, child_id: str, review_id: str, digest: str) -> Any:
        root, _record = self._owned_worktree_record(child_id)
        return self._worktree_service.acknowledge(
            child_id, review_id, digest, root=root
        )

    def integrate_worktree(
        self, child_id: str, review_id: str, digest: str, *, cancel: object | None = None
    ) -> Any:
        root, _record = self._owned_worktree_record(child_id)
        return self._worktree_service.integrate(
            child_id, review_id, digest, root=root, cancel=cancel
        )

    def discard_worktree(
        self,
        child_id: str,
        *,
        force: bool = False,
        review_id: str | None = None,
        cancel: object | None = None,
    ) -> Any:
        root, _record = self._owned_worktree_record(child_id)
        return self._worktree_service.discard(
            child_id,
            root=root,
            force=force,
            acknowledged_review_id=review_id,
            cancel=cancel,
        )

    def list_worktrees(self) -> tuple[Any, ...]:
        """List verified records with fresh checkout status from owned roots."""
        records = []
        for root in tuple(self._worktree_roots.values()):
            if not root.exists():
                continue
            records.extend(
                self._worktree_service.inspect(record.child_id, root=root)
                for record in self._worktree_service.list(root=root)
            )
        return tuple(records)

    def review_worktree(
        self,
        child_id: str,
        *,
        review_id: str | None = None,
        cursor: int = 0,
        limit: int = 1,
    ) -> Any:
        """Review a child only through a registry root owned by this runtime."""
        from .agents.worktrees import WorktreeError

        missing: WorktreeError | None = None
        for root in tuple(self._worktree_roots.values()):
            if not root.exists():
                continue
            try:
                return self._worktree_service.review(
                    child_id,
                    review_id=review_id,
                    cursor=cursor,
                    limit=limit,
                    root=root,
                )
            except WorktreeError as exc:
                if "no owned worktree record" not in str(exc):
                    raise
                missing = exc
        if missing is not None:
            raise missing
        raise WorktreeError(f"no owned worktree record for child {child_id!r}")

    def _build_child_runtime(self, spec: Any) -> _ChildRuntime:
        """The ``RuntimeFactory``: build a nested, restricted child run."""
        config = spec.config if isinstance(spec.config, Config) else self._load_config()
        workspace = Path(spec.workspace).resolve()
        path_guard = self._child_path_guard(spec, workspace)
        config = self._child_workspace_config(self._child_config(spec), path_guard)
        engine = self._child_permission_engine(
            spec, config, workspace=workspace, path_guard=path_guard
        )
        authority = _ChildAuthority(engine=engine, path_guard=path_guard)
        hooks = spec.hooks
        if hooks is not None:
            hook_manager = copy.copy(self._hooks)
            hook_manager._workspace = workspace
            parent_workspace = Path(getattr(hooks, "_workspace", self.workspace))
            hook_specs = getattr(hooks, "_specs", None)
            if isinstance(hook_specs, Mapping):
                rebased_specs: dict[str, tuple[Any, ...]] = {}
                for event, declarations in hook_specs.items():
                    rebased: list[Any] = []
                    for declaration in declarations:
                        cwd = getattr(declaration, "cwd", None)
                        if cwd:
                            candidate = Path(cwd)
                            try:
                                relative = candidate.relative_to(parent_workspace)
                            except ValueError:
                                rebased.append(declaration)
                            else:
                                rebased.append(
                                    replace(declaration, cwd=str(workspace / relative))
                                )
                        else:
                            rebased.append(declaration)
                    rebased_specs[str(event)] = tuple(rebased)
                hook_specs = rebased_specs
            hooks = _HookService(
                hook_manager, hook_specs, workspace=workspace
            )
        child_runner = self._make_subagent_runner(
            session_id=spec.session_id,
            parent_tools=spec.tools,
            parent_depth=spec.depth,
            parent_session=spec.session_id,
            parent_agent_id=spec.agent_id,
            parent_tier=spec.tier,
            parent_model=spec.model,
            root_turn_id=spec.root_turn_id,
            event_sink=spec.emit,
            budget=spec.budget,
            permissions=authority,
            grants=spec.grants,
            config=config,
            hooks=hooks,
            workspace=Path(spec.workspace),
            runtime_supports_workspace=self._supports_child_workspace(),
            worktree_scope=bool(getattr(spec, "worktree_scope", False)),
            worktree_service=self._worktree_service,
            worktree_root=self._owned_worktree_root_for(workspace),
        )
        return _ChildRuntime(
            self,
            replace(spec, config=config, permissions=authority, hooks=hooks),
            child_runner,
        )

    def _child_path_guard(self, spec: Any, workspace: Path) -> PathGuard:
        """Build the effective guard for a child without changing parent state."""
        authority = spec.permissions
        if isinstance(authority, _ChildAuthority):
            parent_guard = authority.path_guard
        else:
            permissions = getattr(
                getattr(spec.config, "v2", None), "permissions", None
            )
            parent_guard = PathGuard(
                self.workspace,
                write_roots=(
                    tuple(permissions.write_roots)
                    if permissions is not None and permissions.write_roots
                    else ("./",)
                ),
                read_denyroots=(
                    tuple(permissions.read_denyroots)
                    if permissions is not None
                    else ()
                ),
                home=self._home,
            )
        if workspace == parent_guard.workspace:
            return parent_guard
        return parent_guard.for_worktree(workspace)

    def _child_workspace_config(self, config: Config, guard: PathGuard) -> Config:
        """Pin permission roots to the child-scoped hard boundary.

        The manager and gate use ``guard`` directly; built-in read/write tools
        also construct guards from config, so rebasing this frozen config is
        necessary to keep their checks in the same workspace.
        """
        permissions = getattr(getattr(config, "v2", None), "permissions", None)
        if permissions is None:
            if guard.workspace == Path(self.workspace).resolve():
                return config
            raise RuntimeError(
                "cannot scope child workspace: configuration has no permissions section"
            )
        try:
            safe_write_roots: list[str] = []
            child = guard.workspace
            for root in guard.write_roots:
                if root.is_relative_to(child):
                    safe_write_roots.append(str(root))
                elif child.is_relative_to(root):
                    safe_write_roots.append(str(child))
            scoped_permissions = msgspec.structs.replace(
                permissions,
                write_roots=(
                    safe_write_roots
                    or [str(child.parent / ".nexus-worktree-no-write")]
                ),
                read_denyroots=[str(root) for root in guard.read_denyroots],
            )
            scoped_v2 = msgspec.structs.replace(
                config.v2, permissions=scoped_permissions
            )
            return replace(config, v2=scoped_v2)
        except Exception as exc:
            raise RuntimeError(
                "cannot scope child workspace permissions; refusing child runtime"
            ) from exc

    def _child_config(self, spec: Any) -> Config:
        base = spec.config if isinstance(spec.config, Config) else self._load_config()
        reference = spec.model
        if reference and "/" not in reference and reference not in self._tiers.order:
            role_provider = getattr(spec, "provider", None)
            if role_provider and getattr(spec, "requested_model", None) == reference:
                reference = f"{role_provider}/{reference}"
        if not reference:
            reference = getattr(base, "model", None)
        # Without a model registry a tier name cannot resolve to a concrete
        # model, so an inherited/clamped tier falls back to the parent's model.
        if reference in self._tiers.order and self._registry is None:
            parent_model = getattr(spec, "parent_model", None)
            reference = parent_model or getattr(base, "model", None)
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

    def _build_child_assembler(
        self, spec: Any, config: Config, *, workspace: Path | None = None
    ) -> Any:
        manager = self._assembler
        if not hasattr(manager, "for_iteration"):
            return manager
        context_manager = manager._context
        child_context = context_manager
        if (
            context_manager is not None
            and workspace is not None
            and context_manager.workspace != workspace
        ):
            child_context = copy.copy(context_manager)
            child_context.workspace = workspace.resolve()
            if child_context._env is not None:
                child_context._env = replace(
                    child_context._env, workspace=child_context.workspace
                )
        # A subagent is not the harness's root identity: no "You are Nexus"
        # preamble; its role body alone opens the system prompt.
        if child_context is not None:
            if child_context is context_manager:
                child_context = copy.copy(context_manager)
            child_context._identity = ""
            if child_context._env is not None:
                child_context._env = replace(child_context._env, identity="")
        child_coordinator = copy.copy(manager)
        child_coordinator._context = child_context
        # The agent body is the child's SOUL; MEMORY is deliberately empty.
        selected_agent_effort = None
        if getattr(spec, "reasoning_effort", None):
            reference = getattr(config, "model", None)
            if reference and "/" in reference:
                provider_name, _, model_name = reference.partition("/")
                resolved = child_coordinator._resolve(provider_name, model_name)
            else:
                resolved = child_coordinator._resolve(None, reference)
            supported_efforts = ()
            if resolved is not None:
                supported = getattr(child_coordinator, "_supported_efforts", None)
                if callable(supported):
                    supported_efforts = supported(
                        resolved,
                        getattr(self, "_registry", None),
                        provider_name=provider_name if reference and "/" in reference else None,
                    )
            if spec.reasoning_effort in supported_efforts:
                selected_agent_effort = spec.reasoning_effort
        assembler = child_coordinator.for_iteration(
            config=config,
            system_files={"soul": spec.system_prompt, "memory": ""},
            skills_index=(),
            mcp_index={},
            reasoning_effort=selected_agent_effort,
        )
        assembler.agent_definition = type(
            "ChildAgentDefinition",
            (),
            {
                "reasoning_effort": selected_agent_effort,
                "fallback": tuple(getattr(spec, "fallback", ()) or ()),
            },
        )()
        assembler._agent_effort_supported = selected_agent_effort is not None
        return assembler

    def _build_child_tool_manager(
        self,
        spec: Any,
        config: Config,
        runner: Any,
        *,
        workspace: Path | None = None,
        path_guard: PathGuard | None = None,
    ) -> ToolManager:
        manifest = self.manifest
        catalog_map: dict[str, Any] = (
            dict(manifest.tools) if manifest is not None else {}
        )
        catalog_map = {
            tool.name: tool
            for tool in self._filter_web_catalog(config, tuple(catalog_map.values()))
        }
        if runner is not None:
            from .tools.builtin.task import build_task_tool

            catalog_map["subagent"] = build_task_tool(runner)
        if bool(getattr(spec, "worktree_scope", False)) or bool(
            getattr(spec, "worktree", None)
        ):
            catalog_map = {
                name: tool
                for name, tool in catalog_map.items()
                if name in WORKTREE_CHILD_TOOLS and tool.origin == "builtin"
            }
        catalog = [catalog_map[name] for name in spec.tools if name in catalog_map]
        authority = (
            spec.permissions if isinstance(spec.permissions, _ChildAuthority) else None
        )
        workspace = workspace or Path(spec.workspace).resolve()
        path_guard = path_guard or (
            authority.path_guard if authority is not None else None
        )
        manager = ToolManager(
            config,
            workspace=workspace,
            tools=catalog,
            path_guard=path_guard,
            job_registry=self._job_registry,
            todo_store=self._effective_todo_store(),
        )
        self._track_tool_manager(manager)
        return manager

    def _child_permission_engine(
        self,
        spec: Any,
        config: Config,
        *,
        workspace: Path | None = None,
        path_guard: PathGuard | None = None,
    ) -> PermissionEngine:
        authority = spec.permissions
        if isinstance(authority, _ChildAuthority):
            if authority.path_guard is path_guard:
                return authority.engine
            source = authority.engine
            return PermissionEngine(
                mode=source.mode,
                allow=tuple(rule.raw for rule in source._allow),
                ask=tuple(rule.raw for rule in source._ask),
                deny=tuple(rule.raw for rule in source._deny),
                on_unattended=source.on_unattended,
                path_guard=path_guard,
            )
        workspace = workspace or Path(spec.workspace).resolve()
        permissions = getattr(getattr(config, "v2", None), "permissions", None)
        if permissions is not None:
            return PermissionEngine(
                mode=permissions.mode,
                allow=permissions.allow,
                ask=permissions.ask,
                deny=permissions.deny,
                on_unattended=permissions.on_unattended,
                path_guard=path_guard,
            )
        return PermissionEngine(path_guard=path_guard)

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
            files_changed=files_changed(
                session.messages, workspace=getattr(spec, "workspace", None)
            ),
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
            cache_dir=project_state_dir(self.workspace, self._home) / "cache" / "mcp",
            workspace=self.workspace,
            home=self._home,
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

    async def reload_model_routes(self) -> bool:
        """Rebuild providers, tiers, registry and router from the current config.

        First-run setup connects a provider and picks the global model without a
        daemon restart. The caller must ensure no turn is running. Returns
        ``False`` (restart needed) when routes were injected. The router is
        re-initialised in place because the session manager and the context
        coordinator hold that object.
        """
        if not self._owns_routes or self._closed:
            return False
        config = self._load_config()
        previous = self._owned_providers
        saved = (self._providers, self._tiers, self._registry, self._registry_loaded)
        try:
            # The builders read these attributes, so set each before the next.
            self._providers = self._build_providers(config)
            self._tiers = self._build_tiers(config)
            self._registry = self._build_registry(config)
            fresh = self._build_router(config)
        except Exception:
            self._providers, self._tiers, self._registry, self._registry_loaded = saved
            raise
        self._registry_loaded = False
        self._owned_providers = []
        for provider in self._providers.values():
            if not any(provider is owned for owned in self._owned_providers):
                self._owned_providers.append(provider)
        ModelRouter.__init__(
            self._router, fresh.providers, aliases=fresh.aliases, default=fresh.default,
            fallback=fresh.fallback, registry=fresh.registry, tiers=fresh.tiers,
        )
        self._assembler._tiers, self._assembler._registry = self._tiers, self._registry
        for provider in previous:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        return True

    # -- lifecycle ---------------------------------------------------------

    def refresh_voice_config(self) -> None:
        """Apply persisted voice settings without rebuilding provider routes."""
        from .config.schema import VoiceSection
        config = Config.load(self.workspace, home=self._home, environ=self._environ)
        section = config.v2.voice if config.v2 else VoiceSection()
        environ = os.environ if self._environ is None else self._environ
        if environ.get("NEXUS_VOICE", "").strip().lower() == "off":
            section = msgspec.structs.replace(section, enabled=False)
        self.voice.configure(section)

    async def aclose(self) -> None:
        """Close providers this runtime owns. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self.voice.shutdown()
        for provider in self._owned_providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
        self._owned_providers = []
        if self._owns_shared_client and self._shared_client is not None:
            await self._shared_client.aclose()
            self._shared_client = None
            self._owns_shared_client = False
        if self._owns_outbound_http_service:
            aclose = getattr(self._outbound_http_service, "aclose", None)
            if callable(aclose):
                outcome = aclose()
                if inspect.isawaitable(outcome):
                    await outcome
            self._owns_outbound_http_service = False
        await self._local_search_http_service.aclose()
        managers = [*self._owned_tools, *self._tracked_tools]
        for manager in managers:
            aclose = getattr(manager, "aclose", None)
            if aclose is not None:
                await aclose()
        self._owned_tools = []
        self._tracked_tools.clear()
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
