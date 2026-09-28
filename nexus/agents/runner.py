"""SubagentRunner: bounded, nested execution of subagent definitions.

Plan sections 5.6 and 15.6-15.8. ``Task`` is one tool with two modes -- a named
role (``subagent_type``) and an ad-hoc agent (``tools``/``model``) -- and the
runner is the harness half that turns a request into a child run without ever
importing ``nexus.runtime``.

The runner is a manager-layer (L3) component. It reaches the child world only
through two injected seams so it can be tested with fakes and so a hot-loaded
tool can never grab the whole stack:

* **RuntimeFactory** -- builds an opaque :class:`ChildRuntime` from a frozen
  :class:`ChildSpec`; the concrete nested ``Runtime`` is supplied by the
  composition root, never constructed here;
* **SessionFacade** -- allocates the child session id (``<parent>/sub/<n>``) and
  closes it. The default facade produces the plan's logical id; a runtime that
  needs a different storage mapping injects its own.

Four bounding rules are computed **before** the child exists (plan section
15.8), so a child can never obtain authority its parent lacks:

1. **Tools intersect.** The child's set is
   ``parent_tools & role_tools & requested_tools``; anything dropped is reported
   back so the model learns rather than silently getting less than it asked for.
2. **Permissions inherit.** The parent's permission snapshot and session grants
   are passed through unchanged; the runner never widens them.
3. **Tier is capped.** The requested/reference tier is resolved and clamped to
   ``max_tier`` (``agent.clamped`` is emitted, never silently swallowed).
4. **Fan-out is capped.** A shared, concurrency-safe :class:`SubagentBudget`
   bounds depth, concurrent children, total fan-out, and the aggregate token and
   cost spend across the whole tree.

Events are relayed onto the parent sink through an **isolated** per-child relay
that stamps an ``agent`` metadata block (id, parent, depth, type, task, tier) so
a UI can rebuild the tree and a replay can fold it from the parent's log. The
runner emits ``agent.spawned`` / ``agent.completed`` and, on a clamp,
``agent.clamped``.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import math
import re
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from ..errors import ConfigError, NexusError, OperationCancelled
from ..events import Event
from ..model.tiers import DEFAULT_TIER, TierResolution, TierTable
from ..tools.names import canonical_tool_name
from ..util import new_id, redact_secrets
from .manager import AgentManager, AgentToolSelection
from .model import (
    MODEL_INHERIT,
    MUTATING_FS_TOOLS,
    AgentNotFoundError,
)

__all__ = [
    "AGENT_CLAMPED",
    "AGENT_COMPLETED",
    "AGENT_SPAWNED",
    "CHILD_SESSION_SEGMENT",
    "DEFAULT_CHILD_TYPE",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FANOUT",
    "DEFAULT_MAX_TIER",
    "MAX_REPORTED_FILES",
    "WORKTREE_CHILD_TOOLS",
    "ChildRuntime",
    "ChildSpec",
    "DefaultSessionFacade",
    "RuntimeFactory",
    "SessionFacade",
    "SubagentBudget",
    "SubagentError",
    "SubagentOutcome",
    "SubagentReservation",
    "SubagentResult",
    "SubagentRunner",
    "SubagentUsage",
    "TaskRequest",
    "files_changed",
]

#: The event names this module emits (plan sections 3.5 and 15.10). They are
#: plain strings; the event catalogue already lists ``agent.spawned``/
#: ``agent.completed`` and UIs tolerate ``agent.clamped``.
AGENT_SPAWNED = "agent.spawned"
AGENT_COMPLETED = "agent.completed"
AGENT_CLAMPED = "agent.clamped"

#: Defaults for the four bounding rules (plan section 15.8).
DEFAULT_MAX_TIER = "medium"
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_FANOUT = 16
#: The role an ad-hoc ``Task`` request uses when none is named.
DEFAULT_CHILD_TYPE = "task"
# First-release worktree profile: only reviewed, shipped tools are exposed.
WORKTREE_CHILD_TOOLS = frozenset(
    {
        "read",
        "glob",
        "grep",
        "edit",
        "write",
        "apply_patch",
        "subagent",
        "todowrite",
        "question",
        "skill",
        "webfetch",
        "websearch",
    }
)
#: The child session path segment: ``<parent>/sub/<n>``.
CHILD_SESSION_SEGMENT = "sub"


class SubagentError(NexusError, ValueError):
    """A subagent request, definition, or bounding rule is invalid."""


class _WorktreeRefusal(SubagentError):
    """A requested worktree could not safely be created or honored."""


# ---------------------------------------------------------------------------
# Request / outcome value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskRequest:
    """A normalized ``Task`` request (named or ad-hoc).

    ``tools`` is ``None`` for inheritance, or an explicit narrowing set. The
    named and ad-hoc modes compose: a named role with ``tools`` narrows that role
    for the one call.
    """

    prompt: str
    subagent_type: str = DEFAULT_CHILD_TYPE
    tools: tuple[str, ...] | None = None
    model: str | None = None
    description: str | None = None
    worktree: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise SubagentError("prompt must be a non-empty string")
        if (
            not isinstance(self.subagent_type, str)
            or not self.subagent_type.strip()
        ):
            raise SubagentError("subagent_type must be a non-empty string")
        if self.tools is not None:
            if isinstance(self.tools, str):
                raise SubagentError("tools must be a list of tool names")
            cleaned: list[str] = []
            for item in self.tools:
                if not isinstance(item, str) or not item.strip():
                    raise SubagentError("tools entries must be non-empty strings")
                cleaned.append(canonical_tool_name(item.strip()))
            object.__setattr__(self, "tools", tuple(cleaned))
        if self.description is not None and (
            not isinstance(self.description, str) or not self.description.strip()
        ):
            raise SubagentError("description must be a non-empty string when given")
        if self.model is not None and (
            not isinstance(self.model, str)
            or not self.model.strip()
            or any(ch.isspace() for ch in self.model)
        ):
            raise SubagentError(
                "model must be a non-empty reference without whitespace"
            )
        if not isinstance(self.worktree, bool):
            raise SubagentError("worktree must be a boolean")

    @classmethod
    def from_value(cls, value: object) -> TaskRequest:
        """Coerce a :class:`TaskRequest`, mapping, or attribute object."""
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            tools = value.get("tools")
            if isinstance(tools, (list, tuple)):
                tools = tuple(str(item) for item in tools)
            elif tools is not None:
                raise SubagentError("tools must be a list of tool names")
            return cls(
                prompt=value.get("prompt"),  # type: ignore[arg-type]
                subagent_type=value.get("subagent_type", DEFAULT_CHILD_TYPE),
                tools=tools,
                model=value.get("model"),
                description=value.get("description"),
                worktree=value.get("worktree", False),
            )
        if value is None:
            raise SubagentError("a Task request is required")
        return cls(
            prompt=value.prompt,  # type: ignore[attr-defined]
            subagent_type=getattr(value, "subagent_type", DEFAULT_CHILD_TYPE),
            tools=_tuple_or_none(getattr(value, "tools", None)),
            model=getattr(value, "model", None),
            description=getattr(value, "description", None),
            worktree=getattr(value, "worktree", False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "subagent_type": self.subagent_type,
            "tools": list(self.tools) if self.tools is not None else None,
            "model": self.model,
            "description": self.description,
            "worktree": self.worktree,
        }


def _tuple_or_none(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return None


@dataclass(frozen=True)
class SubagentUsage:
    """Token/cost spend reported by one child run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.reasoning_tokens
        )

    @classmethod
    def from_value(cls, value: object) -> SubagentUsage:
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            data = value
        else:
            data = {
                "input_tokens": getattr(value, "input_tokens", 0),
                "output_tokens": getattr(value, "output_tokens", 0),
                "cache_read_tokens": getattr(value, "cache_read_tokens", 0),
                "cache_write_tokens": getattr(value, "cache_write_tokens", 0),
                "reasoning_tokens": getattr(value, "reasoning_tokens", 0),
                "cost_usd": getattr(value, "cost_usd", None),
            }
        return cls(
            input_tokens=_nonneg_int(data.get("input_tokens")),
            output_tokens=_nonneg_int(data.get("output_tokens")),
            cache_read_tokens=_nonneg_int(data.get("cache_read_tokens")),
            cache_write_tokens=_nonneg_int(data.get("cache_write_tokens")),
            reasoning_tokens=_nonneg_int(data.get("reasoning_tokens")),
            cost_usd=_opt_nonneg_float(data.get("cost_usd")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


def _nonneg_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _opt_nonneg_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    number = float(value)
    return number if number >= 0.0 else None


@dataclass(frozen=True)
class SubagentOutcome:
    """The final report of one child run (the plan's ``ToolResult`` payload)."""

    agent: str
    session_id: str
    status: str = "completed"
    text: str = ""
    is_error: bool = False
    usage: SubagentUsage = field(default_factory=SubagentUsage)
    iterations: int = 0
    stop_reason: str | None = None
    dropped_tools: tuple[str, ...] = ()
    clamped: bool = False
    tier: str | None = None
    requested_tier: str | None = None
    error: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    worktree: Mapping[str, Any] | None = None
    worktree_scope: bool = False
    #: Workspace files the child changed through file-editing tools, in first-
    #: touch order. Shell side effects are not tracked; roles report those.
    files_changed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.is_error and self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "session_id": self.session_id,
            "status": self.status,
            "is_error": self.is_error,
            "text": self.text,
            "usage": self.usage.to_dict(),
            "iterations": self.iterations,
            "stop_reason": self.stop_reason,
            "dropped_tools": list(self.dropped_tools),
            "clamped": self.clamped,
            "tier": self.tier,
            "requested_tier": self.requested_tier,
            "error": self.error,
            "worktree": dict(self.worktree) if self.worktree is not None else None,
            "files_changed": list(self.files_changed),
        }

    def render(self) -> str:
        """The model-facing body: child text plus clamp/drop notes."""
        lines: list[str] = []
        label = f"{self.agent} (session {self.session_id})" if self.session_id else self.agent
        if self.clamped and self.requested_tier and self.tier:
            lines.append(
                f"[note: requested tier {self.requested_tier!r} was clamped to "
                f"{self.tier!r} by max_tier]"
            )
        if self.dropped_tools:
            lines.append(
                "[note: tools not available to this subagent and thus dropped: "
                + ", ".join(self.dropped_tools)
                + "]"
            )
        if self.worktree_scope and self.dropped_tools:
            lines.append(
                "[worktree safety: excluded tools are unavailable in worktree "
                "subagents and their descendants because they are outside the "
                "reviewed worktree tool profile]"
            )
        body = self.text.strip()
        if body:
            lines.append(body)
        elif self.error:
            lines.append(self.error)
        if self.files_changed:
            shown = self.files_changed[:MAX_REPORTED_FILES]
            more = len(self.files_changed) - len(shown)
            lines.append(
                f"[files changed by {self.agent}: review these edits and include "
                "them in your own summary]"
            )
            lines.extend(f"- {path}" for path in shown)
            if more > 0:
                lines.append(f"- … and {more} more")
        if not lines:
            lines.append(f"{label}: no report")
        return "\n".join(lines)


#: Alias for callers that name the final report a "result".
SubagentResult = SubagentOutcome

#: Bound on paths listed in a rendered report and tracked per child run.
MAX_REPORTED_FILES = 100
#: Canonical file-editing tools whose successful calls count as file changes.
_PATH_EDIT_TOOLS = frozenset({"write", "edit", "multiedit"})
_PATCH_TARGET_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.MULTILINE
)


