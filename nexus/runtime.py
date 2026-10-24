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
  runtime also owns the shell ``JobRegistry`` it creates (it is shared across
  per-turn tool snapshots and closed on :meth:`aclose`).
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
import json
import re
import subprocess
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

import httpx
import msgspec
import os

from .agents import SubagentOutcome, SubagentRunner, SubagentUsage
from .agents.runner import AGENT_CONFIG, qualify_model
from .agents.pool import AgentPool
from .agents.handoff import build_handoff, write_handoff
from .agents.handoff import digest as handoff_digest
from .agents.last_choice import AgentChoiceStore
from .model.effort_preferences import ModelEffortStore
from .agents.model import AgentError, AgentNotFoundError
from .agents.runner import ROOT_ONLY_TOOLS, WORKTREE_CHILD_TOOLS, cancel_child, files_changed
from .tools.names import canonical_tool_name
from .config import Config
from .config.paths import (
    nexus_home,
    project_agents_dir,
    project_key,
    project_state_dir,
    state_db_path,
)
from .context import ContextManager
from .context.manager_memory import MEMORY_KEY
from .context.manager import ContextFullError
from .context.cache import TokenCountCache
from .context.counting import RequestTokenCounter
from .core.bus import DROP_OLDEST, Bus
from .core.cancel import CancelToken
from .core.loop import _coerce_input, run_turn
from .core.turn import TurnLimits
from .agents.manager_prompt import (
    MANAGER_KIND,
    MANAGER_MODEL,
    MANAGER_MODEL_REF,
    MANAGER_PROMPT,
    MANAGER_PROVIDER,
    MANAGER_SIGN_IN_HINT,
)
from .errors import ConfigError, OperationCancelled, ProviderError, SessionError
from .events import Event
from .hooks import HookEvent, HookInvocation, HookManager, HookOutcome
from .model.http import DEFAULT_TIMEOUT
from .model.message import Document, Image, Message, MessageMeta, Text, Thinking, ToolResult, ToolUse
from .model.provider import Provider
from .model.providers.anthropic import AnthropicProvider
from .model.providers.gemini import GeminiProvider
from .model.providers.ollama import OllamaProvider
from .model.providers.openai import OpenAIProvider
from .model.providers.opencode import OpenCodeProvider
from .model.registry import (
    ADAPTER_ANTHROPIC,
    ADAPTER_ANTIGRAVITY,
    ADAPTER_CLAUDE_AGENT,
    ADAPTER_CLAUDE_CODE,
    ADAPTER_GEMINI,
    ADAPTER_OLLAMA,
    ADAPTER_OPENAI,
    ADAPTER_OPENCODE,
    ADAPTER_NVIDIA,
    ADAPTER_XAI,
    OPENAI_COMPATIBLE,
    ModelRegistry,
    RegistryCache,
)
from .model.request import REASONING_EFFORT_ORDER, ModelRequest, ToolSchema
from .model.router import ModelRouter
from .model.usage_gate import (
    GatedFallbacks,
    RouteDecision,
    UsagePolicy,
    UsageSnapshot,
    UsageVerdict,
    choose_route,
    quota_identity,
    verdict as usage_verdict,
)
from .model.reasoning_effort import ReasoningEffortSelection
from .model.selection import ModelSelection
from .model.tiers import (
    DEFAULT_TIER, DEFAULT_TIER_EFFORTS, TierTable,
)
from .net import OutboundHTTPService, SafeOutboundHTTPService
from .net.local_search import LocalSearchHTTPService
from .session import Session, SessionManager
from .session.agent_selection import AgentSelection
from .session.db import StateDatabase
from .session.export import is_user_prompt
from .session.records import MessageRecord
from .tools.builtin._job_recovery import OWNERS_DIRNAME
from .tools.builtin._jobs import JobRegistry
from .tools.builtin.question import ANSWER_WINDOW_S
from .session.context_authz import ContextDenied
from .tools.bundles import MANAGER_PROFILE, MANAGER_TOOL_NAMES, get_profile
from .tools.manager import ToolManager
from .tools.manager_port import STATE_HEADING as MANAGER_STATE_HEADING
from .tools.manager_tools import build_manager_catalog
from .tools.permissions import (
    ApprovalBroker,
    Decision,
    Grant,
    Outcome,
    PathGuard,
    PermissionEngine,
    collect_grants,
    exact_rule,
    permission_bundle_matches,
    synthetic_call,
)
from .tools.questions import QuestionBroker
from .tools.spec import (
    NestedAuthorization,
    NestedOutcome,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
)
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
    "xai": ADAPTER_XAI,
    "nvidia": ADAPTER_NVIDIA,
    "opencode": ADAPTER_OPENCODE,
    "claude-agent": ADAPTER_CLAUDE_AGENT,
    "claude-code": ADAPTER_CLAUDE_CODE,
    "antigravity": ADAPTER_ANTIGRAVITY,
    "agy": ADAPTER_ANTIGRAVITY,
}

#: ``kind`` spellings of the subagent-only Antigravity external worker.
_ANTIGRAVITY_KINDS = frozenset({ADAPTER_ANTIGRAVITY, "agy"})
_ANTIGRAVITY_LABEL = "Antigravity (agy)"
#: Fields an Antigravity section may set; anything else cannot be honoured and is refused.
_ANTIGRAVITY_FIELDS = frozenset({"kind", "executable", "timeout_seconds", "worker_inherits_agy_settings",
                                 "worker_dangerously_skip_permissions"})

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
#: Subagent worktree isolation is off for now: children share the session checkout.
SUBAGENT_WORKTREES = False


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


def _is_manager_session(session: Any) -> bool:
    """Whether ``session`` is the workspace manager (``session.kind == "manager"``)."""
    return getattr(session, "kind", None) == MANAGER_KIND


def _manager_model_metadata() -> dict[str, str]:
    """Request metadata that pins a manager turn to exactly one model (no fallback)."""
    return {
        "model_policy": "pinned",
        "pinned_model": MANAGER_MODEL_REF,
        "pinned_hint": MANAGER_SIGN_IN_HINT,
    }


class _ManagerAssembler:
    """A manager turn's assembler: stamps every request with the pinned-model policy.

    Wraps the context snapshot built from the packaged prompt only (no system
    files, skills, MCP index or role body). The wrapper makes the request name
    the pinned route and carry ``model_policy = "pinned"``, which the router
    turns into "no agent, tier or workspace fallback" and a visible
    :class:`~nexus.errors.ManagerModelUnavailable` on failure.
    """

    session_aware = False

    def __init__(self, inner: Any, *, state_snapshot: Callable[[Any], str] | None = None) -> None:
        object.__setattr__(self, "_inner", inner)
        # ``session -> "## Nexus State" block`` (plan 10), or ``None`` when switched off.
        object.__setattr__(self, "_state_snapshot", state_snapshot)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)

    def assemble(self, session: Any) -> Any:
        inner = getattr(self._inner, "assemble", None)
        built = inner(session) if callable(inner) else self._inner(session)
        if inspect.isawaitable(built):
            async def finish() -> Any:
                return self._stamp(await built, session)

            return finish()
        return self._stamp(built, session)

    def _stamp(self, request: Any, session: Any = None) -> Any:
        metadata = {
            key: value
            for key, value in dict(getattr(request, "metadata", None) or {}).items()
            if key not in ("agent_fallback", "tier", "reasoning_effort_explicit")
        }
        metadata.update(_manager_model_metadata())
        changes: dict[str, Any] = {}
        message_id = _latest_user_message_id(session)
        system = getattr(request, "system", None)
        if isinstance(system, str):
            # Stable prefix first: the packaged prompt, then the machine memory (changes only
            # at compaction), then what changes per request: the message id and the live state.
            text = system
            memory = _manager_memory_block(session)
            if memory:
                text += f"\n\n{memory}"
            if message_id:
                # The id the launch port validates; the model cannot guess it. Id only, no log text.
                text += f"\n\nCurrent user message id (pass as origin_message_id): {message_id}"
            snapshot = self._state_snapshot
            if snapshot is not None:
                try:
                    block = snapshot(session)
                except Exception:  # noqa: BLE001 - a snapshot failure never fails the turn
                    block = f"{MANAGER_STATE_HEADING}\nNexus State: unavailable"
                if block:
                    text += f"\n\n{block}"
            if text != system:
                changes["system"] = text
        return msgspec.structs.replace(
            request, model=MANAGER_MODEL, provider=MANAGER_PROVIDER, metadata=metadata, **changes
        )


def _manager_memory_block(session: Any) -> str:
    """The ``## Manager Memory`` system block from the machine-memory note, or ``""``.

    Read from the ``kv`` table (never ``<workspace>/MEMORY.md``). Best effort: an
    unreadable store yields no block.
    """
    getter = getattr(getattr(session, "_store", None), "kv_get", None)
    if not callable(getter):
        return ""
    try:
        note = getter(MEMORY_KEY)
    except Exception:  # noqa: BLE001 - memory is a hint, never a turn failure
        return ""
    if not isinstance(note, str) or not note.strip():
        return ""
    return "## Manager Memory (machine notes)\n" + note.strip()


def _latest_user_message_id(session: Any) -> str:
    """``m<seq>`` of the newest real user prompt in the session's log, or ``""``.

    The same id scheme the manager host port checks ``origin_message_id`` against.
    Best effort: an unreadable log yields no line, and a launch is then refused by
    the port with the id it expects.
    """
    reader = getattr(session, "read", None)
    if not callable(reader):
        return ""
    try:
        records = reader().records
    except Exception:  # noqa: BLE001 - a prompt hint never fails a turn
        return ""
    for record in reversed(records):
        if isinstance(record, MessageRecord) and is_user_prompt(record.message):
            return f"m{record.seq}"
    return ""


