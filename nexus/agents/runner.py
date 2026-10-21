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
import hashlib
import inspect
import math
import re
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from ..errors import ConfigError, NexusError, OperationCancelled
from ..events import Event
from ..model.tiers import (
    DEFAULT_TIER,
    TierResolution,
    TierTable,
    effective_tier_effort,
)
from ..tools.names import canonical_tool_name
from ..util import new_id, redact_secrets
from .manager import AgentManager, AgentToolSelection
from .model import (
    MODEL_INHERIT,
    MUTATING_FS_TOOLS,
    AgentDef,
    AgentNotFoundError,
)

__all__ = [
    "AGENT_CLAMPED",
    "AGENT_ROUTED",
    "AGENT_RESUMED",
    "AGENT_COMPLETED",
    "AGENT_SPAWNED",
    "CHILD_SESSION_SEGMENT",
    "DEFAULT_CHILD_TYPE",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FANOUT",
    "DEFAULT_MAX_FOLLOWUP_CHARS",
    "DEFAULT_MAX_TIER",
    "ContinuationRefused",
    "TierDecision",
    "MAX_REPORTED_FILES",
    "MAX_ROLE_INDEX_CHARS",
    "ROSTER_CLIP_NOTE",
    "RosterDigests",
    "ROOT_ONLY_TOOLS",
    "WORKTREE_CHILD_TOOLS",
    "ChildRuntime",
    "ChildSpec",
    "DefaultSessionFacade",
    "RuntimeFactory",
    "SessionFacade",
    "PreparedChild",
    "StartedChild",
    "SubagentBudget",
    "SubagentError",
    "SubagentOutcome",
    "SubagentReservation",
    "SubagentResult",
    "SubagentRunner",
    "SubagentUsage",
    "TaskRequest",
    "files_changed",
    "qualify_model",
]

#: Child events that name the concrete model a run actually started on.
_MODEL_START_EVENTS = frozenset({"model.started", "external.started"})