def files_changed(messages: Iterable[object], *, workspace: Path | None = None) -> tuple[str, ...]:
    """Paths a child changed through successful file-editing tool calls.

    Reads the child's own message log: ``tool_use`` blocks for write/edit/
    multiedit (``path``) and apply_patch (its ``*** … File:`` headers), kept
    only when the matching ``tool_result`` is not an error. Paths under
    ``workspace`` are shown relative to it. Bounded by
    :data:`MAX_REPORTED_FILES` distinct paths.
    """
    calls: dict[str, list[str]] = {}
    failed: set[str] = set()
    for message in messages:
        for block in getattr(message, "content", None) or ():
            name = getattr(block, "name", None)
            call_id = getattr(block, "id", None)
            args = getattr(block, "input", None)
            if isinstance(name, str) and isinstance(call_id, str) and isinstance(args, Mapping):
                tool = canonical_tool_name(name)
                if tool in _PATH_EDIT_TOOLS:
                    path = args.get("path", args.get("file_path"))
                    if isinstance(path, str) and path.strip():
                        calls[call_id] = [path.strip()]
                elif tool in {"apply_patch", "ApplyPatch"}:
                    patch = args.get("patch")
                    if isinstance(patch, str):
                        calls[call_id] = _PATCH_TARGET_RE.findall(patch[:1_000_000])
            result_id = getattr(block, "tool_use_id", None)
            if isinstance(result_id, str) and getattr(block, "is_error", False):
                failed.add(result_id)
    seen: dict[str, None] = {}
    for call_id, paths in calls.items():
        if call_id in failed:
            continue
        for raw in paths:
            shown = raw
            if workspace is not None:
                with contextlib.suppress(ValueError, OSError):
                    candidate = Path(raw)
                    if candidate.is_absolute():
                        shown = candidate.relative_to(workspace).as_posix()
            seen.setdefault(_safe(shown, limit=300), None)
            if len(seen) >= MAX_REPORTED_FILES:
                return tuple(seen)
    return tuple(seen)


# ---------------------------------------------------------------------------
# The shared, concurrency-safe tree budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubagentReservation:
    """An admitted-but-unsettled charge against the tree budget."""

    tokens: int = 0
    cost: float = 0.0


