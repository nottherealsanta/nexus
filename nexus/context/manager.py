"""Composable context assembly (plan sections 4, 5.2).

Phase 3 turns the Phase 1 assembler into a small pipeline:

1. **Parts** (:mod:`nexus.context.parts`) render in a fixed order — identity,
   soul, environment, tools, skills/mcp/attachment placeholders, memory,
   history, current user. Tool schemas stay structured on
   :attr:`~nexus.model.request.ModelRequest.tools` and never enter system text.
2. **Budget** (:mod:`nexus.context.budget`) reserves priority-0 parts, allocates
   lower priorities under their configured caps, and hands the remainder to
   history.
3. **Compaction** (:mod:`nexus.context.compact`) trims history to a contiguous
   whole-message suffix and can evict old tool-result content in the assembled
   copy only.
4. **Cache boundaries** (:mod:`nexus.context.cache`) are described in
   provider-neutral metadata when the injected capabilities support prompt
   caching.

What the manager owns and preserves from Phase 1:

* **Per-turn freeze.** :meth:`ContextManager.for_turn` returns a snapshot whose
  config, environment, and rendered ``SOUL.md``/``MEMORY.md`` are captured once,
  so every loop iteration in a turn sees identical inputs. A config edit affects
  only the next turn.
* **Containment and size limits.** ``SOUL.md``/``MEMORY.md`` resolve through
  :func:`nexus.config.paths.resolve_within`; escapes fail closed and oversized
  files are a named error.
* **Compatibility.** :meth:`assemble` returns a :class:`ModelRequest` — or, when
  configured with an async token counter, an awaitable that the loop already
  handles — so existing callers and tests keep working.

Integration seam
----------------
A runtime/provider packet injects the frozen per-turn environment without this
module importing anything concrete::

    context.configure(
        capabilities=resolved.capabilities,
        counter=text_counter,        # callable(text) -> int | awaitable[int]
        model=resolved.model,
        provider=resolved.provider.name,
        note_resolver=notes,         # keyed by tool_use_id
        summarizer=summarizer,       # optional; required for 'summarize'
    )
    snapshot = context.for_turn()    # freezes the above for one turn
    request = snapshot.assemble(session)

Alternatively pass the same keywords directly to :meth:`for_turn`. The manager
never imports a concrete provider, router, or runtime.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import Config
from ..config.paths import resolve_within
from ..core.turn import TurnLimits
from ..errors import ConfigError
from ..model.capabilities import Capabilities
from ..model.message import Message, Text, ToolResult, ToolUse
from ..model.request import ModelRequest, SamplingParams, ToolSchema
from ..model.tokenizer import DEFAULT_TOKENIZER
from ..util import new_id
from .budget import BudgetInputs, PartRequest, allocate, fit_suffix_count, part_caps
from .cache import (
    TokenCountCache,
    boundaries_metadata,
    prompt_cache_boundaries,
    semantic_key,
)
from .compact import (
    CompactionError,
    CompactionResult,
    DropOldestAction,
    SummarizeAction,
    drop_oldest,
    evict_tool_results,
)
from .parts import (
    IDENTITY_PREAMBLE,
    PART_PRIORITY,
    AssemblyContext,
    ContextPart,
    EnvironmentInfo,
    PartOutput,
    builtin_parts,
    canonical_message_text,
    canonical_tool_text,
    capture_environment,
    current_user_index,
    freeze_mcp_index,
    freeze_skills_index,
    render_parts,
)

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "IDENTITY_PREAMBLE",
    "AssemblyEnvironment",
    "CompactionOptions",
    "ContextManager",
    "PreCompactDecision",
    "PreCompactGate",
    "PreCompactRequest",
]

#: Ceiling for a single system file. ``SOUL.md``/``MEMORY.md`` are prose, not
#: data payloads; a file this large is a configuration mistake.
DEFAULT_MAX_FILE_BYTES = 1_000_000

#: How many trailing history messages eviction never touches.
DEFAULT_KEEP_RECENT = 6

#: Distinguishes "no per-iteration override supplied" from ``None`` (clear).
_UNSET: Any = object()


#: The typed compaction options a ``PreCompact`` ``modify`` may set. Any other
#: key, or an invalid value, is rejected by :meth:`from_mapping` (returns
#: ``None``), so a hook can never smuggle arbitrary state into the assembler.
@dataclass(frozen=True)
class CompactionOptions:
    strategy: str | None = None
    keep: int | None = None
    keep_recent: int | None = None
    min_content_chars: int | None = None

    _STRATEGIES = frozenset(
        {"drop_oldest", "evict_tool_results", "summarize", "hybrid"}
    )
    _KEYS = ("strategy", "keep", "keep_recent", "min_content_chars")

    @classmethod
    def from_mapping(cls, value: object) -> CompactionOptions | None:
        """Parse a hook's modified input, or ``None`` when it is not meaningful.

        ``None`` means "disallow": the caller must not apply an unrecognized
        modification. Only the four typed keys are accepted, and the strategy
        must be one of the four known names.
        """
        if value is None or not isinstance(value, Mapping):
            return None
        if set(value) - set(cls._KEYS):
            return None
        strategy = value.get("strategy")
        if strategy is not None and (
            not isinstance(strategy, str) or strategy not in cls._STRATEGIES
        ):
            return None

        def _int(name: str) -> int | None:
            raw = value.get(name)
            if raw is None:
                return None
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ValueError(name)
            return raw

        try:
            return cls(
                strategy=strategy,
                keep=_int("keep"),
                keep_recent=_int("keep_recent"),
                min_content_chars=_int("min_content_chars"),
            )
        except ValueError:
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "keep": self.keep,
            "keep_recent": self.keep_recent,
            "min_content_chars": self.min_content_chars,
        }


@dataclass(frozen=True)
class PreCompactRequest:
    """The frozen facts a ``PreCompact`` hook runs against."""

    session_id: str | None
    turn_id: str | None
    iteration: int
    strategy: str
    keep: int
    history_messages: int
    dropped_messages: int
    evicted_tool_results: int
    input_budget: int
    history_budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "iteration": self.iteration,
            "strategy": self.strategy,
            "keep": self.keep,
            "history_messages": self.history_messages,
            "dropped_messages": self.dropped_messages,
            "evicted_tool_results": self.evicted_tool_results,
            "input_budget": self.input_budget,
            "history_budget": self.history_budget,
        }


@dataclass(frozen=True)
class PreCompactDecision:
    """The result of the pre-compaction gate.

    ``blocked`` prevents compaction entirely (no summary is persisted) and the
    loop fails the turn actionably. ``options`` applies a typed modification.
    ``outcome`` is the sanitized hook outcome the loop persists through the
    session sink.
    """

    blocked: bool = False
    reason: str = ""
    options: CompactionOptions | None = None
    outcome: Mapping[str, Any] | None = None


#: A pre-compaction gate: sync or async, returning a :class:`PreCompactDecision`.
PreCompactGate = Callable[[PreCompactRequest], PreCompactDecision | Any]


def _fit_whole_lines(
    lines: tuple[str, ...], costs: Sequence[int], budget: int
) -> tuple[str, ...]:
    """The longest prefix of ``lines`` whose whole-line costs fit ``budget``.

    A line is either fully included or not included at all, so a token budget can
    never cut a ``name: description`` entry in half. Stops at the first line that
    would overflow, keeping the rendered index a contiguous, deterministic prefix.
    """
    if budget <= 0 or not lines:
        return ()
    total = 0
    kept: list[str] = []
    for line, cost in zip(lines, costs):
        if total + cost > budget:
            break
        total += cost
        kept.append(line)
    return tuple(kept)


def _system_file_text(system_files: Any, name: str) -> str | None:
    """Extract one frozen system-file body without touching the live file.

    Accepts a ``SystemFiles``-like object (``.get(name)``), a plain mapping, or
    ``None``. ``None`` means "no snapshot supplied" so the caller falls back to
    reading; an empty string means the snapshot deliberately has no such file,
    so nothing is reread.
    """
    if system_files is None:
        return None
    entry = None
    getter = getattr(system_files, "get", None)
    if callable(getter):
        entry = getter(name)
    elif isinstance(system_files, Mapping):
        entry = system_files.get(name)
    if entry is None:
        return ""
    content = getattr(entry, "content", None)
    if content is None:
        content = entry if isinstance(entry, str) else ""
    return content if isinstance(content, str) else ""


def _invoke_counter(counter: Any, text: str) -> Any:
    if counter is None:
        return DEFAULT_TOKENIZER.count_tokens(text)
    count = getattr(counter, "count", None)
    if callable(count):
        return count(text)
    if callable(counter):
        return counter(text)
    raise TypeError("token counter must be callable or expose count(text)")


def _is_async(obj: Any) -> bool:
    if obj is None:
        return False
    target = getattr(obj, "count", None)
    if target is None and callable(obj):
        target = obj
    return inspect.iscoroutinefunction(target)


def _is_async_summarizer(summarizer: Any) -> bool:
    if summarizer is None:
        return False
    return inspect.iscoroutinefunction(getattr(summarizer, "summarize", None))


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _request_counter_fn(counter: Any) -> Callable[[ModelRequest], Any] | None:
    if counter is None:
        return None
    method = getattr(counter, "count_request", None)
    if callable(method):
        return method
    if callable(counter):
        return counter
    return None


async def _invoke_request_counter(counter: Any, request: ModelRequest) -> Any:
    fn = _request_counter_fn(counter)
    if fn is None:
        return None
    value = fn(request)
    if inspect.isawaitable(value):
        value = await value
    return value


def _truncate_text(text: str, requested: int, granted: int) -> str:
    """Deterministically cut ``text`` down to its granted token share."""
    if granted >= requested or not text:
        return text
    if granted <= 0:
        return ""
    ratio = granted / requested
    target = max(1, int(len(text) * ratio))
    return text[:target] + "\n…[truncated to fit context]"


@dataclass(frozen=True)
class AssemblyEnvironment:
    """Everything frozen for one turn that the assembly pipeline reads."""

    config: Config
    capabilities: Capabilities
    model: str | None
    provider: str | None
    identity: str
    soul_text: str
    memory_text: str
    environment: EnvironmentInfo
    counter: Any
    counter_async: bool
    note_resolver: Any
    summarizer: Any
    summarizer_async: bool
    request_counter: Any
    max_file_bytes: int
    workspace: Path
    system_override: str | None
    strategy: str
    keep_recent: int
    min_content_chars: int


@dataclass(frozen=True)
class _Costs:
    part_costs: dict[str, int]
    history: tuple[Message, ...]
    history_costs: tuple[int, ...]
    user_costs: tuple[int, ...]
    evicted_messages: tuple[Message, ...] | None
    evicted_costs: tuple[int, ...] | None
    #: Per-line token costs for whole-line parts (the skills index), keyed by part
    #: name. Empty for every part whose text is truncated by ratio instead.
    line_costs: dict[str, tuple[int, ...]]


class ContextManager:
    """Assembles a token-budgeted, compaction-aware :class:`ModelRequest`."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        config: Config | None = None,
        config_loader: Callable[[], Config] | None = None,
        identity: str = IDENTITY_PREAMBLE,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        system: str | None = None,
        capabilities: Any | None = None,
        counter: Any | None = None,
        environment: EnvironmentInfo | None = None,
        note_resolver: Any | None = None,
        summarizer: Any | None = None,
        request_counter: Any | None = None,
        model: str | None = None,
        provider: str | None = None,
        parts: tuple[ContextPart, ...] | None = None,
        cache: TokenCountCache | None = None,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        min_content_chars: int = 0,
        skills_index: Any | None = None,
        skills: Any | None = None,
        mcp_index: Any | None = None,
        pre_compact: PreCompactGate | None = None,
        turn_id: str | None = None,
        iteration: int = 0,
    ) -> None:
        if config is None and config_loader is None:
            raise ConfigError("ContextManager requires a config or config_loader")
        if not isinstance(identity, str):
            # Empty means no identity part: a subagent speaks only as its role.
            raise ValueError("identity must be a string")
        if type(max_file_bytes) is not int or max_file_bytes < 1:
            raise ValueError("max_file_bytes must be a positive integer")
        if system is not None and not isinstance(system, str):
            raise ValueError("system must be a string or None")
        if type(keep_recent) is not int or keep_recent < 0:
            raise ValueError("keep_recent must be a non-negative integer")
        if type(min_content_chars) is not int or min_content_chars < 0:
            raise ValueError("min_content_chars must be a non-negative integer")
        self.workspace = Path(workspace).resolve()
        self._config = config
        self._config_loader = config_loader
        self._identity = identity
        self.max_file_bytes = max_file_bytes
        self._system_override = system
        self._capabilities = capabilities
        self._counter = counter
        self._environment = environment
        self._note_resolver = note_resolver
        self._summarizer = summarizer
        self._request_counter = request_counter
        self._model_override = model
        self._provider_override = provider
        self._reasoning_effort: str | None = None
        self._parts: tuple[ContextPart, ...] = (
            tuple(parts) if parts is not None else builtin_parts()
        )
        for part in self._parts:
            if not getattr(part, "name", None) or not hasattr(part, "render"):
                raise TypeError("parts must expose name and render")
        self.cache = cache
        self.keep_recent = keep_recent
        self.min_content_chars = min_content_chars
        #: Frozen tool schemas for this turn. Populated by the runtime's per-turn
        #: tool snapshot through :meth:`freeze_tools`; descriptions live only on
        #: these schemas and are never interpolated into system text.
        self._tool_schemas: tuple[ToolSchema, ...] = ()
        #: Frozen, sanitized ``name: description`` lines for the current
        #: iteration's skills index. Populated through :meth:`configure`,
        #: :meth:`for_turn`, :meth:`for_iteration`, or :meth:`freeze_skills`. Only
        #: names and descriptions are ever read from the injected snapshot.
        #: ``skills`` is an accepted alias for ``skills_index``.
        self._skills_index: tuple[str, ...] = freeze_skills_index(
            skills_index if skills_index is not None else skills
        )
        #: Frozen MCP index block for the current iteration's connected servers
        #: and resource roots. Populated through :meth:`configure`,
        #: :meth:`for_turn`, or :meth:`for_iteration`; empty renders nothing.
        self._mcp_index: str = freeze_mcp_index(mcp_index)
        #: The optional pre-compaction gate (a ``PreCompact`` hook adapter). When
        #: set, ``_finalize`` runs it *before* any compaction/summarization and
        #: honors a block or typed modify. ``None`` keeps assembly synchronous.
        self._pre_compact: PreCompactGate | None = pre_compact
        #: The turn/iteration the PreCompact request reports. Injected by the
        #: runtime per iteration so a hook sees real, actionable context.
        self._turn_id: str | None = turn_id
        self._iteration: int = int(iteration) if isinstance(iteration, int) else 0
        #: The budget/eviction facts of the current pass, filled in by
        #: ``_finalize`` just before the gate runs.
        self._precompact_budgets: tuple[int, int] = (0, 0)
        self._precompact_evicted: int = 0
        #: Frozen environment for a per-turn snapshot; ``None`` means build one
        #: from the effective config on each assemble.
        self._env: AssemblyEnvironment | None = None
        self._last_budget_metadata: dict[str, Any] = {}
        self._last_compaction_metadata: dict[str, Any] = {}
        self._last_cache_metadata: dict[str, Any] = {}
        self._last_included_parts: dict[str, str] = {}
        self._last_compacted: dict[str, Any] | None = None
        self._last_summary_reuse: dict[str, Any] | None = None
        #: The current assemble's PreCompact outcome (sanitized), or ``None``.
        #: Rides in the request metadata so the loop can persist the hook events
        #: and fail actionably on a block.
        self._last_precompact: dict[str, Any] | None = None
        #: Whether the gate already ran for the current assemble (so a base pass
        #: and a refinement pass hold one hook decision).
        self._precompact_ran = False
        #: Set when the gate blocked: no further pass of this assemble may
        #: compact (so a refinement pass can never persist a partial summary).
        self._precompact_blocked = False

    # -- config ------------------------------------------------------------

    def effective_config(self) -> Config:
        """Return the configuration for this call, reloading if a loader exists."""
        if self._config_loader is not None:
            return self._config_loader()
        assert self._config is not None  # guarded by __init__
        return self._config

    def model_reference(self, config: Config | None = None) -> tuple[str | None, str | None]:
        """The ``(provider, model)`` this manager would use for ``config``.

        Public seam for the runtime's per-turn coordinator so it never needs the
        private ``_reference`` helper. Explicit overrides win; otherwise the
        effective config's model reference is split.
        """
        return self._reference(config if config is not None else self.effective_config())

    def turn_limits(self) -> TurnLimits:
        """Turn limits derived from this manager's effective config snapshot."""
        config = self.effective_config()
        v2 = getattr(config, "v2", None)
        if v2 is None:
            return TurnLimits()
        return TurnLimits(
            max_iterations=v2.agent.max_iterations,
            max_seconds=v2.agent.max_turn_seconds,
        )

    # -- per-turn configuration injection ----------------------------------

    def configure(
        self,
        *,
        capabilities: Any | None = None,
        counter: Any | None = None,
        environment: EnvironmentInfo | None = None,
        note_resolver: Any | None = None,
        summarizer: Any | None = None,
        request_counter: Any | None = None,
        model: str | None = None,
        provider: str | None = None,
        cache: TokenCountCache | None = None,
        skills_index: Any | None = None,
        skills: Any | None = None,
        mcp_index: Any | None = None,
        pre_compact: PreCompactGate | None = None,
    ) -> None:
        """Inject the frozen per-turn environment used by the next snapshot.

        This is the seam a runtime/provider packet calls before ``for_turn`` (or
        before ``assemble`` when it does not snapshot). Passing ``None`` leaves
        the corresponding value unchanged. ``skills_index`` (or its alias
        ``skills``) accepts a raw skill snapshot/index (or already-frozen lines);
        pass ``()`` to clear it. ``mcp_index`` accepts a manifest ``mcp``
        mapping, an iterable of server states, or an already-frozen block; pass
        ``{}`` to clear it.
        """
        if capabilities is not None:
            self._capabilities = capabilities
        if counter is not None:
            self._counter = counter
        if environment is not None:
            self._environment = environment
        if note_resolver is not None:
            self._note_resolver = note_resolver
        if summarizer is not None:
            self._summarizer = summarizer
        if request_counter is not None:
            self._request_counter = request_counter
        if model is not None:
            self._model_override = model
        if provider is not None:
            self._provider_override = provider
        if cache is not None:
            self.cache = cache
        raw_skills = skills_index if skills_index is not None else skills
        if raw_skills is not None:
            self._skills_index = freeze_skills_index(raw_skills)
        if mcp_index is not None:
            self._mcp_index = freeze_mcp_index(mcp_index)
        if pre_compact is not None:
            self._pre_compact = pre_compact

    def for_turn(
        self,
        *,
        capabilities: Any | None = None,
        counter: Any | None = None,
        environment: EnvironmentInfo | None = None,
        note_resolver: Any | None = None,
        summarizer: Any | None = None,
        request_counter: Any | None = None,
        model: str | None = None,
        provider: str | None = None,
        skills_index: Any | None = None,
        skills: Any | None = None,
        mcp_index: Any | None = None,
        agent_definition: Any | None = None,
        reasoning_effort: str | None = None,
    ) -> ContextManager:
        """Return a snapshot manager with config and rendered inputs frozen.

        Called once per turn by ``Session.send`` so every assembly in the turn
        reuses one effective configuration. The snapshot's environment is built
        here, which is also where a path/size error in ``SOUL.md``/``MEMORY.md``
        surfaces (before the turn appends anything). ``skills_index`` (or its
        alias ``skills``) freezes the turn's initial skills snapshot; a
        per-iteration override goes through :meth:`for_iteration`.
        """
        config = self.effective_config()
        raw_skills = skills_index if skills_index is not None else skills
        snapshot = self._spawn(
            config=config,
            capabilities=capabilities,
            counter=counter,
            environment=environment,
            note_resolver=note_resolver,
            summarizer=summarizer,
            request_counter=request_counter,
            model=model,
            provider=provider,
            reasoning_effort=reasoning_effort,
            skills_index=(
                self._skills_index
                if raw_skills is None
                else freeze_skills_index(raw_skills)
            ),
            mcp_index=(
                self._mcp_index
                if mcp_index is None
                else freeze_mcp_index(mcp_index)
            ),
        )
        snapshot._env = snapshot._build_env(config)
        return snapshot

    def _spawn(
        self,
        *,
        config: Config | None = None,
        capabilities: Any | None = None,
        counter: Any | None = None,
        environment: EnvironmentInfo | None = None,
        note_resolver: Any | None = None,
        summarizer: Any | None = None,
        request_counter: Any | None = None,
        model: str | None = None,
        provider: str | None = None,
        skills_index: Any | None = None,
        mcp_index: Any | None = None,
        pre_compact: PreCompactGate | None = None,
        turn_id: str | None = None,
        iteration: int | None = None,
        agent_definition: Any | None = None,
        reasoning_effort: str | None = None,
    ) -> ContextManager:
        """Build a sibling snapshot sharing this manager's frozen inputs.

        ``None`` arguments fall back to this manager's current values. The frozen
        ``_env`` and tool schemas are copied afterwards, so the sibling is
        independent: mutating one never changes the other.
        """
        if config is None:
            config = (
                self._env.config
                if self._env is not None
                else self.effective_config()
            )
        snapshot = ContextManager(
            self.workspace,
            config=config,
            identity=self._identity,
            max_file_bytes=self.max_file_bytes,
            system=self._system_override,
            capabilities=(
                capabilities if capabilities is not None else self._capabilities
            ),
            counter=counter if counter is not None else self._counter,
            environment=(
                environment if environment is not None else self._environment
            ),
            note_resolver=(
                note_resolver if note_resolver is not None else self._note_resolver
            ),
            summarizer=(
                summarizer if summarizer is not None else self._summarizer
            ),
            request_counter=(
                request_counter
                if request_counter is not None
                else self._request_counter
            ),
            model=model if model is not None else self._model_override,
            provider=provider if provider is not None else self._provider_override,
            parts=self._parts,
            cache=self.cache,
            keep_recent=self.keep_recent,
            min_content_chars=self.min_content_chars,
            skills_index=(
                self._skills_index
                if skills_index is None
                else freeze_skills_index(skills_index)
            ),
            mcp_index=(
                self._mcp_index
                if mcp_index is None
                else freeze_mcp_index(mcp_index)
            ),
            pre_compact=(
                pre_compact if pre_compact is not None else self._pre_compact
            ),
            turn_id=turn_id if turn_id is not None else self._turn_id,
            iteration=(
                iteration if iteration is not None else self._iteration
            ),
        )
        snapshot._tool_schemas = self._tool_schemas
        snapshot._reasoning_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else self._reasoning_effort
        )
        if reasoning_effort is None:
            snapshot._reasoning_effort = None
        snapshot.agent_definition = getattr(self, "agent_definition", None)
        snapshot._agent_effort_supported = getattr(
            self, "_agent_effort_supported", True
        )
        snapshot._env = self._env
        return snapshot

    def for_iteration(
        self,
        *,
        config: Config | None = None,
        system_files: Any = None,
        soul_text: str | None = None,
        memory_text: str | None = None,
        capabilities: Any | None = None,
        request_counter: Any | None = None,
        model: str | None = None,
        provider: str | None = None,
        skills_index: Any = _UNSET,
        skills: Any = _UNSET,
        mcp_index: Any = _UNSET,
        pre_compact: PreCompactGate | None = None,
        turn_id: str | None = None,
        iteration: int | None = None,
        agent_definition: Any | None = None,
        reasoning_effort: str | None = None,
    ) -> ContextManager:
        """Return a sibling snapshot for one loop iteration.

        Skills are re-scanned between loop iterations, so the index can change
        inside one turn. This seam freezes the new index into a *new* snapshot
        while leaving this (already-frozen) snapshot byte-for-byte stable — the
        per-turn prompt prefix never mutates under a live iteration. Omit both
        ``skills_index`` and its alias ``skills`` to clone with the current index;
        pass ``()`` to clear it.

        When ``config`` and/or a system-file snapshot is supplied the sibling's
        environment is rebuilt from those frozen values **without rereading the
        live ``SOUL.md``/``MEMORY.md``** — the seam a manifest-driven runtime uses
        so one iteration draws its config, system files, skills index, and tool
        schemas from a single pinned generation. ``system_files`` accepts a
        ``SystemFiles``-like object or a mapping of logical name to content;
        explicit ``soul_text``/``memory_text`` win when both are given. Supplying
        only ``config`` rebuilds the environment from that config (and therefore
        reads the files named by it, the pre-manifest behaviour).
        """
        if skills_index is not _UNSET:
            raw = skills_index
        elif skills is not _UNSET:
            raw = skills
        else:
            raw = _UNSET
        frozen = (
            self._skills_index if raw is _UNSET else freeze_skills_index(raw)
        )
        frozen_mcp = (
            self._mcp_index
            if mcp_index is _UNSET
            else freeze_mcp_index(mcp_index)
        )
        snapshot = self._spawn(
            config=config,
            capabilities=capabilities,
            request_counter=request_counter,
            model=model,
            provider=provider,
            skills_index=frozen,
            mcp_index=frozen_mcp,
            pre_compact=pre_compact,
            turn_id=turn_id,
            iteration=iteration,
            reasoning_effort=reasoning_effort,
        )
        snapshot._reasoning_effort = reasoning_effort
        snapshot._agent_effort_supported = reasoning_effort is not None
        snapshot.agent_definition = agent_definition
        if agent_definition is not None:
            # _spawn keeps a shared frozen environment when config is supplied;
            # iteration snapshots must own a copy before extending instructions.
            if snapshot._env is not None:
                snapshot._env = replace(snapshot._env)
            else:
                snapshot._env = snapshot._build_env(config or snapshot.effective_config())
        if config is not None and (
            system_files is not None
            or soul_text is not None
            or memory_text is not None
        ):
            frozen_soul = (
                _system_file_text(system_files, "soul")
                if soul_text is None
                else soul_text
            )
            frozen_memory = (
                _system_file_text(system_files, "memory")
                if memory_text is None
                else memory_text
            )
            snapshot._env = snapshot._build_env(
                config,
                soul_text="" if frozen_soul is None else frozen_soul,
                memory_text="" if frozen_memory is None else frozen_memory,
            )
        elif config is not None:
            snapshot._env = snapshot._build_env(config)
        if agent_definition is not None:
            snapshot.agent_definition = agent_definition
            snapshot.append_agent_prompt(agent_definition.load_body())
        return snapshot

    def freeze_skills(self, snapshot: Any) -> None:
        """Attach the frozen per-iteration skills snapshot to this manager.

        The snapshot is normalized to sanitized ``name: description`` lines once,
        so a later mutation of the injected object cannot change this manager's
        rendered prompt. ``None``/empty clears the index (the part becomes a
        no-op).
        """
        self._skills_index = freeze_skills_index(snapshot)

    def freeze_skills_index(self, snapshot: Any) -> None:
        """Alias for :meth:`freeze_skills` (matches the part's name)."""
        self.freeze_skills(snapshot)

    @property
    def skills_index(self) -> tuple[str, ...]:
        """The frozen, sanitized skills-index lines for this manager."""
        return self._skills_index

    @property
    def mcp_index(self) -> str:
        """The frozen, bounded MCP index block for this manager (``""`` if none)."""
        return self._mcp_index

    def freeze_tools(self, schemas: Any) -> None:
        """Attach the frozen per-turn tool schemas to this snapshot."""
        frozen = tuple(schemas)
        for schema in frozen:
            if not isinstance(schema, ToolSchema):
                raise TypeError("freeze_tools expects ToolSchema instances")
        self._tool_schemas = frozen

    @property
    def tool_schemas(self) -> tuple[ToolSchema, ...]:
        return self._tool_schemas

    # -- introspection -----------------------------------------------------

    @property
    def last_budget(self) -> dict[str, Any]:
        """Content-free budget accounting from the most recent assembly."""
        return dict(self._last_budget_metadata)

    @property
    def last_compaction(self) -> dict[str, Any]:
        """Content-free compaction accounting from the most recent assembly."""
        return dict(self._last_compaction_metadata)

    @property
    def last_cache(self) -> dict[str, Any]:
        """Content-free cache-boundary metadata from the most recent assembly."""
        return dict(self._last_cache_metadata)

    @property
    def last_compacted(self) -> dict[str, Any] | None:
        """Content-free summary artifact metadata from the most recent assembly."""
        return dict(self._last_compacted) if self._last_compacted else None

    @property
    def last_summary_reuse(self) -> dict[str, Any] | None:
        """Content-free metadata for a reused summary, if one was reused."""
        return dict(self._last_summary_reuse) if self._last_summary_reuse else None

    @property
    def last_accounting(self) -> dict[str, Any]:
        """Combined structured accounting from the most recent assembly."""
        return {
            "budget": self.last_budget,
            "compaction": self.last_compaction,
            "cache": self.last_cache,
        }

    @property
    def last_included_parts(self) -> dict[str, str]:
        """Exact system-text contributions included in the last request.

        This is an in-memory inspection seam. It is deliberately absent from
        request metadata and session events, which must never carry prompt text.
        """
        return dict(self._last_included_parts)

    # -- environment -------------------------------------------------------

    def _build_env(
        self,
        config: Config,
        *,
        soul_text: str | None = None,
        memory_text: str | None = None,
    ) -> AssemblyEnvironment:
        capabilities = (
            self._capabilities
            if self._capabilities is not None
            else Capabilities.conservative()
        )
        environment = self._environment
        if environment is None:
            environment = capture_environment(
                self.workspace, profile=self._profile(config)
            )
        provider, model = self._reference(config)
        return AssemblyEnvironment(
            config=config,
            capabilities=capabilities,
            model=model,
            provider=provider,
            identity=self._identity,
            soul_text=(
                self._read(config.instructions_file)
                if soul_text is None
                else soul_text
            ),
            memory_text=(
                self._read(config.memory_file)
                if memory_text is None
                else memory_text
            ),
            environment=environment,
            counter=self._counter,
            counter_async=_is_async(self._counter),
            note_resolver=self._note_resolver,
            summarizer=self._summarizer,
            summarizer_async=_is_async_summarizer(self._summarizer),
            request_counter=self._request_counter,
            max_file_bytes=self.max_file_bytes,
            workspace=self.workspace,
            system_override=self._system_override,
            strategy=self._strategy(config),
            keep_recent=self.keep_recent,
            min_content_chars=self.min_content_chars,
        )

    def append_agent_prompt(self, prompt: str) -> None:
        """Compose a selected agent prompt after configured instructions."""
        if not isinstance(prompt, str) or not prompt:
            return
        env = self._env
        if env is None:
            raise ConfigError("agent prompt requires a frozen context snapshot")
        combined = "\n\n".join(part for part in (env.soul_text.strip(), prompt) if part)
        self._env = replace(env, soul_text=combined)

    @staticmethod
    def _profile(config: Config) -> str | None:
        v2 = getattr(config, "v2", None)
        agent = getattr(v2, "agent", None)
        profile = getattr(agent, "profile", None)
        return profile if isinstance(profile, str) and profile else None

    @staticmethod
    def _strategy(config: Config) -> str:
        v2 = getattr(config, "v2", None)
        context = getattr(v2, "context", None)
        strategy = getattr(context, "compaction", None)
        return strategy if isinstance(strategy, str) else "drop_oldest"

    def _reference(self, config: Config) -> tuple[str | None, str | None]:
        """Return ``(provider, model)``; explicit overrides win."""
        if self._provider_override is not None or self._model_override is not None:
            return self._provider_override, self._model_override
        return self._model_reference(config)

    @staticmethod
    def _model_reference(config: Config) -> tuple[str | None, str | None]:
        ref = getattr(config, "model", None)
        if not ref:
            return None, None
        if "/" in ref:
            provider, _, model = ref.partition("/")
            return (provider or None), (model or None)
        return None, ref

    @staticmethod
    def _sampling(
        config: Config, *, reasoning_effort: str | None = None
    ) -> SamplingParams:
        v2 = getattr(config, "v2", None)
        params = getattr(getattr(v2, "model", None), "params", None)
        if params is None:
            return SamplingParams(reasoning_effort=reasoning_effort)
        return SamplingParams(
            temperature=params.temperature,
            max_output_tokens=params.max_output_tokens,
            thinking_budget=params.thinking_budget,
            reasoning_effort=reasoning_effort,
        )

    def _read(self, filename: str) -> str:
        # ``resolve_within`` rejects ``../``, symlink escapes, and NUL bytes as
        # ConfigError. Reading a bounded prefix avoids the stat-then-read TOCTOU
        # race and never allocates more than the cap.
        path = resolve_within(self.workspace, filename)
        try:
            with open(path, "rb") as handle:
                data = handle.read(self.max_file_bytes + 1)
        except FileNotFoundError:
            return ""
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"Cannot read context file {filename!r}: {exc}"
            ) from exc
        if len(data) > self.max_file_bytes:
            raise ConfigError(
                f"Context file {filename!r} is too large "
                f"(> {self.max_file_bytes} bytes)"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigError(
                f"Cannot read context file {filename!r}: {exc}"
            ) from exc

    # -- assembly ----------------------------------------------------------

    def assemble(self, session: Any) -> ModelRequest | Any:
        """Build the request for the current state of ``session``.

        Returns a :class:`ModelRequest` synchronously unless an async token
        counter, a request-aware counter, or an async summarizer is configured —
        or a pre-compaction gate or a sync summarizer returns an awaitable — in
        which case it returns an awaitable the loop's ``_maybe_await`` consumes
        transparently.
        """
        self._last_precompact = None
        self._precompact_ran = False
        self._precompact_blocked = False
        config = self.effective_config()
        env = self._env if self._env is not None else self._build_env(config)
        if (
            env.counter_async
            or env.request_counter is not None
            or env.summarizer_async
        ):
            return self._assemble_async(session, env)
        return self._assemble_sync(session, env)

    def _make_context(
        self, env: AssemblyEnvironment, messages: Any, session: Any = None
    ) -> AssemblyContext:
        snapshot = tuple(messages)
        return AssemblyContext(
            workspace=env.workspace,
            config=env.config,
            identity=env.identity,
            soul_text=env.soul_text,
            memory_text=env.memory_text,
            environment=env.environment,
            tool_schemas=tuple(self._tool_schemas),
            messages=snapshot,
            current_user_index=current_user_index(snapshot),
            capabilities=env.capabilities,
            model=env.model,
            provider=env.provider,
            max_file_bytes=env.max_file_bytes,
            note_resolver=env.note_resolver,
            summarizer=env.summarizer,
            session=session,
            skills_index=self._skills_index,
            mcp_index=self._mcp_index,
        )

    def _assemble_sync(self, session: Any, env: AssemblyEnvironment) -> Any:
        ctx = self._make_context(env, session.messages, session)
        outputs = render_parts(self._parts, ctx)

        def count(text: str) -> int:
            value = _invoke_counter(env.counter, text)
            if inspect.isawaitable(value):
                raise TypeError(
                    "token counter returned an awaitable during synchronous "
                    "assembly; configure the manager with the async counter "
                    "before calling assemble()"
                )
            return int(value)

        costs = self._gather(outputs, count, env)
        # A sync summarizer that returns an awaitable (not an ``async def``) is
        # surfaced to the caller, which awaits it on the loop's async path.
        # Returning it here is deliberate: no TypeError, no event loop needed.
        return self._finalize(env, ctx, outputs, costs)

    async def _assemble_async(self, session: Any, env: AssemblyEnvironment) -> ModelRequest:
        ctx = self._make_context(env, session.messages, session)
        outputs = render_parts(self._parts, ctx)

        async def count(text: str) -> int:
            value = _invoke_counter(env.counter, text)
            if inspect.isawaitable(value):
                value = await value
            return int(value)

        costs = await self._gather_async(outputs, count, env)
        request = await _await_if_needed(self._finalize(env, ctx, outputs, costs))
        return await self._maybe_refine(request, env, ctx, outputs, costs)

    def _gather(
        self,
        outputs: tuple[PartOutput | None, ...],
        count: Callable[[str], int],
        env: AssemblyEnvironment,
    ) -> _Costs:
        part_costs: dict[str, int] = {}
        history: tuple[Message, ...] = ()
        user_costs: tuple[int, ...] = ()
        line_costs: dict[str, tuple[int, ...]] = {}
        for output in outputs:
            if output is None:
                continue
            if output.kind == "text":
                part_costs[output.name] = count(output.text) if output.text else 0
            elif output.kind == "tools":
                part_costs["tools"] = sum(
                    count(canonical_tool_text(schema)) for schema in output.tools
                )
            elif output.kind == "skills_index":
                costs = tuple(count(line) for line in output.lines)
                line_costs[output.name] = costs
                part_costs[output.name] = sum(costs)
            elif output.kind == "user":
                user_costs = tuple(
                    count(canonical_message_text(message))
                    for message in output.messages
                )
                part_costs["user"] = sum(user_costs)
            elif output.kind == "history":
                history = output.messages
        history_costs = tuple(
            count(canonical_message_text(message)) for message in history
        )
        evicted = self._eviction_messages(history, env)
        evicted_costs = (
            tuple(count(canonical_message_text(message)) for message in evicted)
            if evicted is not None
            else None
        )
        return _Costs(
            part_costs=part_costs,
            history=history,
            history_costs=history_costs,
            user_costs=user_costs,
            evicted_messages=evicted,
            evicted_costs=evicted_costs,
            line_costs=line_costs,
        )

    async def _gather_async(
        self,
        outputs: tuple[PartOutput | None, ...],
        count: Callable[[str], Any],
        env: AssemblyEnvironment,
    ) -> _Costs:
        part_costs: dict[str, int] = {}
        history: tuple[Message, ...] = ()
        user_costs: tuple[int, ...] = ()
        line_costs: dict[str, tuple[int, ...]] = {}
        for output in outputs:
            if output is None:
                continue
            if output.kind == "text":
                part_costs[output.name] = (
                    await count(output.text) if output.text else 0
                )
            elif output.kind == "tools":
                part_costs["tools"] = sum(
                    [await count(canonical_tool_text(schema)) for schema in output.tools]
                )
            elif output.kind == "skills_index":
                costs = tuple([await count(line) for line in output.lines])
                line_costs[output.name] = costs
                part_costs[output.name] = sum(costs)
            elif output.kind == "user":
                user_costs = tuple(
                    [await count(canonical_message_text(m)) for m in output.messages]
                )
                part_costs["user"] = sum(user_costs)
            elif output.kind == "history":
                history = output.messages
        history_costs = tuple(
            [await count(canonical_message_text(m)) for m in history]
        )
        evicted = self._eviction_messages(history, env)
        evicted_costs = (
            tuple([await count(canonical_message_text(m)) for m in evicted])
            if evicted is not None
            else None
        )
        return _Costs(
            part_costs=part_costs,
            history=history,
            history_costs=history_costs,
            user_costs=user_costs,
            evicted_messages=evicted,
            evicted_costs=evicted_costs,
            line_costs=line_costs,
        )

    @staticmethod
    def _eviction_messages(
        history: tuple[Message, ...], env: AssemblyEnvironment
    ) -> tuple[Message, ...] | None:
        if env.strategy not in ("evict_tool_results", "hybrid"):
            return None
        if not history:
            return None
        return evict_tool_results(
            history,
            note_resolver=env.note_resolver,
            keep_recent=env.keep_recent,
            min_content_chars=env.min_content_chars,
        ).messages

    @staticmethod
    def _repair_pairing(
        history: tuple[Message, ...], user_tail: tuple[Message, ...], keep: int
    ) -> int:
        """Grow ``keep`` so no retained ``ToolResult`` is orphaned.

        Whole-message dropping can otherwise retain a tool result while dropping
        the assistant message that carried its ``ToolUse``. The pinned user tail
        is always retained, so its results constrain the boundary too. The
        correction only ever retains *more*, never fewer, messages.
        """
        if not history:
            return keep
        use_index: dict[str, int] = {}
        for index, message in enumerate(history):
            for block in message.content:
                if isinstance(block, ToolUse):
                    use_index.setdefault(block.id, index)
        start = len(history) - keep

        def result_ids(messages: tuple[Message, ...]):
            for message in messages:
                for block in message.content:
                    if isinstance(block, ToolResult):
                        yield block.tool_use_id

        changed = True
        while changed:
            changed = False
            for tool_use_id in result_ids(history[start:]):
                origin = use_index.get(tool_use_id)
                if origin is not None and origin < start:
                    start = origin
                    changed = True
            for tool_use_id in result_ids(user_tail):
                origin = use_index.get(tool_use_id)
                if origin is not None and origin < start:
                    start = origin
                    changed = True
        return len(history) - start

    # -- finalization ------------------------------------------------------

    def _finalize(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        outputs: tuple[PartOutput | None, ...],
        costs: _Costs,
        *,
        history_budget_override: int | None = None,
        persist_summary: bool = True,
    ) -> ModelRequest | Any:
        if persist_summary:
            # A fresh pass reports only what it actually persists or reuses; a
            # refinement pass reports what the refined (sent) request uses.
            self._last_compacted = None
            self._last_summary_reuse = None
        inputs = BudgetInputs.from_config_and_caps(env.config, env.capabilities)
        caps = part_caps(env.config)
        requests: list[PartRequest] = []
        for output in outputs:
            if output is None:
                continue
            priority = PART_PRIORITY.get(output.name, 3)
            if output.kind == "text":
                requests.append(
                    PartRequest(
                        name=output.name,
                        priority=priority,
                        tokens=costs.part_costs.get(output.name, 0),
                        cap=caps.get(output.name),
                        required=priority == 0,
                    )
                )
            elif output.kind == "tools":
                requests.append(
                    PartRequest(
                        name="tools",
                        priority=priority,
                        tokens=costs.part_costs.get("tools", 0),
                        required=True,
                    )
                )
            elif output.kind == "skills_index":
                requests.append(
                    PartRequest(
                        name=output.name,
                        priority=priority,
                        tokens=costs.part_costs.get(output.name, 0),
                        cap=caps.get(output.name),
                        required=priority == 0,
                    )
                )
            elif output.kind == "user":
                requests.append(
                    PartRequest(
                        name="user",
                        priority=priority,
                        tokens=costs.part_costs.get("user", 0),
                        required=True,
                    )
                )
        plan = allocate(inputs, requests)
        if history_budget_override is not None:
            plan = replace(
                plan, history_budget=max(0, int(history_budget_override))
            )
        else:
            plan = self._apply_compaction_threshold(env, plan, costs)

        eligible_costs = (
            costs.evicted_costs
            if costs.evicted_costs is not None
            else costs.history_costs
        )
        keep = fit_suffix_count(eligible_costs, plan.history_budget)
        keep = self._repair_pairing(costs.history, ctx.user_tail(), keep)
        # Real budget/eviction facts for the PreCompact request, captured just
        # before the gate runs.
        self._precompact_budgets = (int(plan.input_budget), int(plan.history_budget))
        self._precompact_evicted = (
            len(costs.evicted_costs) if costs.evicted_costs is not None else 0
        )
        compaction = self._compact_history(
            env, ctx, costs.history, keep, persist=persist_summary
        )
        if inspect.isawaitable(compaction):
            return self._await_compaction(
                env, ctx, outputs, costs, plan, inputs, eligible_costs,
                compaction,
            )
        return self._build_request(
            env, ctx, outputs, costs, plan, inputs, eligible_costs,
            compaction,
        )

    async def _await_compaction(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        outputs: tuple[PartOutput | None, ...],
        costs: _Costs,
        plan: Any,
        inputs: BudgetInputs,
        eligible_costs: tuple[int, ...],
        compaction: Any,
    ) -> ModelRequest:
        resolved = await compaction
        return self._build_request(
            env, ctx, outputs, costs, plan, inputs, eligible_costs, resolved
        )

    def _build_request(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        outputs: tuple[PartOutput | None, ...],
        costs: _Costs,
        plan: Any,
        inputs: BudgetInputs,
        eligible_costs: tuple[int, ...],
        compaction: CompactionResult,
    ) -> ModelRequest:
        system = self._render_system(env, outputs, costs, plan)
        tools = self._render_tools(outputs)
        final_messages = [*compaction.messages, *ctx.user_tail()]

        text_tokens = sum(
            plan.granted(output.name)
            for output in outputs
            if output is not None and output.kind in ("text", "skills_index")
        )
        user_tokens = costs.part_costs.get("user", 0)
        # Accounting is derived from the *actual* compacted message set: the
        # retained original messages are the compacted messages minus the pinned
        # summary (if any), and the summary's own cost is added back.
        summary_present = compaction.summary_id is not None
        retained_count = len(compaction.messages) - (1 if summary_present else 0)
        retained_count = max(0, min(retained_count, len(eligible_costs)))
        retained_costs = (
            eligible_costs[len(eligible_costs) - retained_count :]
            if retained_count
            else ()
        )
        summary_tokens = (
            int(compaction.tokens_after)
            if summary_present and isinstance(compaction.tokens_after, int)
            else 0
        )
        used_tokens = (
            text_tokens
            + plan.granted("tools")
            + sum(retained_costs)
            + user_tokens
            + summary_tokens
        )

        boundaries = prompt_cache_boundaries(
            env.capabilities,
            system_tools_position=0,
            history_position=len(compaction.messages),
        )
        cache_metadata = {
            "enabled": bool(getattr(env.capabilities, "prompt_caching", False)),
            "boundaries": boundaries_metadata(boundaries),
        }
        self._last_budget_metadata = plan.metadata()
        self._last_compaction_metadata = compaction.metadata()
        self._last_cache_metadata = cache_metadata

        metadata = {
            "context": {
                "input_budget": plan.input_budget,
                # The whole window (before the output reserve and safety
                # margin): what a UI shows usage against.
                "context_window": inputs.context_window,
                "effective_max_output_tokens": inputs.effective_max_output_tokens,
                "safety_margin_tokens": inputs.safety_margin_tokens,
                "history_budget": plan.history_budget,
                "used_tokens": used_tokens,
                "final_messages": len(final_messages),
                "required_tokens": plan.required_tokens,
                "history_retained": len(compaction.messages),
                "history_dropped": compaction.dropped,
                "evicted": compaction.evicted,
                "strategy": compaction.strategy,
                # Only an actual, newly-persisted compaction is reported here so
                # the loop emits ``context.compacted`` once and never for a
                # reused artifact.
                "compacted": dict(self._last_compacted)
                if self._last_compacted
                else None,
                "summary_reused": dict(self._last_summary_reuse)
                if self._last_summary_reuse
                else None,
            },
            "cache": cache_metadata,
        }
        if self._last_precompact is not None:
            metadata["pre_compact"] = dict(self._last_precompact)
        agent_fallback = getattr(
            getattr(self, "agent_definition", None), "fallback", None
        )
        if isinstance(agent_fallback, (tuple, list)) and agent_fallback:
            # Tried by the router before the workspace ``models.fallback``.
            metadata["agent_fallback"] = [
                str(ref) for ref in agent_fallback[:8] if isinstance(ref, str)
            ]
        agent_effort = getattr(
            getattr(self, "agent_definition", None), "reasoning_effort", None
        )
        effort = self._reasoning_effort
        if effort is None:
            effort = getattr(self, "_requested_agent_effort", None)
        if not getattr(self, "_agent_effort_supported", True):
            agent_effort = None
        if (
            effort is None
            and agent_effort
            and env.capabilities.thinking
            and getattr(self, "_agent_effort_supported", True)
        ):
            effort = agent_effort
        return ModelRequest(
            messages=final_messages,
            system=system,
            tools=tools,
            params=self._sampling(
                env.config,
                reasoning_effort=effort,
            ),
            model=env.model,
            provider=env.provider,
            metadata=metadata,
        )

    # -- proactive compaction threshold -----------------------------------

    @staticmethod
    def _compaction_fraction(config: Config) -> float | None:
        v2 = getattr(config, "v2", None)
        context = getattr(v2, "context", None)
        value = getattr(context, "compact_at_fraction", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if value <= 0.0 or value > 1.0:
            return None
        return value

    def _apply_compaction_threshold(
        self, env: AssemblyEnvironment, plan: Any, costs: _Costs
    ) -> Any:
        """Reduce the history budget to the configured fraction when triggered.

        The threshold is a fraction of the available input budget (which already
        excludes the output reserve and safety margin). Required parts and their
        grants are never touched; when the projected total is at or below the
        threshold the plan is returned unchanged, so no compaction happens.
        Identical inputs always produce the same decision.
        """
        fraction = self._compaction_fraction(env.config)
        if fraction is None:
            return plan
        optional_grants = sum(
            allocation.granted_tokens
            for allocation in plan.allocations
            if allocation.priority > 0
        )
        used_no_history = plan.required_tokens + optional_grants
        eligible = (
            costs.evicted_costs
            if costs.evicted_costs is not None
            else costs.history_costs
        )
        history_total = sum(eligible)
        trigger = int(plan.input_budget * fraction)
        if used_no_history + history_total <= trigger:
            return plan
        target = max(0, trigger - used_no_history)
        if target < plan.history_budget:
            return replace(plan, history_budget=target)
        return plan

    async def _maybe_refine(
        self,
        request: ModelRequest,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        outputs: tuple[PartOutput | None, ...],
        costs: _Costs,
    ) -> ModelRequest:
        """Second deterministic compaction pass driven by an exact provider count.

        Estimates are advisory. When a request-aware counter reports the exact
        input token count and it exceeds the input budget, history is shrunk by
        the deficit and the request rebuilt **once**. The refined request is
        compacted with the same rules (including summary reuse/extension), so it
        always includes the persisted/reused summary it reports and never claims
        a summary while dropping unsummarized content.
        """
        counter = env.request_counter
        if counter is None:
            return request
        try:
            actual = await _invoke_request_counter(counter, request)
        except Exception:  # noqa: BLE001 - counting must never fail a turn
            actual = None
        context = request.metadata.get("context")
        if not isinstance(context, dict):
            return request
        if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
            return request
        context["actual_tokens"] = actual
        budget = context.get("input_budget", 0)
        if not isinstance(budget, int) or actual <= budget:
            return request
        base = context.get("history_budget", 0)
        if not isinstance(base, int):
            base = 0
        reduced = max(0, base - (actual - budget))
        context["refined"] = True
        refined = await _await_if_needed(
            self._finalize(
                env,
                ctx,
                outputs,
                costs,
                history_budget_override=reduced,
                persist_summary=True,
            )
        )
        refined_context = refined.metadata.get("context")
        if isinstance(refined_context, dict):
            refined_context["actual_tokens"] = actual
            refined_context["refined"] = True
        return refined

    # -- summary persistence and reuse ------------------------------------

    @staticmethod
    def _can_persist(ctx: AssemblyContext) -> bool:
        session = ctx.session
        if session is None:
            return False
        return callable(getattr(session, "append_summary", None))

    def _message_seqs(self, ctx: AssemblyContext) -> tuple[int, ...]:
        session = ctx.session
        getter = getattr(session, "message_seqs", None)
        if callable(getter):
            try:
                return tuple(int(value) for value in getter())
            except Exception:  # noqa: BLE001 - seq range is best-effort metadata
                return ()
        return ()

    @staticmethod
    def _latest_summary(ctx: AssemblyContext) -> Any | None:
        session = ctx.session
        getter = getattr(session, "latest_summary", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001 - a bad artifact must not fail a turn
                return None
        return None

    @staticmethod
    def _same_summary_strategy(record: Any, strategy: str) -> bool:
        """Whether a durable artifact was produced by the same strategy.

        Reuse/extension across ``summarize`` and ``hybrid`` is not proven
        semantically equivalent, so it is refused rather than relabelled.
        """
        return getattr(record, "strategy", None) == strategy

    def _covered_count(self, ctx: AssemblyContext, record: Any) -> int:
        """How many leading messages a durable summary covers."""
        if record is None:
            return 0
        source_to = getattr(record, "source_to_seq", 0) or 0
        source_messages = getattr(record, "source_messages", 0) or 0
        seqs = self._message_seqs(ctx)
        total = len(ctx.messages)
        if source_to and seqs:
            count = sum(1 for seq in seqs if seq <= source_to)
            if count:
                return max(0, min(count, total))
        return max(0, min(int(source_messages), total))

    @staticmethod
    def _materialize_summary(record: Any) -> Message:
        source_messages = int(getattr(record, "source_messages", 0) or 0)
        text = getattr(record, "text", "") or ""
        return Message(
            role="user",
            content=[
                Text(text=f"[summary of {source_messages} earlier messages]\n{text}")
            ],
        )

    @staticmethod
    def _prior_summary_message(record: Any) -> Message:
        """The prior artifact as summarizer input (raw text, no nested wrapper)."""
        return Message(role="assistant", content=[Text(text=getattr(record, "text", "") or "")])

    @staticmethod
    def _summary_digest(
        messages: Sequence[Message], strategy: str, prior_id: str
    ) -> str:
        payload = {
            "strategy": strategy,
            "prior": prior_id,
            "messages": [canonical_message_text(message) for message in messages],
        }
        return semantic_key(payload)

    @staticmethod
    def _find_summary(ctx: AssemblyContext, digest: str, strategy: str) -> Any | None:
        session = ctx.session
        getter = getattr(session, "summary_for", None)
        if callable(getter):
            try:
                return getter(digest, strategy)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _reuse_result(
        self,
        record: Any,
        suffix: tuple[Message, ...],
        strategy: str,
        dropped: int,
        evicted: int,
        evicted_actions: Sequence[Any],
    ) -> CompactionResult:
        text = getattr(record, "text", "") or ""
        source_messages = int(getattr(record, "source_messages", dropped) or dropped)
        summary_id = getattr(record, "summary_id", "") or None
        tokens_before = getattr(record, "tokens_before", None)
        tokens_after = getattr(record, "tokens_after", None)
        self._last_summary_reuse = {
            "strategy": strategy,
            "summary_id": summary_id,
            "source_from_seq": getattr(record, "source_from_seq", 0),
            "source_to_seq": getattr(record, "source_to_seq", 0),
            "source_messages": source_messages,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "summary_seq": getattr(record, "seq", None),
        }
        summary_message = Message(
            role="user",
            content=[
                Text(text=f"[summary of {source_messages} earlier messages]\n{text}")
            ],
        )
        action = SummarizeAction(
            summarized_messages=source_messages,
            summary_chars=len(text),
            tokens=tokens_after,
        )
        return CompactionResult(
            messages=(summary_message, *suffix),
            actions=(*evicted_actions, action),
            strategy=strategy,
            dropped=dropped,
            evicted=evicted,
            summary_id=summary_id,
            summary_reused=True,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    def _drop_result(
        self,
        suffix: tuple[Message, ...],
        strategy: str,
        dropped: int,
        evicted: int,
        evicted_actions: Sequence[Any],
    ) -> CompactionResult:
        action = DropOldestAction(
            dropped_messages=dropped, retained_messages=len(suffix)
        )
        return CompactionResult(
            messages=suffix,
            actions=(*evicted_actions, action),
            strategy=strategy,
            dropped=dropped,
            evicted=evicted,
        )

    def _finish_summary(
        self,
        artifact: Any,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        dropped: int,
        suffix: tuple[Message, ...],
        strategy: str,
        digest: str,
        evicted: int,
        evicted_actions: Sequence[Any],
    ) -> CompactionResult:
        text = artifact.text if hasattr(artifact, "text") else str(artifact)
        if not isinstance(text, str) or not text.strip():
            # Fail before persisting anything: no partial artifact is created.
            raise CompactionError("summarizer returned an empty summary")
        append = getattr(ctx.session, "append_summary", None)
        if not callable(append):  # pragma: no cover - guarded by _can_persist
            raise CompactionError("no durable session sink for summary artifact")
        seqs = self._message_seqs(ctx)
        source_from_seq = seqs[0] if seqs else 0
        if seqs and dropped:
            source_to_seq = seqs[min(dropped, len(seqs)) - 1]
        elif seqs:
            source_to_seq = seqs[-1]
        else:
            source_to_seq = 0
        before = sum(
            DEFAULT_TOKENIZER.count_tokens(canonical_message_text(message))
            for message in history[:dropped]
        )
        after = DEFAULT_TOKENIZER.count_tokens(text)
        summary_id = new_id()
        record = append(
            text=text,
            summary_id=summary_id,
            strategy=strategy,
            input_digest=digest,
            source_from_seq=source_from_seq,
            source_to_seq=source_to_seq,
            source_messages=dropped,
            tokens_before=before,
            tokens_after=after,
            provider=ctx.provider,
            model=ctx.model,
        )
        self._last_compacted = {
            "strategy": strategy,
            "summary_id": summary_id,
            "input_digest": digest,
            "source_from_seq": source_from_seq,
            "source_to_seq": source_to_seq,
            "source_messages": dropped,
            "tokens_before": before,
            "tokens_after": after,
            "summary_seq": getattr(record, "seq", None),
        }
        summary_message = Message(
            role="user",
            content=[Text(text=f"[summary of {dropped} earlier messages]\n{text}")],
        )
        action = SummarizeAction(
            summarized_messages=dropped,
            summary_chars=len(text),
            tokens=getattr(artifact, "tokens", None),
        )
        return CompactionResult(
            messages=(summary_message, *suffix),
            actions=(*evicted_actions, action),
            strategy=strategy,
            dropped=dropped,
            evicted=evicted,
            summary_id=summary_id,
            summary_reused=False,
            tokens_before=before,
            tokens_after=after,
        )

    async def _finish_summary_async(self, produced: Any, *args: Any) -> CompactionResult:
        artifact = await produced
        return self._finish_summary(artifact, *args)

    def _durable_summary(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        dropped: int,
        suffix: tuple[Message, ...],
        strategy: str,
        persist: bool,
        *,
        evicted: int = 0,
        evicted_actions: Sequence[Any] = (),
    ) -> CompactionResult | Any:
        """Summarize the dropped prefix, reusing or extending a durable artifact.

        * a durable artifact that already covers at least ``dropped`` messages is
          reused verbatim (nothing persisted, no model call);
        * otherwise the prior artifact plus the newly covered messages are
          summarized into one new artifact with an expanded source range;
        * with no durable sink or summarizer the prefix is dropped instead.
        """
        if dropped <= 0:
            return CompactionResult(
                messages=history,
                strategy=strategy,
                evicted=evicted,
                actions=tuple(evicted_actions),
            )
        prior = self._latest_summary(ctx)
        if prior is not None and not self._same_summary_strategy(prior, strategy):
            # A summary produced under a different strategy is not reused or
            # relabelled: the prefix is summarized fresh under ``strategy``.
            prior = None
        covered = self._covered_count(ctx, prior) if prior is not None else 0
        if prior is not None and covered >= dropped:
            return self._reuse_result(
                prior, history[covered:], strategy, covered, evicted, evicted_actions
            )
        if not persist or env.summarizer is None or not self._can_persist(ctx):
            return self._drop_result(
                suffix, strategy, dropped, evicted, evicted_actions
            )
        extension = history[covered:dropped]
        if prior is not None and covered > 0:
            input_messages = [self._prior_summary_message(prior), *extension]
            prior_id = getattr(prior, "summary_id", "") or ""
        else:
            input_messages = list(history[:dropped])
            prior_id = ""
        digest = self._summary_digest(input_messages, strategy, prior_id)
        existing = self._find_summary(ctx, digest, strategy)
        if existing is not None:
            return self._reuse_result(
                existing, history[dropped:], strategy, dropped, evicted, evicted_actions
            )
        produced = env.summarizer.summarize(input_messages)
        args = (
            env,
            ctx,
            history,
            dropped,
            suffix,
            strategy,
            digest,
            evicted,
            evicted_actions,
        )
        if inspect.isawaitable(produced):
            return self._finish_summary_async(produced, *args)
        return self._finish_summary(produced, *args)

    # -- compaction strategies --------------------------------------------

    def _compact_history(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        keep: int,
        *,
        persist: bool = True,
    ) -> CompactionResult | Any:
        """Run the ``PreCompact`` gate (once per assemble) then dispatch.

        The gate is consulted for the first compaction of an assembly --
        including a refinement pass -- so it always runs before any trimming,
        eviction, summarization, or drop. A block returns the un-compacted
        history (nothing is persisted) and records the block in metadata.
        """
        if self._precompact_blocked:
            # A prior pass of this assembly was blocked: never compact, never
            # persist a summary, on any subsequent pass either.
            return CompactionResult(
                messages=tuple(history), strategy="pre_compact_blocked"
            )
        gate = self._pre_compact
        if (
            gate is not None
            and not self._precompact_ran
            and self._will_compact(env, history, keep)
        ):
            self._precompact_ran = True
            return self._compact_with_gate(env, ctx, history, keep, persist, gate)
        return self._dispatch_compact(env, ctx, history, keep, persist)

    @staticmethod
    def _will_compact(
        env: AssemblyEnvironment, history: tuple[Message, ...], keep: int
    ) -> bool:
        if keep < len(history):
            return True
        return env.strategy in ("evict_tool_results", "hybrid")

    async def _compact_with_gate(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        keep: int,
        persist: bool,
        gate: PreCompactGate,
    ) -> CompactionResult:
        request = PreCompactRequest(
            session_id=getattr(getattr(ctx, "session", None), "id", None),
            turn_id=self._turn_id,
            iteration=self._iteration,
            strategy=env.strategy,
            keep=keep,
            history_messages=len(history),
            dropped_messages=max(0, len(history) - keep),
            evicted_tool_results=self._precompact_evicted,
            input_budget=self._precompact_budgets[0],
            history_budget=self._precompact_budgets[1],
        )
        decision = await _await_if_needed(gate(request))
        outcome = (
            dict(decision.outcome)
            if getattr(decision, "outcome", None) is not None
            else None
        )
        if getattr(decision, "blocked", False):
            self._precompact_blocked = True
            self._last_precompact = {
                "blocked": True,
                "reason": str(
                    getattr(decision, "reason", "") or "blocked by PreCompact hook"
                ),
                "outcome": outcome,
                "options": None,
            }
            return CompactionResult(
                messages=tuple(history), strategy="pre_compact_blocked"
            )
        options = getattr(decision, "options", None)
        compaction_env = env
        effective_keep = keep
        if isinstance(options, CompactionOptions):
            compaction_env = replace(
                env,
                strategy=options.strategy or env.strategy,
                keep_recent=(
                    options.keep_recent
                    if options.keep_recent is not None
                    else env.keep_recent
                ),
                min_content_chars=(
                    options.min_content_chars
                    if options.min_content_chars is not None
                    else env.min_content_chars
                ),
            )
            if options.keep is not None:
                effective_keep = options.keep
        self._last_precompact = {
            "blocked": False,
            "reason": "",
            "outcome": outcome,
            "options": options.to_dict() if options is not None else None,
        }
        return await _await_if_needed(
            self._dispatch_compact(
                compaction_env, ctx, history, effective_keep, persist
            )
        )

    def _dispatch_compact(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        keep: int,
        persist: bool = True,
    ) -> CompactionResult | Any:
        """Dispatch to the configured strategy.

        Summarizer failure policy: explicit ``summarize`` propagates a
        non-cancellation failure as an actionable error; ``hybrid`` degrades to
        eviction/drop with accurate metadata; cancellation always propagates.
        """
        strategy = env.strategy
        if strategy == "evict_tool_results":
            return self._evict_compact(env, history, keep)
        if strategy == "summarize":
            return self._summarize_compact(env, ctx, history, keep, persist)
        if strategy == "hybrid":
            return self._hybrid_compact(env, ctx, history, keep, persist)
        return drop_oldest(history, keep=keep)

    @staticmethod
    def _evict_compact(
        env: AssemblyEnvironment, history: tuple[Message, ...], keep: int
    ) -> CompactionResult:
        evicted = evict_tool_results(
            history,
            note_resolver=env.note_resolver,
            keep_recent=env.keep_recent,
            min_content_chars=env.min_content_chars,
        )
        retained = evicted.messages[len(evicted.messages) - keep :] if keep else ()
        dropped = len(history) - keep
        actions = list(evicted.actions)
        if dropped > 0:
            actions.append(
                DropOldestAction(dropped_messages=dropped, retained_messages=keep)
            )
        return CompactionResult(
            messages=retained,
            actions=tuple(actions),
            strategy="evict_tool_results",
            dropped=dropped,
            evicted=evicted.evicted,
        )

    def _summarize_compact(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        keep: int,
        persist: bool,
    ) -> CompactionResult | Any:
        if keep >= len(history) or not history:
            return CompactionResult(messages=history, strategy="summarize")
        dropped = len(history) - keep
        suffix = history[len(history) - keep :]
        return self._durable_summary(
            env, ctx, history, dropped, suffix, "summarize", persist
        )

    def _hybrid_compact(
        self,
        env: AssemblyEnvironment,
        ctx: AssemblyContext,
        history: tuple[Message, ...],
        keep: int,
        persist: bool,
    ) -> CompactionResult | Any:
        evicted = evict_tool_results(
            history,
            note_resolver=env.note_resolver,
            keep_recent=env.keep_recent,
            min_content_chars=env.min_content_chars,
        )
        if keep >= len(history) or not history:
            # No message is dropped, but eviction may still have replaced large
            # tool-result content. Return the *evicted* set so the sent request,
            # actions, and ``evicted`` count all agree.
            return CompactionResult(
                messages=evicted.messages,
                actions=tuple(evicted.actions),
                strategy="hybrid",
                evicted=evicted.evicted,
            )
        dropped = len(history) - keep
        suffix = evicted.messages[len(evicted.messages) - keep :]
        try:
            produced = self._durable_summary(
                env,
                ctx,
                evicted.messages,
                dropped,
                suffix,
                "hybrid",
                persist,
                evicted=evicted.evicted,
                evicted_actions=evicted.actions,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - hybrid degrades to eviction/drop
            # A non-cancellation summarizer failure must not break a hybrid turn:
            # fall back to the evicted suffix with accurate metadata. The
            # explicit ``summarize`` strategy stays actionable (no catch there).
            return self._drop_result(
                suffix, "hybrid", dropped, evicted.evicted, evicted.actions
            )
        if inspect.isawaitable(produced):
            return self._hybrid_degrade_async(
                produced, suffix, dropped, evicted.evicted, evicted.actions
            )
        return produced

    async def _hybrid_degrade_async(
        self,
        produced: Any,
        suffix: tuple[Message, ...],
        dropped: int,
        evicted: int,
        evicted_actions: Sequence[Any],
    ) -> CompactionResult:
        try:
            return await produced
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - hybrid degrades to eviction/drop
            return self._drop_result(
                suffix, "hybrid", dropped, evicted, evicted_actions
            )

    # -- rendering helpers -------------------------------------------------

    def _render_system(
        self,
        env: AssemblyEnvironment,
        outputs: tuple[PartOutput | None, ...],
        costs: _Costs,
        plan: Any,
    ) -> str | None:
        if env.system_override is not None:
            self._last_included_parts = (
                {"system_override": env.system_override}
                if env.system_override.strip()
                else {}
            )
            return env.system_override
        pieces: list[str] = []
        included: dict[str, str] = {}
        for output in outputs:
            if output is None:
                continue
            granted = plan.granted(output.name)
            if granted <= 0:
                continue
            if output.kind == "text":
                if not output.text:
                    continue
                requested = costs.part_costs.get(output.name, 0)
                text = _truncate_text(output.text, requested, granted)
                if text.strip():
                    pieces.append(text)
                    included[output.name] = text
            elif output.kind == "skills_index":
                # Whole lines only: a token budget drops entries, never halves one.
                kept = _fit_whole_lines(
                    output.lines, costs.line_costs.get(output.name, ()), granted
                )
                if kept:
                    text = "\n".join(kept)
                    pieces.append(text)
                    included[output.name] = text
        self._last_included_parts = included
        return "\n\n".join(pieces) or None

    @staticmethod
    def _render_tools(
        outputs: tuple[PartOutput | None, ...],
    ) -> list[ToolSchema]:
        for output in outputs:
            if output is not None and output.kind == "tools":
                return list(output.tools)
        return []