def qualify_model(provider: object, model: object) -> str | None:
    """``provider/model`` for a started model, without doubling a qualified id.

    ``None`` when no model is named: a tier label is never a stand-in.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    if not isinstance(provider, str) or not provider.strip():
        return model
    provider = provider.strip()
    return model if model.startswith(f"{provider}/") else f"{provider}/{model}"

#: The event names this module emits (plan sections 3.5 and 15.10). They are
#: plain strings; the event catalogue already lists ``agent.spawned``/
#: ``agent.completed`` and UIs tolerate ``agent.clamped``.
AGENT_SPAWNED = "agent.spawned"
AGENT_COMPLETED = "agent.completed"
AGENT_CLAMPED = "agent.clamped"
#: Quota-aware routing decision for one child (docs/models.md); recorded only when
#: a candidate was skipped or every candidate was blocked.
AGENT_ROUTED = "agent.routed"
#: The root sent a follow-up: a new run of an existing child (plan 3.4).
AGENT_RESUMED = "agent.resumed"

#: Defaults for the four bounding rules (plan section 15.8).
DEFAULT_MAX_TIER = "high"
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_FANOUT = 16
#: Longest follow-up ``subagent_send`` accepts (``agents.max_followup_chars``).
DEFAULT_MAX_FOLLOWUP_CHARS = 20_000
#: Longest follow-up text copied into the root's ``agent.resumed`` event.
RESUMED_EVENT_CHARS = 3_900
#: The role an ad-hoc ``Task`` request uses when none is named.
DEFAULT_CHILD_TYPE = "task"
#: Namespace a model may prefix onto requested tool names (OpenAI Responses).
_TOOL_NAMESPACE = "functions."
#: Budget for the roster the ``subagent`` tool advertises (whole lines only).
MAX_ROLE_INDEX_CHARS = 4_000
#: Own-line note appended when the roster dropped tier lines or role descriptions.
ROSTER_CLIP_NOTE = "[Subagent roster and tier models clipped]"


def _tier_line(tier: str, row: Sequence[tuple[str, str | None]]) -> str:
    listed = ", ".join(
        ref + (f" (effort: {effort})" if effort is not None else "") for ref, effort in row
    )
    return f"{tier} models (in order): {listed or 'none configured'}"


def _role_prefix(entry: Any) -> str:
    """``name [tiers: ...; effort: x]``: the part of a role line that is never clipped."""
    details = []
    if entry.tiers:
        details.append("tiers: " + ", ".join(
            f"{tier} (default)" if index == 0 and len(entry.tiers) > 1 else tier
            for index, tier in enumerate(entry.tiers)
        ))
    if entry.reasoning_effort:
        details.append(f"effort: {entry.reasoning_effort}")
    return f"{entry.name} [{'; '.join(details)}]" if details else entry.name


class RosterDigests(NamedTuple):
    """Digests of the tier lines the ``subagent`` description actually shows.

    Models and efforts are hashed apart so a change notice can name exactly
    which of the two moved; role descriptions are never part of either.
    """

    models: str
    efforts: str

    def to_dict(self) -> dict[str, str]:
        return {"models": self.models, "efforts": self.efforts}
# First-release worktree profile: only reviewed, shipped tools are exposed.
WORKTREE_CHILD_TOOLS = frozenset(
    {
        "read",
        "glob",
        "grep",
        "edit",
        "write",
        "apply_patch",
        "todowrite",
        "question",
        "skill",
        "webfetch",
        "websearch",
    }
)
#: Tools only the root session may hold; a child never gets them.
ROOT_ONLY_TOOLS = frozenset({"subagent", "subagent_send", "subagent_stop", "subagent_list"})
#: The child session path segment: ``<parent>/sub/<n>``.
CHILD_SESSION_SEGMENT = "sub"


class SubagentError(NexusError, ValueError):
    """A subagent request, definition, or bounding rule is invalid."""


class ContinuationRefused(SubagentError):
    """``subagent_send`` was refused; the message is written for the root model."""


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
    context_read: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.context_read, bool):
            raise SubagentError("context_read must be boolean")
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
                name = item.strip()
                # Some providers show tools to the model as ``functions.<name>``
                # and it echoes that namespace back; the registry name is bare.
                if name.startswith(_TOOL_NAMESPACE) and len(name) > len(_TOOL_NAMESPACE):
                    name = name[len(_TOOL_NAMESPACE):]
                cleaned.append(canonical_tool_name(name))
            object.__setattr__(self, "tools", tuple(cleaned))
        if self.description is not None and (
            not isinstance(self.description, str) or not self.description.strip()
        ):
            raise SubagentError("description must be a non-empty string when given")
        # Providers may fill an optional model override with an empty string.
        # Treat that as omission before both permission routing and spawning.
        if isinstance(self.model, str):
            object.__setattr__(self, "model", self.model.strip() or None)
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
    #: Model-facing note describing a tier the role or ``max_tier`` moved.
    clamp_note: str = ""
    error: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    worktree: Mapping[str, Any] | None = None
    worktree_scope: bool = False
    #: Workspace files the child changed through file-editing tools, in first-
    #: touch order. Shell side effects are not tracked; roles report those.
    files_changed: tuple[str, ...] = ()
    #: Markdown report of a failed run's context (see ``agents.handoff``); the
    #: inline digest is in ``handoff`` and ``handoff_path`` is where it was saved.
    handoff: str = ""
    handoff_path: str | None = None
    #: Concrete ``provider/model`` references the child actually started, in
    #: order, taken from its durable ``model.started``/``external.started``
    #: events. Empty means unavailable (never started, or an external worker
    #: started without reporting a model), not "the tier's model".
    models: tuple[str, ...] = ()
    #: The root can continue this child with ``subagent_send`` (changes the
    #: wording of the model-facing hints in :meth:`render`).
    continuable: bool = False

    @property
    def ok(self) -> bool:
        return not self.is_error and self.status == "completed"

    @property
    def actual_model(self) -> str | None:
        """The model the child last ran on (the final fallback), else ``None``."""
        return self.models[-1] if self.models else None

    def model_line(self) -> str:
        """The model-facing disclosure of the actual model beside its tier."""
        tier = self.tier or "unknown"
        if not self.models:
            selected = self.metrics.get("selected_model")
            if selected:
                return f"[selected route: {selected} (not started); model: none started; tier: {tier}]"
            return (
                f"[model: unavailable (not reported); tier: {tier} "
                "is a routing label, not a model]"
            )
        line = f"[model: {self.models[-1]}; tier: {tier}"
        if len(self.models) > 1:
            line += "; fallback path: " + " -> ".join(self.models)
        return line + "]"

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
            "actual_model": self.actual_model,
            "actual_models": list(self.models),
            **({"selected_model": self.metrics["selected_model"]} if self.metrics.get("selected_model") else {}),
            "error": self.error,
            "worktree": dict(self.worktree) if self.worktree is not None else None,
            "files_changed": list(self.files_changed),
            "handoff_path": self.handoff_path,
            "attention": self.attention,
            **self._external_dict(),
        }

    @property
    def attention(self) -> str | None:
        """What blocked the child, for the root to resolve or escalate (else ``None``)."""
        value = self.metrics.get("attention")
        if isinstance(value, str) and value:
            return value
        return self.error if self.status == "needs_attention" and self.error else None

    @property
    def usage_unknown(self) -> bool:
        """True when ``usage`` is zero only because nothing was measured."""
        return self.metrics.get("usage") == "unknown"

    def _external_dict(self) -> dict[str, Any]:
        """Extra fields for a run executed by an external worker (else empty)."""
        if not self.usage_unknown and "authority" not in self.metrics:
            return {}
        keys = ("authority", "provider", "model", "transcript_path", "claude_session", "external_session",
                "external_kind", "external_label", "inherits_agy_settings", "provenance",
                "tool_calls", "files_changed_note", "files_changed_total")
        return {
            "usage_unknown": self.usage_unknown,
            "external": {key: self.metrics[key] for key in keys if key in self.metrics},
        }

    def render(self) -> str:
        """The model-facing body: child text plus clamp/drop notes."""
        lines: list[str] = [self.model_line()]
        label = f"{self.agent} (session {self.session_id})" if self.session_id else self.agent
        if self.clamp_note:
            lines.append(self.clamp_note)
        elif self.clamped and self.requested_tier and self.tier:
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
        if self.metrics.get("authority"):
            lines.append(f"[authority: {self.metrics['authority']}]")
        if self.usage_unknown:
            lines.append(f"[usage: unknown — external {self.metrics.get('external_label') or 'Claude Code'} worker]")
        attention = self.attention
        if attention:
            lines.append(
                f"[needs attention: {attention}. You are the escalation point for this subagent: "
                "first try to resolve it yourself (re-delegate a narrower task, a different role or "
                "workspace, or work you can do within your own permissions). You cannot grant "
                "yourself or a child more authority than you hold; if the work needs it, ask the "
                "user in your reply, state exactly what is needed and why, and wait for their answer.]"
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
        if self.metrics.get("files_changed_note"):
            lines.append(f"[note: {self.metrics['files_changed_note']}]")
        if self.handoff:
            where = f"saved at {self.handoff_path}" if self.handoff_path else "not saved to disk"
            again = (
                f"continue it with subagent_send (agent_id {self.session_id or self.agent})"
                if self.continuable
                else "start a new subagent and put what it needs from this report in its prompt"
            )
            lines.append(
                f"[{self.agent} did not finish (stop reason: {self.stop_reason or 'error'}). "
                f"The digest below is {where}. If the work still matters, {again}.]"
            )
            lines.append(self.handoff)
        if len(lines) == 1:
            lines.append(f"{label}: no report")
        return "\n".join(lines)

    def render_report(
        self,
        *,
        run: int = 1,
        role: str = "",
        model: str | None = None,
        max_chars: int | None = None,
    ) -> str:
        """The message a finished background run delivers to the root (plan 3.2).

        A fixed header (agent, role, route, run, status) and the statement that this
        is an automated report, not a user message and not authority, then
        :meth:`render`. ``max_chars`` clips the body with an announced clip.
        """
        from .pool import format_report

        return format_report(self.as_report(run=run, role=role, model=model), max_chars=max_chars)

    def as_report(
        self, *, run: int = 1, role: str = "", model: str | None = None, agent_id: str = ""
    ) -> Any:
        """This outcome as a pool ``AgentReport`` (unclipped body).

        ``agent_id`` is the child's id (its session id, what ``subagent_send`` takes);
        ``self.agent`` is only the role name.
        """
        from .pool import AgentReport

        return AgentReport(
            agent_id=agent_id or self.session_id or self.agent, run=run, status=self.status, role=role,
            model=self.actual_model or model, tier=self.tier,
            session_id=self.session_id, body=self.render(), continuable=self.continuable,
        )


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
        max_live: int | None = None,
    ) -> None:
        if max_live is not None and (
            isinstance(max_live, bool) or not isinstance(max_live, int) or max_live < 1
        ):
            raise SubagentError("max_live must be a positive integer or None")
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
        #: Most children held at once (running, or idle once children persist);
        #: ``None`` = unbounded. Unlike the per-window counters it spans turns.
        self.max_live = max_live

        self._live = 0
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
        """Total children admitted to the tree so far (this window)."""
        return self._children

    @property
    def live(self) -> int:
        """Children admitted and not yet released; spans windows."""
        return self._live

    def begin_window(self) -> None:
        """Start a new root-turn window: spend and fan-out reset, live children stay.

        The pool owns one budget per root session so concurrency and the live
        count follow children across turns; the token/cost/fan-out counters keep
        their per-root-turn meaning. A child's usage is charged to the window in
        which it settles, and reservations stay held until then.
        """
        self._children = 0
        self._spent_tokens = 0
        self._spent_cost = 0.0

    def release_live(self) -> None:
        if self._live > 0:
            self._live -= 1

    def adopt_live(self, count: int) -> None:
        """Charge ``count`` slots for children rebuilt from a log (idle, held)."""
        if count > 0:
            self._live += count

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
            "max_live": self.max_live,
            "live": self._live,
            "children": self._children,
            "active": self._active,
            "token_budget": self.token_budget,
            "spent_tokens": self._spent_tokens,
            "cost_budget": self.cost_budget,
            "spent_cost": self._spent_cost,
        }

    # -- admission ---------------------------------------------------------

    async def reserve(
        self, *, tokens: int = 0, cost: float = 0.0, live: bool = True
    ) -> SubagentReservation | None:
        """Admit one child and charge an estimate, or return ``None``.

        The charge is a *reservation*: it is released on cancellation and
        replaced by the measured usage on :meth:`settle`. Reserving before the
        child runs is what keeps the aggregate budget honest under concurrency.
        """
        async with self._lock:
            # A continuation reuses the live slot its child already holds.
            if live and self.max_live is not None and self._live >= self.max_live:
                return None
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
            if live:
                self._live += 1
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
    root_id: str = ""
    owner_spawn_seq: int = 0
    owner_turn_id: str = ""
    #: The role's ordered fallback model references (tried before the global chain).
    fallback: tuple[str, ...] = ()
    #: Model-facing note when the role's tiers or ``max_tier`` moved the request.
    clamp_note: str = ""
    #: ``0`` for the first run; the run number of a continuation, whose
    #: ``follow_up`` (also ``prompt``) is the root's message appended to the
    #: child's existing conversation.
    resume: int = 0
    follow_up: str = ""


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
    durable: bool = False,
) -> Any:
    """Forward telemetry best-effort, but propagate durable state failures."""
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
            outcome = await outcome
        return outcome
    except Exception:  # noqa: BLE001 - optional observation remains best effort
        if durable:
            raise


class _ChildRelay:
    """An isolated relay that tags every child event with its agent metadata."""

    __slots__ = ("_meta", "_models", "_session", "_sink")

    def __init__(
        self,
        sink: object | None,
        meta: Mapping[str, Any],
        session: str,
    ) -> None:
        self._sink = sink
        self._models: list[str] = []
        self._meta = dict(meta)
        self._meta["id"] = self._meta.get("agent_id", self._meta.get("id"))
        self._session = session

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._meta)

    @property
    def models(self) -> tuple[str, ...]:
        """Distinct concrete models the direct child started, in order."""
        return tuple(self._models)

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
            if event_type in _MODEL_START_EVENTS:
                ref = qualify_model(payload.get("provider"), payload.get("model"))
                if ref is not None and (not self._models or self._models[-1] != ref):
                    self._models.append(ref)
        await _forward(
            self._sink, event_type, payload, session=self._session,
            durable=event_type == "todo.updated",
        )


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TierDecision:
    """Which tier a child runs on and how the request was adjusted.

    ``allowed`` is the role's usable ``tiers`` (empty when the role sets none),
    ``requested`` what the call asked for, ``role_clamped`` whether the role's
    list moved the request, and ``concrete`` a concrete model the call named.
    """

    resolution: TierResolution
    allowed: tuple[str, ...] = ()
    requested: str | None = None
    role_clamped: bool = False
    concrete: str | None = None

    @property
    def tier(self) -> str:
        return str(self.resolution.tier)

    @property
    def clamped(self) -> bool:
        return bool(self.resolution.clamped) or self.role_clamped

    @property
    def reason(self) -> str:
        if self.role_clamped:
            return "role"
        return "max_tier" if self.resolution.clamped else ""


@dataclass
class PreparedChild:
    """A child that is admitted, routed and announced but not yet running.

    Produced by :meth:`SubagentRunner._prepare` (everything through
    ``agent.spawned``) and consumed by :meth:`SubagentRunner._execute`. ``error``
    carries a failure that happened while preparing; ``_execute`` re-raises it so
    the one set of handlers settles budget, worktree and ``agent.completed``.
    """

    req: TaskRequest
    role: Any
    depth: int
    selection: Any
    decision: Any
    resolution: Any
    tier: str
    child_model: str | None
    child_provider: str | None
    clamped: bool
    clamp_note: str
    dropped: tuple[str, ...]
    sink: object | None
    session_id: str
    task_id: str
    agent_id: str
    parent_cancel: object | None
    cancel: _ChildCancel
    meta: dict[str, Any]
    relay: _ChildRelay
    reservation: SubagentReservation
    call_id: str
    worktree: Mapping[str, Any] | None
    worktree_root: Path | None
    mark_finished: Any
    child_workspace: Path
    spawn_event: object | None
    error: BaseException | None
    index: int
    #: True for a ``start()`` child: its report is delivered to the root.
    detached: bool = False
    #: The spawn-time authority a continuation is held to (the parent's own
    #: objects at spawn); ``None`` means "this runner's current ones".
    permissions: object | None = None
    grants: tuple[object, ...] | None = None
    #: Run number (1 = spawn) and, for a continuation, the root's message.
    run: int = 1
    resume: bool = False
    follow_up: str = ""
    #: The child may be continued after this run (persistent, Nexus-native).
    continuable: bool = False
    #: Why not, in words for the root.
    not_continuable: str = ""


@dataclass(frozen=True)
class StartedChild:
    """What :meth:`SubagentRunner.start` returns.

    When the child was refused or failed before launch ``outcome`` is set and
    nothing runs; otherwise ``agent_id`` names the running child.
    """

    outcome: SubagentOutcome | None = None
    agent_id: str = ""
    session_id: str = ""
    role: str = ""
    model: str | None = None
    tier: str | None = None
    description: str = ""
    workspace: str = ""
    worktree: Mapping[str, Any] | None = None
    dropped_tools: tuple[str, ...] = ()
    clamp_note: str = ""
    tools: tuple[str, ...] = ()
    #: Run number (1 for a spawn, more for a continuation).
    run: int = 1

    @property
    def started(self) -> bool:
        return self.outcome is None


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
        tier_probe: Callable[[str], bool] | None = None,
        tier_candidates: Callable[[str], Sequence[str]] | None = None,
        tier_efforts: Callable[[], Mapping[str, Mapping[str, str]]] | None = None,
        route_gate: Callable[[str], Any] | None = None,
        pool: Any | None = None,
        persistent: bool | None = None,
        continuation_blocker: Callable[[str | None, str | None], str | None] | None = None,
        max_followup_chars: int | None = None,
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
        #: Says whether a tier currently resolves to a runnable model. Absent
        #: (tests, static wiring) every tier is assumed runnable.
        self._tier_probe = tier_probe
        #: The router's live per-route efforts, so the roster advertises what the
        #: next request will run even when ``config`` is an older generation.
        self._tier_efforts = tier_efforts
        self._tier_candidates = tier_candidates
        #: ``tier -> RouteDecision | None``: skips providers whose plan quota is
        #: nearly spent. Absent (tests, static wiring) the tier runs as configured.
        self._route_gate = route_gate
        #: The session's agent pool (``nexus.agents.pool.RootAgents``); ``start``
        #: needs it, ``spawn`` does not.
        self._pool = pool
        #: Children outlive their run (idle, continuable with ``resume``) when the
        #: pool exists and ``agents.background`` is on (or ``persistent`` says so).
        self._persistent_override = persistent
        #: ``(model, provider) -> reason | None``: why a child on that route could
        #: not be continued (an external worker), else ``None``.
        self._continuation_blocker = continuation_blocker
        self._max_followup_chars = max_followup_chars
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
    def background(self) -> bool:
        """Whether ``subagent`` returns at once (``agents.background``)."""
        section = getattr(getattr(self._config, "v2", None), "agents", None)
        return self._pool is not None and getattr(section, "background", False) is True

    @property
    def persistent(self) -> bool:
        """Whether a child keeps its slot and can be continued after its run."""
        if self._pool is None:
            return False
        if self._persistent_override is not None:
            return self._persistent_override is True
        section = getattr(getattr(self._config, "v2", None), "agents", None)
        return getattr(section, "background", False) is True

    @property
    def max_followup_chars(self) -> int:
        if self._max_followup_chars is not None:
            return self._max_followup_chars
        section = getattr(getattr(self._config, "v2", None), "agents", None)
        value = getattr(section, "max_followup_chars", DEFAULT_MAX_FOLLOWUP_CHARS)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else DEFAULT_MAX_FOLLOWUP_CHARS

    @property
    def agents(self) -> AgentManager:
        return self._agents

    def role_index(self, *, max_chars: int = MAX_ROLE_INDEX_CHARS) -> str:
        """Sanitized ``name: description`` lines for every spawnable role.

        Progressive disclosure, like the skills index: only the names and
        descriptions of definitions eligible in the ``subagent`` context, never
        a body, path, or tool list, bounded to whole lines.
        """
        return self._render_roster(max_chars)[0]

    def tier_lines(self) -> list[str]:
        """One whole ``<tier> models (in order): ...`` line per tier, unclipped."""
        return [_tier_line(tier, row) for tier, row in self._tier_rows()]

    def roster_digests(self, *, max_chars: int = MAX_ROLE_INDEX_CHARS) -> RosterDigests:
        """Digests of the tier lines the description advertises at ``max_chars``.

        Same code path as :meth:`role_index`, so a digest change is exactly a
        change of the advertised tier models or efforts (never of a role
        description, and not of a tier line the budget dropped).
        """
        return self._render_roster(max_chars)[1]

    def _tier_rows(self) -> list[tuple[str, list[tuple[str, str | None]]]]:
        """``(tier, [(reference, effective effort)])`` as the router would run them."""
        efforts = self._tier_efforts() if self._tier_efforts is not None else None
        if efforts is None:
            efforts = getattr(
                getattr(getattr(self._config, "v2", None), "models", None), "tier_efforts", None
            )
        efforts = efforts or {}
        pinned = set(self._tiers.overrides.values())
        rows = []
        for tier in self._tiers.order:
            refs = (
                self._tier_candidates(tier)
                if self._tier_candidates
                else [
                    ref
                    for ref, name in {**self._tiers.builtin, **self._tiers.overrides}.items()
                    if name == tier
                ]
            )
            configured = efforts.get(tier) or {}
            rows.append((tier, [
                (ref, effective_tier_effort(
                    tier, ref, configured=configured.get(ref), pinned=tier in pinned
                ))
                for ref in refs
            ]))
        return rows

    def _render_roster(self, max_chars: int) -> tuple[str, RosterDigests]:
        """Render tier lines then the role index in whole lines under ``max_chars``.

        Role names (with tiers and any effort override) and the clip-note line
        are reserved first; tier lines then take what is left, dropping whole
        lines from the end, and role descriptions fill the remainder in order.
        A role whose full line does not fit still appears by name.
        """
        eligible = {
            agent.name for agent in self._agents.agents if agent.eligible_in("subagent")
        }
        roles = [
            (_role_prefix(entry), entry.description)
            for entry in self._agents.index
            if entry.name in eligible
        ]
        rows = self._tier_rows()
        reserved = len(ROSTER_CLIP_NOTE) + 1 + sum(len(prefix) + 1 for prefix, _ in roles)
        tier_budget = max_chars - reserved - 2
        kept: list[tuple[str, list[tuple[str, str | None]]]] = []
        used = 0
        for row in rows:
            cost = len(_tier_line(*row)) + 1
            if used + cost > tier_budget:
                break
            kept.append(row)
            used += cost
        clipped = len(kept) < len(rows)
        available = max_chars - used - (2 if kept else 0) - len(ROSTER_CLIP_NOTE) - 1
        role_lines: list[str] = []
        for index, (prefix, description) in enumerate(roles):
            later = sum(len(p) + 1 for p, _ in roles[index + 1:])
            full = f"{prefix}: {description}"
            if len(full) + 1 + later <= available:
                role_lines.append(full)
                available -= len(full) + 1
            elif len(prefix) + 1 + later <= available:
                role_lines.append(prefix)
                available -= len(prefix) + 1
                clipped = True
            else:
                clipped = True
        sections = [
            section
            for section in ("\n".join(_tier_line(*row) for row in kept), "\n".join(role_lines))
            if section
        ]
        rendered = "\n\n".join(sections)
        if clipped:
            rendered += ("\n" if rendered else "") + ROSTER_CLIP_NOTE
        # Only a budget smaller than the clip note itself can overflow.
        rendered = rendered[:max(0, max_chars)]
        models = "\n".join(f"{tier}\t{','.join(ref for ref, _ in row)}" for tier, row in kept)
        efforts = "\n".join(
            tier + "\t" + ",".join(ref + "=" + (effort or "") for ref, effort in row)
            for tier, row in kept
        )
        return rendered, RosterDigests(
            hashlib.sha256(models.encode()).hexdigest(),
            hashlib.sha256(efforts.encode()).hexdigest(),
        )

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
        """Concrete request override, then the role default, then a tier hint."""
        req = TaskRequest.from_value(request)
        if req.model and req.model not in self._tiers.order and req.model != MODEL_INHERIT:
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
        if role is not None and role.model and role.model != MODEL_INHERIT:
            if role.model in self._tiers.order or "/" in role.model:
                return role.model
            if role.provider:
                return f"{role.provider}/{role.model}"
            return role.model
        if role is not None and role.provider and self._parent_model:
            inherited_model = self._parent_model.rsplit("/", 1)[-1]
            return f"{role.provider}/{inherited_model}"
        return req.model

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

    def _role_for(self, req: TaskRequest) -> AgentDef | None:
        try:
            return self._agents.resolve(req.subagent_type, context="subagent")
        except AgentNotFoundError:
            return None

    def allowed_tiers(self, role: object) -> tuple[str, ...]:
        """The role's ``tiers`` that name a tier this table knows, in order."""
        declared = tuple(getattr(role, "tiers", ()) or ())
        return tuple(tier for tier in declared if tier in self._tiers.order)

    def _nearest_allowed(self, tier: str, allowed: tuple[str, ...]) -> str:
        """The allowed tier closest to ``tier`` (ties go to the cheaper one)."""
        want = self._tiers.rank(tier)
        if want is None:
            return allowed[0]
        return min(
            allowed,
            key=lambda name: (abs((self._tiers.rank(name) or 0) - want), self._tiers.rank(name) or 0),
        )

    def _runnable(self, tier: str) -> bool:
        probe = self._tier_probe
        if probe is None:
            return True
        try:
            return bool(probe(tier))
        except Exception:  # noqa: BLE001 - a broken probe must not block delegation
            return True

    def tier_decision(self, request: object) -> TierDecision:
        """The tier a child runs on, with what was asked and why it moved.

        Order (first match wins): a tier named by the call; the tier of a
        concrete model named by the call; the role's pinned model; the role's
        first tier. A tier outside the role's ``tiers`` moves to the nearest
        allowed one (``role_clamped``); the global ``max_tier`` applies last.
        A role with no ``tiers`` keeps the earlier behavior unchanged.
        """
        req = TaskRequest.from_value(request)
        role = self._role_for(req)
        allowed = self.allowed_tiers(role) if role is not None else ()
        if not allowed:
            reference = self.role_model_reference(req)
            if reference is None or reference == MODEL_INHERIT:
                resolution = self._tiers.resolve(
                    None, parent=self._parent_tier, max_tier=self._max_tier
                )
            else:
                resolution = self._tiers.resolve(
                    reference, parent=self._parent_tier, max_tier=self._max_tier
                )
            return TierDecision(resolution=resolution)

        asked = req.model if req.model and req.model != MODEL_INHERIT else None
        requested: str | None = None
        if asked is not None:
            requested = (
                asked if asked in self._tiers.order else self._tiers.resolve(asked).tier
            )
        pinned = role.model if role.model and role.model != MODEL_INHERIT else None
        if requested is not None:
            chosen = requested
        elif pinned is not None:
            chosen = (
                pinned if pinned in self._tiers.order else self._tiers.resolve(pinned).tier
            )
        else:
            chosen = allowed[0]
        role_clamped = chosen not in allowed
        if role_clamped:
            chosen = self._nearest_allowed(chosen, allowed)
        # A tier with no runnable model falls through to the role's other tiers
        # (the cheaper ones first), so a misconfigured tier never blocks work.
        if not self._runnable(chosen):
            for other in allowed:
                if other != chosen and self._runnable(other):
                    chosen = other
                    break
        resolution = self._tiers.resolve(
            chosen, parent=self._parent_tier, max_tier=self._max_tier
        )
        return TierDecision(
            resolution=resolution,
            allowed=allowed,
            requested=requested or asked,
            role_clamped=role_clamped,
            concrete=asked if asked is not None and asked not in self._tiers.order else None,
        )

    def resolve_tier(self, request: object) -> TierResolution:
        """Resolve and clamp the effective tier for a request."""
        return self.tier_decision(request).resolution

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
            available=[
                tool
                for tool in self._parent_tools
                if canonical_tool_name(str(tool)) not in ROOT_ONLY_TOOLS
            ],
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
        if any(name.startswith("mcp__") for name in selected):
            selected |= selection.selected & {"McpSearch", "McpCall"}
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

    def _tiered_child_model(
        self,
        role: AgentDef,
        req: TaskRequest,
        reference: str | None,
        decision: TierDecision,
    ) -> str | None:
        """The child's model for a role that declares ``tiers``.

        An explicit concrete model (named by the call, or pinned by the role
        when the call names nothing) is kept while its tier is allowed. Any
        other case runs the tier, which the router resolves to the first
        runnable model of that tier (Settings -> Models). A tier with no
        runnable model runs on the parent's model instead of failing.
        """
        pinned = role.model if role.model and role.model != MODEL_INHERIT else None
        call_named = bool(req.model) and req.model != MODEL_INHERIT
        concrete = (
            decision.concrete
            if call_named
            else (pinned if pinned and pinned not in self._tiers.order else None)
        )
        if concrete and not decision.clamped and reference:
            return reference
        if not self._runnable(decision.tier) and self._parent_model:
            return self._parent_model
        return decision.tier

    def _clamp_note(self, role: AgentDef, decision: TierDecision) -> str:
        """The model-facing explanation of a role or ``max_tier`` adjustment."""
        if not decision.clamped:
            return ""
        asked = decision.requested or decision.resolution.reference or ""
        if decision.role_clamped:
            listed = ", ".join(decision.allowed)
            note = (
                f"[note: {role.name} runs on {listed} only; requested "
                f"{asked!r} ran on {decision.tier!r}"
            )
            if decision.resolution.clamped:
                note += f", capped by max_tier {self._max_tier!r}"
            return note + "]"
        return (
            f"[note: requested tier {asked!r} was clamped to "
            f"{decision.tier!r} by max_tier]"
        )

    # -- child session -----------------------------------------------------

    async def _next_child_id(self) -> tuple[int, str]:
        allocator = getattr(self._sessions, "allocate_child_id", None)
        if callable(allocator):
            # Logical paths remain stable for UI/tool correlation; the physical
            # parent session is carried separately in the ownership edge.
            allocated = allocator(self._parent_agent_id or self._parent_session)
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
        """Spawn one child, bounded and authority-intersected, and wait for it.

        Expected refusals (unknown role, depth/fan-out/budget, a factory error)
        come back as an error :class:`SubagentOutcome` so the model can see why;
        only cancellation raises :class:`~nexus.errors.OperationCancelled`.
        This is :meth:`_prepare` followed by an awaited :meth:`_execute`; the
        background path is :meth:`start`.
        """
        prepared = await self._prepare(request, cancel=cancel, emit=emit, call_id=call_id)
        if isinstance(prepared, SubagentOutcome):
            return prepared
        pool = self._pool
        if pool is not None and self.persistent and prepared.error is None:
            # A persistent child is a pool record even when the caller waits, so
            # the root can list, continue and stop it later.
            record = self._register(prepared)
            return await pool.run_inline(record, self._execute(prepared))
        return await self._execute(prepared)

    def _register(self, prepared: PreparedChild) -> Any:
        """Create the pool record of a prepared child (it holds a live slot)."""
        assert self._pool is not None
        return self._pool.register(
            agent_id=prepared.agent_id,
            session_id=prepared.session_id,
            role=prepared.role.name,
            model=prepared.child_model,
            tier=prepared.tier,
            tools=tuple(sorted(prepared.selection.selected)),
            cancel=prepared.cancel,
            worktree=prepared.worktree,
            continuable=prepared.continuable,
            recipe=prepared,
            meta=prepared.meta,
            description=prepared.req.description or prepared.role.description,
            not_continuable=prepared.not_continuable,
            budget=self._budget,
        )

    async def start(
        self,
        request: object,
        *,
        emit: object | None = None,
        call_id: str = "",
    ) -> StartedChild:
        """Spawn one child and return as soon as it is running in the background.

        Everything up to and including ``agent.spawned`` happens here, through
        ``emit`` (the spawning tool call's emitter, so the launch orders inside
        it). The run itself is a task owned by the session's agent pool: it has
        its own cancel token (no parent token, so ending the root turn does not
        cancel it) and reports its events through the root event port. A refusal
        or a failure before launch comes back as ``StartedChild.outcome`` with
        nothing running.
        """
        pool = self._pool
        if pool is None:
            raise SubagentError("no agent pool is configured; use spawn() to wait for the child")
        prepared = await self._prepare(
            request, cancel=None, emit=emit, call_id=call_id,
            detached_sink=pool.event_sink,
        )
        if isinstance(prepared, SubagentOutcome):
            return StartedChild(outcome=prepared)
        if prepared.error is not None:
            # Failed before launch: conclude inline so the caller sees why.
            return StartedChild(outcome=await self._execute(prepared))
        selected = prepared.selection
        started = StartedChild(
            agent_id=prepared.agent_id,
            session_id=prepared.session_id,
            role=prepared.role.name,
            model=prepared.child_model,
            tier=prepared.tier,
            description=prepared.req.description or prepared.role.description,
            workspace=str(prepared.child_workspace),
            worktree=dict(prepared.worktree) if prepared.worktree is not None else None,
            dropped_tools=prepared.dropped,
            clamp_note=prepared.clamp_note,
            tools=tuple(sorted(selected.selected)),
        )
        pool.start_run(self._register(prepared), self._execute(prepared))
        return started

    # -- continuation, stop, list ---------------------------------------------

    async def _evict_idle_for_slot(self) -> None:
        """Close the least-recently-run idle child when a spawn would exceed ``max_live``.

        Idle children hold a slot until ``subagent_stop``; a root that never stops
        them would otherwise lock itself out. Only children that are not running are
        evicted (``agent.stopped`` with ``by="system"``, the reason names the
        eviction); when every live child is running the spawn is refused as before.
        """
        pool = self._pool
        budget = self._budget
        if pool is None or not self.persistent or budget.max_live is None:
            return
        if budget.live < budget.max_live:
            return
        victim = pool.eviction_candidate()
        if victim is None:
            return
        await pool.stop(
            victim.agent_id,
            by="system",
            reason=(
                f"evicted to make room for a new subagent: {budget.live} of "
                f"{budget.max_live} live slots were in use and this was the "
                "least recently run idle subagent"
            ),
        )

    def _rebuilt_pool(self) -> Any:
        pool = self._pool
        if pool is None:
            raise ContinuationRefused(
                "no agent pool is configured, so there are no persistent subagents"
            )
        pool.ensure_rebuilt(self._budget)
        return pool

    def agent_permission_key(self, agent_id: object) -> str:
        """The gate key of an existing child, ``"<role>:<tier>"``: what spawning it was keyed on.

        ``deny = ["Task(*:high)"]`` therefore also blocks continuing or stopping a
        high-tier child. An unknown id gets a key no rule names, and the tool
        refuses it anyway.
        """
        pool = self._pool
        if pool is not None and isinstance(agent_id, str):
            pool.ensure_rebuilt(self._budget)
            record = pool.get(agent_id)
            if record is not None:
                return f"{record.role}:{record.tier or 'inherit'}"
        return "unknown:unknown"

    def list_agents(self) -> list[Any]:
        """One row per subagent of this session, bounded to ``max_live`` rows."""
        pool = self._pool
        if pool is None:
            return []
        pool.ensure_rebuilt(self._budget)
        limit = self._budget.max_live or 16
        return pool.rows(limit=limit)

    @property
    def wake_paused(self) -> bool:
        """Report delivery is paused after repeated automatic wake-ups."""
        return bool(self._pool.wake_paused) if self._pool is not None else False

    async def stop(self, agent_id: object, *, reason: str = "", by: str = "root") -> Any:
        """Cancel a running child or close an idle one for good (idempotent)."""
        pool = self._rebuilt_pool()
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ContinuationRefused("'agent_id' must be a non-empty string")
        return await pool.stop(agent_id.strip(), by=by, reason=reason)

    def _recipe_from_record(self, record: Any) -> PreparedChild:
        """Rebuild the spawn recipe of a child this process did not start (after a restart).

        Authority is derived afresh through the same path as spawn:
        ``current root ceiling & role & recorded tools``; a continuation can only
        shrink, never widen, relative to the spawn.
        """
        data = record.spawn_data or {}
        meta = dict(record.meta)
        try:
            role = self._agents.resolve(record.role or str(meta.get("type") or ""), context="subagent")
        except AgentNotFoundError as exc:
            raise ContinuationRefused(
                f"the role {record.role!r} of subagent {record.agent_id} no longer exists, so it "
                "cannot be continued; start a new subagent and put what it needs in the prompt"
            ) from exc
        requested = data.get("requested_tier")
        req = TaskRequest(
            prompt="(continuation)",
            subagent_type=role.name,
            tools=tuple(record.tools),
            model=requested if isinstance(requested, str) and requested.strip() else None,
            description=record.description or None,
            worktree=record.worktree is not None,
        )
        selection = self.select_tools(role, req)
        decision = self.tier_decision(req)
        resolution = decision.resolution
        worktree = dict(record.worktree) if record.worktree is not None else None
        worktree_root = None
        child_workspace = self._workspace
        mark_finished = None
        if worktree is not None:
            worktree_root = self._worktree_root
            if self._worktree_root_for is not None:
                worktree_root = Path(self._worktree_root_for(self._workspace))
            mark_finished = getattr(self._worktree_service, "mark_finished", None)
            child_workspace = Path(str(worktree.get("path") or self._workspace))
        task_id = str(meta.get("task") or new_id())
        index = meta.get("index")
        return PreparedChild(
            req=req, role=role, depth=self._depth + 1, selection=selection, decision=decision,
            resolution=resolution, tier=str(record.tier or resolution.tier),
            child_model=record.model or str(resolution.tier),
            child_provider=self.role_provider(req), clamped=bool(data.get("clamped")),
            clamp_note="", dropped=tuple(str(n) for n in (data.get("dropped_tools") or ())),
            sink=None, session_id=record.session_id, task_id=task_id, agent_id=record.agent_id,
            parent_cancel=None, cancel=_ChildCancel(None), meta=meta,
            relay=_ChildRelay(None, meta, record.session_id),
            reservation=SubagentReservation(), call_id="", worktree=worktree,
            worktree_root=worktree_root, mark_finished=mark_finished,
            child_workspace=child_workspace, spawn_event=None, error=None,
            index=index if isinstance(index, int) and not isinstance(index, bool) else 0,
            continuable=True,
        )

    async def resume(
        self,
        agent_id: object,
        message: object,
        *,
        wait: bool = False,
        emit: object | None = None,
        cancel: object | None = None,
        call_id: str = "",
    ) -> StartedChild:
        """Run another turn on an existing child, with ``message`` as the new user input.

        The child keeps its id, session (so it sees everything it saw and did
        before), model route, role and worktree. Authority is the spawn-time
        ceiling: its recorded tools, permission snapshot and grants; it never
        widens. ``agent.resumed`` goes through ``emit`` (the calling tool's
        emitter) so it orders inside the call; the run is a pool task reporting
        through the root event port, or, with ``wait``, awaited here with the
        caller's ``cancel`` token. Every refusal raises
        :class:`ContinuationRefused` with a message for the root model.
        """
        if self._depth >= 1:
            raise ContinuationRefused("subagents cannot continue subagents; only the root session may")
        pool = self._pool
        if pool is None or not self.persistent:
            raise ContinuationRefused(
                "subagents are not persistent here (agents.background = false): a "
                "subagent ends with its report; start a new one instead"
            )
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ContinuationRefused("'agent_id' must be a non-empty string")
        agent_id = agent_id.strip()
        if not isinstance(message, str) or not message.strip():
            raise ContinuationRefused("'message' must be a non-empty string")
        limit = self.max_followup_chars
        if len(message) > limit:
            raise ContinuationRefused(
                f"the follow-up is {len(message)} characters; the limit is {limit} "
                "(agents.max_followup_chars). Shorten it, or put the long material in a "
                "file and tell the subagent where to read it."
            )
        pool.ensure_rebuilt(self._budget)
        record = pool.get(agent_id)
        if record is None:
            raise ContinuationRefused(
                f"no subagent {agent_id!r} in this session; subagent_list shows the ones you started"
            )
        if record.state == "stopped":
            raise ContinuationRefused(
                f"subagent {agent_id} was stopped"
                + (f" ({record.stopped_reason})" if record.stopped_reason else "")
                + "; start a new subagent and put what it needs in the prompt"
            )
        if record.state == "running":
            raise ContinuationRefused(
                f"subagent {agent_id} is still running (run {record.runs}). Its report arrives "
                "when it finishes; wait for it, or stop it with subagent_stop and then send."
            )
        if not record.continuable:
            raise ContinuationRefused(
                f"subagent {agent_id} cannot be continued: "
                f"{record.not_continuable or 'it is not continuable'}"
            )
        recipe: PreparedChild = record.recipe or self._recipe_from_record(record)
        try:
            role = self._agents.resolve(recipe.role.name, context="subagent")
        except AgentNotFoundError as exc:
            raise ContinuationRefused(
                f"the role {recipe.role.name!r} no longer exists; start a new subagent"
            ) from exc

        parent_cancel = cancel if wait else None
        child_cancel = _ChildCancel(parent_cancel)
        pool.begin_run(record, cancel=child_cancel)
        run = record.runs
        launch_sink = emit if emit is not None else self._event_sink
        sink = launch_sink if wait else pool.event_sink
        meta = dict(recipe.meta)
        worktree = dict(recipe.worktree) if recipe.worktree is not None else None
        reservation: SubagentReservation | None = None
        reopened = False
        try:
            reservation = await self._budget.reserve(
                tokens=self._reserved_tokens, cost=self._reserved_cost, live=False
            )
            if reservation is None:
                raise ContinuationRefused(
                    "the subagent tree budget is exhausted "
                    f"({_budget_label(self._budget)}); the follow-up was not sent"
                )
            if worktree is not None:
                reopen = getattr(self._worktree_service, "reopen", None)
                if not callable(reopen) or recipe.worktree_root is None:
                    raise ContinuationRefused("this subagent's worktree cannot be reopened here")
                try:
                    reopened_record = await _maybe_await(
                        reopen(record.session_id, root=recipe.worktree_root)
                    )
                except OperationCancelled:
                    raise
                except Exception as exc:
                    raise ContinuationRefused(
                        f"the worktree of subagent {agent_id} cannot be reopened: {_safe(exc)}"
                    ) from exc
                reopened = True
                worktree = {**worktree, **_worktree_record(reopened_record)}
                worktree.update(base=worktree.get("base_commit"), owner=worktree.get("owner_uid"))
                meta["worktree"] = dict(worktree)
            shown = message[:RESUMED_EVENT_CHARS]
            if len(message) > len(shown):
                shown += (
                    f"\n[follow-up clipped: showed {len(shown)} of {len(message)} characters; "
                    "the subagent received all of it]"
                )
            await _forward(
                launch_sink,
                AGENT_RESUMED,
                {
                    "agent": dict(meta), **meta, "run": run, "from": "root", "message": shown,
                    "message_chars": len(message), "background": not wait, "continuable": True,
                    "worktree": dict(worktree) if worktree is not None else None,
                },
                session=self._parent_session,
                durable=True,
            )
        except BaseException:
            pool.abort_run(record)
            if reservation is not None:
                with contextlib.suppress(Exception):
                    await self._budget.release_reservation(reservation)
            if reopened and recipe.mark_finished is not None:
                with contextlib.suppress(Exception):
                    await _maybe_await(
                        recipe.mark_finished(
                            record.session_id, {"status": "cancelled"}, root=recipe.worktree_root
                        )
                    )
            raise
        _LIVE_CHILDREN[agent_id] = child_cancel
        prepared = replace(
            recipe,
            role=role, sink=sink, cancel=child_cancel, parent_cancel=parent_cancel, meta=meta,
            relay=_ChildRelay(sink, meta, record.session_id), reservation=reservation,
            call_id=call_id if isinstance(call_id, str) else "", worktree=worktree, error=None,
            detached=not wait, run=run, resume=True, follow_up=message, continuable=True,
            not_continuable="",
        )
        record.recipe = prepared
        record.meta = meta
        if worktree is not None:
            record.worktree = worktree
        if wait:
            outcome = await pool.run_inline(record, self._execute(prepared))
            return StartedChild(outcome=outcome, agent_id=agent_id, run=run)
        pool.start_run(record, self._execute(prepared))
        return StartedChild(
            agent_id=agent_id, session_id=record.session_id, role=role.name,
            model=prepared.child_model, tier=prepared.tier,
            description=record.description, workspace=str(prepared.child_workspace),
            worktree=dict(worktree) if worktree is not None else None,
            tools=tuple(sorted(prepared.selection.selected)), run=run,
        )

    def _blocker_for(self, model: str | None, provider: str | None) -> str | None:
        """Why a child on this route cannot be continued (``None`` when it can)."""
        if self._pool is None or not self.persistent:
            return (
                "persistent subagents are off (agents.background = false): this "
                "subagent ended with its report"
            )
        if self._continuation_blocker is not None:
            try:
                return self._continuation_blocker(model, provider)
            except Exception:  # noqa: BLE001 - an unreadable route is not continuable
                return "its route could not be checked, so it is not continued"
        return None

    async def _prepare(
        self,
        request: object,
        *,
        cancel: object | None,
        emit: object | None,
        call_id: str,
        detached_sink: object | None = None,
    ) -> PreparedChild | SubagentOutcome:
        """Validate, route, reserve budget, create the worktree, emit ``agent.spawned``.

        Returns a refusal :class:`SubagentOutcome` for an expected refusal, else a
        :class:`PreparedChild`. A failure after the reservation is captured in
        ``PreparedChild.error`` and re-raised by :meth:`_execute`, so one set of
        handlers settles budget, worktree and ``agent.completed`` for both the
        blocking and the detached path. ``detached_sink`` (the root event port)
        carries every event after launch; the launch events still use ``emit``.
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

        if self._depth >= 1:
            return self._refusal(
                agent=req.subagent_type,
                error="Task: subagents cannot spawn subagents; only the root session may",
                request=req,
            )

        try:
            role = self._agents.resolve(req.subagent_type, context="subagent")
        except AgentNotFoundError:
            role = None
        if role is None:
            available =", ".join(
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
        decision = self.tier_decision(req)
        resolution = decision.resolution
        tier = resolution.tier
        reference = self.role_model_reference(req)
        child_model = self._child_model(reference, resolution)
        # An explicit tier routes through its ordered subagent model list.
        # Only an inherited/omitted model uses the parent's concrete model.
        if decision.allowed:
            child_model = self._tiered_child_model(role, req, reference, decision)
        elif (
            self._parent_model
            and (
                req.model is None
                or req.model == MODEL_INHERIT
            )
            and (not role.model or role.model == MODEL_INHERIT)
        ):
            child_model = (
                f"{role.provider}/{self._parent_model.rsplit('/', 1)[-1]}"
                if role.provider
                else self._parent_model
            )
        child_provider = self.role_provider(req)
        route_decision = None
        if child_model in self._tiers.order:
            child_provider = None
            if self._route_gate is not None:
                try:
                    route_decision = self._route_gate(child_model)
                except Exception:  # noqa: BLE001 - routing never fails a spawn
                    route_decision = None
                if route_decision is not None and route_decision.rerouted:
                    # Pin this child to the chosen route; the tier stays its label.
                    child_model = route_decision.chosen
        clamped = decision.clamped
        clamp_note = self._clamp_note(role, decision)
        dropped = tuple(sorted(selection.dropped))
        blocker = self._blocker_for(child_model, child_provider)

        launch_sink = emit if emit is not None else self._event_sink
        #: A detached child reports through the root event port, never the turn's emitter.
        sink = detached_sink if detached_sink is not None else launch_sink
        index, session_id = await self._next_child_id()
        manager = getattr(self._sessions, "manager", None)
        store = getattr(manager, "store", None)
        if store is not None:
            # External workers cannot participate in host context ancestry.
            store.grant_child_context_read(
                session_id, allowed=req.context_read and child_provider in {None, "nexus"}
            )
        task_id = new_id()
        # Session ids are stable identities inside a parent log and globally
        # unique within the persistent session facade's allocation scope.
        agent_id = session_id
        # Per-child cancellation (host ``AgentCancel``): the child sees the
        # parent's token OR its own, so one child can be stopped alone.
        parent_cancel = cancel
        cancel = _ChildCancel(parent_cancel)
        meta = {
            "id": agent_id,
            "parent": self._parent_agent_id or self._parent_session,
            "parent_session": self._parent_session,
            "root_id": (
                self._sessions.root_id(self._parent_session)
                if callable(getattr(self._sessions, "root_id", None))
                else (self._parent_session if not self._parent_agent_id else "")
            ),
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
        if self._pool is not None and self.persistent:
            # Children started before this process knew them hold slots too.
            self._pool.ensure_rebuilt(self._budget)
        await self._evict_idle_for_slot()
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
            await self._emit_completed(launch_sink, meta, outcome)
            return outcome

        _LIVE_CHILDREN[agent_id] = cancel
        worktree: Mapping[str, Any] | None = None
        child_workspace = self._workspace
        mark_finished = None
        worktree_root: Path | None = None
        spawn_event: object | None = None
        error: BaseException | None = None
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
            spawn_event = await _forward(
                launch_sink,
                AGENT_SPAWNED,
                {
                    "agent": dict(meta),
                    **meta,
                    "description": req.description or role.description,
                    "model": child_model,
                    "requested_tier": (
                        decision.requested or resolution.reference or self._parent_tier
                    ),
                    "allowed_tiers": list(decision.allowed),
                    "clamped": clamped,
                    "tools": sorted(selection.selected),
                    "dropped_tools": list(dropped),
                    "worktree": dict(worktree) if worktree is not None else None,
                    "background": detached_sink is not None,
                    "continuable": blocker is None,
                },
                session=self._parent_session,
            )
            if clamped:
                await _forward(
                    launch_sink,
                    AGENT_CLAMPED,
                    {
                        "agent": dict(meta),
                        **meta,
                        "requested_tier": (
                            decision.requested or resolution.reference or self._parent_tier
                        ),
                        "reason": decision.reason,
                        "allowed_tiers": list(decision.allowed),
                        "max_tier": self._max_tier,
                        "diagnostics": list(resolution.diagnostics),
                    },
                    session=self._parent_session,
                )
            if route_decision is not None and (route_decision.skipped or route_decision.all_blocked):
                await _forward(
                    launch_sink,
                    AGENT_ROUTED,
                    {"agent": dict(meta), **meta, **route_decision.to_event()},
                    session=self._parent_session,
                )
        except (Exception, asyncio.CancelledError) as exc:
            error = exc
        return PreparedChild(
            req=req, role=role, depth=depth, selection=selection, decision=decision,
            resolution=resolution, tier=tier, child_model=child_model,
            child_provider=child_provider, clamped=clamped, clamp_note=clamp_note,
            dropped=dropped, sink=sink, session_id=session_id, task_id=task_id,
            agent_id=agent_id, parent_cancel=parent_cancel, cancel=cancel, meta=meta,
            relay=relay, reservation=reservation, call_id=call_id, worktree=worktree,
            worktree_root=worktree_root, mark_finished=mark_finished,
            child_workspace=child_workspace, spawn_event=spawn_event, error=error,
            index=index, detached=detached_sink is not None,
            permissions=self._permissions, grants=self._grants,
            continuable=blocker is None, not_continuable=blocker or "",
        )

    async def _execute(self, p: PreparedChild) -> SubagentOutcome:
        """Run a prepared child, finalize its worktree, settle budget, emit ``agent.completed``."""
        req, role, depth, selection = p.req, p.role, p.depth, p.selection
        decision, resolution, tier = p.decision, p.resolution, p.tier
        child_model, child_provider = p.child_model, p.child_provider
        clamped, clamp_note, dropped, sink = p.clamped, p.clamp_note, p.dropped, p.sink
        session_id, task_id, agent_id = p.session_id, p.task_id, p.agent_id
        parent_cancel, cancel, meta, relay = p.parent_cancel, p.cancel, p.meta, p.relay
        reservation, call_id, worktree = p.reservation, p.call_id, p.worktree
        worktree_root, mark_finished = p.worktree_root, p.mark_finished
        child_workspace, spawn_event = p.child_workspace, p.spawn_event
        acquired = False
        reservation_done = False
        release_reservation_on_exit = False
        outcome: SubagentOutcome | None = None
        spec: ChildSpec | None = None
        #: Set once the child runtime exists (so a session/log exists to continue).
        built: dict[str, bool] = {"child": False}
        run = p.run
        cancellation: OperationCancelled | None = None
        task_cancelled = False
        try:
            if p.error is not None:
                raise p.error
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
                prompt=p.follow_up if p.resume else req.prompt,
                resume=run if p.resume else 0,
                follow_up=p.follow_up if p.resume else "",
                session_id=session_id,
                parent_session=self._parent_session,
                parent_agent_id=self._parent_agent_id,
                parent_call_id=call_id if isinstance(call_id, str) else "",
                root_turn_id=self._root_turn_id,
                root_id=str(meta.get("root_id") or ""),
                owner_spawn_seq=int(getattr(spawn_event, "seq", 0) or 0),
                owner_turn_id=str(getattr(spawn_event, "turn", "") or ""),
                depth=depth,
                tools=tuple(sorted(selection.selected)),
                dropped_tools=dropped,
                model=child_model,
                requested_model=req.model,
                parent_model=self._parent_model,
                provider=child_provider,
                reasoning_effort=role.reasoning_effort or getattr(
                    getattr(getattr(self._config, "v2", None), "models", None),
                    "tier_efforts", {},
                ).get(tier, {}).get(child_model),
                fallback=tuple(role.fallback),
                tier=tier,
                requested_tier=(
                    decision.requested or resolution.reference or self._parent_tier
                ),
                clamped=clamped,
                clamp_note=clamp_note,
                max_iterations=role.max_iterations or 0,
                context_tokens=role.context_tokens,
                workspace=child_workspace,
                worktree=worktree,
                worktree_scope=self._worktree_scope or req.worktree,
                config=self._config,
                permissions=p.permissions if p.permissions is not None else self._permissions,
                grants=tuple(p.grants) if p.grants is not None else self._grants,
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
            outcome = await self._run_child(spec, cancel, built)
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
                **_external_partial(spec),
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
                **_external_partial(spec),
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
                error=_safe(f"{type(exc).__name__}: {exc}") if isinstance(exc, NexusError) else type(exc).__name__,
                metrics={"selected_model": _safe(getattr(exc, "selected_model", ""))},
                dropped_tools=dropped,
                clamped=clamped,
                tier=tier,
                requested_tier=resolution.reference,
            )
        finally:
            _LIVE_CHILDREN.pop(agent_id, None)
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
            # A continuable child keeps its live slot while idle; it is freed by
            # ``stop`` (or now, for a run that leaves nothing to continue).
            continuable, blocker = self._continuation_after(p, outcome, built["child"])
            if not continuable:
                self._release_live(agent_id, blocker)
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
        if not outcome.models and relay.models:
            outcome = replace(outcome, models=relay.models)
        outcome = replace(outcome, continuable=continuable)
        await self._emit_completed(sink, meta, outcome, run=run, continuable=continuable)
        if p.detached and self._pool is not None:
            # ``agent.completed`` is durable: the pool delivers the report to the
            # root when this run's task settles.
            self._pool.stash_report(
                agent_id, outcome.as_report(run=run, role=role.name, model=child_model, agent_id=agent_id)
            )
        if task_cancelled:
            raise asyncio.CancelledError
        if cancellation is not None and (
            _is_cancelled(parent_cancel) or not cancel.own_cancelled
        ):
            raise cancellation
        return outcome

    async def _run_child(
        self,
        spec: ChildSpec,
        cancel: object | None,
        built: dict[str, bool] | None = None,
    ) -> SubagentOutcome:
        child = await _maybe_await(self._factory(spec))
        if child is None:
            raise SubagentError("runtime_factory returned no child runtime")
        if built is not None:
            built["child"] = True
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
        *,
        run: int = 1,
        continuable: bool = False,
    ) -> None:
        await _forward(
            sink,
            AGENT_COMPLETED,
            {
                "agent": dict(meta),
                **dict(meta),
                "run": run,
                "continuable": continuable,
                "status": outcome.status,
                "ok": outcome.ok,
                "is_error": outcome.is_error,
                "usage": outcome.usage.to_dict(),
                **({"usage_unknown": True} if outcome.usage_unknown else {}),
                "iterations": outcome.iterations,
                "stop_reason": outcome.stop_reason,
                "dropped_tools": list(outcome.dropped_tools),
                "clamped": outcome.clamped,
                **({"selected_model": outcome.metrics["selected_model"]} if outcome.metrics.get("selected_model") else {}),
                **(
                    {"resolved_model": outcome.actual_model}
                    if outcome.actual_model
                    else {}
                ),
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

    def _continuation_after(
        self, p: PreparedChild, outcome: SubagentOutcome | None, child_built: bool
    ) -> tuple[bool, str]:
        """Can the child be continued after this run, and if not, why."""
        if not p.continuable:
            return False, p.not_continuable
        if not (p.resume or child_built):
            return False, "it never started, so there is no conversation to continue"
        if outcome is not None and (outcome.usage_unknown or "external_kind" in outcome.metrics):
            label = outcome.metrics.get("external_label") or "Claude Code"
            return False, (
                f"{label} workers run one assignment per session; start a new "
                "subagent and put what it needs from the report in the prompt"
            )
        return True, ""

    def _release_live(self, agent_id: str, blocker: str = "") -> None:
        """Give back a child's ``max_live`` slot (the record, when there is one, knows)."""
        pool = self._pool
        if pool is not None and pool.get(agent_id) is not None:
            pool.set_continuable(agent_id, False, blocker)
            pool.release_slot(agent_id)  # a no-op when the slot is already free
            return
        self._budget.release_live()

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
    if budget.max_live is not None:
        parts.append(f"live {budget.live}/{budget.max_live}")
    return ", ".join(parts) or "limit reached"