class SubagentBudget:
    """Depth/concurrency/fan-out/aggregate budget shared by a whole spawn tree.

    One instance is created at the root and passed down to every descendant, so
    every limit is enforced across the tree, not per parent. All accounting runs
    under one :class:`asyncio.Lock` (the tree is single-event-loop by contract),
    and concurrency additionally uses an :class:`asyncio.Semaphore`.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_fanout: int | None = DEFAULT_MAX_FANOUT,
        token_budget: int | None = None,
        cost_budget: float | None = None,
    ) -> None:
        if (
            isinstance(max_concurrent, bool)
            or not isinstance(max_concurrent, int)
            or max_concurrent < 1
        ):
            raise SubagentError("max_concurrent must be a positive integer")
        if (
            isinstance(max_depth, bool)
            or not isinstance(max_depth, int)
            or max_depth < 0
        ):
            raise SubagentError("max_depth must be a non-negative integer")
        if max_fanout is not None and (
            isinstance(max_fanout, bool)
            or not isinstance(max_fanout, int)
            or max_fanout < 0
        ):
            raise SubagentError("max_fanout must be a non-negative integer or None")
        if token_budget is not None and (
            isinstance(token_budget, bool)
            or not isinstance(token_budget, int)
            or token_budget < 0
        ):
            raise SubagentError("token_budget must be a non-negative integer or None")
        if cost_budget is not None and (
            isinstance(cost_budget, bool)
            or not isinstance(cost_budget, (int, float))
            or not math.isfinite(float(cost_budget))
            or cost_budget < 0
        ):
            raise SubagentError("cost_budget must be a finite number or None")

        self.max_concurrent = max_concurrent
        self.max_depth = max_depth
        self.max_fanout = max_fanout
        self.token_budget = token_budget
        self.cost_budget = None if cost_budget is None else float(cost_budget)

        self._slots = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._active = 0
        self._children = 0
        self._reserved_tokens = 0
        self._reserved_cost = 0.0
        self._spent_tokens = 0
        self._spent_cost = 0.0

    # -- introspection -----------------------------------------------------

    @property
    def active(self) -> int:
        return self._active

    @property
    def children(self) -> int:
        """Total children admitted to the tree so far."""
        return self._children

    @property
    def spent_tokens(self) -> int:
        return self._spent_tokens

    @property
    def spent_cost(self) -> float:
        return self._spent_cost

    @property
    def remaining_tokens(self) -> int | None:
        if self.token_budget is None:
            return None
        return max(
            0,
            self.token_budget - self._spent_tokens - self._reserved_tokens,
        )

    @property
    def remaining_cost(self) -> float | None:
        if self.cost_budget is None:
            return None
        return max(
            0.0, self.cost_budget - self._spent_cost - self._reserved_cost
        )

    def allows_depth(self, depth: int) -> bool:
        return depth <= self.max_depth

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_concurrent": self.max_concurrent,
            "max_fanout": self.max_fanout,
            "children": self._children,
            "active": self._active,
            "token_budget": self.token_budget,
            "spent_tokens": self._spent_tokens,
            "cost_budget": self.cost_budget,
            "spent_cost": self._spent_cost,
        }

    # -- admission ---------------------------------------------------------

    async def reserve(
        self, *, tokens: int = 0, cost: float = 0.0
    ) -> SubagentReservation | None:
        """Admit one child and charge an estimate, or return ``None``.

        The charge is a *reservation*: it is released on cancellation and
        replaced by the measured usage on :meth:`settle`. Reserving before the
        child runs is what keeps the aggregate budget honest under concurrency.
        """
        async with self._lock:
            if self.max_fanout is not None and self._children >= self.max_fanout:
                return None
            if (
                self.token_budget is not None
                and self._spent_tokens
                + self._reserved_tokens
                + max(0, tokens)
                > self.token_budget
            ):
                return None
            if (
                self.cost_budget is not None
                and self._spent_cost
                + self._reserved_cost
                + max(0.0, cost)
                > self.cost_budget
            ):
                return None
            self._children += 1
            self._reserved_tokens += max(0, tokens)
            self._reserved_cost += max(0.0, cost)
            return SubagentReservation(tokens=max(0, tokens), cost=max(0.0, cost))

    async def settle(
        self, reservation: SubagentReservation, usage: SubagentUsage
    ) -> None:
        """Replace a reservation with the measured spend."""
        async with self._lock:
            self._reserved_tokens = max(
                0, self._reserved_tokens - reservation.tokens
            )
            self._reserved_cost = max(
                0.0, self._reserved_cost - reservation.cost
            )
            self._spent_tokens += usage.total_tokens
            if usage.cost_usd is not None:
                self._spent_cost += usage.cost_usd

    async def release_reservation(self, reservation: SubagentReservation) -> None:
        """Drop a reservation whose child never ran (refused/cancelled)."""
        async with self._lock:
            self._reserved_tokens = max(
                0, self._reserved_tokens - reservation.tokens
            )
            self._reserved_cost = max(
                0.0, self._reserved_cost - reservation.cost
            )

    # -- concurrency -------------------------------------------------------

    async def acquire(self) -> None:
        await self._slots.acquire()
        self._active += 1

    def release(self) -> None:
        if self._active > 0:
            self._active -= 1
        self._slots.release()


# ---------------------------------------------------------------------------
# Injected seams
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildSpec:
    """Everything a :class:`RuntimeFactory` needs to build one child run.

    The runner computes all authority here, *before* the child exists. The
    ``permissions``/``grants`` values are the parent's own objects, passed
    through unchanged -- the runner never manufactures a broader grant.
    """

    task_id: str
    agent: str
    description: str
    system_prompt: str
    prompt: str
    session_id: str
    parent_session: str
    depth: int
    tools: tuple[str, ...]
    dropped_tools: tuple[str, ...]
    model: str | None
    requested_model: str | None
    parent_model: str | None
    provider: str | None
    reasoning_effort: str | None
    tier: str
    requested_tier: str
    clamped: bool
    max_iterations: int
    context_tokens: int | None
    workspace: Path
    worktree: Mapping[str, Any] | None
    worktree_scope: bool
    config: object | None
    permissions: object | None
    grants: tuple[object, ...]
    budget: SubagentBudget
    cancel: object | None
    emit: Callable[[str, dict[str, Any] | None], Awaitable[None]] | None
    #: The parent iteration's pinned lifecycle-hook runner, passed opaquely so a
    #: child's ``run_turn`` enforces the same hooks (a subagent cannot bypass a
    #: PreToolUse block). ``None`` disables child hooks.
    hooks: object | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    agent_id: str = ""
    parent_agent_id: str | None = None
    parent_call_id: str = ""
    root_turn_id: str = ""
    #: The role's ordered fallback model references (tried before the global chain).
    fallback: tuple[str, ...] = ()


class ChildRuntime(Protocol):
    """A built child run. ``run`` executes and returns the final report."""

    async def run(self) -> SubagentOutcome: ...

    async def aclose(self) -> None: ...


class RuntimeFactory(Protocol):
    """Builds a :class:`ChildRuntime` from a frozen :class:`ChildSpec`."""

    def __call__(
        self, spec: ChildSpec, /
    ) -> ChildRuntime | Awaitable[ChildRuntime]: ...


class SessionFacade(Protocol):
    """Allocates the child session id and tears the child session down."""

    def child_id(self, parent_id: str, index: int) -> str: ...

    def allocate_child_id(self, parent_id: str) -> tuple[int, str]: ...

    async def aclose(self, session_id: str) -> None: ...


class DefaultSessionFacade:
    """The plan's logical child session id: ``<parent>/sub/<n>``."""

    def __init__(self, *, segment: str = CHILD_SESSION_SEGMENT) -> None:
        if not isinstance(segment, str) or not segment:
            raise SubagentError("child session segment must be a non-empty string")
        self._segment = segment
        self._indices: dict[str, int] = {}
        self._index_lock = threading.Lock()

    def child_id(self, parent_id: str, index: int) -> str:
        return f"{parent_id}/{self._segment}/{int(index)}"

    def allocate_child_id(self, parent_id: str) -> tuple[int, str]:
        with self._index_lock:
            index = self._indices.get(parent_id, 0) + 1
            self._indices[parent_id] = index
        return index, self.child_id(parent_id, index)

    async def aclose(self, session_id: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Event relay
# ---------------------------------------------------------------------------


async def _forward(
    sink: object | None,
    event_type: str,
    data: Mapping[str, Any],
    *,
    session: str | None,
) -> None:
    """Best-effort forward of one event; a broken sink never breaks a spawn."""
    if sink is None:
        return
    try:
        if callable(getattr(sink, "emit", None)):
            outcome = sink.emit(  # type: ignore[union-attr]
                Event(type=event_type, data=dict(data), session=session)
            )
        elif callable(getattr(sink, "publish", None)):
            outcome = sink.publish(  # type: ignore[union-attr]
                Event(type=event_type, data=dict(data), session=session)
            )
        elif callable(sink):
            outcome = sink(event_type, dict(data))
        else:
            return
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:  # noqa: BLE001 - a broken sink must not fail a spawn
        return


class _ChildRelay:
    """An isolated relay that tags every child event with its agent metadata."""

    __slots__ = ("_meta", "_session", "_sink")

    def __init__(
        self,
        sink: object | None,
        meta: Mapping[str, Any],
        session: str,
    ) -> None:
        self._sink = sink
        self._meta = dict(meta)
        self._meta["id"] = self._meta.get("agent_id", self._meta.get("id"))
        self._session = session

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._meta)

    async def emit(
        self, event_type: str, data: dict[str, Any] | None = None
    ) -> None:
        payload = dict(data or {})
        for key in ("error", "text"):
            value = payload.get(key)
            if isinstance(value, str):
                payload[key] = redact_secrets(value)[:4000]
        result = payload.get("result")
        if isinstance(result, Mapping):
            safe_result = dict(result)
            content = safe_result.get("content")
            if isinstance(content, list):
                safe_result["content"] = [
                    {**dict(item), "text": redact_secrets(item["text"])[:100_000]}
                    if isinstance(item, Mapping) and isinstance(item.get("text"), str)
                    else item
                    for item in content[:256]
                ]
            payload["result"] = safe_result
        # A descendant event arrives already tagged by its own relay. Preserve
        # that identity; only direct child events receive this relay's metadata.
        nested = payload.get("agent")
        if isinstance(nested, Mapping):
            inner = dict(nested)
            if event_type == AGENT_SPAWNED:
                inner.setdefault("parent_agent_id", self._meta.get("id"))
                inner.setdefault("parent_session", self._meta.get("session"))
                inner.setdefault("root_turn_id", self._meta.get("root_turn_id"))
            payload["agent"] = inner
        else:
            payload["agent"] = dict(self._meta)
        await _forward(self._sink, event_type, payload, session=self._session)


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