def _roster_digest(runner: Any) -> dict[str, str] | None:
    """Digests of the roster ``build_task_tool(runner)`` advertises, if readable."""
    digests = getattr(runner, "roster_digests", None)
    if not callable(digests):
        return None
    try:
        return digests().to_dict()
    except Exception:  # noqa: BLE001 - mirrors the description's tolerance of a broken roster
        return None


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
        project_root: Path | None = None,
        state_home: Path | None = None,
        agent_id: str = "root",
        skills: Any | None = None,
        extensions: Any | None = None,
        activations: Any | None = None,
        subagents: Any | None = None,
        outbound_http: OutboundHTTPService | None = None,
        local_search_http: LocalSearchHTTPService | None = None,
        questions: Any | None = None,
        subagent_roster_digest: Mapping[str, str] | None = None,
        record_write: Callable[..., None] | None = None,
    ) -> None:
        self.manager = manager
        self._record_write = record_write
        #: Digests of the tier models/efforts the ``subagent`` description in
        #: this iteration's schemas advertises, or ``None`` when it is not
        #: advertised. The loop reads it to record roster changes in history.
        self.subagent_roster_digest = subagent_roster_digest
        self._workspace = Path(workspace)
        self._project_root = project_root or self._workspace
        self._state_home = state_home
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
        #: Bound per iteration by the loop (``_NestedPipeline``); the
        #: ``ToolContext`` receives the typed wrappers below (plan 03 section 3.5).
        self.invoke_tool = None
        self.authorize_nested = None

    @property
    def max_parallel(self) -> int:
        return self.manager.max_parallel

    @property
    def nested_call_limit(self) -> int:
        code_mode = getattr(getattr(self._config, "v2", None), "code_mode", None)
        return int(getattr(code_mode, "max_nested_calls", 64))

    def nested_traits(self, name: object) -> dict[str, Any] | None:
        """Mutation and refusal facts for a nested call target.

        A read-only profile (``research``) refuses a mutating nested call with a
        clear message, as ``McpCall`` refuses mutating targets; such a tool is
        not in the selected catalog, so it is looked up in the full one.
        """
        selected = self.manager.get(name)
        if selected is not None:
            return {"mutates": bool(selected.spec.mutates), "refusal": None}
        full = getattr(self.manager, "_by_name", {}).get(name) if isinstance(name, str) else None
        if full is not None and full.spec.mutates and get_profile(self.manager.profile).read_only:
            return {
                "mutates": True,
                "refusal": f"the {self.manager.profile} profile refuses nested calls that change state",
            }
        return None

    async def _invoke_nested(self, name, args, *, parent_call_id, nested_id=None):
        call = await self.invoke_tool(
            name, args, parent_call_id=parent_call_id, nested_id=nested_id
        )
        result = call.result
        return NestedOutcome(
            result=ToolExecutionResult(
                content=list(result.content),
                is_error=result.is_error,
                display=getattr(result, "display", None),
                metrics=getattr(result, "metrics", None),
                context_note=getattr(result, "context_note", None),
                diff=getattr(result, "diff", None),
            ),
            status=call.status,
            permission=call.permission,
        )

    async def _authorize_nested(self, permission_key, *, parent_call_id, summary=None):
        grant = await self.authorize_nested(
            permission_key, parent_call_id=parent_call_id, summary=summary or {}
        )
        return NestedAuthorization(
            allowed=grant.allowed, decision=grant.decision,
            rule=grant.rule, message=grant.message,
        )

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
            record_write=(lambda path, data, before=None: self._record_write(self._session_id, self._turn_id, call.id, path, data, before)) if self._record_write else None,
            project_root=self._project_root,
            state_home=self._state_home,
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
            invoke_tool=self._invoke_nested if self.invoke_tool is not None else None,
            authorize_nested=(
                self._authorize_nested if self.authorize_nested is not None else None
            ),
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
        async def _emit(event) -> object:
            outcome = emit(event.type, event.data)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome

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
        self._proxy_calls = set()

    def plan(self, prepared):
        self._proxy_calls = {entry.call.id for entry in prepared.entries if entry.target is not None}
        plan = self.engine.plan(
            prepared.calls(),
            prepared.spec_map(),
            grants=self._grants,
            attended=self._attended,
        )
        evaluations = []
        specs = prepared.spec_map()
        for evaluation in plan.evaluations:
            spec = specs.get(evaluation.call.name)
            permission_key = (
                spec.resolve_permission_key(dict(evaluation.call.input))
                if spec is not None
                else None
            )
            if (
                evaluation.call.name == "subagent"
                and evaluation.call.input.get("worktree") is True
                and isinstance(permission_key, str)
                and permission_key.endswith(":worktree")
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

    def plan_key(self, permission_key: str, call_id: str):
        """Plan a synthetic ``ContextQuery(<name>)`` key through the same engine.

        Same frozen engine, grants and attended flag as :meth:`plan`; ``deny``
        rules stay absolute and the unattended policy applies (plan 03 3.2).
        """
        call, spec = synthetic_call(permission_key, call_id)
        return self.engine.evaluate(
            call, spec, grants=self._grants, attended=self._attended
        )

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
        if evaluation.call.id in self._proxy_calls:
            request = replace(request, preview="via McpCall\n" + (request.preview or ""))
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
        self._runtime = runtime
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
        if resolved is None and selection is None and agent_definition is not None:
            # A stale agent default must not be routed through an unrelated
            # workspace default. Preserve the requested route for descriptive
            # metadata, while leaving execution to report the unresolved route.
            return provider_name, model, None
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
        if _is_manager_session(session):
            return {"provider": MANAGER_PROVIDER, "model": MANAGER_MODEL}
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
            getattr(getattr(config, "v2", None), "agent", None), "name", "orchestrator"
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
        if _is_manager_session(session):
            # The pinned manager model runs at the provider default effort.
            return {"supported_levels": (), "stored_override": None, "effective_effort": None, "source": None}
        config = self.effective_config()
        agent_definition = getattr(session, "_turn_agent_definition", None)
        if agent_definition is None:
            agent_definition = self._root_agent_definition(session, config)
        provider_name, _model, resolved = self._root_route(
            session, config=config, agent_definition=agent_definition
        )
        metadata = self._effort_metadata(
            session, agent_definition, resolved, provider_name=provider_name
        )
        if resolved is not None and getattr(session, "model_selection", None) is None:
            reference = self._route_reference(config, agent_definition=agent_definition)[1]
            selected = DEFAULT_TIER_EFFORTS.get((reference, f"{provider_name}/{resolved.model}"))
            if (
                selected is not None
                and getattr(session, "reasoning_effort_selection", None) is None
                and getattr(agent_definition, "reasoning_effort", None) is None
                and not any(value == reference for value in self._tiers.overrides.values())
            ):
                metadata["effective_effort"] = selected
                metadata["source"] = "tier-default"
        return metadata

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

        A manager session never reaches that logic: it gets the explicit
        pinned branch below (packaged prompt, one model, no fallback).
        """
        if _is_manager_session(session):
            return self._manager_for_turn(session)
        manager = self._context
        for_turn = getattr(manager, "for_turn", None)
        if not callable(for_turn):
            return manager
        session_model_selection = getattr(session, "model_selection", None)
        config = self.effective_config()
        selection = getattr(session, "agent_selection", None) if session is not None else None
        configured_name = getattr(
            getattr(getattr(config, "v2", None), "agent", None), "name", "orchestrator"
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
        tier_name = None
        explicit_model = session_model_selection is not None or model_selection is not None
        if not explicit_model and resolved is not None:
            reference = getattr(agent_definition, "model", None)
            if not reference:
                _provider, reference, _configured_provider, _configured_model = self._route_reference(
                    config, agent_definition=agent_definition
                )
            if isinstance(reference, str) and reference in getattr(self._tiers, "order", ()):
                tier_name = reference
        capabilities = getattr(resolved, "capabilities", None)
        request_counter = None
        selected_agent_effort = None
        explicit_session_model = session_model_selection is not None
        if (
            tier_name is not None
            and not any(value == tier_name for value in self._tiers.overrides.values())
            and not explicit_session_model
            and getattr(session, "reasoning_effort_selection", None) is None
            and getattr(agent_definition, "reasoning_effort", None) is None
        ):
            selected_agent_effort = DEFAULT_TIER_EFFORTS.get((tier_name, f"{provider_name}/{model}"))
        if resolved is not None:
            request_counter = RequestTokenCounter(
                resolved.provider,
                cache=self._token_cache,
                provider_name=provider_name,
            )
            if selected_agent_effort is None:
                selected_agent_effort = self._effective_effort(
                    session, agent_definition, resolved, provider_name=provider_name
                )
        root_options = {}
        if self._runtime is not None and session is not None:
            root = self._runtime.validate_session_root(session.id)
            if root.kind != "main":
                root_options["workspace"] = root.path
        assembler = for_turn(
            **root_options,
            capabilities=capabilities,
            request_counter=request_counter,
            model=model,
            provider=provider_name,
            reasoning_effort=selected_agent_effort,
        )
        if (resolved is not None and config.v2 is not None and config.v2.context.on_overflow == "handoff"
                and config.v2.context.handoff_brief == "summarized"):
            from .context.handoff_model import HandoffSummarizer
            from .context.budget import BudgetInputs
            reserve = int(BudgetInputs.from_config_and_caps(config, capabilities).input_budget * 0.1)
            assembler._summarizer = HandoffSummarizer(resolved.provider, model, reserve)
        if root_options and session is not None:
            active_turn = getattr(session, "active_turn_id", None)
            if active_turn is None or getattr(session, "_turn_root_agents_key", None) != active_turn:
                session._turn_root_agents = getattr(getattr(assembler, "_env", None), "agents_text", "")
                session._turn_root_agents_key = active_turn
        assembler._tier_name = tier_name
        if self._runtime is not None and session is not None:
            assembler._session_worktree = self._runtime._session_worktree_facts(session.id)
        assembler._effort_explicit = (
            getattr(session, "reasoning_effort_selection", None) is not None
            or getattr(agent_definition, "reasoning_effort", None) is not None
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

    def _manager_for_turn(self, session: Any) -> Any:
        """The manager turn's assembler: packaged prompt, pinned model, nothing else.

        Built from an empty system-file/skills/MCP snapshot (the live files are
        never read), with the packaged prompt as the whole system text, no
        summarizer (so no second model is ever called), no role body and no
        agent fallback. Route resolution failure is not raised here: the
        request still names the pinned route and the loop turns the router's
        :class:`~nexus.errors.ManagerModelUnavailable` into a visible failed turn.
        """
        if session is not None:
            session._turn_agent_definition = None
        resolved = None
        resolve = getattr(self._resolver, "resolve", None)
        if resolve is not None:
            try:
                resolved = resolve(
                    ModelRequest(
                        messages=[], provider=MANAGER_PROVIDER, model=MANAGER_MODEL,
                        metadata=_manager_model_metadata(),
                    )
                )
            except Exception:  # noqa: BLE001 - reported by the router at call time
                resolved = None
        capabilities = getattr(resolved, "capabilities", None)
        request_counter = (
            RequestTokenCounter(resolved.provider, cache=self._token_cache, provider_name=MANAGER_PROVIDER)
            if resolved is not None
            else None
        )
        config = self.effective_config()
        for_iteration = getattr(self._context, "for_iteration", None)
        if config is None or not callable(for_iteration):
            return _ManagerAssembler(self._context)
        assembler = for_iteration(
            config=config,
            soul_text="",
            memory_text="",
            agents_text="",
            capabilities=capabilities,
            request_counter=request_counter,
            model=MANAGER_MODEL,
            provider=MANAGER_PROVIDER,
            skills_index=(),
            mcp_index={},
            reasoning_effort=None,
        )
        env = getattr(assembler, "_env", None)
        if env is not None:
            assembler._env = replace(
                env,
                soul_text="",
                memory_text="",
                agents_text="",
                system_override=MANAGER_PROMPT,
                summarizer=None,
                summarizer_async=False,
            )
        assembler._summarizer = None
        assembler.agent_definition = None
        assembler.agent_selection_source = "manager"
        return _ManagerAssembler(assembler, state_snapshot=self._manager_state_provider())

    def _manager_state_provider(self) -> Callable[[Any], str] | None:
        """``session -> "## Nexus State" block`` for each request, or ``None`` when off.

        Asks the port the manager tools use (``ManagerPort.state_snapshot``), so
        nothing in ``tools/`` or here imports ``host/``. A port without the method
        yields no block; a raising port is reported by the assembler as unavailable.
        """
        runtime = self._runtime
        if runtime is None:
            return None
        section = runtime._manager_section()
        if section is not None and not section.state_snapshot:
            return None
        factory = runtime.manager_port_factory
        if factory is None:
            return None
        ports: dict[int, Any] = {}

        def provide(session: Any) -> str:
            port = ports.get(id(session))
            if port is None:
                port = ports[id(session)] = factory(session)
            snapshot = getattr(port, "state_snapshot", None)
            return str(snapshot()) if callable(snapshot) else ""

        return provide

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
        workspace: Path | None = None,
        agents_text: str | None = None,
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
        root_options = {"workspace": workspace} if workspace is not None else {}
        if agents_text is not None:
            root_options["agents_text"] = agents_text
        assembler = for_iteration(
            **root_options,
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
        if (request_counter is not None and config.v2 is not None and config.v2.context.on_overflow == "handoff"
                and config.v2.context.handoff_brief == "summarized" and assembler._env is not None):
            from dataclasses import replace
            from .context.handoff_model import HandoffSummarizer
            from .context.budget import BudgetInputs
            reserve = int(BudgetInputs.from_config_and_caps(config, capabilities).input_budget * 0.1)
            assembler._env = replace(assembler._env,
                summarizer=HandoffSummarizer(resolved.provider, model, reserve), summarizer_async=True)
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
        if _is_manager_session(self._session) or _is_manager_session(session):
            # Second builder of the structural manager catalog: a manager turn
            # never merges a manifest (MCP, skills, extensions, hooks) into its
            # eight tools. ``_make_tool_turn`` returns no environment factory for
            # it, so reaching this is a wiring bug, not a fallback to extend.
            raise ConfigError("manager sessions do not use the extension manifest")
        manifest = runtime._selected_manifest(lease.manifest, session)
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
                workspace=getattr(self._path_guard, "workspace", runtime.workspace),
            )
        session_id = getattr(session, "id", self._session.id)
        pre_compact = runtime._pre_compact_gate(
            hooks_service, session_id, self._turn_id, iteration, cancel
        )
        assembler = runtime._assembler.for_iteration(
            config=config,
            system_files=system_files,
            **({"workspace": self._path_guard.workspace,
                "agents_text": getattr(self._session, "_turn_root_agents", "")}
               if self._path_guard is not None and self._path_guard.workspace != runtime.workspace else {}),
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
        facts = getattr(runtime, "_session_worktree_facts", None)
        assembler._session_worktree = facts(session_id) if callable(facts) else ()
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
        session_tools = getattr(runtime, "_session_worktree_tools", lambda _session: ())(session_id)
        base_catalog = runtime._filter_web_catalog(config, (*base_catalog, *session_tools))
        base_catalog = tuple(
            tool for tool in base_catalog
            if tool.name not in self._session.disabled_extensions.get("tools", ())
        )
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
        roster_digest = None
        if runner is not None:
            from .tools.builtin.task import build_task_tools

            catalog = (*base_catalog, *build_task_tools(runner))
            roster_digest = _roster_digest(runner)
        catalog = tuple(
            tool for tool in catalog
            if tool.name not in self._session.disabled_extensions.get("tools", ())
        )
        manager = runtime._build_iteration_manager(
            config,
            manifest,
            restrict=restrict,
            catalog=catalog,
            path_guard=self._path_guard,
        )
        if self._agent_definition is not None:
            from .tools.bundles import BUNDLES

            profile = (
                manager.authority_for_profile(self._agent_definition.profile)
                if self._agent_definition.profile
                else manager.authority_names
            )
            selected = runtime._agents.select_tools(
                self._agent_definition,
                available=manager.authority_names,
                profile=profile,
                bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                mutating=manager.authority_mutating,
            )
            manager = runtime._build_iteration_manager(
                config,
                manifest,
                restrict=tuple(selected.selected),
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
            workspace=getattr(self._path_guard, "workspace", runtime.workspace),
            session_id=session_id,
            turn_id=self._turn_id,
            config=config,
            project_root=runtime.workspace,
            record_write=getattr(getattr(runtime, "_session_worktrees", None), "record_write", None),
            state_home=getattr(runtime, "_home", None),
            skills=runtime._selected_skills(session),
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
            subagent_roster_digest=(
                roster_digest if "subagent" in manager.names else None
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
        from .tools.builtin.task import build_task_tools

        provisional = runtime._build_iteration_manager(
            config,
            runtime.manifest,
            restrict=restrict,
            catalog=(*base_catalog, *build_task_tools(None)),
            path_guard=self._path_guard,
        )
        parent_tools = list(provisional.authority_names)
        if agent_definition is not None:
            from .tools.bundles import BUNDLES

            root_profile = (
                provisional.authority_for_profile(agent_definition.profile)
                if agent_definition.profile
                else provisional.authority_names
            )
            root_selection = runtime._agents.select_tools(
                agent_definition,
                available=provisional.authority_names,
                profile=root_profile,
                bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                mutating=provisional.authority_mutating,
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
            workspace=getattr(self._path_guard, "workspace", runtime.workspace),
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
    #: Awaitable callable the session runs at every step boundary (after a model
    #: step's tool calls, before the next request): detects merges and applies a
    #: queued worktree move. Returns whether the root changed, in which case the
    #: session rebuilds this bundle for the new root. ``None`` = no boundary work.
    step_boundary: Any | None = None


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

    def __init__(self, manager: Any, specs_by_event: Any, *, workspace: Path, project_root: Path | None = None, root_for=None) -> None:
        self._root_for = root_for
        self._manager = manager
        self._specs = {
            str(event): tuple(specs)
            for event, specs in (specs_by_event or {}).items()
            if specs
        }
        self._workspace = workspace
        self._project_root = project_root or getattr(manager, "workspace", workspace)

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
        root_options = {}
        if isinstance(self._manager, HookManager):
            workspace = self._workspace
            if self._root_for is not None and invocation.session_id:
                workspace = self._root_for(invocation.session_id).path
            root_options = {"workspace": workspace, "project_root": self._project_root}
        return await self._manager.run(
            HookEvent.coerce(event), invocation, cancel=cancel, specs=specs, **root_options
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
        parent_call_id: str | None = None,
    ) -> Any:
        invocation = HookInvocation(
            event=HookEvent.PRE_TOOL_USE.value,
            tool=tool,
            key=key,
            bundle=bundle,
            tool_input=dict(tool_input or {}),
            session_id=session_id,
            turn_id=turn_id,
            data={"parent_call_id": parent_call_id} if parent_call_id else {},
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
        parent_call_id: str | None = None,
    ) -> Any:
        invocation = HookInvocation(
            event=HookEvent.POST_TOOL_USE.value,
            tool=tool,
            key=key,
            bundle=bundle,
            tool_input=dict(tool_input or {}),
            session_id=session_id,
            turn_id=turn_id,
            data={
                "result": dict(result or {}),
                **({"parent_call_id": parent_call_id} if parent_call_id else {}),
            },
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


class _RootOnlyContext:
    """Restricts the ``context`` namespace to root sessions (plan 03 section 3.9, v1).

    Delegates everything else to the wrapped :class:`ContextNamespaceProvider`
    (contract I2); a child, manager, or unknown caller is refused fail-closed.
    """

    def __init__(self, provider: Any, sessions: Any) -> None:
        self._provider = provider
        self._sessions = sessions

    def _require_root(self, caller: str) -> None:
        try:
            kind = self._sessions.kind_of(caller)
        except Exception:  # noqa: BLE001 - fail closed
            kind = ""
        if kind != "root":
            raise ContextDenied("context access is available to root sessions only")

    def resolve(self, caller: str, *args: Any, **kwargs: Any) -> Any:
        self._require_root(caller)
        return self._provider.resolve(caller, *args, **kwargs)

    def check(self, caller: str, *args: Any, **kwargs: Any) -> Any:
        self._require_root(caller)
        return self._provider.check(caller, *args, **kwargs)

    def bind(self, caller: str, *args: Any, **kwargs: Any) -> Any:
        self._require_root(caller)
        return self._provider.bind(caller, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)


#: The turn-frozen authority a child inherits: the parent's permission engine and
#: path guard, passed through unchanged (never broadened).
@dataclass(frozen=True)
class _ChildAuthority:
    engine: Any
    path_guard: Any


def _child_session_id(logical: str) -> str:
    """Compatibility wrapper for the stable stored child id mapping."""
    from .session.child_ids import stored_child_id

    return stored_child_id(str(logical or "subagent"))


def _child_ownership(spec: Any) -> dict[str, Any]:
    """Explicit physical owner edge; logical paths are compatibility only."""
    nested = bool(getattr(spec, "parent_agent_id", ""))
    parent = str(getattr(spec, "parent_session", ""))
    return {
        "kind": "external" if getattr(spec, "provider", "") in {"claude_code", "agy"} else "child",
        "root_id": getattr(spec, "root_id", "") or ("" if nested else parent),
        "owner_agent_id": spec.agent_id,
        "owner_spawn_seq": getattr(spec, "owner_spawn_seq", 0),
        "owner_namespace": "agents" if nested else "main",
        "owner_id": parent or None,
        "owner_turn_id": getattr(spec, "owner_turn_id", "") or getattr(spec, "root_turn_id", "") or None,
        "owner_call_id": getattr(spec, "parent_call_id", "") or None,
        "agent_id": spec.agent_id,
        "logical_session_id": spec.session_id,
    }


class _ChildSessionFacade:
    """The runner's ``SessionFacade``: logical ids, real (valid) child logs.

    The plan's `<parent>/sub/<n>` id is kept for agent metadata and the event
    tree; a sanized id of the same shape is used for the on-disk session so the
    child is a real, replayable session rather than an in-memory fake.
    """

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager

    @property
    def manager(self) -> SessionManager:
        return self._manager

    def child_id(self, parent_id: str, index: int) -> str:
        return f"{parent_id}/sub/{int(index)}"

    def allocate_child_id(self, parent_id: str) -> tuple[int, str]:
        return self._manager.store.reserve_child_id(parent_id)

    def root_id(self, parent_id: str) -> str:
        row = self._manager.store.session_row(parent_id)
        return str(row.get("root_id") or "") if row else parent_id

    async def aclose(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            self._manager.evict(self._manager.store.resolve_child_id(session_id))

    def child_config(self, parent_id: str, identity: str) -> dict[str, Any] | None:
        """The ``agent.config`` record of a child of ``parent_id``, else ``None``.

        ``None`` is every non-answer alike -- unknown id, another session's child,
        a trashed or external child, a legacy child that never wrote a record -- so
        a caller cannot tell a foreign child from a missing one. Ownership is the
        stored owner edge (parent session, main namespace), not an id prefix.
        """
        store = self._manager.store
        try:
            real_id = store.resolve_child_id(identity)
            row = store.session_row(real_id)
            if (
                not row or row.get("trash_id") or row.get("kind") != "child"
                or row.get("owner_id") != parent_id
                or row.get("owner_namespace") != "main"
                or identity not in (row.get("logical_session_id"), row.get("agent_id"))
            ):
                return None
            for record in store.records(real_id):
                event = getattr(record, "event", None)
                if getattr(event, "type", None) == AGENT_CONFIG:
                    data = event.data
                    return dict(data) if isinstance(data, Mapping) else None
        except Exception:  # noqa: BLE001 - an unreadable child is simply not continuable
            return None
        return None


def _child_models(session: Any) -> tuple[str, ...]:
    """Concrete models a child session started, from its own durable events.

    Reads ``model.started``/``external.started`` (the child's own, not relayed
    grandchild events) in order, collapsing consecutive repeats; empty when the
    child never started a model.
    """
    models: list[str] = []
    for event in getattr(session, "events", ()) or ():
        if getattr(event, "type", None) not in ("model.started", "external.started"):
            continue
        data = getattr(event, "data", None)
        if not isinstance(data, Mapping) or isinstance(data.get("agent"), Mapping):
            continue
        ref = qualify_model(data.get("provider"), data.get("model"))
        if ref is not None and (not models or models[-1] != ref):
            models.append(ref)
    return tuple(models)


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
        "context.handoff",
        "context.degraded",
        "context.notice",
        "permission.requested",
        "permission.resolved",
        "input.queued",
        "input.moved",
        "input.mode_changed",
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
        #: Tool calls the permission engine denied because no approver was
        #: attached (``unattended`` codes): the root must resolve or escalate.
        self.blocked: list[str] = []

    def attention(self) -> str | None:
        if not self.blocked:
            return None
        return ("the child needed approval it could not get and was denied: "
                + "; ".join(dict.fromkeys(self.blocked)))

    async def emit(self, event: Event) -> Event:
        if event.type == "permission.resolved" and len(self.blocked) < 20:
            data = event.data
            if str(data.get("code", "")).startswith("unattended") and "deny" in str(data.get("decision", "")):
                tool = data.get("tool") or data.get("tool_name") or "tool"
                self.blocked.append(f"{tool}: {str(data.get('reason') or data.get('code'))[:160]}")
        persistence_error = None
        try:
            record = self._session.append_event(event)
            if record is not None:
                event = record.event
        except Exception as exc:  # noqa: BLE001 - do not expose exception text
            persistence_error = exc
        if self._relay is None or event.type in _CHILD_RELAY_SUPPRESS:
            return event
        async def relay(event_type: str, data: Mapping[str, Any]) -> None:
            outcome = self._relay(event_type, data)
            if inspect.isawaitable(outcome):
                await outcome

        with contextlib.suppress(Exception):
            await relay(event.type, dict(event.data))
        if persistence_error is not None:
            # The owner log gets a stable, non-sensitive diagnostic. Observation
            # can fail without killing the child.
            with contextlib.suppress(Exception):
                await relay("error", {
                    "code": "child_log_persist_failed",
                    "message": "Could not persist a child event in its session log.",
                    "event_type": event.type,
                })
        return event


#: ``[providers.<name>]`` fields that configure the external Claude Code worker.
_CLAUDE_WORKER_FIELDS = (
    "worker_permission_mode", "worker_tools", "worker_allowed_tools", "worker_disallowed_tools",
    "worker_setting_sources", "worker_mcp_config", "worker_restricted",
)


#: An exact Claude model ID named without a provider prefix (aliases and tiers do not match).
_CLAUDE_MODEL_ID = re.compile(r"claude-[a-z]+-[0-9][a-z0-9.-]*")

#: Child ``stop_reason`` per external worker status.
_EXTERNAL_STOP_REASON = {
    "completed": "end_turn", "failed": "error", "interrupted": "timeout",
    "cancelled": "cancelled", "needs_attention": "needs_attention",
}


def _reported_usage(value: object) -> SubagentUsage | None:
    """Token counts an external CLI actually reported, else ``None`` (never estimated)."""
    if value is None:
        return None
    usage = SubagentUsage.from_value(value)
    return usage if usage.total_tokens > 0 else None


@dataclass(frozen=True)
class _ExternalAssignment:
    """An explicit external-worker child's resolved worker, authority and model."""

    worker: Any
    policy: Any
    provider: str
    model: str
    #: Which external CLI runs it: provenance recorded on the durable outcome.
    kind: str = ADAPTER_CLAUDE_CODE
    label: str = "Claude Code"
    #: Antigravity only: the explicit opt-in to agy's own settings, MCP servers and plugins.
    inherits_settings: bool = False
    skip_permissions: bool = False
    #: The worker's own provenance statement (for example "Antigravity (own tools)").
    provenance: str = ""


class _ChildRuntime:
    """One child run: a restricted nested turn over the parent's providers.

    It is deliberately not a second :class:`Runtime`: the child borrows the
    parent's router and agent manager, gets the tools the runner already
    intersected into ``spec.tools``, inherits the parent's permission engine and
    grants. It never spawns: ``runner`` is ``None`` for every real child.
    """

    def __init__(self, runtime: Runtime, spec: Any, runner: Any) -> None:
        self._runtime = runtime
        self._spec = spec
        self._runner = runner
        self._router = runtime._router
        #: Set for an explicit ``claude-code`` child: that assignment is executed
        #: by the external Claude Code worker, never by a Nexus ``run_turn`` loop.
        self._external: _ExternalAssignment | None = None
        #: Agent SDK providers this child built for ``claude-code`` (backend ``sdk``).
        self._owned: list[Any] = []
        v2 = getattr(getattr(spec, "config", None), "v2", None)
        sections = getattr(v2, "providers", {}) or {}
        sdk_backend = getattr(getattr(v2, "models", None), "claude_code_subagent_backend", "worker") == "sdk"
        reference = str(getattr(spec, "model", None) or "")
        in_tiers = reference in getattr(getattr(runtime, "_tiers", None), "order", ())
        if in_tiers:
            route = runtime._external_tier_route(reference, claude=not sdk_backend)
            reference = route or reference
            in_tiers = route is None
        head, sep, tail = reference.partition("/")
        provider_name = head if sep else getattr(spec, "provider", None)
        if not sep and provider_name is None:
            agents = getattr(runtime, "_agents", None)
            role = agents.get(getattr(spec, "agent_id", None)) if agents is not None else None
            provider_name = getattr(role, "provider", None)
        section = sections.get(provider_name)
        # Claude Code is never available to ordinary Nexus children: only an
        # explicitly configured role reaches the external worker below.
        parent_providers = getattr(runtime, "_providers", {})
        providers = {
            name: provider for name, provider in parent_providers.items()
            if Runtime._adapter_kind(name, sections.get(name)) not in (ADAPTER_CLAUDE_CODE, ADAPTER_ANTIGRAVITY)
        }
        if sdk_backend:
            # Backend ``sdk``: claude-code children run in the normal Nexus child
            # loop on the Agent SDK provider (no built-in Claude tools; Nexus tools,
            # permissions and PathGuard apply), even when main selection is off.
            providers.update(self._sdk_providers(runtime, sections, parent_providers))
        if providers.keys() != parent_providers.keys() or any(
                providers[name] is not parent_providers[name] for name in providers):
            self._router = ModelRouter(
                providers,
                aliases=runtime._router.aliases, default=runtime._router.default,
                fallback=runtime._router.fallback, registry=runtime._registry, tiers=runtime._tiers,
                tier_efforts=getattr(getattr(getattr(getattr(spec, "config", None), "v2", None), "models", None), "tier_efforts", {}),
            )
        headroom = getattr(runtime, "usage_headroom", None)
        if callable(headroom):
            # Provider-error fallbacks of this child skip providers whose quota is spent.
            self._router = GatedFallbacks(self._router, headroom)
        if Runtime._adapter_kind(provider_name, section) == ADAPTER_ANTIGRAVITY:
            self._refuse_inherited_external(
                runtime, spec, label=_ANTIGRAVITY_LABEL, route=f"{provider_name}")
            # An exact agy model ID only: no tier, alias or inherited name stands in for it.
            model = tail if sep else ("" if in_tiers or reference in ("", "inherit") else reference)
            try:
                self._external = self._plan_antigravity(runtime, spec, provider_name, section, model)
            except ConfigError as exc:
                exc.selected_model = f"{provider_name}/{model}"
                raise
        elif not sdk_backend and Runtime._adapter_kind(provider_name, section) == ADAPTER_CLAUDE_CODE:
            self._refuse_inherited_external(runtime, spec)
            model = tail if sep else (reference if _CLAUDE_MODEL_ID.fullmatch(reference) else "")
            self._external = self._plan_external(runtime, spec, provider_name, section, model)

    def _sdk_providers(
        self, runtime: Any, sections: Mapping[str, Any], parent: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Agent SDK providers for every ``claude-code`` route, keyed like the root's.

        The root's own provider is reused when main selection built one; otherwise
        this child constructs (and later closes) its own, so enabling the SDK
        backend never makes Claude Code selectable as a main model.
        """
        construct = getattr(runtime, "_construct_provider", None)
        names = dict.fromkeys(("claude-code", *sections))
        out: dict[str, Any] = {}
        for name in names:
            section = sections.get(name)
            if Runtime._adapter_kind(name, section) != ADAPTER_CLAUDE_CODE:
                continue
            provider = parent.get(name)
            if provider is None:
                if not callable(construct):
                    continue
                provider = construct(name, ADAPTER_CLAUDE_CODE, section, "")
                if provider is None:
                    continue
                self._owned.append(provider)
            out[name] = provider
        return out

    @staticmethod
    def _refuse_inherited_external(
        runtime: Any, spec: Any, *, label: str = "Claude Code", route: str = "claude-code"
    ) -> None:
        """A child never reaches an external CLI's own tools merely by inheriting the parent's model.

        Running a ``claude-code``/``antigravity`` assignment widens authority (the
        CLI's tools are not mediated by Nexus), so the route must be named by the
        ``Task`` call, the role (``model``/``provider``) or a configured tier.
        """
        model = str(getattr(spec, "model", None) or "")
        parent = str(getattr(spec, "parent_model", None) or "")
        order = getattr(getattr(runtime, "_tiers", None), "order", ())
        if not model or model != parent or model in order:
            return
        requested = getattr(spec, "requested_model", None)
        if requested and requested != "inherit":
            return
        try:
            role = runtime._agents.resolve(getattr(spec, "agent", None), context="subagent")
        except Exception:  # noqa: BLE001 - no role registry (or unknown role): nothing pins the route
            role = None
        if role is not None and ((role.model and role.model != "inherit") or role.provider):
            return
        raise ConfigError(
            f"refusing to run {getattr(spec, 'agent', 'this child')!r} on {label} by inheriting the "
            f"parent's model {model!r}: {label}'s own tools are not mediated by Nexus. Name "
            f"{route}/<model> on the role (model/provider) or on the Task call")

    @staticmethod
    def _plan_external(
        runtime: Any, spec: Any, provider_name: str, section: Any, model: str
    ) -> _ExternalAssignment:
        """Validate and resolve the external authority before anything starts.

        Refusals (token/cost budgets, a bad worker section) raise :class:`ConfigError`, which the runner
        reports as a child that failed to start.
        """
        from .agents.claude_code_worker import (
            ClaudeCodeWorker,
            WorkerConfig,
            resolve_policy,
        )

        if getattr(spec, "worktree_scope", False) or getattr(spec, "worktree", None):
            # A worktree child is limited to the reviewed no-shell tool profile; Claude
            # Code's own tools (including any shell the worker policy grants) are not.
            raise ConfigError(
                "claude-code children cannot run in a worktree: Claude Code's own tools are not "
                "limited to the no-shell worktree tool profile")
        budget = getattr(spec, "budget", None)
        if (getattr(budget, "token_budget", None) is not None
                or getattr(budget, "cost_budget", None) is not None):
            raise ConfigError("claude-code children cannot enforce token or cost budgets")
        runtime._validate_claude_section(provider_name, section)
        if not model:
            # No silent default: which Claude model ran must be the one that was named.
            raise ConfigError(
                f"refusing to run {getattr(spec, 'agent', 'this child')!r} on Claude Code without an "
                f"exact model: name {provider_name}/<model id> (for example "
                f"{provider_name}/claude-haiku-4-5) on the role, tier or Task call")
        # Read-only is prompt guidance only (docs/decisions.md): no child runs
        # as a restricted read-only Claude Code worker.
        policy = resolve_policy(WorkerConfig.from_section(section), read_only=False)
        worker = ClaudeCodeWorker(WorkerConfig.from_section(section), environ=runtime._environ)
        return _ExternalAssignment(
            worker=worker, policy=policy, provider=provider_name, model=model
        )

    @staticmethod
    def _plan_antigravity(
        runtime: Any, spec: Any, provider_name: str, section: Any, model: str
    ) -> _ExternalAssignment:
        """Validate and resolve an Antigravity (``agy``) child before anything starts.

        agy's own tools are not mediated by Nexus, so every restriction Nexus
        cannot enforce (budgets, worktree scope, unsupported worker
        fields) is refused here rather than silently dropped.
        """
        from .agents.antigravity_worker import (
            PROVENANCE,
            AntigravityWorker,
            WorkerConfig,
            resolve_policy,
        )

        agent = getattr(spec, "agent", "this child")
        if getattr(spec, "worktree_scope", False) or getattr(spec, "worktree", None):
            raise ConfigError(
                "antigravity children cannot run in a worktree: agy's own tools are not "
                "limited to the no-shell worktree tool profile")
        budget = getattr(spec, "budget", None)
        if (getattr(budget, "token_budget", None) is not None
                or getattr(budget, "cost_budget", None) is not None):
            raise ConfigError("antigravity children cannot enforce token or cost budgets")
        Runtime._validate_antigravity_section(provider_name, section)
        if not model:
            raise ConfigError(
                f"refusing to run {agent!r} on Antigravity without an exact model: name "
                f"{provider_name}/<model id from `agy models`> on the role, tier or Task call")
        Runtime._check_antigravity_model(runtime, provider_name, model)
        # Read-only is prompt guidance only (docs/decisions.md): never refused here.
        config = WorkerConfig.from_section(section)
        policy = resolve_policy(config, read_only=False)
        worker = AntigravityWorker(config, environ=runtime._environ)
        return _ExternalAssignment(
            worker=worker, policy=policy, provider=provider_name, model=model,
            kind=ADAPTER_ANTIGRAVITY, label=_ANTIGRAVITY_LABEL, provenance=PROVENANCE,
            inherits_settings=config.worker_inherits_agy_settings is True,
            skip_permissions=policy.skip_permissions)

    async def run(self) -> SubagentOutcome:
        if self._external is not None:
            return await self._run_external()
        runtime = self._runtime
        spec = self._spec
        workspace = Path(getattr(spec, "workspace", runtime.workspace)).resolve()
        facade = runtime._ensure_child_sessions()
        real_id = facade.manager.store.resolve_child_id(spec.session_id)
        resuming = bool(getattr(spec, "resume", False))
        # A follow-up reopens the stored session and never creates one: a child
        # deleted since the runner checked it fails instead of restarting empty.
        session = facade.manager.open(
            real_id, create=not resuming, recover=False, ownership=_child_ownership(spec),
        )
        # A Nexus-run child on a ``spawned`` edge is readable by its ancestors
        # through the context namespace (plan 03 section 3.2 rule 3); external
        # workers (the other ``open`` below) keep the default 0. A follow-up keeps
        # the allocation policy it was created with.
        grant = getattr(facade.manager.store, "grant_child_context_read", None)
        if callable(grant) and not resuming:  # lightweight runtime fakes have no allocation table
            with contextlib.suppress(SessionError):
                grant(spec.session_id, allowed=True)
        record = getattr(spec, "config_record", None)
        if record and not resuming:
            # The immutable record a later follow-up is built from. It goes
            # straight to the child's own log (before the first user message, so
            # it commits with it) and through no relay: the prompt it holds
            # never reaches the parent log, the root or a UI.
            with contextlib.suppress(Exception):
                session.append_event(Event(type=AGENT_CONFIG, data=dict(record), session=real_id))
        history = len(session.messages) if resuming else 0
        try:
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
                project_root=getattr(runtime, "workspace", workspace),
                state_home=getattr(runtime, "_home", None),
                session_id=real_id,
                turn_id="",
                config=config,
                agent_id=spec.agent_id,
                subagents=None,
                outbound_http=getattr(runtime, "_outbound_http_service", None),
                local_search_http=getattr(runtime, "_local_search_http_service", None),
                questions=(
                    runtime._question_service(spec.session_id)
                    if hasattr(runtime, "_question_service")
                    else None
                ),
            )
            max_iterations = int(spec.max_iterations or 0)
            # Subagents have no wall-clock deadline (``[agent] max_turn_seconds``
            # applies to root turns only); iterations and cancellation still bound them.
            limits = TurnLimits(max_iterations=max_iterations, max_seconds=0.0)
            if resuming:
                # Refuse before the follow-up is appended: the stored transcript is
                # append-only, so a message persisted ahead of a failed assembly
                # could not be taken back and would strand the child behind it.
                try:
                    await assembler.preflight((
                        *assembler._active_messages(session),
                        Message(role="user", content=_coerce_input(spec.prompt)),
                    ))
                except (ContextFullError, ProviderError) as exc:
                    # Expected refusals only (no room, or the token counter / provider
                    # failed): the log is still untouched. Programming errors and
                    # cancellation propagate.
                    return self._failed(
                        spec, session, f"{type(exc).__name__}: {exc}",
                        metrics={"unchanged": True}, handoff=False,
                    )
            lease = session.begin_turn(limits=limits)
            sink = _ChildEventSink(session, spec.emit)
            crash: Exception | None = None
            outcome = None
            try:
                outcome = await run_turn(
                    session=session,
                    user_input=spec.prompt,
                    assemble=assembler,
                    provider_for=self._router,
                    emit=sink,
                    tools=dispatcher,
                    gate=gate,
                    lease=lease,
                    persist_user_message=True,
                    hooks=spec.hooks,
                    relay_transcript=True,
                )
            except Exception as exc:  # noqa: BLE001 - a hard failure still hands its context back
                crash = exc
            finally:
                with contextlib.suppress(Exception):
                    lease.release()
            if crash is not None:
                return self._failed(spec, session, f"{type(crash).__name__}: {crash}")
            cost = runtime._child_cost(session, outcome)
            result = (
                runtime._child_outcome(spec, session, outcome, cost=cost, since=history)
                if history
                else runtime._child_outcome(spec, session, outcome, cost=cost)
            )
            attention = sink.attention()
            escalated = getattr(runtime, "_child_escalations", {}).pop(spec.session_id, [])
            if escalated:
                said = "the child escalated: " + "; ".join(escalated)
                attention = f"{attention}; {said}" if attention else said
            if attention:
                result = replace(result, metrics={**result.metrics, "attention": attention})
            if getattr(result, "status", None) == "failed":
                result = self._with_handoff(result, spec, session)
            return result
        finally:
            self._drop_escalations()
            kick = getattr(runtime, "usage_kick", None)
            if callable(kick):  # the child spent quota: refresh (rate-limited) for the next spawn
                with contextlib.suppress(Exception):
                    kick()

    def _drop_escalations(self) -> None:
        """Forget this child's recorded escalations (failure and cancel paths never read them)."""
        escalations = getattr(self._runtime, "_child_escalations", None)
        if isinstance(escalations, dict):
            escalations.pop(getattr(self._spec, "session_id", None), None)

    def _external_report(
        self, workspace: Path, before: Any, after: Any, result: Any
    ) -> tuple[list[str], dict[str, Any]]:
        """Verified file changes plus the authority/usage disclosure of an external run.

        ``result`` is ``None`` when the worker never returned (cancel or crash);
        the authority, unknown usage and file report still apply.
        """
        from .agents.claude_code_worker import changed_paths
        from .agents.runner import MAX_REPORTED_FILES

        external = self._external
        assert external is not None
        if before is not None and after is not None:
            changed = changed_paths(before, after)
            note = ("computed from the workspace's git status/mtimes and any new commits before "
                    f"and after the run, independently of {external.label}'s claims; gitignored files are "
                    "not checked; it is workspace-wide and may include concurrent changes by "
                    "other writers")
        else:
            changed = []
            note = "file changes could not be verified (workspace snapshot failed); none are reported"
        if len(changed) > MAX_REPORTED_FILES:
            note += f"; {len(changed)} paths changed, only the first {MAX_REPORTED_FILES} are listed"
        metrics: dict[str, Any] = {
            "provider": external.provider, "model": external.model, "usage": "unknown",
            "authority": external.policy.authority(workspace),
            "files_changed_note": note, "files_changed_total": len(changed),
            "external_kind": external.kind, "external_label": external.label,
        }
        if external.kind == ADAPTER_ANTIGRAVITY:
            metrics["inherits_agy_settings"] = external.inherits_settings
            metrics["skip_permissions"] = external.skip_permissions
            metrics["provenance"] = external.provenance
        if result is not None:
            session_key = "claude_session" if external.kind == ADAPTER_CLAUDE_CODE else "external_session"
            session_id = (result.claude_session if external.kind == ADAPTER_CLAUDE_CODE
                          else getattr(result, "claude_session", None) or getattr(result, "session", None) or "")
            metrics.update(
                {session_key: session_id}, transcript_path=result.transcript_path,
                tool_calls=result.tool_calls, elapsed_seconds=round(result.elapsed_seconds, 3))
            if external.kind != ADAPTER_CLAUDE_CODE and _reported_usage(getattr(result, "usage", None)) is not None:
                metrics["usage"] = "reported"  # only when the CLI's own stream carried token counts
            if result.attention:
                metrics["attention"] = result.attention
            if result.clip_notes:
                metrics["clip_notes"] = list(result.clip_notes)
        return changed, metrics

    def _context_snapshot(self, instructions: str, workspace: Path) -> dict[str, Any]:
        """What the external worker was actually given, in the ``context.assembled`` request shape."""
        from .core.loop import MAX_SNAPSHOT_SYSTEM_CHARS

        external = self._external
        assert external is not None
        policy = external.policy
        label = external.label
        if instructions:
            head = (f"{label} has no Nexus system prompt. The role instructions below were "
                    "prepended to the task prompt it received:\n\n" + instructions)
        else:
            head = f"No role instructions were sent; {label} received only the task prompt."
        system = f"{head}\n\n[authority] {policy.authority(workspace)}"
        policy_tools = getattr(policy, "tools", None)
        if policy_tools is not None:
            tools = [{"name": name, "description": f"{label}'s own tool (not mediated by Nexus)",
                      "input_schema": {}} for name in policy_tools]
        else:
            # Nexus cannot list the CLI's built-ins, but must not show "no tools".
            denied = ", ".join(getattr(policy, "disallowed_tools", ()) or ()) or "none"
            tools = [{"name": f"{label} default set",
                      "description": (f"all of {label}'s built-in tools for this version, "
                                      f"except disallowed: {denied} (not mediated by Nexus)"),
                      "input_schema": {}}]
        return {
            "iteration": 0, "messages": 1, "provider": external.provider, "model": external.model,
            "tools": len(tools),
            "request": {"system": redact_secrets(system[:MAX_SNAPSHOT_SYSTEM_CHARS]), "tools": tools},
        }

    async def _run_external(self) -> SubagentOutcome:
        """Run an explicit ``claude-code`` child through the external worker.

        Claude Code executes its own tools; Nexus records the user prompt, the
        assistant text and ``external.*`` activity read back from Claude's
        transcript in the child's own log (relayed upward like any child's), and
        verifies file changes itself. No Nexus tool dispatcher is involved.
        """
        from .agents.claude_code_worker import snapshot_workspace
        from .agents.runner import MAX_REPORTED_FILES

        runtime, spec, external = self._runtime, self._spec, self._external
        assert external is not None
        workspace = Path(getattr(spec, "workspace", runtime.workspace)).resolve()
        facade = runtime._ensure_child_sessions()
        real_id = facade.manager.store.resolve_child_id(spec.session_id)
        session = facade.manager.open(
            real_id, create=True, recover=False, ownership=_child_ownership(spec),
        )
        # External workers have no wall-clock deadline; only cancellation stops them.
        limits = TurnLimits(max_iterations=0, max_seconds=0.0)
        lease = session.begin_turn(limits=limits)
        sink = _ChildEventSink(session, spec.emit)

        async def emit(kind: str, data: dict[str, Any]) -> None:
            await sink.emit(Event(type=kind, data=dict(data), session=real_id, turn=lease.turn_id))

        terminal = False
        result = None
        crash: Exception | None = None
        before = after = None
        try:
            await emit("turn.started", {"limits": {"max_iterations": 0, "max_seconds": 0.0}})
            # Claude Code has no system-prompt channel here, so the role's
            # instructions travel as a labelled prefix of the one prompt it gets.
            instructions = str(getattr(spec, "system_prompt", "") or "").strip()
            prompt = (f"Role instructions (Nexus agent role {spec.agent!r}):\n{instructions}\n\n"
                      f"Task:\n{spec.prompt}") if instructions else spec.prompt
            await emit("context.assembled", self._context_snapshot(instructions, workspace))
            session.append_message(Message(
                role="user", content=[Text(spec.prompt)], meta=MessageMeta(turn_id=lease.turn_id)))
            with contextlib.suppress(Exception):
                before = await asyncio.to_thread(snapshot_workspace, workspace)
            try:
                result = await external.worker.run(
                    prompt=prompt, model=external.model, workspace=workspace,
                    policy=external.policy,
                    cancel=getattr(spec, "cancel", None), emit=emit)
            except Exception as exc:  # noqa: BLE001 - refusal/preflight failure is reported, not raised
                crash = exc
            with contextlib.suppress(Exception):
                after = await asyncio.to_thread(snapshot_workspace, workspace, before)
            if result is not None and result.text:
                session.append_message(Message(
                    role="assistant", content=[Text(result.text)],
                    meta=MessageMeta(provider=external.provider, model=external.model,
                                     turn_id=lease.turn_id)))
            if crash is None and result is not None:
                if result.status == "completed":
                    reported = (_reported_usage(getattr(result, "usage", None))
                                if external.kind != ADAPTER_CLAUDE_CODE else None)
                    await emit("turn.completed", {
                        "stop_reason": "end_turn", "iterations": 0,
                        "usage": reported.to_dict() if reported else {},
                        **({} if reported else {"usage_unknown": True})})
                elif result.status == "cancelled":
                    await emit("turn.cancelled", {"reason": result.error, "iterations": 0})
                else:
                    await emit("turn.failed", {
                        "error": redact_secrets(result.error or result.status)[:500], "iterations": 0})
                terminal = True
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await emit("turn.cancelled", {"reason": "cancelled", "iterations": 0})
            terminal = True
            # The runner builds the cancelled outcome; hand it the disclosure it
            # would otherwise lose (authority, unknown usage, verified file changes).
            with contextlib.suppress(Exception):
                after = await asyncio.to_thread(snapshot_workspace, workspace, before)
            with contextlib.suppress(Exception):
                changed, metrics = self._external_report(workspace, before, after, None)
                spec.metadata["external_partial"] = {
                    "metrics": metrics, "files_changed": tuple(changed[:MAX_REPORTED_FILES])}
            raise
        finally:
            if not terminal:
                with contextlib.suppress(Exception):
                    await emit("turn.failed", {
                        "error": redact_secrets(str(crash))[:500] if crash else "interrupted", "iterations": 0})
            with contextlib.suppress(Exception):
                lease.release()
        changed, metrics = self._external_report(workspace, before, after, result)
        if crash is not None or result is None:
            return self._failed(
                spec, session, f"{type(crash).__name__}: {crash}" if crash else "no result",
                metrics=metrics, files_changed=tuple(changed[:MAX_REPORTED_FILES]))
        outcome = SubagentOutcome(
            agent=spec.agent, session_id=spec.session_id, status=result.status,
            text=result.text, is_error=result.is_error,
            usage=(_reported_usage(getattr(result, "usage", None)) if metrics.get("usage") == "reported" else None)
            or SubagentUsage(),
            stop_reason=_EXTERNAL_STOP_REASON.get(result.status, "error"),
            dropped_tools=tuple(spec.dropped_tools), clamped=bool(spec.clamped), tier=spec.tier,
            requested_tier=spec.requested_tier, error=result.error, metrics=metrics,
            files_changed=tuple(changed[:MAX_REPORTED_FILES]),
            models=_child_models(session),
        )
        if outcome.status in ("failed", "interrupted"):
            outcome = self._with_handoff(outcome, spec, session)
        return outcome

    def _with_handoff(
        self, result: SubagentOutcome, spec: Any, session: Any, *, detail: str = ""
    ) -> SubagentOutcome:
        """Attach the failure handoff: an inline digest plus a saved markdown report."""
        report = build_handoff(
            agent=result.agent,
            session_id=result.session_id,
            prompt=spec.prompt,
            status=result.status,
            stop_reason=result.stop_reason,
            error=result.error,
            iterations=result.iterations,
            messages=list(getattr(session, "messages", ()) or ()),
            files_changed=result.files_changed,
            total_tokens=result.usage.total_tokens,
            detail=detail,
        )
        saved = write_handoff(nexus_home(self._runtime._home), result.session_id, report)
        return replace(
            result,
            handoff=handoff_digest(report),
            handoff_path=str(saved) if saved is not None else None,
        )

    def _failed(
        self,
        spec: Any,
        session: Any,
        message: str,
        *,
        metrics: Mapping[str, Any] | None = None,
        files_changed: tuple[str, ...] = (),
        handoff: bool = True,
    ) -> SubagentOutcome:
        """A failed outcome for a run that raised or could not start."""
        message = redact_secrets(message)  # raw exception text may carry credentials
        base = SubagentOutcome(
            agent=spec.agent,
            session_id=spec.session_id,
            status="failed",
            is_error=True,
            text=message,
            error=message[:500],
            stop_reason="error",
            dropped_tools=tuple(spec.dropped_tools),
            clamped=bool(spec.clamped),
            tier=spec.tier,
            requested_tier=spec.requested_tier,
            metrics=dict(metrics or {}),
            files_changed=files_changed,
            models=_child_models(session),
        )
        return self._with_handoff(base, spec, session) if handoff else base

    async def aclose(self) -> None:
        self._drop_escalations()
        if self._external is not None:
            await self._external.worker.aclose()
        owned, self._owned = self._owned, []
        for provider in owned:
            with contextlib.suppress(Exception):
                await provider.aclose()


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
        xai_auth_factory: Callable[..., Any] | None = None,
        api_key_auth_factory: Callable[..., Any] | None = None,
        voice_engine_factory: Callable[..., Any] | None = None,
        voice_store: Any | None = None,
        manager_port_factory: Callable[[Session], Any | None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._session_roots = OrderedDict()
        #: ``callable(Session) -> ManagerPort | None``, injected by the host. A
        #: manager session's eight tools talk to the port it returns; ``None``
        #: (no factory or no port) fails the manager turn visibly. Assignable
        #: after construction because the host builds the port from the runtime.
        self.manager_port_factory = manager_port_factory
        self._home = Path(home) if home is not None else None
        self._agent_choices = AgentChoiceStore(nexus_home(self._home) / "agent_models.json")
        self._model_efforts = ModelEffortStore(nexus_home(self._home) / "model_efforts.json")
        # SHARED_DAEMON_PLAN §3 B1: capture once at the ownership boundary.
        # An explicit empty mapping must not fall back to the daemon environment.
        environ = dict(os.environ if environ is None else environ)
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
        self._xai_auth_factory = xai_auth_factory
        self._api_key_auth_factory = api_key_auth_factory

        # Tool infrastructure. A caller may inject a ready ToolManager, a
        # PermissionEngine, or a full per-turn factory; otherwise the runtime
        # builds a fresh ToolManager snapshot per turn while sharing one owned
        # JobRegistry across turns (so shell jobs survive between turns
        # without a mutable runtime-global manager).
        self._tools = tools
        self._permissions = permissions
        self._tool_factory = tool_factory
        self._job_registry = job_registry
        #: ``escalate`` reports of running children, keyed by logical child session id.
        self._child_escalations: dict[str, list[str]] = {}
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
                self._job_registry = JobRegistry(
                    environ=self._environ,
                    event_sink=self._emit_job_event,
                    completion_sink=self._deliver_job_completion,
                    claim_sink=self._retract_job_completion,
                    # Rooted like the session database and locks: the daemon's
                    # ``home`` override (or ``$NEXUS_HOME``) plus the project key.
                    state_dir=project_state_dir(self.workspace, self._home)
                    / OWNERS_DIRNAME,
                )
                self._owns_job_registry = True

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
            from .voice.engine import BarkEngine

            def voice_engine_factory(path):
                return BarkEngine(path, device=self.voice.config.device)

            voice_engine_factory.available = BarkEngine.available
        self.voice = VoiceManager(
            voice_config, engine_factory=voice_engine_factory, environ=self._environ,
            store=voice_store if voice_store is not None else ModelStore(nexus_home(self._home), revision=voice_config.revision),
        )

        self.live = self._build_live(initial, voice_environ)

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
        #: Probe/catalogue results shared by every registry this runtime builds.
        self._registry_cache = RegistryCache()
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
        #: Plan-usage gate for sub-agent spawns (docs/models.md). The host injects
        #: the snapshot (``set_usage_snapshot``) and a cheap ``usage_kick``.
        self._usage_snapshot: UsageSnapshot | None = None
        self._usage_policy: UsagePolicy = self._build_usage_policy(initial)
        self.usage_kick: Callable[[], None] | None = None
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
        #: One subagent tree budget per root session, its window reset each root turn.
        self.agent_pool = AgentPool()
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
        from .worktree_sessions import SessionWorktrees
        self._session_worktrees = SessionWorktrees(self)
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
                workspace_required=self._session_worktrees.required,
                prepare_workspace=self._session_worktrees.prepare,
                before_terminal=self._session_worktrees.finish,
                hooks=self._session_hooks,
                busy_check=self.session_has_active_jobs,
                job_probe=self.shell_job_may_be_live,
                manager_compaction=self._manager_compaction_policy,
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
                workspace_required=self._session_worktrees.required,
                prepare_workspace=self._session_worktrees.prepare,
                before_terminal=self._session_worktrees.finish,
                hooks=self._session_hooks,
                busy_check=self.session_has_active_jobs,
                job_probe=self.shell_job_may_be_live,
                manager_compaction=self._manager_compaction_policy,
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
    def questions(self) -> QuestionBroker:
        """The operator-question broker the host answers through."""
        return self._questions

    def _question_service(self, session_id: str) -> _QuestionService:
        """Bind questions through explicit stored identity, never path ancestry."""
        root_id = str(session_id)
        if self._child_sessions is not None:
            store = self._child_sessions.manager.store
            physical = store.resolve_child_id(session_id)
            row = store.session_row(physical)
            if row is not None and row.get("root_id"):
                root_id = row["root_id"]
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
        await self._session_worktrees.recover()
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

    async def ensure_models(self) -> None:
        """Initialize configured model catalogues once, without forcing a refresh.

        Host model reads need the same catalogue as turn resolution, including
        configured subscription aliases such as Claude (plan section 15.3).
        """
        await self._ensure_registry()

    async def refresh_models(self) -> Any:
        """Force a catalogue refresh, publishing ``registry.*`` events.

        Returns the resulting :class:`~nexus.model.registry.RegistryStatus`, or
        ``None`` when no registry is configured.
        """
        if self._registry is None:
            return None
        await self._ensure_registry(force=True)
        return self._registry.status()

    def provider_models_info(self, provider: str) -> dict[str, Any]:
        """``{count, refreshed_at, error}`` of the cached ``agy models`` list (zeros when never probed)."""
        info = self._registry.external_info() if self._registry is not None else {}
        return dict(info.get(provider) or next(iter(info.values()), {"count": 0, "refreshed_at": 0.0, "error": ""}))

    async def refresh_provider_models(self, provider: str) -> dict[str, Any]:
        """Re-run ``agy models`` now (the Antigravity Refresh button); the catalogue is not refetched."""
        if self._registry is None:
            raise ConfigError("no model registry is configured")
        await self._ensure_registry()
        await self._registry.refresh_external()
        return self.provider_models_info(provider)

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
                job_registry=self._job_registry,            )
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
        # An active session is inspectable too: the preview reads only the
        # durable records, so Tools/Skills/MCP/AGENTS.md open read-only mid-turn
        # (the first turn has already locked the selections).
        class _PreviewSession:
            """Read-only subset of Session used during local preparation."""

            id = getattr(session, "id", "")
            messages = tuple(getattr(session, "messages", ()))
            model_selection = getattr(session, "model_selection", None)
            agent_selection = getattr(session, "agent_selection", None)
            reasoning_effort_selection = getattr(
                session, "reasoning_effort_selection", None
            )
            disabled_extensions = getattr(session, "disabled_extensions", {})
            mcp_loading_choices = getattr(session, "mcp_loading_choices", {})
            mcp_loading_frozen = getattr(session, "mcp_loading_frozen", None)
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

            def active_messages(self):
                getter = getattr(session, "active_messages", None)
                return getter() if callable(getter) else (self.messages, self.message_seqs())

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
            selection_manager = self._build_iteration_manager(assembler.effective_config(), manifest)
            _, unavailable_tools = self._web_tool_availability(assembler.effective_config())
            selectable_tools = set(selection_manager.authority_names) | set(unavailable_tools)
            turn_runner = getattr(getattr(tool_turn, "dispatcher", None), "_subagents", None)
            if turn_runner is not None:
                selectable_tools.update(("subagent", "subagent_continue"))
            if agent is not None:
                from .tools.bundles import BUNDLES

                selectable_tools = set(self._agents.select_tools(
                    agent,
                    available=selectable_tools,
                    profile=(selection_manager.authority_for_profile(agent.profile)
                             if agent.profile else selectable_tools),
                    bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                    mutating=selection_manager.authority_mutating,
                ).selected)
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
            # Resolve disabled rows against the same unfiltered generation,
            # never against the session-filtered dispatch catalog.
            from .tools.builtin import BUILTIN_TOOLS, OPT_IN_TOOLS, WEB_TOOLS

            tool_definitions = {
                tool.name: tool for tool in BUILTIN_TOOLS + OPT_IN_TOOLS + WEB_TOOLS
            }
            tool_definitions.update({tool.name: tool for tool in selection_manager.tools})
            mcp_sources = {}
            for server_name, server in getattr(manifest, "mcp", {}).items():
                tool_definitions.update({tool.name: tool for tool in getattr(server, "tools", ())})
                mcp_sources.update({tool.name: f"mcp:{server_name}" for tool in getattr(server, "tools", ())})
            tool_definitions.update(getattr(manifest, "tools", {}))
            runner = getattr(getattr(tool_turn, "dispatcher", None), "_subagents", None)
            if runner is not None:
                from .tools.builtin.task import build_task_tools

                for task_tool in build_task_tools(runner):
                    tool_definitions[task_tool.name] = task_tool

            def tool_row(schema, *, enabled, config_enabled):
                tool = tool_definitions.get(schema.name)
                spec = getattr(tool, "spec", None)
                row = {
                    "name": schema.name,
                    "enabled": enabled,
                    "config_enabled": config_enabled,
                    "description": schema.description,
                    "input_schema": dict(schema.input_schema),
                    "group": (
                        f"mcp:{schema.name.split('__', 2)[1]}"
                        if schema.name.startswith("mcp__") and schema.name.count("__") >= 2
                        else getattr(spec, "group", "") or schema.name
                    ),
                    "bundle": getattr(spec, "bundle", ""),
                }
                if tool is not None:
                    if tool.origin == "mcp" and schema.name.startswith("mcp__"):
                        row["source"] = mcp_sources.get(
                            schema.name, f"mcp:{schema.name.split('__', 2)[1]}"
                        )
                    elif tool.origin in {"builtin", "mcp"}:
                        row["source"] = "built-in"
                    else:
                        row["source"] = "extension"
                        if tool.source:
                            source = Path(tool.source)
                            row["origin"] = (
                                str(source.relative_to(self.workspace))
                                if source.is_relative_to(self.workspace) else str(source)
                            )
                if spec is not None:
                    row["read_only"] = not spec.mutates
                    # ToolSpec has no permission category today. Do not infer
                    # one from its bundle or permission-key callback.
                    permission = getattr(spec, "permission", None)
                    if permission is not None:
                        row["permission"] = permission
                    if spec.timeout_s is not None:
                        row["timeout_s"] = spec.timeout_s
                return row

            def skill_index_line(name: str) -> str:
                return next((line for line in parts.get("skills_index", "").splitlines()
                             if line.startswith(f"{name}:")), "")

            def skill_frontmatter(entry: Any) -> dict[str, str]:
                """Declared fields as flat, capped strings (name is the card heading)."""
                fields = {
                    "description": (entry.sanitized_description() if callable(getattr(entry, "sanitized_description", None))
                                    else getattr(entry, "description", "")),
                    "allowed-tools": ", ".join(getattr(entry, "allowed_tools", ()) or ()),
                    "bundles": ", ".join(getattr(entry, "bundles", ()) or ()),
                    "model": getattr(entry, "model", None) or "",
                    "version": getattr(entry, "version", "") or "",
                }
                return {key: redact_secrets(str(value))[:300] for key, value in fields.items() if value}

            enabled_names = {schema.name for schema in request.tools}
            mcp_servers = []
            if self._mcp is not None:
                for status in self._mcp.statuses()[:64]:
                    snapshot = self._mcp.server_snapshot(status.name)
                    names = list(snapshot.tool_names())[:2000] if snapshot else []
                    config = self._mcp.definition(status.name).config
                    command_label = ""
                    if config.transport == "stdio":
                        basename = config.command.replace("\\", "/").rsplit("/", 1)[-1]
                        command_label = redact_secrets(self._mcp.redact_display(
                            f"{basename} ({len(config.args)} args)"
                        ))
                    mcp_servers.append({
                        "name": status.name,
                        "transport": config.transport,
                        "command_label": command_label,
                        # connected / disabled / failed / backoff / not connected (lazy, never tried)
                        "status": ("connected" if status.connected else "disabled" if not status.enabled
                                   else "failed" if status.health.value in {"failed", "closed"} or status.last_error
                                   else "backoff" if status.health.value == "backoff"
                                   else "connecting" if status.health.value == "connecting" else "not connected"),
                        "scope": getattr(self._extensions, "mcp_scopes", {}).get(status.name, "project"),
                        "source_path": getattr(self._extensions, "mcp_sources", {}).get(status.name, ""),
                        "url": redact_secrets(self._mcp.redact_display(config.url))[:300] if config.url else "",
                        "ignored_keys": list(config.ignored_keys),
                        "include_tools": list(config.include_tools)[:64],
                        "exclude_tools": list(config.exclude_tools)[:64],
                        "config_enabled": status.enabled,
                        "enabled": status.enabled and status.name not in session.disabled_extensions["mcp"],
                        "tool_loading": (session.mcp_loading_frozen.get(status.name, "search")
                            if session.mcp_loading_frozen is not None else session.mcp_loading_choices.get(status.name, getattr(snapshot, "tool_loading", "search"))),
                        "tool_loading_source": "session" if status.name in session.mcp_loading_choices else getattr(snapshot, "tool_loading_source", "default"),
                        "config_tool_loading": getattr(snapshot, "tool_loading", "search"),
                        "schema_tokens": sum(len(json.dumps(tool.spec.to_schema().input_schema)) + len(tool.spec.description) for tool in getattr(snapshot, "tools", ())) // 4,
                        "tool_count": status.tool_count,
                        "tools": names,
                        # Cost now: this server's index lines, plus its schemas when they are sent in full.
                        "context_tokens": (
                            sum(len(line) for line in parts.get("mcp_index", "").splitlines() if status.name in line) // 4
                            + (sum(len(json.dumps(tool.spec.to_schema().input_schema)) + len(tool.spec.description)
                                   for tool in getattr(snapshot, "tools", ())) // 4
                               if status.enabled and status.name not in session.disabled_extensions["mcp"]
                               and (session.mcp_loading_frozen.get(status.name, "search") if session.mcp_loading_frozen is not None
                                    else session.mcp_loading_choices.get(status.name, getattr(snapshot, "tool_loading", "search"))) == "all"
                               else 0)
                        ),
                        "resource_count": len(getattr(snapshot, "resources", ()) or ()),
                        "prompt_count": len(getattr(snapshot, "prompts", ()) or ()),
                        "error": redact_secrets(self._mcp.redact_display(str(getattr(snapshot, "error", "") or "")))[:300],
                    })
            if self._mcp is not None:
                # Entries that could not be parsed, and files that could not be
                # read, are rows too: a broken server is shown, never hidden.
                scopes = getattr(self._extensions, "mcp_scopes", {})
                sources = getattr(self._extensions, "mcp_sources", {})
                for failure in tuple(getattr(self._mcp, "failures", ()))[:64]:
                    mcp_servers.append({
                        "name": failure.name, "status": "invalid", "invalid": True,
                        "scope": scopes.get(failure.name, "project"), "source_path": sources.get(failure.name, ""),
                        "transport": "", "command_label": "", "config_enabled": True, "enabled": False,
                        "tool_loading": "search", "tool_loading_source": "default", "config_tool_loading": "search",
                        "schema_tokens": 0, "tool_count": 0, "tools": [], "context_tokens": 0,
                        "resource_count": 0, "prompt_count": 0,
                        "error": redact_secrets(self._mcp.redact_display(failure.error))[:300],
                    })
                for problem in tuple(getattr(self._extensions, "mcp_file_errors", ()))[:8]:
                    mcp_servers.append({
                        "name": problem["path"], "status": "warning" if problem.get("warning") else "invalid file",
                        "invalid": True, "file_error": True, "scope": problem["scope"], "source_path": problem["path"],
                        "transport": "", "command_label": "", "config_enabled": True, "enabled": False,
                        "tool_loading": "search", "tool_loading_source": "default", "config_tool_loading": "search",
                        "schema_tokens": 0, "tool_count": 0, "tools": [], "context_tokens": 0,
                        "resource_count": 0, "prompt_count": 0,
                        "error": redact_secrets(self._mcp.redact_display(str(problem["error"])))[:300],
                    })
            return {
                "context_locked": session.context_locked,
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
                        "enabled": getattr(entry, "name", "").casefold() not in session.disabled_extensions["skills"],
                        "scope": "project" if "workspace" in str(getattr(getattr(entry, "provenance", None), "tier", "")).lower() else "global",
                        "origin": str(getattr(getattr(entry, "provenance", None), "relpath", "")),
                        "frontmatter": skill_frontmatter(entry),
                        "index_line": skill_index_line(getattr(entry, "name", "")),
                        "context_tokens": len(skill_index_line(getattr(entry, "name", ""))) // 4,
                        # Recorded sizes only: no file is read during preview.
                        "skill_tokens": int(getattr(getattr(entry, "provenance", None), "file_size", 0) or 0) // 4,
                        "resources": len(getattr(entry, "resources", ()) or ()),
                    }
                    for entry in skills_snapshot[:512]
                ],
                "mcp_index": parts.get("mcp_index", ""),
                "mcp_servers": mcp_servers,
                "tools": [
                    tool_row(schema, enabled=True, config_enabled=True)
                    for schema in request.tools[:512]
                ] + [
                    tool_row(
                        tool_definitions[name].to_schema(),
                        enabled=False,
                        config_enabled=name not in unavailable_tools,
                    )
                    for name in sorted(set(session.disabled_extensions.get("tools", ())) | set(unavailable_tools))[:512]
                    if name in selectable_tools and name not in enabled_names and name in tool_definitions
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
        handle = self._sessions.open(session_id, create=create, recover=recover)
        if getattr(handle, "_manager_compaction", False) is None:  # an injected SessionManager
            handle.bind(manager_compaction=self._manager_compaction_policy)
        return handle

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
        if _is_manager_session(handle):
            raise ConfigError(f"the manager model is pinned to {MANAGER_MODEL_REF} and cannot be changed")
        handle.select_model(selection)
        remembered = self._model_efforts.get(f"{selection.provider}/{selection.model}")
        if remembered is not None:
            effort = remembered[0]
            if effort is not None and effort in self.root_reasoning_effort_metadata(handle)["supported_levels"]:
                handle.select_reasoning_effort(ReasoningEffortSelection(effort=effort))
            else:
                handle.clear_reasoning_effort()
        self.remember_agent_choice(handle)
        return selection

    def remember_agent_choice(self, handle: Session) -> None:
        """Record the session's model and effort as the last choice for its agent."""
        selection = getattr(handle, "model_selection", None)
        if selection is None:
            return
        effort = getattr(getattr(handle, "reasoning_effort_selection", None), "effort", None)
        self._agent_choices.put(self.effective_session_agent(handle)[0], selection.reference, effort)

    def remember_model_effort(self, handle: Session) -> None:
        """Remember explicit effort choices independently of the selected agent."""
        selection = getattr(handle, "model_selection", None)
        if selection is not None:
            ref = f"{selection.provider}/{selection.model}"
        else:
            route = self.root_route_metadata(handle)
            if not route["provider"] or not route["model"]:
                return
            ref = f"{route['provider']}/{route['model']}"
        effort = getattr(getattr(handle, "reasoning_effort_selection", None), "effort", None)
        self._model_efforts.put(ref, effort)

    def remembered_model_effort(self, provider: str, model: str) -> tuple[str | None] | None:
        """Descriptive per-model preference for host model-picker rows."""
        return self._model_efforts.get(f"{provider}/{model}")

    def _restore_agent_choice(self, handle: Session) -> None:
        """Re-apply the model and effort last chosen for the session's agent."""
        remembered = self._agent_choices.get(self.effective_session_agent(handle)[0])
        if remembered is None:
            return
        model, effort = remembered
        try:
            handle.select_model(self._validate_model_selection(model))
        except ConfigError:
            return  # the model is gone (provider removed); keep the current one
        if effort is not None and effort in self.root_reasoning_effort_metadata(handle)["supported_levels"]:
            handle.select_reasoning_effort(ReasoningEffortSelection(effort=effort))
        else:
            handle.clear_reasoning_effort()

    def _selected_skills(self, session):
        manager = self._skills
        disabled = getattr(session, "disabled_extensions", {}).get("skills", ())

        class SelectedSkills:
            def get(self, name):
                return None if name.casefold() in disabled else manager.get(name)

            def __getattr__(self, name):
                return getattr(manager, name)

        return SelectedSkills()

    def _selected_manifest(self, manifest, session):
        disabled = getattr(session, "disabled_extensions", {})
        skills = disabled.get("skills", ())
        servers = disabled.get("mcp", ())
        off_tools = disabled.get("tools", ())
        if not skills and not servers and not off_tools and not manifest.mcp:
            return manifest
        disabled_tools = {
            tool.name for name, server in manifest.mcp.items() if name in servers
            for tool in getattr(server, "tools", ())
        } | set(off_tools)
        tools = {name: tool for name, tool in manifest.tools.items() if name not in disabled_tools}
        choices = getattr(session, "mcp_loading_choices", {})
        frozen = getattr(session, "mcp_loading_frozen", None)
        modes = {name: (frozen.get(name, "search") if frozen is not None else
                       choices.get(name, getattr(value, "tool_loading", "search")))
                 for name, value in manifest.mcp.items()}
        if frozen is None and hasattr(session, "prepare_mcp_loading"):
            session.prepare_mcp_loading(modes)
        selected_servers = {
            name: (replace(value, tool_loading=modes[name],
                           tool_loading_source="session" if name in choices else getattr(value, "tool_loading_source", "default"))
                   if not isinstance(value, Mapping) else
                   {**value, "tool_loading": modes[name], "tool_loading_source": "session" if name in choices else "default"})
            for name, value in manifest.mcp.items() if name not in servers}
        enabled_servers = ({status.name for status in self._mcp.statuses() if status.enabled}
                           if self._mcp is not None else set(selected_servers))
        search_servers = {name: value for name, value in selected_servers.items()
                          if modes[name] == "search" and name in enabled_servers}
        for server in search_servers.values():
            for target in getattr(server, "tools", ()):
                tools.pop(target.name, None)
        if search_servers and self._mcp is not None:
            from .mcp.bridge import build_search_tools

            for tool in build_search_tools(self._mcp, modes, disabled=servers,
                    allowed={target.name for server in search_servers.values() for target in getattr(server, "tools", ())
                             if target.name not in off_tools}):
                if tool.name not in off_tools:
                    tools[tool.name] = tool
        if not any(getattr(server, "resources", ()) or getattr(server, "resource_templates", ()) for server in selected_servers.values()):
            tools.pop("ReadMcpResource", None)
        resource_tool = tools.get("ReadMcpResource")
        if resource_tool is not None and servers:
            async def read_resource(arguments, context):
                if arguments.get("server") in servers:
                    from .tools.spec import ToolExecutionResult
                    return ToolExecutionResult.text("This MCP server is switched off for this session.", is_error=True)
                return await resource_tool.run(arguments, context)

            tools["ReadMcpResource"] = replace(resource_tool, run=read_resource)
        return msgspec.structs.replace(
            manifest,
            skills={name: value for name, value in manifest.skills.items() if name.casefold() not in skills},
            skill_tools={name: value for name, value in manifest.skill_tools.items() if name.casefold() not in skills},
            mcp=selected_servers,
            tools=tools,
        )

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
        if _is_manager_session(handle):
            raise ConfigError("the manager session has a packaged role that cannot be changed")
        if handle.context_locked:
            raise ConfigError("Agents are locked after the first turn to preserve the prompt cache. Start a new session to change agents.")
        if name is None:
            handle.reset_agent()
            self._restore_agent_choice(handle)
            return self.effective_session_agent(handle)
        handle.select_agent(AgentSelection(name=definition.name))
        self._restore_agent_choice(handle)
        return definition.name, "session"

    def effective_session_agent(self, handle: Session) -> tuple[str, str]:
        selection = getattr(handle, "agent_selection", None)
        if selection is not None:
            return selection.name or "orchestrator", "session"
        return self.default_root_agent(), "config"

    def default_root_agent(self) -> str:
        """The root agent a session without a selection runs: ``[agent] name``.

        New sessions start with it (``orchestrator`` unless configured otherwise).
        Read from disk on each call, so a Settings change applies at once.
        """
        config = self._load_config()
        name = getattr(getattr(getattr(config, "v2", None), "agent", None), "name", "orchestrator")
        if self._agents is not None:
            try:
                name = self._agents.resolve(name, context="root").name
            except Exception:  # noqa: BLE001, S110 - an unresolvable name is still reported
                pass
        return str(name or "orchestrator")

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
        sections = getattr(getattr(self._load_config(), "v2", None), "providers", {}) or {}
        head = ref.partition("/")[0]
        if self._adapter_kind(head, sections.get(head)) == ADAPTER_ANTIGRAVITY:
            raise ConfigError("Antigravity is Subagents only; choose it in a subagent tier or Task, not as the main model")
        if ref in self._tiers.order:
            raise ConfigError("tiers are for subagents only; choose a provider/model id")
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

    def _session_root_grants(self, session, root, manager):
        """Replay exact file grants by relative path within this session only.

        The root_set history identifies the checkout an audited canonical grant
        belonged to. Shell/MCP keys and explicitly absolute glob rules retain
        their exact meaning. Hard PathGuard checks still precede every match.
        """
        from .session.root import SessionRoot
        source = SessionRoot.main(self.workspace)
        path_tools = {canonical_tool_name(spec.name) for spec in manager.specs
                      if spec.path_mode or permission_bundle_matches("fs", spec.bundle)}
        grants = []
        for event in session.events:
            if event.type == "session.root_set":
                source = SessionRoot.from_data(self.workspace, event.data)
            elif event.type == "permission.resolved":
                for grant in _grants_from_events((event,)):
                    rule = grant.compiled()
                    if rule.kind == "tool_exact" and rule.tool in path_tools and rule.pattern:
                        try:
                            relative = Path(rule.pattern).relative_to(source.path)
                        except ValueError:
                            pass
                        else:
                            if ".." not in relative.parts:
                                grant = replace(grant, rule=exact_rule(rule.tool, str(root.path / relative)))
                    grants.append(grant)
        return tuple(grants)

    def session_root(self, session: str):
        """Replay the last durable root selection; an absent selection is main.

        The index contains only sessions actually opened by this runtime and is
        bounded independently of the number of durable historical sessions.
        """
        from .session.root import SessionRoot
        handle = self.sessions.open(session, create=False, recover=False)
        roots = self._session_roots
        row = handle._store.session_row(session)
        revision = (row.get("created_at"), row.get("last_seq", 0)) if row else (None, 0)
        cached = roots.get(session)
        if cached is not None and cached[0] == revision:
            roots.move_to_end(session)
            return cached[1]
        root = SessionRoot.main(self.workspace)
        for event in handle.read(force=True).events():
            if event.type == "session.root_set":
                root = SessionRoot.from_data(self.workspace, event.data)
        roots[session] = (revision, root)
        while len(roots) > 512:
            roots.popitem(last=False)
        return root

    def _validate_linked_root(self, path: Path):
        from .host_support.project_identity import resolve_project
        from .host_support.git_head import _read
        identity = resolve_project(path)
        if identity.root != self.workspace or identity.launch_worktree != str(path):
            raise ValueError("Session root must be a linked worktree of this project")
        # Authenticate the reverse registration too: copying another checkout's
        # .git pointer must not grant an arbitrary directory worktree authority.
        pointer = _read(path / ".git").strip()
        gitdir = (path / pointer.removeprefix("gitdir:").strip()).resolve()
        registration = (gitdir / _read(gitdir / "gitdir").strip()).resolve()
        if registration != (path / ".git").resolve():
            raise ValueError("Session root is not the registered linked worktree")
        return identity

    def validate_session_root(self, session: str):
        """Fail closed before running tools in a missing or replaced checkout."""
        root = self.session_root(session)
        if not self.workspace.is_dir():
            raise ValueError("Project root is missing; sessions are read-only")
        if not root.path.is_dir():
            raise ValueError(f"Session worktree {root.to_data(self.workspace)['path_display']} no longer exists")
        if root.kind != "main":
            self._validate_linked_root(root.path)
            if root.kind == "worktree":
                # A worktree belongs to the project: any of its sessions may work
                # in it, so the record must belong to this project, not to a session.
                record = self._worktree_service.get_worktree(
                    root.worktree_id, root=self._worktree_root, parent_workspace=self.workspace)
                if record.lifecycle != "active" or Path(record.path).resolve() != root.path:
                    raise ValueError("Session worktree is not active; move to the main checkout")
        return root

    def session_path_guard(self, session: str, *, config=None):
        """Project authority, rebased within the session's hard write boundary."""
        root = self.validate_session_root(session)
        config = config or self._load_config()
        permissions = getattr(getattr(config, "v2", None), "permissions", None)
        guard = PathGuard(self.workspace,
            write_roots=tuple(permissions.write_roots) if permissions is not None else ("./",),
            read_denyroots=tuple(permissions.read_denyroots) if permissions is not None else (),
            home=self._home)
        return guard if root.kind == "main" else guard.for_worktree(root.path)

    def initialize_session_cwd(self, session: str, directory: Path):
        """Preserve an in-root launch directory for a newly opened session."""
        from .tools.builtin._shell_logs import read_session_cwd, write_session_cwd
        root = self.validate_session_root(session)
        directory = Path(directory).resolve()
        if (directory != root.path and directory.is_relative_to(root.path)
                and read_session_cwd(self.workspace, session, home=self._home) in (None, str(root.path))):
            try:
                write_session_cwd(self.workspace, session, str(directory), home=self._home)
            except OSError:
                self.sessions.open(session, create=False, recover=False).emit_session_event(
                    "context.notice", {"kind": "session_root_cwd",
                    "message": "Session opened; the launch directory could not be saved as shell cwd. Use shell cd to select it."})

    def set_session_root(self, session: str, path, *, kind="external", held=False, event_extra=None, **metadata):
        """Persist a root change under the session lock at a turn boundary.

        External roots must already be linked worktrees of the same project.
        ``held`` is for the owner of the session lock (the running turn applying
        a queued move at a step boundary, or an idle host action that already
        holds the lock): it skips the idle checks and the lock, and the event
        joins the running turn. ``event_extra`` (reason, request_id, action) is
        merged into the durable ``session.root_set`` data.
        """
        from .errors import SessionBusy
        from .session.root import SessionRoot
        handle = self.sessions.open(session, create=False, recover=False)
        if not held:
            if handle.active or handle.queued_ids or self.session_has_active_jobs(session):
                raise SessionBusy("Session root can only change at a turn boundary with no jobs or queued inputs")
            running_shells = set()
            for event in handle.events:
                if event.type == "shell.started":
                    running_shells.add(event.data.get("shell_id"))
                elif event.type == "shell.completed":
                    running_shells.discard(event.data.get("shell_id"))
            if running_shells:
                raise SessionBusy("Session root cannot change while a composer shell command is running")
        target = Path(path).resolve()
        if kind == "main":
            if target != self.workspace:
                raise ValueError("Main session root must be the project root")
            root = SessionRoot.main(self.workspace)
        else:
            self._validate_linked_root(target)
            from .host_support.git_head import git_head
            metadata.setdefault("branch", git_head(target).get("branch", ""))
            root = SessionRoot(kind=kind, path=target, project_key=project_key(self.workspace), **metadata)
        if not held:
            handle._lock.acquire(shared=False, blocking=False)
        try:
            if not held and self.sessions._has_pending_work(session):
                raise SessionBusy("Session root requires a turn boundary without queued inputs or pending work")
            data = {**root.to_data(self.workspace), **(event_extra or {})}
            def emit(kind, payload):
                if held:  # joins the running turn's stream when there is one
                    self._session_worktrees._event(handle, kind, payload)
                else:
                    handle.emit_session_event(kind, payload)
            emit("session.root_set", data)
            self._session_worktrees._occupancy_cache = (0.0, {})
            handle._turn_root_agents_key = None
            from .tools.builtin._shell_logs import write_session_cwd
            try:
                write_session_cwd(self.workspace, session, str(root.path), home=self._home)
            except OSError:
                emit("context.notice", {"kind": "session_root_cwd",
                    "message": "Working root selected; shell cwd could not be reset. Use shell cd '.' before running commands."})
            row = handle._store.session_row(session)
            revision = (row.get("created_at"), row.get("last_seq", 0)) if row else (None, 0)
            self._session_roots[session] = (revision, root)
            self._session_roots.move_to_end(session)
            while len(self._session_roots) > 512:
                self._session_roots.popitem(last=False)
        finally:
            if not held:
                handle._lock.release()
        return root

    def _session_worktree_tools(self, session):
        return self._session_worktrees.tools(session)

    def _worktree_merge_policy(self):
        """What the permission engine trusts for the agent's own ``git -C <main> merge --ff-only``."""
        if not (self.workspace / ".git").exists():
            return None
        from .tools.permissions import WorktreeMergePolicy
        controller = self._session_worktrees
        mains = {os.path.normpath(str(self.workspace)), os.path.normpath(str(self.workspace.resolve()))}
        return WorktreeMergePolicy(frozenset(mains), controller.is_project_branch, controller.is_base_branch)

    def _session_worktree_facts(self, session_id):
        """Labelled facts for the model's ``<session_worktree>`` block; () outside a Git project."""
        try:
            return self._session_worktrees.environment_facts(session_id)
        except Exception:
            return ()

    def list_session_worktrees(self, session):
        """The project's Nexus worktrees and main checkout as seen from ``session`` (read-only, bounded).

        The same rows the model gets from ``worktree_list``: per worktree name, branch, base,
        dirty, merged/unmerged, the sessions in it and the current marker.
        """
        return self._session_worktrees.list(session)

    async def move_session_worktree(self, session, to, reason=""):
        """Move an idle session to ``"main"`` or an existing worktree by name (user override).

        The agent's own moves go through ``worktree_move`` and apply mid-turn at a step boundary.
        """
        return await self._session_worktrees.move_session(session, to, reason=reason)

    def session_worktree_status(self, session):
        return self._session_worktrees.status(session)

    def approve_worktree_setup(self, session, decision, *, digest=None):
        return self._session_worktrees.approve(session, decision, digest=digest)

    async def _ask_worktree_setup(self, handle, policy, digest):
        from types import SimpleNamespace
        from uuid import uuid4
        from .tools.permissions import PermissionRequest
        gate = _PermissionGateAdapter(PermissionEngine(mode="ask", on_unattended="deny"),
            broker=ApprovalBroker(scope="project"), attended=True)
        command = policy.setup if isinstance(policy.setup, str) else " ".join(policy.setup or ())
        preview = ("This project runs worktree setup: " + command +
            f"\nSubmodules: {policy.submodules}\nAllow always approves this exact setup for this project; Deny skips it.")
        request = PermissionRequest(id=str(uuid4()), call_id="worktree-setup-" + digest[:12],
            tool="worktree_setup", key=digest, bundle="task", preview=redact_secrets(preview)[:8192],
            default_rule=exact_rule("worktree_setup", digest))
        previous = handle._active_tools
        handle._active_tools = SimpleNamespace(gate=gate)
        gate.open(request)
        try:
            self._session_worktrees._emit(handle, "permission.requested", request.to_dict(), handle.active_turn_id)
            decision = Decision.from_value(await gate.await_decision(request, cancel=handle.cancel_token))
            self._session_worktrees._emit(handle, "permission.resolved", dict(gate.resolution(request.id) or request.to_dict()), handle.active_turn_id)
            if decision.allows:
                mode = "allow_project" if decision is Decision.ALLOW_ALWAYS else "allow_once"
                self._session_worktrees.approve(handle.id, mode, digest=digest, _during_setup=True)
                return True
            return False
        finally:
            gate.cancel_pending()
            handle._active_tools = previous

    async def promote_session(self, session, *, reason="", continuation="", name="", base=""):
        """Create a worktree for an idle session and move into it (user override / launch).

        ``name`` is an exact worktree name (also the session title); ``continuation`` is
        queued after the move.
        """
        return await self._session_worktrees.promote(
            session, reason=reason, continuation=continuation, name=name, base=base)

    async def detach_session_worktree(self, session):
        """Move an idle session back to the main checkout (the branch is kept)."""
        return await self._session_worktrees.detach(session)

    def session_has_active_jobs(self, session_id: str) -> bool:
        """Whether ``session_id`` still owns a running shell job (this runtime only).

        The session manager's eviction/archive guard: a job outlives its tool
        call, so its session must stay open until the job ends or is stopped.
        """
        active = getattr(self._job_registry, "active", None)
        if not callable(active) or not isinstance(session_id, str):
            return False
        return bool(active(session_id=session_id))

    def shell_job_may_be_live(self, session_id: str, job_id: str) -> bool:
        """Whether a started ``shell`` job may still be running (fail closed).

        The session manager's probe for restart recovery: ``False`` only when this
        runtime's registry proves nobody owns the job any more. A registry without
        recovery support (injected, or no state directory) answers ``True``.
        """
        probe = getattr(self._job_registry, "job_may_be_live", None)
        if not callable(probe):
            return True
        return bool(probe(session_id, job_id))

    async def recover_shell_orphans(self) -> dict[str, int] | None:
        """Stop the process groups a dead daemon left behind (call once at start).

        Verified, bounded and best effort (see ``tools/builtin/_job_recovery.py``).
        Returns plain counts for the daemon log, or ``None`` when recovery is off.
        """
        recover = getattr(self._job_registry, "recover_orphans", None)
        if not callable(recover):
            return None
        result = await recover()
        if result is None:
            return None
        return {
            "live_owners": result.live_owners,
            "dead_owners": result.dead_owners,
            "invalid": result.invalid,
            "jobs": result.jobs,
            "gone": result.gone,
            "terminated": result.terminated,
            "killed": result.killed,
            "failed": result.failed,
            "unverified": result.unverified,
        }

    def has_active_shell_jobs(self) -> bool:
        """Whether any session has a running shell job (the daemon idle guard)."""
        probe = getattr(self._job_registry, "has_active", None)
        return bool(probe()) if callable(probe) else False

    def persistent_shell_jobs(self, limit: int = 16) -> tuple[int, list[dict[str, Any]]]:
        """``(total, rows)`` for the running persistent jobs (daemon status, orphan policy).

        Plain, bounded rows: ids, a short redacted command, cwd, pid, start time.
        """
        probe = getattr(self._job_registry, "persistent_jobs", None)
        if not callable(probe):
            return 0, []
        jobs = probe()
        rows = [
            {
                "session": key,
                "job_id": job.job_id,
                "command": redact_secrets(job.command)[:120],
                "cwd": str(job.cwd),
                "pid": job.pid,
                "started_at": job.created_at,
            }
            for key, job in jobs[:limit]
        ]
        return len(jobs), rows

    def shell_orphan_seconds(self) -> float:
        """``tools.shell_orphan_s``: idle seconds before persistent jobs are stopped (0 = never)."""
        try:
            value = float(self._load_config().v2.tools.shell_orphan_s)
        except Exception:  # noqa: BLE001 - an unreadable config must not stop jobs
            return 0.0
        return value if value > 0 else 0.0

    async def stop_persistent_shell_jobs(self, reason: str) -> int:
        """Stop every running persistent job, recording ``reason`` on its exit event."""
        stopper = getattr(self._job_registry, "stop_persistent", None)
        return int(await stopper(reason)) if callable(stopper) else 0

    def _emit_job_event(
        self, session_id: str, event_type: str, data: dict[str, Any]
    ) -> None:
        """Registry sink: record a ``shell.job_*`` event on the owning live session.

        Best effort by design. A session with no live handle (a sub-agent's, a
        deleted one) records nothing; the registry itself stays the live truth.
        """
        emit = getattr(self._sessions, "emit_session_event", None)
        if not callable(emit):
            return
        with contextlib.suppress(Exception):
            emit(session_id, event_type, data)

    #: Host seam for a recorded completion: ``(session_id, admission)``. The host
    #: facade installs it to start a turn for an idle session; without one the
    #: notification simply waits in the session queue for the next turn.
    job_completion_notifier: Callable[[str, Any], None] | None = None

    def _deliver_job_completion(
        self, session_id: str, data: dict[str, Any], text: str
    ) -> bool:
        """Registry sink: record the exit and queue its notification atomically.

        Returns whether the owning live session took it. ``False`` makes the
        registry record the exit as a plain ``shell.job_exited`` instead.
        """
        record = getattr(self._sessions, "record_job_completion", None)
        if not callable(record):
            return False
        try:
            limit = int(self._load_config().v2.tools.shell_max_auto_wakes)
        except Exception:  # noqa: BLE001 - fall back to the default cap
            limit = 10
        admission = record(session_id, data, text, max_auto_wakes=limit)
        if admission is None:
            return False
        notifier = self.job_completion_notifier
        if notifier is not None and getattr(admission, "wake", False):
            with contextlib.suppress(Exception):
                notifier(session_id, admission)
        return True

    def _retract_job_completion(self, session_id: str, job_id: str) -> None:
        retract = getattr(self._sessions, "retract_job_completion", None)
        if callable(retract):
            with contextlib.suppress(Exception):
                retract(session_id, job_id)

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

        The session's shell jobs are stopped **first**: their ``shell.job_exited``
        events need the live handle, and the manager refuses to evict a session
        that still has running jobs.

        A session the manager would refuse to close (active turn, live viewers)
        raises ``SessionBusy`` **before** any job is stopped, so a refused close
        leaves its jobs running.
        """
        precheck = getattr(self._sessions, "ensure_closable", None)
        if callable(precheck):
            precheck(session_id)
        await self.close_session_jobs(session_id)
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
        self.agent_pool.discard_if_idle(session_id)
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

    def _manager_section(self) -> Any:
        """The effective ``[manager]`` section, or ``None`` when the config is unreadable."""
        try:
            return getattr(getattr(self._load_config(), "v2", None), "manager", None)
        except Exception:  # noqa: BLE001 - a config problem never blocks a manager turn
            return None

    def _manager_compaction_policy(self) -> tuple[bool, float]:
        """``(enabled, idle_seconds)`` for the Manager's lazy compaction (plan 10)."""
        section = self._manager_section()
        if section is None:
            return False, 0.0
        enabled = bool(section.enabled and section.auto_compaction)
        return enabled, float(section.idle_compaction_minutes) * 60.0

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

    def auto_archive_idle_seconds(self) -> int:
        """Idle seconds before a session is auto-archived; ``0`` means disabled.

        The legacy ``auto_archive_days = 0`` still disables the sweep.
        """
        section = getattr(getattr(self._load_config(), "v2", None), "sessions", None)
        days = getattr(section, "auto_archive_days", 2)
        seconds = getattr(section, "auto_archive_idle_seconds", 3600)
        if days == 0:
            return 0
        if type(seconds) is int and 0 <= seconds <= 3650 * 86400:
            return seconds
        return 3600

    def usage_refresh_seconds(self) -> int:
        """Seconds between background plan-usage reads, from live config (so a Settings save applies at once)."""
        from .config.schema import (
            DEFAULT_USAGE_REFRESH_SECONDS,
            MAX_USAGE_REFRESH_SECONDS,
            MIN_USAGE_REFRESH_SECONDS,
        )

        try:
            section = getattr(getattr(self._load_config(), "v2", None), "models", None)
            value = getattr(section, "usage_refresh_seconds", DEFAULT_USAGE_REFRESH_SECONDS)
        except Exception:  # noqa: BLE001 - a bad config falls back to the default cadence
            return DEFAULT_USAGE_REFRESH_SECONDS
        if type(value) is int and MIN_USAGE_REFRESH_SECONDS <= value <= MAX_USAGE_REFRESH_SECONDS:
            return value
        return DEFAULT_USAGE_REFRESH_SECONDS

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
        if _is_manager_session(session):
            # Explicit manager branch, before any injected factory, manager or
            # engine: a manager turn gets its own eight-tool catalog and a fresh
            # permission engine, never the runtime's shared ones.
            return self._make_manager_tool_turn(
                config=config, session=session, turn_id=turn_id, attended=attended
            )
        if self._tool_factory is not None:
            return self._tool_factory(
                config=config,
                session=session,
                turn_id=turn_id,
                attended=attended,
            )
        if config is None:
            config = self._load_config()
        root = self.validate_session_root(session.id)
        workspace = root.path
        root_guard = self.session_path_guard(session.id, config=config)
        if root.kind != "main":
            config = self._child_workspace_config(config, root_guard)
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
                    workspace=workspace,
                    tools=filtered,
                    job_registry=self._job_registry,                    path_guard=manager.path_guard,
                )
                self._track_tool_manager(manager)
        if manager is None:
            catalog = self._filter_web_catalog(config, (*ToolManager._builtin_catalog(), *self._session_worktree_tools(session.id)))
            if self._agents is not None:
                from .tools.builtin.task import build_task_tools

                catalog = (
                    tuple(tool for tool in catalog if tool.bundle != "meta")
                    + build_task_tools(None)
                )
            manager = ToolManager(
                config,
                workspace=workspace,
                tools=catalog,
                job_registry=self._job_registry,            )
            self._track_tool_manager(manager)
        if (
            agent_definition is not None
            and "subagent" not in manager.names
            and self._agents is not None
        ):
            from .tools.builtin.task import build_task_tools

            original_names = manager.names
            manager = ToolManager(
                config,
                workspace=workspace,
                tools=(*manager.tools, *build_task_tools(None)),
                restrict=(*original_names, "subagent", "subagent_continue"),
                job_registry=self._job_registry,                path_guard=manager.path_guard,
            )
            self._track_tool_manager(manager)
        if agent_definition is not None:
            from .tools.bundles import BUNDLES

            profile = (
                manager.authority_for_profile(agent_definition.profile)
                if agent_definition.profile
                else manager.authority_names
            )
            selected = self._agents.select_tools(
                agent_definition,
                available=manager.authority_names,
                profile=profile,
                bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                mutating=manager.authority_mutating,
            )
            if self._extensions is None:
                manager = ToolManager(
                    config,
                    workspace=workspace,
                    tools=manager.tools,
                    restrict=tuple(name for name in manager.names if name in selected.selected),
                    job_registry=self._job_registry,                    path_guard=manager.path_guard,
                )
                self._track_tool_manager(manager)
            else:
                catalog = self._filter_web_catalog(
                    config, manager.tools, add_available=False
                )
                # Code mode registers its tool inside the iteration manager, so
                # the builtin catalog above never lists it. Select against an
                # unrestricted iteration manager, as the per-iteration factory
                # does, or ``code`` is dropped before it can be registered.
                candidate = self._build_iteration_manager(
                    config,
                    self.manifest,
                    catalog=catalog,
                    path_guard=manager.path_guard,
                )
                selected = self._agents.select_tools(
                    agent_definition,
                    available=candidate.authority_names,
                    profile=(
                        candidate.authority_for_profile(agent_definition.profile)
                        if agent_definition.profile
                        else candidate.authority_names
                    ),
                    bundle_map={name: bundle.tools for name, bundle in BUNDLES.items()},
                    mutating=candidate.authority_mutating,
                )
                manager = self._build_iteration_manager(
                    config,
                    self.manifest,
                    catalog=catalog,
                    restrict=tuple(
                        name for name in candidate.names if name in selected.selected
                    ),
                    path_guard=manager.path_guard,
                )
        if root.kind != "main":
            baseline = manager.path_guard
            baseline = baseline if baseline.workspace == workspace else baseline.for_worktree(workspace)
            intersections = []
            for allowed in root_guard.write_roots:
                for restricted in baseline.write_roots:
                    if allowed.is_relative_to(restricted):
                        intersections.append(str(allowed))
                    elif restricted.is_relative_to(allowed):
                        intersections.append(str(restricted))
            root_guard = PathGuard(workspace, write_roots=tuple(dict.fromkeys(intersections)),
                read_denyroots=tuple(str(path) for path in dict.fromkeys(
                    (*root_guard.read_denyroots, *baseline.read_denyroots))),
                home=self._home, _allow_empty_write_roots=True, _worktree_boundary=True)
            manager = ToolManager(config, workspace=workspace, tools=manager.tools,
                                  restrict=manager.names, job_registry=self._job_registry,
                                  path_guard=root_guard)
            self._track_tool_manager(manager)
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
        if engine is not None and root.kind != "main":
            engine = PermissionEngine(mode=engine.mode,
                allow=tuple(rule.raw for rule in engine._allow),
                ask=tuple(rule.raw for rule in engine._ask),
                deny=tuple(rule.raw for rule in engine._deny),
                on_unattended=engine.on_unattended, path_guard=root_guard)
        if engine is None:
            permissions = getattr(getattr(config, "v2", None), "permissions", None)
            if permissions is None:
                engine = PermissionEngine(workspace=workspace, home=self._home)
            else:
                engine = PermissionEngine.from_config(
                    permissions, workspace=workspace, home=self._home
                )
        if (
            agent_definition is not None
            and getattr(agent_definition, "write_roots", ())
            and getattr(getattr(getattr(config, "v2", None), "settings", None), "confirm_edits", False)
        ):
            engine.require_confirmation_for(
                spec.name for spec in manager.specs if getattr(spec, "mutates", False)
            )
        engine.allow_worktree_merge(self._worktree_merge_policy())
        if root.kind != "main":
            engine.path_guard = manager.path_guard
        grants = self._session_root_grants(session, root, manager)
        gate = _PermissionGateAdapter(
            engine, manager=manager, grants=grants, attended=attended
        )
        hooks_service = None
        current_manifest = self.manifest
        if self._hooks is not None:
            hooks_service = _HookService(
                self._hooks,
                getattr(current_manifest, "hooks", None),
                workspace=workspace,
            )
        runner = None
        roster_digest = None
        if self._agents is not None and self._tools is None:
            authority = _ChildAuthority(engine=engine, path_guard=manager.path_guard)
            static_runner = self._make_subagent_runner(
                session_id=session.id,
                parent_tools=manager.authority_names,
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
                budget=self._turn_subagent_budget(session.id, turn_id, config),
                workspace=workspace,
                runtime_supports_workspace=self._supports_child_workspace(),
            )
            if static_runner is not None and "subagent" in manager.names:
                from .tools.builtin.task import build_continue_tool, build_task_tool

                roster_digest = _roster_digest(static_runner)
                manager = ToolManager(
                    config,
                    workspace=workspace,
                    tools=(
                        *(
                            build_task_tool(static_runner) if tool.name == "subagent"
                            else build_continue_tool(static_runner) if tool.name == "subagent_continue"
                            else tool
                            for tool in manager.tools
                        ),
                    ),
                    restrict=manager.names,
                    job_registry=self._job_registry,                    path_guard=manager.path_guard,
                )
                self._track_tool_manager(manager)
            runner = self._make_subagent_runner(
                session_id=session.id,
                parent_tools=manager.authority_names,
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
                budget=self._turn_subagent_budget(session.id, turn_id, config),
                workspace=workspace,
                runtime_supports_workspace=self._supports_child_workspace(),
            )
        gate.bind_manager(manager)
        # Track the manager actually returned for this turn, after all catalog
        # rebuilds. Intermediate snapshots above share runtime-owned services
        # and own no independent resources. An unchanged injected manager stays
        # caller-owned unless it was explicitly registered as owned at init.
        dispatcher = _ToolDispatcherAdapter(
            manager,
            workspace=workspace,
            session_id=session.id,
            turn_id=turn_id,
            config=config,
            project_root=self.workspace,
            record_write=self._session_worktrees.record_write,
            state_home=self._home,
            agent_id="root",
            skills=self._selected_skills(session),
            extensions=self._extensions,
            activations=self._activations,
            subagents=runner,
            outbound_http=self._outbound_http_service,
            local_search_http=self._local_search_http_service,
            questions=self._question_service(session.id),
            subagent_roster_digest=roster_digest,
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
                budget=self._turn_subagent_budget(session.id, turn_id, config),
                model_selection=model_selection,
                agent_definition=agent_definition,
            )
        if manifest_ref is not None:
            # Select the first-turn modes before the sink records turn.started.
            # The sink persists them through both live event streams.
            self._selected_manifest(manifest_ref.get(), session)
        return ToolTurn(
            manager=manager,
            engine=engine,
            dispatcher=dispatcher,
            gate=gate,
            schemas=tuple(manager.schemas()),
            manifest_ref=manifest_ref,
            environment_for=environment_for,
            hooks=hooks_service,
            step_boundary=lambda: self._session_worktrees.step_boundary(session),
        )

    def _make_manager_tool_turn(
        self,
        *,
        config: Config | None,
        session: Session,
        turn_id: str,
        attended: bool,
    ) -> ToolTurn:
        """Build the manager turn's tool environment (plan 02 M3, docs/manager.md).

        Structural: the catalog is :func:`build_manager_catalog` and nothing
        else, so no built-in, MCP, skill, extension or role tool and no hook can
        reach a manager turn. The permission engine is built fresh for the turn
        (mode ``deny``; exactly the manager catalog tools allowed by name, so
        launch, cancel, send and show run without approval, attended or not; any other
        tool name is denied). Session grants are never read, and the session's
        unattended fallback is pinned to deny for this turn regardless of
        ``permissions.on_unattended``.
        """
        if config is None:
            config = self._load_config()
        factory = self.manager_port_factory
        port = factory(session) if factory is not None else None
        if port is None:
            raise ConfigError("the manager session is unavailable: no manager port is configured")
        manager = ToolManager(
            config,
            workspace=self.workspace,
            profile=MANAGER_PROFILE,
            tools=build_manager_catalog(port),
            restrict=MANAGER_TOOL_NAMES,
            job_registry=self._job_registry,
        )
        self._track_tool_manager(manager)
        engine = PermissionEngine(
            mode="deny",
            allow=tuple(MANAGER_TOOL_NAMES),
            on_unattended="deny",
            workspace=self.workspace,
            home=self._home,
        )
        # Pinned for this turn regardless of ``permissions.on_unattended``; every
        # manager catalog tool is allowed by name so nothing in the catalog asks.
        session._turn_unattended = "deny"
        gate = _PermissionGateAdapter(engine, manager=manager, grants=(), attended=attended)
        dispatcher = _ToolDispatcherAdapter(
            manager,
            workspace=self.workspace,
            session_id=session.id,
            turn_id=turn_id,
            config=config,
            agent_id="root",
        )
        return ToolTurn(
            manager=manager,
            engine=engine,
            dispatcher=dispatcher,
            gate=gate,
            schemas=tuple(manager.schemas()),
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
            current.workspace,
            write_roots=[str(root) for root in intersections],
            read_denyroots=[str(root) for root in current.read_denyroots],
            home=home,
            _allow_empty_write_roots=True,
            _worktree_boundary=current.workspace != self.workspace,
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
        independently of the manifest when deriving the default catalog.
        Explicit catalogs are already selected and must not be expanded;
        config/service availability is still checked before advertisement.
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
        add_available = catalog is None
        if catalog is None:
            tools = getattr(manifest, "tools", None)
            catalog = tuple(tools.values()) if isinstance(tools, Mapping) else ()
        catalog = tuple(catalog)
        catalog = self._filter_web_catalog(config, catalog, add_available=add_available)
        manager = ToolManager(
            config,
            workspace=path_guard.workspace if path_guard is not None else self.workspace,
            tools=catalog,
            restrict=restrict,
            job_registry=self._job_registry,            path_guard=path_guard or PathGuard(
                self.workspace,
                write_roots=tuple(config.v2.permissions.write_roots) if config.v2 else ("./",),
                read_denyroots=tuple(config.v2.permissions.read_denyroots) if config.v2 else (),
                home=self._home,
            ),
        )
        code_config = getattr(getattr(config, "v2", None), "code_mode", None)
        if code_config is not None and code_config.enabled and manager.profile == "coding":
            from .tools.codemode.catalog import build_catalog
            from .tools.codemode.runner import Runner
            from .tools.codemode.tool import availability, register
            if availability()["available"] and (restrict is None or "code" in restrict):
                runner = getattr(self, "_code_runner", None)
                if runner is None:
                    runner = self._code_runner = Runner(
                        code_config.max_workers,
                        context_provider=self._code_context_provider(code_config),
                    )
                # ``manager.tools`` is already this iteration's selection: the
                # session's disabled tools/MCP servers were removed from the
                # catalog before the manager was built, and ``restrict`` carries
                # the skill/agent allowlist. The callable catalog is therefore
                # per iteration and never includes a tool disabled for the
                # session.
                callable_catalog = build_catalog(manager.tools, code_config.expose)
                options: dict[str, Any] = {}
                accepted = inspect.signature(register).parameters
                for name in ("inline_budget_tokens", "pinned"):
                    if name in accepted:
                        options[name] = getattr(code_config, name)
                code_tool = register(runner, callable_catalog, code_config,
                                     events=lambda sid: self._sessions.open(sid, create=False).events,
                                     context_session=lambda sid: self._sessions.open(sid, create=False),
                                     **options)
                manager = ToolManager(
                    config, workspace=path_guard.workspace if path_guard is not None else self.workspace, tools=(*manager.tools, code_tool),
                    restrict=restrict, job_registry=self._job_registry,
                    path_guard=manager.path_guard,
                )
        self._track_tool_manager(manager)
        return manager

    def _code_context_provider(self, code_config: Any) -> Any | None:
        """The ``context`` namespace provider for code mode, or ``None`` (off).

        Plan 03 sections 3.2 and 3.9. Roots only in v1; sub-calls run on the
        calling session's own resolved model unless ``sub_model`` is set.
        """
        if not getattr(code_config, "context", False) or self._sessions is None:
            return None
        from .session.context_port import ContextReadPort
        from .tools.codemode.context_ns import ContextLimits, ContextNamespaceProvider

        limits = ContextLimits(
            max_sub_calls=code_config.max_sub_calls,
            max_sub_input_tokens=code_config.max_sub_input_tokens,
            max_total_sub_tokens=code_config.max_total_sub_tokens,
            max_depth=code_config.max_depth,
            sub_model=code_config.sub_model,
        )
        provider = ContextNamespaceProvider(
            ContextReadPort(self._sessions),
            llm_factory=self._code_llm,
            budget_factory=self._code_budget,
            limits=limits,
        )
        return _RootOnlyContext(provider, self._sessions)

    def _code_llm(self, session_id: str) -> Any | None:
        """The session's own provider and model for ``llm()``; no fallback or upgrade."""
        config = self.effective_config()
        code_config = getattr(getattr(config, "v2", None), "code_mode", None)
        try:
            sub_model = getattr(code_config, "sub_model", None)
            if sub_model:
                resolved = self._router.resolve(ModelRequest(messages=[], model=sub_model))
                provider_name, model = resolved.provider.name, resolved.model
            else:
                session = self._sessions.open(session_id, create=False)
                provider_name, model, resolved = self._root_route(
                    session, config=config,
                    agent_definition=getattr(session, "_turn_agent_definition", None),
                    model_selection=None,
                )
            if resolved is None:
                return None
        except Exception:  # noqa: BLE001 - no resolvable model means llm() is unavailable
            return None
        from .tools.codemode.context_ns import ContextLlm

        return ContextLlm(provider=resolved.provider, model=model, sampling=None)

    def _code_budget(self, session_id: str) -> Any:
        """A bounded budget for one program's sub-calls (shared tree budget if configured)."""
        budget = self._new_subagent_budget(None)
        if budget is not None:
            return budget
        from .agents import SubagentBudget

        code_config = getattr(getattr(self.effective_config(), "v2", None), "code_mode", None)
        return SubagentBudget(
            max_concurrent=1,
            max_depth=int(getattr(code_config, "max_depth", 1)),
            max_fanout=int(getattr(code_config, "max_sub_calls", 16)),
            token_budget=int(getattr(code_config, "max_total_sub_tokens", 200_000)),
        )

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
        if manager._owns_job_registry:
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
                                 if self._adapter_kind(name, section) in (ADAPTER_CLAUDE_AGENT, ADAPTER_CLAUDE_CODE)})
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
            shared_cache=self._registry_cache,
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
            if normalized in _ANTIGRAVITY_KINDS:
                return ADAPTER_ANTIGRAVITY
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
        if kind == ADAPTER_ANTIGRAVITY:
            return None  # an external CLI run only by the subagent worker, never a Nexus provider
        if kind in (ADAPTER_CLAUDE_AGENT, ADAPTER_CLAUDE_CODE):
            from .model.providers.claude_agent import ClaudeAgentProvider

            self._validate_claude_section(name, section)
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
        if kind == ADAPTER_XAI:
            return self._construct_xai(name, section, api, auth, kwargs)
        if kind == ADAPTER_NVIDIA:
            return self._construct_nvidia(name, api, auth, kwargs)
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
                from .model.providers.openai import EndpointFallback, _is_opencode_host
                factory = self._api_key_auth_factory or StoredKeyAuth
                kwargs.update(api_key=None, auth_headers=factory(name, profile=getattr(section, "profile", None) or "default"))
                # OpenCode Go serves different models on /chat/completions and
                # /responses; remember the dialect the model accepts.
                if _is_opencode_host(base_url or ""):
                    kwargs["api_selector"] = EndpointFallback(api or "chat")
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

    def _construct_nvidia(self, name: str, api: Any, auth: Any, kwargs: dict[str, Any]) -> Provider:
        """Build the NVIDIA preset on Chat Completions.

        The key is a pasted one (``auth = "keychain"``), an ``api_key`` reference
        or, with neither, ``${env:NVIDIA_API_KEY}``. Other sign-ins are refused.
        """
        from .model.providers.nvidia import NvidiaProvider

        if auth not in (None, "keychain"):
            raise ConfigError(f"providers.{name}: NVIDIA connects with an API key "
                              "(NVIDIA_API_KEY or a key pasted in Settings → Providers)")
        if api not in (None, "chat"):
            raise ConfigError(f"providers.{name}: NVIDIA is served over Chat Completions; use api = \"chat\"")
        if auth == "keychain":
            from .auth.api_key import StoredKeyAuth
            factory = self._api_key_auth_factory or StoredKeyAuth
            kwargs.update(api_key=None, auth_headers=factory(name, profile="default"))
        provider = NvidiaProvider(api=api, **kwargs)
        provider.name = name
        return provider

    def _construct_xai(
        self, name: str, section: Any, api: Any, auth: Any, kwargs: dict[str, Any]
    ) -> Provider:
        """Build the xAI preset on the SuperGrok subscription sign-in.

        The subscription (``auth = "xai_oauth"``) is the only supported
        credential: API keys and every other sign-in are refused, so no key is
        ever read from config or the environment for xAI.
        """
        from .auth.xai import XAIHeaders, XAIOAuthManager
        from .model.providers.xai import XAIProvider

        if auth != "xai_oauth" or getattr(section, "api_key", None) is not None:
            raise ConfigError(f"providers.{name}: xAI connects only with a SuperGrok subscription "
                              "(auth = \"xai_oauth\"); sign in from Settings → Providers")
        factory = self._xai_auth_factory or XAIOAuthManager
        manager = factory(profile=getattr(section, "profile", None) or "default")
        kwargs.update(api_key=None, auth_headers=XAIHeaders(manager))
        provider = XAIProvider(api=api, **kwargs)
        provider.name = name
        return provider

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

    def claude_code_main_enabled(self) -> bool:
        # Read the effective config (a daemon holds no pinned ``_config``), so the
        # Settings toggle takes effect without a restart.
        try:
            config = self._load_config()
        except Exception:  # noqa: BLE001 - an unreadable config means "off"
            return False
        models = getattr(getattr(config, "v2", None), "models", None)
        return bool(getattr(models, "claude_code_main_enabled", False))

    @staticmethod
    def _validate_claude_section(name: str, section: Any) -> None:
        """Whitelist of fields a Claude subscription provider accepts.

        Only ``kind``, ``executable`` and ``timeout_seconds`` are common; the
        ``worker_*`` external-execution policy applies to ``claude-code`` only.
        """
        if any(getattr(section, field, None) is not None for field in
               ("api_key", "base_url", "api", "auth", "command", "args", "env", "inherit_env", "permission_policy", "profile")):
            raise ConfigError(
                f"{name} uses Claude Code subscription login; only executable, timeout_seconds "
                "and (claude-code only) worker_* fields are supported")
        if Runtime._adapter_kind(name, section) != ADAPTER_CLAUDE_CODE and any(
                getattr(section, field, None) is not None for field in _CLAUDE_WORKER_FIELDS):
            raise ConfigError(f"{name}: worker_* fields apply only to kind = claude-code")
        if getattr(section, "worker_dangerously_skip_permissions", False) is True:
            raise ConfigError(f"{name}: worker_dangerously_skip_permissions applies only to kind = antigravity")

    @staticmethod
    def _validate_antigravity_section(name: str, section: Any) -> None:
        """Only ``executable``, ``timeout_seconds`` and the inheritance setting are honoured.

        Every other field (Claude ``worker_*`` policy, credentials, endpoints) would be
        silently dropped by the agy worker, so it is refused instead.
        """
        stray = sorted(
            field for field in (
                "api_key", "base_url", "api", "auth", "profile", "command", "args", "env",
                "inherit_env", "permission_policy", *_CLAUDE_WORKER_FIELDS)
            if getattr(section, field, None) is not None)
        if stray:
            raise ConfigError(
                f"{name} is the Antigravity (agy) subagent worker; only "
                f"{', '.join(sorted(_ANTIGRAVITY_FIELDS - {'kind'}))} are supported, not: {', '.join(stray)}")
        if not isinstance(getattr(section, "worker_inherits_agy_settings", True), bool):
            raise ConfigError(f"{name}: worker_inherits_agy_settings must be true or false")
        if not isinstance(getattr(section, "worker_dangerously_skip_permissions", False), bool):
            raise ConfigError(f"{name}: worker_dangerously_skip_permissions must be true or false")

    @staticmethod
    def _check_antigravity_model(runtime: Any, provider_name: str, model: str) -> None:
        """An exact, currently reported ``agy models`` ID (a refresh hint otherwise)."""
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}", model) is None:
            raise ConfigError(f"{provider_name}/{model[:64]!r}: not a valid agy model id")
        registry = getattr(runtime, "_registry", None)
        status = getattr(registry, "provider_status", lambda _name: None)(provider_name)
        if status is None or not getattr(registry, "_loaded", False):
            return  # no discovered catalogue to check against; the worker validates at start
        if registry.get(f"{provider_name}/{model}") is None:
            known = ", ".join(sorted(info.id for info in registry.list(provider=provider_name))[:12])
            raise ConfigError(
                f"{provider_name}/{model} is not in the models `agy models` reported"
                + (f" ({known})" if known else " (it reported none: is agy installed and signed in?)")
                + "; refresh the model list in Settings after fixing agy")

    @staticmethod
    def _refuse_subagent_only_selection(config: Config, sections: Mapping[str, Any]) -> None:
        """External worker adapters are subagent-only model selections."""
        v2 = getattr(config, "v2", None)
        models = getattr(v2, "models", None)
        legacy_model = getattr(v2, "model", None)
        refs = [
            getattr(config, "model", None),
            getattr(legacy_model, "default", None), getattr(legacy_model, "fast", None),
            getattr(legacy_model, "plan", None), *(getattr(legacy_model, "fallback", None) or ()),
            getattr(models, "default", None), getattr(models, "fast", None), getattr(models, "plan", None),
            *(getattr(models, "fallback", None) or ()),
        ]
        for ref in refs:
            head = ref.partition("/")[0] if isinstance(ref, str) and "/" in ref else ""
            kind = Runtime._adapter_kind(head, sections.get(head)) if head else None
            kind = kind or _CORE_ADAPTERS.get(head)
            if kind == ADAPTER_ANTIGRAVITY:
                raise ConfigError(
                    f"{ref!r}: Antigravity (agy) runs only as an explicit subagent route; it cannot be "
                    "the main, plan, fast or fallback model")
            if kind == ADAPTER_CLAUDE_CODE and not getattr(
                getattr(getattr(config, "v2", None), "models", None),
                "claude_code_main_enabled", False,
            ):
                raise ConfigError(
                    f"{ref!r}: Claude Code main-model selection requires "
                    "claude_code_main_enabled = true")
            if kind == ADAPTER_CLAUDE_CODE and getattr(
                getattr(getattr(config, "v2", None), "models", None),
                "claude_code_subagent_backend", "worker",
            ) == "worker":
                raise ConfigError(
                    f"{ref!r}: Claude Code worker routes are subagent-only; use "
                    "claude_code_subagent_backend = 'sdk' for main-agent selection")

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
        self._refuse_subagent_only_selection(config, sections)

        # (1) One instance per configured section, under its own name(s).
        for name, section in sections.items():
            kind = self._adapter_kind(name, section)
            if kind is None:
                continue
            if kind == ADAPTER_ANTIGRAVITY:
                self._validate_antigravity_section(name, section)
                continue  # subagent-only: run by the external worker, never a Nexus provider
            if kind == ADAPTER_CLAUDE_CODE:
                self._validate_claude_section(name, section)
                if not bool(getattr(getattr(v2, "models", None), "claude_code_main_enabled", False)):
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
            if kind == ADAPTER_CLAUDE_CODE and not bool(getattr(getattr(v2, "models", None), "claude_code_main_enabled", False)):
                continue
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
        if not bool(getattr(getattr(v2, "models", None), "claude_code_main_enabled", False)):
            providers.pop("claude-code", None)
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
            tier_efforts=getattr(getattr(v2, "models", None), "tier_efforts", {}),
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
            known_tools.update(("subagent", "subagent_continue"))
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
            return HookManager(self.workspace, home=self._home, config=config, environ=self._environ)
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
        return _HookService(self._hooks, specs, workspace=self.workspace, root_for=self.validate_session_root)

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
        real_id = store.resolve_child_id(child_session)
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
            mapping[bundle].extend(getattr(tool, "deferred_targets", ()))
        return mapping

    @staticmethod
    def _catalog_profile_names(catalog, name):
        from .tools.bundles import get_profile, profile_tools

        profile = get_profile(name)
        names = set(profile_tools(name))
        for tool in catalog or ():
            if tool.bundle not in profile.bundles or tool.name in profile.exclude:
                continue
            if not profile.read_only or not tool.mutates:
                names.add(tool.name)
            names.update(target for target in tool.deferred_targets
                         if not profile.read_only or target not in tool.deferred_mutating)
        return frozenset(names)

    @staticmethod
    def _mutating_names(catalog: Sequence[Any] | None = None) -> tuple[str, ...]:
        names: set[str] = set()
        for tool in catalog or ():
            names.update(getattr(tool, "deferred_mutating", ()))
            spec = getattr(tool, "spec", tool)
            if getattr(spec, "mutates", False):
                name = getattr(spec, "name", None)
                if isinstance(name, str):
                    names.add(name)
        return tuple(sorted(names))

    async def close_agent(
        self, session_id: str, agent_id: str, *, reason: str = "", by: str = "user"
    ) -> tuple[bool, str]:
        """Host ``AgentClose``, kept for wire compatibility. Returns ``(closed, message)``.

        Subagents are foreground-only, so a child is closed once its run ends:
        closing a running child cancels that child alone (like ``AgentCancel``),
        and a child that already ended, including a legacy idle one from an
        older release, has nothing left to close. Nothing is written to the log.
        ``session_id`` is accepted for the wire shape; the host checks that the
        agent belongs to it.
        """
        if cancel_child(agent_id, (reason or f"closed by {by}")[:200]):
            return True, (
                f"Cancelled subagent {agent_id}; its parent turn continues. "
                "Subagents end with their run, so it is closed."
            )
        return True, (
            f"Subagent {agent_id} is not running. Subagents end with their run, "
            "so there is nothing left to close."
        )

    def _turn_subagent_budget(
        self, session_id: str, turn_id: str, config: Config | None
    ) -> Any | None:
        """The root session's pool-owned budget, its window reset once per root turn.

        Concurrency spans turns (a cancelled turn's children may still be winding
        down); tokens, cost and fan-out reset at each root turn start.
        """
        if self._agents is None:
            return None
        effective = config if isinstance(config, Config) else self._load_config()
        section = self._agents_section(effective)
        if section is None or not getattr(section, "enabled", True):
            return None
        signature = tuple(
            getattr(section, name, None)
            for name in ("max_concurrent", "max_depth", "max_fanout", "token_budget",
                         "cost_budget")
        )
        return self.agent_pool.budget_window(
            session_id,
            turn_id,
            signature=signature,
            factory=lambda: self._new_subagent_budget(effective),
        )

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
        root_worktree_tools = {"worktree_list", "worktree_create", "worktree_move", "worktree_commit"}
        parent_tools = tuple(name for name in parent_tools if name not in root_worktree_tools)
        if catalog is not None:
            catalog = tuple(tool for tool in catalog if tool.name not in root_worktree_tools)
        effective = config if isinstance(config, Config) else self._load_config()
        section = self._agents_section(effective)
        if section is None or not getattr(section, "enabled", True):
            return None
        if worktree_service is not None and worktree_service is not self._worktree_service:
            raise ValueError("subagent runners must use the Runtime-owned WorktreeService")
        managed_worktree_service = None
        parent_checkout = Path(workspace or self.workspace).expanduser().resolve()
        git_checkout = parent_checkout.is_dir() and subprocess.run(
            ["git", "-C", str(parent_checkout), "rev-parse", "--show-toplevel"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
        # Subagent worktrees are disabled for now (docs/decisions.md): children
        # share the session checkout and the `subagent` tool hides `worktree`.
        if SUBAGENT_WORKTREES and runtime_supports_workspace and git_checkout:
            from .agents.session_subagents import SessionSubagentService

            managed_worktree_service = SessionSubagentService(
                self._session_worktrees,
                parent_session or session_id,
                turn_id=root_turn_id,
            )
            task_worktree_root = self._worktree_root
        else:
            task_worktree_root = worktree_root or self._worktree_root
        if worktree_root is not None and Path(worktree_root) != Path(task_worktree_root):
            raise ValueError("subagent runners must use the Runtime-owned worktree registry")
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

        # Tiers choose subagent routes; the root model's price is not a ceiling.
        # Nested children still inherit their parent's tier and role restrictions.
        max_tier = self._tiers.order[-1]
        if parent_depth == 0:
            parent_tier = max_tier
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
                # Only the root session spawns: values above 1 are ignored, 0 disables.
                max_depth=min(int(getattr(section, "max_depth", 3)), 1),
                max_fanout=getattr(section, "max_fanout", 16),
                token_budget=getattr(section, "token_budget", None),
                cost_budget=getattr(section, "cost_budget", None),
                default_type=str(getattr(section, "default_type", "task")),
                profile_for=profile_for or (lambda name: self._catalog_profile_names(catalog, name)),
                config=effective,
                event_sink=event_sink,
                bundle_map=self._bundle_map(catalog),
                mutating_tools=self._mutating_names(catalog),
                hooks=hooks,
                worktree_service=managed_worktree_service or self._worktree_service,
                managed_worktrees=managed_worktree_service is not None,
                tier_probe=self._tier_runnable,
                route_gate=self.route_subagent,
                tier_candidates=self._router.tier_candidates,
                tier_efforts=lambda: getattr(self._router, "tier_efforts", None),
                worktree_root=task_worktree_root,
                worktree_root_for=(
                    None if managed_worktree_service is not None
                    else self._owned_worktree_root_for
                ),
                runtime_supports_workspace=runtime_supports_workspace,
                worktree_scope=worktree_scope,
                stop_child_jobs=self._stop_child_jobs,
                parent_events=lambda sid=parent_session or session_id: self._parent_log_events(sid),
            )
        except Exception:  # noqa: BLE001 - an invalid agents config disables Task
            return None

    def _parent_log_events(self, session_id: str) -> list[Event]:
        """The durable events of one parent session: the lifecycle authority for ``resume``."""
        return [
            record.event
            for record in self._sessions.store.records(session_id)
            if getattr(record, "event", None) is not None
        ]

    async def _stop_child_jobs(self, child_session_id: str) -> list[str]:
        """Stop the shell jobs a child left running; return their ids.

        Quiet: the child's report lists them, no notification is queued.
        """
        registry = self._job_registry
        active = getattr(registry, "active", None)
        if not callable(active) or not child_session_id:
            return []
        stopped: list[str] = []
        for job in active(session_id=child_session_id):
            result = await registry.kill(
                job.job_id, session_id=child_session_id, quiet=True
            )
            if result.outcome == "killed":
                stopped.append(job.job_id)
        return stopped

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
        if any(root.path == checkout and root.kind != "main"
               for _, root in self._session_roots.values()):
            self._validate_linked_root(checkout)
            return self._worktree_root_for(checkout)
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
                record = self._worktree_service.inspect(child_id, root=root)
                if getattr(record, "kind", None) == "session":
                    from .worktree_cleanup import modern_worktree_row

                    return modern_worktree_row(record, root=root)
                return record
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
            return sessions.manager._live_handle(sessions.manager.store.resolve_child_id(child_id)) is not None
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
        """List owned subagent and modern session worktree records."""
        records = []
        for root in tuple(self._worktree_roots.values()):
            if not root.exists():
                continue
            records.extend(
                self._worktree_service.inspect(record.child_id, root=root)
                for record in self._worktree_service.list(root=root)
                if getattr(record, "kind", None) != "session"
            )
            from .agents.session_worktrees import list_sessions
            from .worktree_cleanup import modern_worktree_row

            records.extend(
                modern_worktree_row(record, root=root)
                for record in list_sessions(root=root)
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

    # -- plan-usage gate (sub-agents only) ---------------------------------

    def _build_usage_policy(self, config: Any = None) -> UsagePolicy:
        try:
            config = config if config is not None else self._load_config()
            section = getattr(getattr(getattr(config, "v2", None), "models", None), "subagent_usage", None)
            return UsagePolicy.from_section(section) if section is not None else UsagePolicy()
        except Exception:  # noqa: BLE001 - a bad config must never block delegation
            return UsagePolicy()

    def _usage_sections(self) -> Mapping[str, Any]:
        try:
            config = self._config if self._config is not None else self._load_config()
            return getattr(getattr(config, "v2", None), "providers", None) or {}
        except Exception:  # noqa: BLE001
            return {}

    def set_usage_snapshot(self, snapshot: UsageSnapshot | None) -> None:
        """Replace the plan-usage snapshot (pushed by the host's snapshot service)."""
        self._usage_snapshot = snapshot

    def usage_policy(self) -> UsagePolicy:
        """The current ``[models.subagent_usage]`` policy, rebuilt from live config."""
        self._usage_policy = self._build_usage_policy()
        return self._usage_policy

    def usage_headroom(self, reference: str) -> UsageVerdict:
        """The quota verdict for a route reference (``provider/model``) or a bare provider name."""
        provider = str(reference).split("/", 1)[0]
        sections = self._usage_sections()
        kind = Runtime._adapter_kind(provider, sections.get(provider))
        quota = quota_identity(provider, kind)
        return usage_verdict(self._usage_snapshot, quota, self._usage_policy, now=time.time())

    def _route_candidate_eligible(self, reference: str) -> bool:
        """Whether a tier candidate could start a child: a runnable Nexus provider or an external worker."""
        head = reference.split("/", 1)[0]
        sections = self._usage_sections()
        if Runtime._adapter_kind(head, sections.get(head)) in (ADAPTER_ANTIGRAVITY, ADAPTER_CLAUDE_CODE):
            return "/" in reference
        info = self._router._registry_lookup(reference)
        return info is not None and getattr(info, "provider", None) in self._router._providers

    def route_subagent(self, tier: str) -> RouteDecision | None:
        """Walk ``tier``'s candidates skipping providers whose quota is nearly spent.

        Spawn-time and sub-agents only: callers are the runner's child spawn and
        the child runtime, never a root, title or compaction request. ``None``
        when the gate is off or the tier has no candidate to choose between.
        """
        kick = self.usage_kick
        if callable(kick):
            with contextlib.suppress(Exception):
                kick()
        policy = self.usage_policy()
        if not policy.enabled:
            return None
        try:
            candidates = tuple(self._router.tier_candidates(tier))
            return choose_route(tier, candidates, self.usage_headroom,
                                eligible=self._route_candidate_eligible, observed_at=time.time())
        except Exception:  # noqa: BLE001 - fail open: routing never fails a spawn
            return None

    def _tier_runnable(self, tier: str) -> bool:
        """Whether ``tier`` currently resolves to a model a provider can run."""
        if self._router.tier_runnable(tier):
            return True
        return self._claude_code_tier_route(tier) is not None

    def _claude_code_tier_route(self, tier: str) -> str | None:
        """The ``claude-code/<model>`` or ``antigravity/<model>`` entry a tier would run on, else ``None``."""
        return Runtime._external_tier_route(self, tier)

    def _external_tier_route(self, tier: str, *, claude: bool = True) -> str | None:
        """The external-worker entry a tier would run on, else ``None``.

        Children only: a tier's candidates are walked in order, and the first one
        that is either a configured ``claude-code`` section (when ``claude``) or
        ``antigravity`` section (run by an external worker) or runnable by a Nexus
        provider decides. A tier whose first usable entry is a Nexus provider never
        reaches a worker. The root's router is untouched: it cannot stream either.
        """
        sections = getattr(getattr(self._config, "v2", None), "providers", None) or {}
        for reference in self._router.tier_candidates(tier):
            head = reference.split("/", 1)[0]
            kind = Runtime._adapter_kind(head, sections.get(head))
            if kind == ADAPTER_ANTIGRAVITY or (
                claude and kind == ADAPTER_CLAUDE_CODE
                and getattr(getattr(getattr(self._config, "v2", None), "models", None),
                            "claude_code_subagent_backend", "worker") == "worker"
            ):
                return reference if "/" in reference else None
            if kind == ADAPTER_CLAUDE_CODE:
                return None  # the SDK backend serves this tier through a Nexus provider
            info = self._router._registry_lookup(reference)
            if info is not None and getattr(info, "provider", None) in self._router._providers:
                return None
        return None

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
        # Only the root session spawns: a child gets no runner, so neither its
        # catalog nor its extension/code-mode context can reach ``subagent``.
        return _ChildRuntime(
            self,
            replace(spec, config=config, permissions=authority, hooks=hooks),
            None,
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
        if not parent_guard._worktree_boundary:
            # Main/external checkouts retain the normal PathGuard semantics:
            # configured absolute roots stay tied to their named checkout.
            return parent_guard.for_worktree(workspace)
        parent_workspace = parent_guard.workspace

        def child_scope(specs, roots):
            translated = []
            for original, canonical in zip(specs, roots):
                if canonical.is_relative_to(parent_workspace):
                    relative = canonical.relative_to(parent_workspace)
                    translated.append(relative.as_posix() if relative.parts else "./")
                else:
                    translated.append(original)
            return translated

        # Session-root guards carry canonical absolute intersections. When a
        # parent is already in a Nexus-owned checkout, those roots name paths
        # in the parent checkout, so PathGuard.for_worktree would correctly see
        # them as disjoint from the sibling child checkout and remove them.
        # Re-express only scopes inside the parent as project-relative scopes;
        # external absolute grants remain absolute and therefore cannot widen
        # into the child checkout.
        write_roots = child_scope(
            parent_guard._write_root_specs, parent_guard.write_roots
        )
        read_denyroots = child_scope(
            parent_guard._read_denyroot_specs, parent_guard.read_denyroots
        )
        if not parent_guard.write_roots:
            return PathGuard(
                workspace,
                write_roots=(),
                read_denyroots=read_denyroots,
                home=parent_guard._home,
                _allow_empty_write_roots=True,
                _worktree_boundary=True,
                settings_scopes=parent_guard._settings_scopes,
            )
        inherited = PathGuard(
            parent_workspace,
            write_roots=write_roots,
            read_denyroots=read_denyroots,
            home=parent_guard._home,
            settings_scopes=parent_guard._settings_scopes,
        )
        inherited_child = inherited.for_worktree(workspace)
        permissions = getattr(getattr(spec.config, "v2", None), "permissions", None)
        policy_guard = PathGuard(
            parent_workspace,
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
        ).for_worktree(workspace)
        intersections = []
        for configured in policy_guard.write_roots:
            for inherited_root in inherited_child.write_roots:
                if configured.is_relative_to(inherited_root):
                    intersections.append(configured)
                elif inherited_root.is_relative_to(configured):
                    intersections.append(inherited_root)
        read_denies = tuple(dict.fromkeys((
            *policy_guard._read_denyroot_specs,
            *(str(root) for root in policy_guard.read_denyroots),
            *inherited_child._read_denyroot_specs,
            *(str(root) for root in inherited_child.read_denyroots),
        )))
        return PathGuard(
            workspace,
            write_roots=tuple(str(root) for root in dict.fromkeys(intersections)),
            read_denyroots=read_denies,
            home=parent_guard._home,
            _allow_empty_write_roots=True,
            _worktree_boundary=True,
            settings_scopes=parent_guard._settings_scopes,
        )

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
        if getattr(spec, "resume", False):
            # A follow-up appends to the stored transcript and never compacts it:
            # an assembly that would drop or summarize history refuses instead.
            assembler._refuse_compaction = True
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
        logical_id = str(getattr(spec, "session_id", ""))
        root_id = str(getattr(spec, "root_id", ""))
        if manifest is not None and root_id:
            if self._sessions.store.exists(root_id):
                root_session = self._sessions.open(root_id, create=False, recover=True)
                manifest = self._selected_manifest(manifest, root_session)
        catalog_map: dict[str, Any] = (
            dict(manifest.tools) if manifest is not None else {}
        )
        catalog_map = {
            tool.name: tool
            for tool in self._filter_web_catalog(config, tuple(catalog_map.values()))
        }
        # Only the root session spawns, whatever the catalog or an extension offers.
        for name in [n for n in catalog_map if canonical_tool_name(n) in ROOT_ONLY_TOOLS]:
            del catalog_map[name]
        if bool(getattr(spec, "worktree_scope", False)) or bool(
            getattr(spec, "worktree", None)
        ):
            catalog_map = {
                name: tool
                for name, tool in catalog_map.items()
                if name in WORKTREE_CHILD_TOOLS and tool.origin == "builtin"
            }
        catalog = [catalog_map[name] for name in spec.tools if name in catalog_map]
        # Subagents only: the parent's tool set never contains `escalate`, so it
        # is added after the parent ∩ role ∩ requested intersection (it grants nothing).
        from .tools.builtin.escalate import MAX_ESCALATIONS, build_escalate_tool

        escalations = getattr(self, "_child_escalations", {}).setdefault(logical_id, [])

        def record_escalation(text: str) -> bool:
            if len(escalations) >= MAX_ESCALATIONS:
                return False
            escalations.append(text)
            return True

        catalog.append(build_escalate_tool(record_escalation))
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
            restrict=(*spec.tools, "escalate"),
            path_guard=path_guard,
            job_registry=self._job_registry,        )
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
        spec: Any, session: Any, outcome: Any, *, cost: float | None = None, since: int = 0
    ) -> SubagentOutcome:
        text = ""
        # ``since``: a follow-up reports its own run, not the earlier runs' text or edits.
        messages = list(session.messages)[since:]
        for message in reversed(messages):
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
                messages, workspace=getattr(spec, "workspace", None)
            ),
            models=_child_models(session),
        )

    def _child_cost(self, session: Any, outcome: Any) -> float | None:
        """Price a child turn from the registry's ``ModelInfo`` cost, or ``None``.

        The child's concrete provider/model comes from its ``model.started``
        event; pricing is ``$``/Mtok from ``models.dev``. Unknown pricing (no
        registry, no catalogue entry, or no cost data — 421 catalogue models are
        local/open-weight) returns ``None``, so the aggregate ``spent_cost``
        counts only priced models: a ``cost_budget`` is therefore a bound over
        priced models only, and ``token_budget`` is the universal bound. This is
        documented rather than guessed. Tiered pricing applies per child turn:
        the highest ``Cost.tiers`` entry whose ``context`` is below the turn's
        prompt size (input + cache read + cache write tokens) replaces the base
        rates for all of that turn's tokens.
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
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cache_read_tokens = int(getattr(usage, "cache_read_tokens", 0) or 0)
        cache_write_tokens = int(getattr(usage, "cache_write_tokens", 0) or 0)
        # Tiered pricing: the highest tier whose threshold is below the prompt
        # size (input + cache read + cache write) replaces the base rates.
        rates = pricing
        prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
        for tier in getattr(pricing, "tiers", None) or ():
            if prompt_tokens > int(getattr(tier, "context", 0) or 0):
                rates = tier
        input_rate = float(getattr(rates, "input", 0.0) or 0.0)
        output_rate = float(getattr(rates, "output", 0.0) or 0.0)
        total = (
            input_tokens * input_rate
            + int(getattr(usage, "output_tokens", 0) or 0) * output_rate
            + cache_read_tokens * float(getattr(rates, "cache_read", 0.0) or 0.0)
            + cache_write_tokens * float(getattr(rates, "cache_write", 0.0) or 0.0)
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

    async def reload_tier_config(self) -> bool:
        """Apply a ``[models.tiers]`` / ``tier_efforts`` edit without rebuilding providers.

        Rebuilds only the tier table, re-indexes the loaded registry from its
        held catalogue (no probe, no parse), and updates the router and the
        context coordinator in place. Returns ``False`` (restart needed) when
        routes were injected.
        """
        if not self._owns_routes or self._closed:
            return False
        config = self._load_config()
        tiers = self._build_tiers(config)
        efforts = getattr(getattr(getattr(config, "v2", None), "models", None), "tier_efforts", {})
        self._tiers = tiers
        if self._registry is not None:
            self._registry.retier(tiers)
        self._router.set_tiers(tiers, tier_efforts=efforts)
        self._assembler._tiers = tiers
        self._usage_policy = self._build_usage_policy(config)
        return True

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
        self._usage_policy = self._build_usage_policy(config)
        for provider in previous:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
        return True

    # -- lifecycle ---------------------------------------------------------

    def _live_section(self, config: Config, environ: Mapping[str, str]) -> Any:
        from .config.schema import LiveSection

        section = config.v2.live if config.v2 else LiveSection()
        if environ.get("NEXUS_LIVE", "").strip().lower() == "off":
            section = msgspec.structs.replace(section, enabled=False)
        return section

    def _build_live(self, config: Config, environ: Mapping[str, str]) -> Any:
        """The daemon's GPT Live manager (docs/live.md); nothing connects until a call starts."""
        from .live.manager import LiveManager

        def profile() -> str:
            v2 = self._load_config().v2
            return getattr(((v2.providers if v2 else {}) or {}).get("codex"), "profile", None) or "default"

        def auth() -> Any:
            from .auth.codex import CodexOAuthManager

            return (self._codex_auth_factory or CodexOAuthManager)(profile=profile())

        def transport_factory(raw_log: Any = None) -> Any:
            from .live.transport import CodexLiveTransport

            return CodexLiveTransport(auth(), raw_log=raw_log)

        async def availability() -> str | None:
            from .live.transport import websocket_available

            if not websocket_available():
                return "unsupported"
            return None if await auth().status() else "no_credential"

        return LiveManager(
            self._live_section(config, environ),
            transport_factory=transport_factory, availability=availability,
            home=nexus_home(self._home), workspace=self.workspace, environ=environ,
        )

    def refresh_live_config(self) -> None:
        """Apply persisted ``[live]`` settings to the next call."""
        environ = os.environ if self._environ is None else self._environ
        self.live.configure(self._live_section(Config.load(self.workspace, home=self._home, environ=self._environ), environ))

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
        self.agent_pool.clear()
        await self.voice.shutdown()
        code_runner = getattr(self, "_code_runner", None)
        if code_runner is not None:
            with contextlib.suppress(Exception):
                await code_runner.close()
        if (live := getattr(self, "live", None)) is not None:
            await live.shutdown()
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