class _ChildCancel:
    """A child's cancel view: the parent's token or a child-only request."""

    __slots__ = ("_parent", "_event", "_reason")

    def __init__(self, parent: object | None) -> None:
        self._parent = parent
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def own_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or _is_cancelled(self._parent)

    @property
    def reason(self) -> str | None:
        if self._event.is_set():
            return self._reason
        return _cancel_reason(self._parent)

    def cancel(self, reason: str | None = None) -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    async def wait(self) -> None:
        waiters = {asyncio.ensure_future(self._event.wait())}
        parent_wait = getattr(self._parent, "wait", None)
        if callable(parent_wait):
            waiters.add(asyncio.ensure_future(parent_wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled(self.reason or "cancelled")

    def __await__(self):
        return self.wait().__await__()


#: Live children by agent id (the child session id), for host ``AgentCancel``.
_LIVE_CHILDREN: dict[str, _ChildCancel] = {}


def cancel_child(agent_id: str, reason: str | None = None) -> bool:
    """Cancel one running child only; ``False`` when it is not running."""
    token = _LIVE_CHILDREN.get(agent_id)
    if token is None or token.cancelled:
        return False
    token.cancel(reason or "cancelled by user")
    return True


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
    text = redact_secrets(str(value).replace("\x00", "")) if value is not None else ""
    return text if len(text) <= limit else text[:limit] + "…"


def _external_partial(spec: ChildSpec | None) -> dict[str, Any]:
    """Outcome fields an external child left behind when it was cancelled.

    A cancelled external worker may already have changed files, so the
    cancelled outcome keeps its authority, unknown usage and verified changes.
    """
    partial = (spec.metadata or {}).get("external_partial") if spec is not None else None
    if not isinstance(partial, Mapping):
        return {}
    return {"metrics": dict(partial.get("metrics") or {}),
            "files_changed": tuple(partial.get("files_changed") or ())}


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
            clamp_note=value.clamp_note or spec.clamp_note,
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
        clamp_note=spec.clamp_note,
        tier=spec.tier,
        requested_tier=spec.requested_tier,
        error=getattr(value, "error", None),
        metrics=dict(getattr(value, "metrics", {}) or {}),
    )