class SubagentRunner:
    """Compute child authority, bound the tree, and run children via a factory."""

    def __init__(
        self,
        *,
        agents: AgentManager,
        runtime_factory: RuntimeFactory,
        workspace: str | Path,
        parent_session: str,
        parent_agent_id: str | None = None,
        root_turn_id: str = "",
        tiers: TierTable | None = None,
        sessions: SessionFacade | None = None,
        parent_tools: Sequence[str] = (),
        parent_tier: str = DEFAULT_TIER,
        parent_depth: int = 0,
        permissions: object | None = None,
        grants: Sequence[object] = (),
        budget: SubagentBudget | None = None,
        max_tier: str = DEFAULT_MAX_TIER,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_fanout: int | None = DEFAULT_MAX_FANOUT,
        token_budget: int | None = None,
        cost_budget: float | None = None,
        default_type: str = DEFAULT_CHILD_TYPE,
        profile_for: Callable[[str], Iterable[str]] | None = None,
        config: object | None = None,
        event_sink: object | None = None,
        bundle_map: Mapping[str, Sequence[str]] | None = None,
        mutating_tools: Sequence[str] = (),
        reserved_tokens: int = 0,
        reserved_cost: float = 0.0,
        child_session_segment: str = CHILD_SESSION_SEGMENT,
        hooks: object | None = None,
        parent_model: str | None = None,
        worktree_service: object | None = None,
        worktree_root: str | Path | None = None,
        worktree_root_for: Callable[[str | Path], str | Path] | None = None,
        runtime_supports_workspace: bool = False,
        worktree_scope: bool = False,
    ) -> None:
        if not callable(runtime_factory):
            raise SubagentError("runtime_factory must be callable")
        if not isinstance(parent_session, str) or not parent_session.strip():
            raise SubagentError("parent_session must be a non-empty string")
        if (
            isinstance(parent_depth, bool)
            or not isinstance(parent_depth, int)
            or parent_depth < 0
        ):
            raise SubagentError("parent_depth must be a non-negative integer")
        self._depth = parent_depth
        if not isinstance(default_type, str) or not default_type.strip():
            raise SubagentError("default_type must be a non-empty string")
        if reserved_tokens < 0:
            raise SubagentError("reserved_tokens must be non-negative")
        if reserved_cost < 0 or not math.isfinite(float(reserved_cost)):
            raise SubagentError("reserved_cost must be a finite non-negative number")

        self._agents = agents
        self._factory = runtime_factory
        self._workspace = Path(workspace)
        self._worktree_service = worktree_service
        self._worktree_root = Path(worktree_root) if worktree_root is not None else None
        self._worktree_root_for = worktree_root_for
        self._runtime_supports_workspace = runtime_supports_workspace is True
        self._worktree_scope = bool(worktree_scope)
        self._parent_session = parent_session
        self._parent_agent_id = parent_agent_id
        self._root_turn_id = root_turn_id
        self._parent_tools = frozenset(
            str(name) for name in parent_tools if isinstance(name, str)
        )
        self._parent_tier = parent_tier or DEFAULT_TIER
        self._parent_model = parent_model
        self._parent_model_ref = parent_model
        self._tiers: TierTable = tiers if tiers is not None else TierTable()
        if self._tiers.rank(max_tier) is None:
            raise ConfigError(
                f"unknown max_tier {max_tier!r} (known: {self._tiers.order})"
            )
        self._max_tier = max_tier
        self._sessions: SessionFacade = (
            sessions if sessions is not None else DefaultSessionFacade(
                segment=child_session_segment
            )
        )
        self._permissions = permissions
        self._grants = tuple(grants)
        self._config = config
        self._event_sink = event_sink
        self._bundle_map = (
            {str(name): tuple(tools) for name, tools in bundle_map.items()}
            if bundle_map is not None
            else None
        )
        self._mutating_tools = frozenset(
            str(name) for name in mutating_tools
        ) | MUTATING_FS_TOOLS
        self._default_type = default_type
        self._profile_for = profile_for
        self._reserved_tokens = int(reserved_tokens)
        self._reserved_cost = float(reserved_cost)
        #: The parent iteration's pinned hook runner (opaque), forwarded to every
        #: child so a subagent enforces the same lifecycle policy.
        self._hooks = hooks

        self._counter_lock = asyncio.Lock()
        self._counter = 0

        #: A shared tree budget passed to children; the root creates it.
        self._budget = (
            budget
            if budget is not None
            else SubagentBudget(
                max_concurrent=max_concurrent,
                max_depth=max_depth,
                max_fanout=max_fanout,
                token_budget=token_budget,
                cost_budget=cost_budget,
            )
        )

    # -- introspection -----------------------------------------------------

    @property
    def default_type(self) -> str:
        return self._default_type

    @property
    def agents(self) -> AgentManager:
        return self._agents

    @property
    def budget(self) -> SubagentBudget:
        return self._budget

    @property
    def depth(self) -> int:
        return self._depth

    @property
    def max_tier(self) -> str:
        return self._max_tier

    @property
    def parent_session(self) -> str:
        return self._parent_session

    @property
    def parent_tools(self) -> frozenset[str]:
        return self._parent_tools

    # -- tier and permission key ------------------------------------------

    def role_model_reference(self, request: object) -> str | None:
        """The reference to resolve: the request's, else the role's, else inherit."""
        req = TaskRequest.from_value(request)
        if req.model:
            if "/" not in req.model and req.model not in self._tiers.order:
                try:
                    role = self._agents.resolve(req.subagent_type, context="subagent")
                except AgentNotFoundError:
                    role = None
                if role is not None and role.provider:
                    return f"{role.provider}/{req.model}"
            return req.model
        try:
            role = self._agents.resolve(req.subagent_type, context="subagent")
        except AgentNotFoundError:
            role = None
        if role is not None and role.model:
            if role.model in self._tiers.order or "/" in role.model:
                return role.model
            if role.provider:
                return f"{role.provider}/{role.model}"
            return role.model
        if role is not None and role.provider and self._parent_model:
            inherited_model = self._parent_model.rsplit("/", 1)[-1]
            return f"{role.provider}/{inherited_model}"
        return None

    def role_provider(self, request: object) -> str | None:
        """Provider supplied by a role default, unless Task.model overrides it."""
        req = TaskRequest.from_value(request)
        if req.model and "/" in req.model:
            return req.model.split("/", 1)[0]
        try:
            role = self._agents.resolve(req.subagent_type, context="subagent")
        except AgentNotFoundError:
            return None
        if req.model and "/" in req.model:
            return req.model.split("/", 1)[0]
        return role.provider

    def resolve_tier(self, request: object) -> TierResolution:
        """Resolve and clamp the effective tier for a request."""
        reference = self.role_model_reference(request)
        if reference is None or reference == MODEL_INHERIT:
            return self._tiers.resolve(
                None, parent=self._parent_tier, max_tier=self._max_tier
            )
        return self._tiers.resolve(
            reference, parent=self._parent_tier, max_tier=self._max_tier
        )

    def permission_key(self, request: object) -> str:
        """The gate key ``"<subagent_type>:<tier>"`` (plan section 15.6)."""
        req = TaskRequest.from_value(request)
        resolution = self.resolve_tier(req)
        return f"{req.subagent_type}:{resolution.tier}"

    # -- authority ---------------------------------------------------------

    def _bundle_map_or_default(self) -> Mapping[str, Sequence[str]]:
        if self._bundle_map is not None:
            return self._bundle_map
        from ..tools.bundles import BUNDLES

        return {name: bundle.tools for name, bundle in BUNDLES.items()}

    def select_tools(
        self, role: object, request: object
    ) -> AgentToolSelection:
        """``parent_tools & role_tools & requested_tools`` (never a grant)."""
        req = TaskRequest.from_value(request)
        profile = (
            self._profile_for(role.profile)
            if role.profile and self._profile_for is not None
            else None
        )
        selection = self._agents.select_tools(
            role,
            available=self._parent_tools,
            profile=profile,
            bundle_map=self._bundle_map_or_default(),
            mutating=self._mutating_tools,
        )
        if self._worktree_scope or req.worktree:
            selected = selection.selected & WORKTREE_CHILD_TOOLS
            excluded = selection.selected - selected
            selection = replace(
                selection,
                selected=frozenset(selected),
                dropped=selection.dropped | excluded,
                stripped=selection.stripped | excluded,
            )
        if req.tools is None:
            return selection
        requested = frozenset(req.tools)
        selected = selection.selected & requested
        dropped = selection.dropped | (requested - selection.selected)
        return replace(
            selection,
            selected=frozenset(selected),
            dropped=frozenset(dropped),
        )

    def _child_model(
        self, reference: str | None, resolution: TierResolution
    ) -> str | None:
        """The model reference handed to the child runtime."""
        if reference is None or reference == MODEL_INHERIT:
            return resolution.tier
        if reference in self._tiers.order:
            return resolution.tier
        if resolution.clamped:
            # A concrete reference above the cap becomes the capped tier, which
            # the child runtime's router then resolves to a runnable model.
            return resolution.tier
        return reference

    # -- child session -----------------------------------------------------

    async def _next_child_id(self) -> tuple[int, str]:
        allocator = getattr(self._sessions, "allocate_child_id", None)
        if callable(allocator):
            allocated = allocator(self._parent_session)
            if inspect.isawaitable(allocated):
                return await allocated
            return allocated
        async with self._counter_lock:
            self._counter += 1
            index = self._counter
        # Indices are scoped by the session facade as before. The shared tree
        # budget is shared independently of identity allocation.
        return index, self._sessions.child_id(self._parent_session, index)

    # -- spawn -------------------------------------------------------------

    async def spawn(
        self,
        request: object,
        *,
        cancel: object | None = None,
        emit: object | None = None,
        call_id: str = "",
    ) -> SubagentOutcome:
        """Spawn one child, bounded and authority-intersected.

        Expected refusals (unknown role, depth/fan-out/budget, a factory error)
        come back as an error :class:`SubagentOutcome` so the model can see why;
        only cancellation raises :class:`~nexus.errors.OperationCancelled`.
        """
        try:
            req = TaskRequest.from_value(request)
        except SubagentError as exc:
            return self._refusal(
                agent=getattr(request, "subagent_type", DEFAULT_CHILD_TYPE),
                error=f"Task: {exc}",
            )

        if _is_cancelled(cancel):
            raise OperationCancelled(_cancel_reason(cancel) or "cancelled")

        try:
            role = self._agents.resolve(req.subagent_type, context="subagent")
        except AgentNotFoundError:
            role = None
        if role is None:
            available = ", ".join(
                agent.name for agent in self._agents.agents if agent.eligible_in("subagent")
            ) or "(none)"
            return self._refusal(
                agent=req.subagent_type,
                error=(
                    f"Task: unknown subagent_type {req.subagent_type!r}; "
                    f"defined agents: {available}"
                ),
                request=req,
            )

        depth = self._depth + 1
        if not self._budget.allows_depth(depth):
            return self._refusal(
                agent=role.name,
                error=(
                    f"Task: refusing to spawn {role.name!r}: depth {depth} "
                    f"exceeds max_depth {self._budget.max_depth}"
                ),
                request=req,
            )

        selection = self.select_tools(role, req)
        resolution = self.resolve_tier(req)
        tier = resolution.tier
        reference = self.role_model_reference(req)
        child_model = self._child_model(reference, resolution)
        if (
            self._parent_model
            and (req.model is None or req.model == MODEL_INHERIT)
            and (not role.model or role.model == MODEL_INHERIT)
        ):
            child_model = (
                f"{role.provider}/{self._parent_model.rsplit('/', 1)[-1]}"
                if role.provider
                else self._parent_model
            )
        child_provider = self.role_provider(req)
        if child_model in self._tiers.order:
            child_provider = None
        clamped = bool(resolution.clamped)
        dropped = tuple(sorted(selection.dropped))

        sink = emit if emit is not None else self._event_sink
        index, session_id = await self._next_child_id()
        task_id = new_id()
        # Session ids are stable identities inside a parent log and globally
        # unique within the persistent session facade's allocation scope.
        agent_id = session_id
        meta = {
            "id": agent_id,
            "parent": self._parent_agent_id or self._parent_session,
            "parent_session": self._parent_session,
            "parent_agent_id": self._parent_agent_id,
            "parent_call_id": call_id if isinstance(call_id, str) else "",
            "root_turn_id": self._root_turn_id,
            "depth": depth,
            "type": role.name,
            "task": task_id,
            "agent_id": agent_id,
            "tier": tier,
            "index": index,
            "session": session_id,
        }
        relay = _ChildRelay(sink, meta, session_id)
        reservation = await self._budget.reserve(
            tokens=self._reserved_tokens, cost=self._reserved_cost
        )
        if reservation is None:
            outcome = self._refusal(
                agent=role.name,
                error=(
                    "Task: the subagent tree budget is exhausted "
                    f"({_budget_label(self._budget)}); no child was spawned"
                ),
                request=req,
                session_id=session_id,
            )
            await self._emit_completed(sink, meta, outcome)
            return outcome

        acquired = False
        reservation_done = False
        release_reservation_on_exit = False
        outcome: SubagentOutcome | None = None
        worktree: Mapping[str, Any] | None = None
        child_workspace = self._workspace
        cancellation: OperationCancelled | None = None
        task_cancelled = False
        mark_finished = None
        try:
            if req.worktree:
                if not self._runtime_supports_workspace:
                    raise _WorktreeRefusal(
                        "worktree requested but the child runtime does not declare "
                        "workspace isolation support"
                    )
                if self._worktree_service is None or self._worktree_root is None:
                    raise _WorktreeRefusal(
                        "worktree requested but no WorktreeService/root is configured"
                    )
                create_worktree = getattr(self._worktree_service, "create", None)
                mark_finished = getattr(self._worktree_service, "mark_finished", None)
                if not callable(create_worktree) or not callable(mark_finished):
                    raise _WorktreeRefusal("configured WorktreeService is incomplete")
                try:
                    worktree_root = self._worktree_root
                    if self._worktree_root_for is not None:
                        worktree_root = Path(self._worktree_root_for(self._workspace))
                    record = await _maybe_await(
                        create_worktree(
                            self._workspace, session_id, root=worktree_root
                        )
                    )
                    worktree = _worktree_record(record)
                except OperationCancelled:
                    raise
                except Exception as exc:
                    raise _WorktreeRefusal(
                        f"worktree creation refused: {type(exc).__name__}: {_safe(exc)}"
                    ) from exc
                worktree.update(
                    base=worktree.get("base_commit"),
                    owner=worktree.get("owner_uid"),
                )
                child_workspace = Path(str(worktree["path"]))
                meta["worktree"] = dict(worktree)
            await _forward(
                sink,
                AGENT_SPAWNED,
                {
                    "agent": dict(meta),
                    **meta,
                    "description": req.description or role.description,
                    "model": child_model,
                    "requested_tier": resolution.reference or self._parent_tier,
                    "clamped": clamped,
                    "tools": sorted(selection.selected),
                    "dropped_tools": list(dropped),
                    "worktree": dict(worktree) if worktree is not None else None,
                },
                session=self._parent_session,
            )
            if clamped:
                await _forward(
                    sink,
                    AGENT_CLAMPED,
                    {
                        "agent": dict(meta),
                        **meta,
                        "requested_tier": resolution.reference or self._parent_tier,
                        "max_tier": self._max_tier,
                        "diagnostics": list(resolution.diagnostics),
                    },
                    session=self._parent_session,
                )
            await self._budget.acquire()
            acquired = True
            if _is_cancelled(cancel):
                raise OperationCancelled(_cancel_reason(cancel) or "cancelled")
            system_prompt = self._agents.load_body(role)
            spec = ChildSpec(
                task_id=task_id,
                agent_id=agent_id,
                agent=role.name,
                description=req.description or role.description,
                system_prompt=system_prompt,
                prompt=req.prompt,
                session_id=session_id,
                parent_session=self._parent_session,
                parent_agent_id=self._parent_agent_id,
                parent_call_id=call_id if isinstance(call_id, str) else "",
                root_turn_id=self._root_turn_id,
                depth=depth,
                tools=tuple(sorted(selection.selected)),
                dropped_tools=dropped,
                model=child_model,
                requested_model=req.model,
                parent_model=self._parent_model,
                provider=child_provider,
                reasoning_effort=role.reasoning_effort,
                fallback=tuple(role.fallback),
                tier=tier,
                requested_tier=resolution.reference or self._parent_tier,
                clamped=clamped,
                max_iterations=role.max_iterations or 0,
                context_tokens=role.context_tokens,
                workspace=child_workspace,
                worktree=worktree,
                worktree_scope=self._worktree_scope or req.worktree,
                config=self._config,
                permissions=self._permissions,
                grants=self._grants,
                budget=self._budget,
                cancel=cancel,
                emit=relay.emit,
                hooks=self._hooks,
                metadata={
                    "agent": dict(meta),
                    "selection": _selection_dict(selection),
                    "workspace": str(child_workspace),
                    "worktree": dict(worktree) if worktree is not None else None,
                },
            )
            outcome = await self._run_child(spec, cancel)
        except OperationCancelled:
            release_reservation_on_exit = True
            cancellation = OperationCancelled(
                _cancel_reason(cancel) or "cancelled"
            )
            outcome = SubagentOutcome(
                agent=role.name,
                session_id=session_id,
                status="cancelled",
                is_error=True,
                text=f"Task: subagent {role.name!r} was cancelled",
                error="OperationCancelled",
                dropped_tools=dropped,
                clamped=clamped,
                tier=tier,
                requested_tier=resolution.reference,
            )
        except asyncio.CancelledError:
            release_reservation_on_exit = True
            task_cancelled = True
            outcome = SubagentOutcome(
                agent=role.name,
                session_id=session_id,
                status="cancelled",
                is_error=True,
                text=f"Task: subagent {role.name!r} was cancelled",
                error="CancelledError",
                dropped_tools=dropped,
                clamped=clamped,
                tier=tier,
                requested_tier=resolution.reference,
            )
        except Exception as exc:  # noqa: BLE001 - a factory/tool failure is visible
            outcome = SubagentOutcome(
                agent=role.name,
                session_id=session_id,
                status=(
                    "refused" if isinstance(exc, _WorktreeRefusal) else "failed"
                ),
                is_error=True,
                text=(
                    f"Task: subagent {role.name!r} "
                    f"{'was refused' if isinstance(exc, _WorktreeRefusal) else 'failed to start'}: "
                    f"{type(exc).__name__}: {_safe(exc)}"
                ),
                error=type(exc).__name__,
                dropped_tools=dropped,
                clamped=clamped,
                tier=tier,
                requested_tier=resolution.reference,
            )
        finally:
            if worktree is not None:
                try:
                    mark_finished = mark_finished or self._worktree_service.mark_finished
                    final_outcome = outcome or {
                        "status": "cancelled"
                        if cancellation is not None
                        else "failed"
                    }
                    finalized, interrupted = await _shielded(
                        _maybe_await(
                            mark_finished(
                                session_id,
                                final_outcome,
                                root=worktree_root,
                            )
                        )
                    )
                    if interrupted:
                        task_cancelled = True
                        if outcome is not None and outcome.status == "completed":
                            outcome = replace(
                                outcome,
                                status="cancelled",
                                is_error=True,
                                error="subagent cancelled during worktree finalization",
                                text=(outcome.text + "\nsubagent cancelled during worktree finalization").strip(),
                            )
                        cancellation = cancellation or OperationCancelled(
                            _cancel_reason(cancel) or "cancelled"
                        )
                    finalized_record = _worktree_record(finalized)
                    status = str(finalized_record.get("final_dirty_status", ""))
                    worktree = {
                        **worktree,
                        **finalized_record,
                        "dirty": bool(status),
                        "dirty_status": status,
                    }
                    meta["worktree"] = dict(worktree)
                except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - retain checkout on finalization failure
                    task_cancelled = task_cancelled or isinstance(
                        exc, asyncio.CancelledError
                    )
                    worktree = {
                        **worktree,
                        "finalization_error": f"{type(exc).__name__}: {_safe(exc)}",
                    }
                    meta["worktree"] = dict(worktree)
                    message = f"worktree finalization failed: {type(exc).__name__}: {_safe(exc)}"
                    if outcome is None:
                        outcome = SubagentOutcome(
                            agent=role.name,
                            session_id=session_id,
                            status=(
                                "cancelled"
                                if cancellation is not None
                                else "failed"
                            ),
                            is_error=True,
                            text=message,
                            error=message,
                            dropped_tools=dropped,
                            clamped=clamped,
                            tier=tier,
                            requested_tier=resolution.reference,
                        )
                    else:
                        outcome = replace(
                            outcome,
                            status=(
                                "failed"
                                if outcome.status == "completed"
                                else outcome.status
                            ),
                            is_error=True,
                            text=(outcome.text + "\n" + message).strip(),
                            error=(outcome.error + "; " if outcome.error else "") + message,
                        )
            if outcome is not None and worktree is not None:
                outcome = replace(
                    outcome,
                    worktree=dict(worktree),
                    metrics={**dict(outcome.metrics), "worktree": dict(worktree)},
                )
            if acquired:
                self._budget.release()
            if not reservation_done:
                accounting = (
                    self._budget.release_reservation(reservation)
                    if release_reservation_on_exit or outcome is None
                    else self._budget.settle(reservation, outcome.usage)
                )
                _accounted, interrupted = await _shielded(accounting)
                task_cancelled = task_cancelled or interrupted
                reservation_done = True

        if outcome is None:  # pragma: no cover - a BaseException path never reaches
            raise SubagentError("subagent run produced no outcome")
        await self._emit_completed(sink, meta, outcome)
        if task_cancelled:
            raise asyncio.CancelledError
        if cancellation is not None:
            raise cancellation
        return outcome

    async def _run_child(
        self,
        spec: ChildSpec,
        cancel: object | None,
    ) -> SubagentOutcome:
        child = await _maybe_await(self._factory(spec))
        if child is None:
            raise SubagentError("runtime_factory returned no child runtime")
        run_task = asyncio.ensure_future(_maybe_await(child.run()))
        cancel_task: asyncio.Task | None = None
        try:
            if cancel is not None and hasattr(cancel, "wait"):
                cancel_task = asyncio.ensure_future(cancel.wait())
                done, _ = await asyncio.wait(
                    {run_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done:
                    await _cancel_and_drain(run_task)
                    raise OperationCancelled(_cancel_reason(cancel) or "cancelled")
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
                return _coerce_outcome(run_task.result(), spec)
            return _coerce_outcome(await run_task, spec)
        except asyncio.CancelledError:
            await _cancel_and_drain(run_task)
            raise
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
            with contextlib.suppress(Exception):
                await child.aclose()
            with contextlib.suppress(Exception):
                await self._sessions.aclose(spec.session_id)

    async def _emit_completed(
        self,
        sink: object | None,
        meta: Mapping[str, Any],
        outcome: SubagentOutcome,
    ) -> None:
        await _forward(
            sink,
            AGENT_COMPLETED,
            {
                "agent": dict(meta),
                **dict(meta),
                "status": outcome.status,
                "ok": outcome.ok,
                "is_error": outcome.is_error,
                "usage": outcome.usage.to_dict(),
                "iterations": outcome.iterations,
                "stop_reason": outcome.stop_reason,
                "dropped_tools": list(outcome.dropped_tools),
                "clamped": outcome.clamped,
                "error": (
                    redact_secrets(outcome.error)[:500] if outcome.error else None
                ),
                "worktree": (
                    dict(outcome.worktree)
                    if outcome.worktree is not None
                    else None
                ),
            },
            session=self._parent_session,
        )

    # -- helpers -----------------------------------------------------------

    def _refusal(
        self,
        *,
        agent: str,
        error: str,
        request: TaskRequest | None = None,
        session_id: str = "",
    ) -> SubagentOutcome:
        return SubagentOutcome(
            agent=agent,
            session_id=session_id,
            status="refused",
            is_error=True,
            text=error,
            error=error,
            clamped=False,
            tier=self._parent_tier,
            requested_tier=(request.model if request is not None else None),
        )

    # -- descent -----------------------------------------------------------

    def for_child(
        self,
        spec: ChildSpec,
        *,
        cancel: object | None = None,
        parent_tools: Sequence[str] | None = None,
    ) -> SubagentRunner:
        """A runner for a child of this one, sharing the tree budget.

        The nested runtime calls this so a grandchild inherits the child's tool
        authority, tier, and depth while the tree budget (and its semaphore)
        stays shared.
        """
        return SubagentRunner(
            agents=self._agents,
            runtime_factory=self._factory,
            workspace=spec.workspace,
            parent_session=spec.session_id,
            parent_agent_id=spec.agent_id,
            root_turn_id=spec.root_turn_id,
            tiers=self._tiers,
            sessions=self._sessions,
            parent_tools=(
                tuple(parent_tools)
                if parent_tools is not None
                else tuple(spec.tools)
            ),
            parent_tier=spec.tier,
            parent_depth=spec.depth,
            permissions=self._permissions,
            grants=self._grants,
            budget=self._budget,
            max_tier=self._max_tier,
            default_type=self._default_type,
            profile_for=self._profile_for,
            config=self._config,
            event_sink=self._event_sink,
            bundle_map=self._bundle_map,
            mutating_tools=self._mutating_tools,
            reserved_tokens=self._reserved_tokens,
            reserved_cost=self._reserved_cost,
            hooks=self._hooks,
            parent_model=spec.model,
            worktree_service=self._worktree_service,
            worktree_root=self._worktree_root,
            worktree_root_for=self._worktree_root_for,
            runtime_supports_workspace=self._runtime_supports_workspace,
            worktree_scope=self._worktree_scope or spec.worktree_scope,
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _selection_dict(selection: AgentToolSelection) -> dict[str, Any]:
    return {
        "available": sorted(selection.available),
        "ceiling": sorted(selection.ceiling),
        "requested": sorted(selection.requested),
        "selected": sorted(selection.selected),
        "dropped": sorted(selection.dropped),
        "stripped": sorted(selection.stripped),
        "read_only": selection.read_only,
    }


def _budget_label(budget: SubagentBudget) -> str:
    parts: list[str] = []
    if budget.token_budget is not None:
        parts.append(f"tokens {budget.spent_tokens}/{budget.token_budget}")
    if budget.cost_budget is not None:
        parts.append(f"cost {budget.spent_cost:g}/{budget.cost_budget:g}")
    if budget.max_fanout is not None:
        parts.append(f"children {budget.children}/{budget.max_fanout}")
    return ", ".join(parts) or "limit reached"


def _is_cancelled(cancel: object | None) -> bool:
    if cancel is None:
        return False
    return bool(getattr(cancel, "cancelled", False))


def _cancel_reason(cancel: object | None) -> str | None:
    if cancel is None:
        return None
    reason = getattr(cancel, "reason", None)
    return reason if isinstance(reason, str) else None


def _safe(value: object, *, limit: int = 300) -> str:
    text = str(value).replace("\x00", "") if value is not None else ""
    return text if len(text) <= limit else text[:limit] + "…"


def _worktree_record(record: object) -> dict[str, Any]:
    """Normalize an owned worktree record for child metadata and tool results."""
    fields = (
        "child_id",
        "parent_workspace",
        "base_commit",
        "branch",
        "path",
        "owner_uid",
        "created_at",
        "dirty_status",
        "lifecycle",
        "final_status",
        "final_dirty_status",
        "finalized_at",
    )
    if isinstance(record, Mapping):
        values = {name: record.get(name) for name in fields}
    else:
        values = {name: getattr(record, name, None) for name in fields}
    if not isinstance(values["path"], (str, Path)):
        raise SubagentError("WorktreeService returned a record without a path")
    return {
        name: str(value) if isinstance(value, Path) else value
        for name, value in values.items()
        if value is not None
    }


async def _maybe_await(value: object) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _shielded(awaitable: Awaitable[Any]) -> tuple[Any, bool]:
    """Finish a lifecycle operation even if its caller is cancelled."""
    task = asyncio.ensure_future(awaitable)
    interrupted = False
    while True:
        try:
            return await asyncio.shield(task), interrupted
        except asyncio.CancelledError:
            interrupted = True
            if task.done():
                if task.cancelled():
                    raise
                return task.result(), interrupted


async def _cancel_and_drain(run_task: asyncio.Task) -> None:
    if not run_task.done():
        run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await run_task


def _coerce_outcome(value: object, spec: ChildSpec) -> SubagentOutcome:
    if isinstance(value, SubagentOutcome):
        return replace(
            value,
            session_id=value.session_id or spec.session_id,
            dropped_tools=value.dropped_tools or spec.dropped_tools,
            clamped=value.clamped or spec.clamped,
            tier=value.tier or spec.tier,
            requested_tier=value.requested_tier or spec.requested_tier,
            worktree_scope=value.worktree_scope or spec.worktree_scope,
        )
    if value is None:
        raise SubagentError("child runtime returned no outcome")
    text = getattr(value, "text", None)
    if text is None:
        text = getattr(value, "report", None)
    if text is None:
        text = str(value)
    status = getattr(value, "status", "completed")
    is_error = bool(getattr(value, "is_error", False)) or status in (
        "failed",
        "cancelled",
        "refused",
    )
    return SubagentOutcome(
        agent=getattr(value, "agent", spec.agent),
        session_id=getattr(value, "session_id", spec.session_id),
        status=status if isinstance(status, str) else "completed",
        text=str(text),
        is_error=is_error,
        usage=SubagentUsage.from_value(getattr(value, "usage", None)),
        iterations=_nonneg_int(getattr(value, "iterations", 0)),
        stop_reason=getattr(value, "stop_reason", None),
        dropped_tools=spec.dropped_tools,
        clamped=spec.clamped,
        tier=spec.tier,
        requested_tier=spec.requested_tier,
        error=getattr(value, "error", None),
        metrics=dict(getattr(value, "metrics", {}) or {}),
    )
