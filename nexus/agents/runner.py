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
from ..model.registry import ADAPTER_CLAUDE_CODE
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
    "AGENT_COMPLETED",
    "AGENT_CONFIG",
    "AGENT_RESUMED",
    "AGENT_SPAWNED",
    "RESUME_UNRESOLVED_KEY",
    "CHILD_CONFIG_VERSION",
    "CHILD_SESSION_SEGMENT",
    "ChildLifecycle",
    "DEFAULT_CHILD_TYPE",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_CONTINUATIONS_PER_TURN",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_FANOUT",
    "DEFAULT_MAX_RUNS_PER_CHILD",
    "fold_child_lifecycle",
    "DEFAULT_MAX_TIER",
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
    if provider == "nvidia":
        # Started models are wire IDs, including their vendor namespace.
        return f"{provider}/{model}"
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
#: A follow-up run of a finished child (``subagent_continue``).
AGENT_RESUMED = "agent.resumed"
#: The child-log record of how a child was configured (never relayed upward).
AGENT_CONFIG = "agent.config"
#: Schema version of the ``agent.config`` record.
CHILD_CONFIG_VERSION = 1
#: ``agent.resumed.message`` is clipped here (the view clips at 4000).
RESUME_MESSAGE_CHARS = 3_900

#: Defaults for the four bounding rules (plan section 15.8).
DEFAULT_MAX_TIER = "high"
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_FANOUT = 16
#: Gate key for a resume whose child cannot be resolved (unknown, foreign, legacy).
RESUME_UNRESOLVED_KEY = "resume:unresolved"
#: Follow-up runs allowed per root turn, across all children. They never count
#: as fan-out (a continuation is not a new child).
DEFAULT_MAX_CONTINUATIONS_PER_TURN = 8
#: Total runs one child may ever have (the first run plus its follow-ups).
DEFAULT_MAX_RUNS_PER_CHILD = 4
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
        "question",
        "skill",
        "webfetch",
        "websearch",
    }
)
# Automatically isolated writer children retain the parent's approved code
# execution tools inside their checkout. Explicit legacy worktree requests keep
# the smaller reviewed tool surface above.
AUTO_WORKTREE_CHILD_TOOLS = WORKTREE_CHILD_TOOLS | frozenset(
    {"shell", "code_start", "code_attach", "code_run", "code_reset"})
#: Tools only the root session may hold; a child never gets them.
ROOT_ONLY_TOOLS = frozenset({"subagent", "subagent_continue"})
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
    #: ``None`` means *omitted*. Without ``resume`` it becomes the default role;
    #: with ``resume`` it stays ``None`` so the recorded role, never the default,
    #: decides identity and authorization.
    subagent_type: str | None = DEFAULT_CHILD_TYPE
    tools: tuple[str, ...] | None = None
    model: str | None = None
    description: str | None = None
    #: ``None`` selects the safe default (isolate children whose selected tools
    #: can mutate files); ``False`` shares the parent checkout and ``True``
    #: requests an isolated worktree even for a read-only child.
    worktree: bool | None = None
    context_read: bool = False
    #: The logical id of a finished child to send ``prompt`` to as a follow-up.
    resume: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context_read, bool):
            raise SubagentError("context_read must be boolean")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise SubagentError("prompt must be a non-empty string")
        if self.resume is not None and (
            not isinstance(self.resume, str) or not self.resume.strip()
        ):
            raise SubagentError("resume must be a non-empty agent_id string")
        if isinstance(self.resume, str):
            object.__setattr__(self, "resume", self.resume.strip())
        if self.subagent_type is None and self.resume is None:
            object.__setattr__(self, "subagent_type", DEFAULT_CHILD_TYPE)
        if self.subagent_type is not None and (
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
        if self.worktree is not None and not isinstance(self.worktree, bool):
            raise SubagentError("worktree must be a boolean when given")

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
            resume = value.get("resume")
            return cls(
                prompt=value.get("prompt"),  # type: ignore[arg-type]
                subagent_type=value.get(
                    "subagent_type", None if resume is not None else DEFAULT_CHILD_TYPE
                ),
                tools=tools,
                model=value.get("model"),
                description=value.get("description"),
                worktree=value.get("worktree"),
                resume=resume,
            )
        if value is None:
            raise SubagentError("a Task request is required")
        resume = getattr(value, "resume", None)
        return cls(
            prompt=value.prompt,  # type: ignore[attr-defined]
            subagent_type=getattr(
                value, "subagent_type", None if resume is not None else DEFAULT_CHILD_TYPE
            ),
            tools=_tuple_or_none(getattr(value, "tools", None)),
            model=getattr(value, "model", None),
            description=getattr(value, "description", None),
            worktree=getattr(value, "worktree", None),
            resume=resume,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "subagent_type": self.subagent_type,
            "tools": list(self.tools) if self.tools is not None else None,
            "model": self.model,
            "description": self.description,
            "worktree": self.worktree,
            **({"resume": self.resume} if self.resume is not None else {}),
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
    #: The child's logical id (``<parent>/sub/<n>``), the handle for ``resume``.
    #: Empty for a refusal that never reached a child.
    agent_id: str = ""
    #: Which run of the child this report closes (1 = the spawn).
    run: int = 1
    #: Whether ``subagent_continue(agent_id=...)`` would be accepted now; ``None`` unknown.
    continuable: bool | None = None
    #: Short reason shown when ``continuable`` is false.
    continuable_note: str = ""

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
            **(
                {"agent_id": self.agent_id, "run": self.run, "continuable": self.continuable}
                if self.agent_id
                else {}
            ),
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
            lines.append(
                f"[{self.agent} did not finish (stop reason: {self.stop_reason or 'error'}). "
                f"The digest below is {where}. If the work still matters, start a new "
                "subagent and put what it needs from this report in its prompt.]"
            )
            lines.append(self.handoff)
        if len(lines) == 1:
            lines.append(f"{label}: no report")
        if self.agent_id:
            lines.append(f"agent_id: {self.agent_id}")
            lines.append(f"run: {self.run}")
            if self.continuable is not None:
                lines.append(
                    "continuable: yes (follow up with the subagent_continue tool and this agent_id)"
                    if self.continuable
                    else "continuable: no"
                    + (f" ({self.continuable_note})" if self.continuable_note else "")
                )
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
        max_continuations: int | None = DEFAULT_MAX_CONTINUATIONS_PER_TURN,
        max_runs_per_child: int | None = DEFAULT_MAX_RUNS_PER_CHILD,
    ) -> None:
        for label, bound in (
            ("max_continuations", max_continuations),
            ("max_runs_per_child", max_runs_per_child),
        ):
            if bound is not None and (
                isinstance(bound, bool) or not isinstance(bound, int) or bound < 0
            ):
                raise SubagentError(f"{label} must be a non-negative integer or None")
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
        #: Follow-up runs per root-turn window, and total runs per child. Neither
        #: is fan-out: a follow-up never raises ``children``.
        self.max_continuations = max_continuations
        self.max_runs_per_child = max_runs_per_child

        self._slots = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()
        self._active = 0
        self._children = 0
        self._continuations = 0
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

    def begin_window(self) -> None:
        """Start a new root-turn window: spend and fan-out reset, concurrency stays.

        The pool owns one budget per root session so the concurrency bound also
        covers a cancelled turn's children that are still winding down; the
        token/cost/fan-out counters keep their per-root-turn meaning. A child's
        usage is charged to the window in which it settles, and reservations
        stay held until then.
        """
        self._children = 0
        self._continuations = 0
        self._spent_tokens = 0
        self._spent_cost = 0.0

    @property
    def continuations(self) -> int:
        """Follow-up runs admitted this window (never part of ``children``)."""
        return self._continuations

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
            "continuations": self._continuations,
            "max_continuations": self.max_continuations,
            "max_runs_per_child": self.max_runs_per_child,
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

    async def reserve_continuation(
        self, *, runs: int, tokens: int = 0, cost: float = 0.0
    ) -> tuple[SubagentReservation | None, str]:
        """Admit one follow-up run of an existing child, or say why not.

        Same token/cost admission as :meth:`reserve`, but it never touches the
        fan-out counter (``children``): a follow-up is not a new child. It is
        bounded instead by ``max_continuations`` per window and by
        ``max_runs_per_child`` (``runs`` is the child's durable run count, so
        failed and cancelled runs count and a restart cannot reset it). The
        counter is not given back on release: a failed or cancelled follow-up
        still used its allowance, and nothing retries automatically.
        """
        async with self._lock:
            if self.max_runs_per_child is not None and runs >= self.max_runs_per_child:
                return None, (
                    f"this subagent already ran {runs} times "
                    f"(limit {self.max_runs_per_child}); start a new subagent"
                )
            if (
                self.max_continuations is not None
                and self._continuations >= self.max_continuations
            ):
                return None, (
                    f"this turn already sent {self._continuations} follow-ups "
                    f"(limit {self.max_continuations})"
                )
            if (
                self.token_budget is not None
                and self._spent_tokens + self._reserved_tokens + max(0, tokens)
                > self.token_budget
            ) or (
                self.cost_budget is not None
                and self._spent_cost + self._reserved_cost + max(0.0, cost)
                > self.cost_budget
            ):
                return None, f"the subagent tree budget is exhausted ({_budget_label(self)})"
            self._continuations += 1
            self._reserved_tokens += max(0, tokens)
            self._reserved_cost += max(0.0, cost)
            return SubagentReservation(tokens=max(0, tokens), cost=max(0.0, cost)), ""

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
    #: True when ``prompt`` is a follow-up to an existing child session: the
    #: runtime must open the stored session (never create one) and run another
    #: turn on it. ``run`` is the 1-based run number.
    resume: bool = False
    run: int = 1
    #: The immutable ``agent.config`` record a first run persists in the child's
    #: own log (secret-safe; never relayed). ``None`` for a follow-up.
    config_record: Mapping[str, Any] | None = None
    #: A follow-up of an external Claude Code child: the stored ``agent.config``
    #: record (its ``external`` block names the Claude session to resume and the
    #: worker policy it ran under). ``None`` for a first run and for Nexus children.
    resume_record: Mapping[str, Any] | None = None


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
) -> Any:
    """Forward telemetry best-effort."""
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
        return None


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
    #: The role body the child runs with (spawn) and the immutable record of how
    #: it was configured: written on a first run, loaded for a follow-up.
    system_prompt: str = ""
    config_record: dict[str, Any] | None = None
    reasoning_effort: str | None = None
    #: 1-based run number; ``resume`` marks a follow-up on an existing session.
    run: int = 1
    resume: bool = False


_LIFECYCLE_EVENTS = frozenset({"agent.spawned", "agent.resumed", "agent.completed", "agent.stopped"})


@dataclass(frozen=True)
class ChildLifecycle:
    """What a parent log says about one child (``found`` is false when it names none)."""

    found: bool = False
    #: Runs started so far (1 = the spawn); failed and cancelled runs count.
    runs: int = 0
    #: Status of the last finished run (``None`` while a run is open).
    status: str | None = None
    #: A run was started and never closed.
    open: bool = False
    stopped: bool = False
    background: bool = False
    #: The last finished run was executed by an external worker (usage unknown),
    #: and which one (``external_kind``/``external_label``; empty on records from
    #: before the kind was logged).
    external: bool = False
    external_kind: str = ""
    external_label: str = ""


def fold_child_lifecycle(events: Iterable[Event], agent_id: str) -> ChildLifecycle:
    """Fold the parent's lifecycle events for exactly ``agent_id``.

    Identity is the exact logical id in the event's ``agent`` block, so a
    descendant's relayed events and other children never match.
    """
    found = open_run = stopped = background = False
    runs = 0
    status: str | None = None
    before: str | None = None
    external = False
    kind = label = ""
    for event in events:
        if event.type not in _LIFECYCLE_EVENTS or not isinstance(event.data, Mapping):
            continue
        meta = event.data.get("agent")
        ident = meta.get("id") if isinstance(meta, Mapping) else event.data.get("id")
        if ident != agent_id:
            continue
        run = event.data.get("run")
        run = run if isinstance(run, int) and not isinstance(run, bool) and run > 0 else None
        if event.type == "agent.spawned":
            found, open_run, runs, status = True, True, 1, None
            background = bool(event.data.get("background"))
            external, kind, label = False, "", ""
        elif not found:
            continue
        elif event.type == "agent.resumed":
            before = status
            open_run, status = True, None
            runs = run if run is not None else runs + 1
        elif event.type == "agent.completed":
            open_run = False
            status = str(event.data.get("status") or "completed")
            if event.data.get("unchanged") and before is not None:
                status = before  # the refused follow-up left the child as it was
            else:
                external = bool(event.data.get("usage_unknown"))
                kind = str(event.data.get("external_kind") or "") if external else ""
                label = str(event.data.get("external_label") or "") if external else ""
            if run is not None:
                runs = max(runs, run)
        elif event.type == "agent.stopped":
            stopped = True
    return ChildLifecycle(
        found=found, runs=runs, status=status, open=open_run,
        stopped=stopped, background=background,
        external=external, external_kind=kind, external_label=label,
    )


_UNKNOWN_CHILD = (
    "no finished subagent with that agent_id belongs to this session; "
    "start a new subagent"
)


def _canonical_sha(record: Mapping[str, Any]) -> str:
    """Digest of a JSON record; the parent log carries this, never the body."""
    import json

    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:16]


def _clip_followup(prompt: str) -> str:
    """The redacted follow-up text for ``agent.resumed.message`` (the child gets it whole)."""
    text = redact_secrets(prompt)
    if len(text) <= RESUME_MESSAGE_CHARS:
        return text
    return text[: RESUME_MESSAGE_CHARS - 1] + "…"


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
        managed_worktrees: bool = False,
        runtime_supports_workspace: bool = False,
        worktree_scope: bool = False,
        tier_probe: Callable[[str], bool] | None = None,
        tier_candidates: Callable[[str], Sequence[str]] | None = None,
        tier_efforts: Callable[[], Mapping[str, Mapping[str, str]]] | None = None,
        route_gate: Callable[[str], Any] | None = None,
        stop_child_jobs: Callable[[str], Any] | None = None,
        parent_events: Callable[[], Iterable[Event]] | None = None,
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
        self.managed_worktrees = managed_worktrees is True
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
        #: ``async (child_session_id) -> job ids``: stops the shell jobs a child
        #: left running when its run ends. Children never wake on a job exit, so
        #: a job that outlives the child's run would have no owner.
        self._stop_child_jobs = stop_child_jobs
        #: ``() -> durable events of this runner's parent session``: the lifecycle
        #: authority for ``resume`` (finished, closed, running). Absent, no child
        #: is continuable.
        self._parent_events = parent_events
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
                role = self._role_for(req)
                if role is not None and role.provider:
                    return f"{role.provider}/{req.model}"
            return req.model
        role = self._role_for(req)
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
        role = self._role_for(req)
        if role is None:
            return None
        return role.provider

    def _role_for(self, req: TaskRequest) -> AgentDef | None:
        if req.subagent_type is None:  # a resume request names no role
            return None
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
        if req.resume is not None:
            # A follow-up is authorized as the child it continues: the recorded
            # role and tier, never the default role and never the caller's
            # claim. Anything unresolvable (unknown, foreign, legacy) fails
            # closed to a key no role rule names.
            record = self._child_record(req.resume)
            if record is None:
                return RESUME_UNRESOLVED_KEY
            return f"{record.get('role')}:{record.get('tier')}"
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
        if self._worktree_scope or req.worktree is True:
            worktree_tools = (
                AUTO_WORKTREE_CHILD_TOOLS
                if self.managed_worktrees and req.worktree is True
                else WORKTREE_CHILD_TOOLS
            )
            selected = selection.selected & worktree_tools
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
        This is :meth:`_prepare` followed by an awaited :meth:`_execute`. A
        child always runs inside the call that started it: the parent's
        ``cancel`` token (or host ``AgentCancel`` for this child alone) stops
        it, and its report is the return value.
        """
        prepared = await self._prepare(request, cancel=cancel, emit=emit, call_id=call_id)
        if isinstance(prepared, SubagentOutcome):
            return prepared
        return await self._execute(prepared)

    async def _prepare(
        self,
        request: object,
        *,
        cancel: object | None,
        emit: object | None,
        call_id: str,
    ) -> PreparedChild | SubagentOutcome:
        """Validate, route, reserve budget, create the worktree, emit ``agent.spawned``.

        Returns a refusal :class:`SubagentOutcome` for an expected refusal, else a
        :class:`PreparedChild`. A failure after the reservation is captured in
        ``PreparedChild.error`` and re-raised by :meth:`_execute`, so one set of
        handlers settles budget, worktree and ``agent.completed``.
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
                agent=req.subagent_type or "subagent",
                error="Task: subagents cannot spawn subagents; only the root session may",
                request=req,
            )

        if req.resume is not None:
            return await self._prepare_resume(
                req, cancel=cancel, emit=emit, call_id=call_id
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

        sink = emit if emit is not None else self._event_sink
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

        _LIVE_CHILDREN[agent_id] = cancel
        worktree: Mapping[str, Any] | None = None
        child_workspace = self._workspace
        mark_finished = None
        worktree_root: Path | None = None
        spawn_event: object | None = None
        error: BaseException | None = None
        system_prompt = ""
        reasoning_effort: str | None = None
        config_record: dict[str, Any] | None = None
        config_sha = ""
        try:
            mutating_names = {
                canonical_tool_name(name) for name in self._mutating_tools
            }
            may_mutate = bool(selection.selected & mutating_names)
            isolate_worktree = (
                req.worktree is True
                or (
                    req.worktree is None
                    and may_mutate
                    and self.managed_worktrees
                )
            )
            if isolate_worktree and req.worktree is None:
                selected = selection.selected & AUTO_WORKTREE_CHILD_TOOLS
                excluded = selection.selected - selected
                selection = replace(
                    selection,
                    selected=frozenset(selected),
                    dropped=selection.dropped | excluded,
                    stripped=selection.stripped | excluded,
                )
                dropped = tuple(sorted(selection.dropped))
            if isolate_worktree:
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
                    record, interrupted = await _shielded(_worktree_step(
                        create_worktree, self._workspace, session_id, root=worktree_root))
                    worktree = _worktree_record(record)
                    if interrupted:
                        raise asyncio.CancelledError
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
            system_prompt = self._agents.load_body(role)
            reasoning_effort = self._reasoning_effort(role, tier, child_model)
            config_record = self._config_record(
                role=role, req=req, system_prompt=system_prompt,
                selection=selection, dropped=dropped, child_model=child_model,
                child_provider=child_provider, tier=tier, decision=decision,
                resolution=resolution, clamped=clamped, clamp_note=clamp_note,
                reasoning_effort=reasoning_effort, workspace=child_workspace,
                task_id=task_id, index=index,
                isolated=worktree is not None or self._worktree_scope,
            )
            config_sha = _canonical_sha(config_record)
            spawn_event = await _forward(
                sink,
                AGENT_SPAWNED,
                {
                    "agent": dict(meta),
                    **meta,
                    "config_sha": config_sha,
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
                    sink,
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
            system_prompt=system_prompt, config_record=config_record,
            reasoning_effort=reasoning_effort,
        )

    # -- continuation ------------------------------------------------------

    def _reasoning_effort(self, role: AgentDef, tier: str, child_model: str | None) -> str | None:
        return role.reasoning_effort or getattr(
            getattr(getattr(self._config, "v2", None), "models", None),
            "tier_efforts", {},
        ).get(tier, {}).get(child_model)

    def _config_record(
        self, *, role: AgentDef, req: TaskRequest, system_prompt: str,
        selection: AgentToolSelection, dropped: tuple[str, ...],
        child_model: str | None, child_provider: str | None, tier: str,
        decision: TierDecision, resolution: TierResolution, clamped: bool,
        clamp_note: str, reasoning_effort: str | None, workspace: Path,
        task_id: str, index: int, isolated: bool,
    ) -> dict[str, Any]:
        """The immutable ``agent.config`` record a first run persists in the child log.

        Names, references, the redacted role prompt and limits only: no grants,
        permission rules, environment, endpoints or credentials. Authority is
        always recomputed from the *current* parent on a follow-up.
        """
        safe_prompt = redact_secrets(system_prompt)
        return {
            "version": CHILD_CONFIG_VERSION,
            "role": role.name,
            "role_sha": hashlib.sha256(system_prompt.encode()).hexdigest()[:16],
            "system_prompt": safe_prompt,
            "prompt_redacted": safe_prompt != system_prompt,
            "description": req.description or role.description,
            "tools": sorted(selection.selected),
            "dropped_tools": list(dropped),
            "model": child_model,
            "requested_model": req.model,
            "provider": child_provider,
            "fallback": list(role.fallback),
            "tier": tier,
            "requested_tier": decision.requested or resolution.reference or self._parent_tier,
            "clamped": clamped,
            "clamp_note": clamp_note,
            "reasoning_effort": reasoning_effort,
            "max_iterations": role.max_iterations or 0,
            "context_tokens": role.context_tokens,
            "workspace": str(workspace),
            "context_read": req.context_read,
            "worktree": bool(isolated),
            "task": task_id,
            "index": index,
        }

    def _lifecycle(self, agent_id: str) -> ChildLifecycle | None:
        if self._parent_events is None:
            return None
        try:
            return fold_child_lifecycle(self._parent_events(), agent_id)
        except Exception:  # noqa: BLE001 - an unreadable log means not continuable
            return None

    def _child_record(self, agent_id: str) -> Mapping[str, Any] | None:
        """The stored ``agent.config`` of one of *this* session's children, else ``None``.

        The facade verifies the child's ownership edge, so a foreign or unknown
        id and a legacy child without a record are indistinguishable here.
        """
        reader = getattr(self._sessions, "child_config", None)
        if not callable(reader):
            return None
        try:
            record = reader(self._parent_session, agent_id)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(record, Mapping) or record.get("version") != CHILD_CONFIG_VERSION:
            return None
        if not isinstance(record.get("role"), str) or not isinstance(record.get("tier"), str):
            return None
        return record

    async def _prepare_resume(
        self,
        req: TaskRequest,
        *,
        cancel: object | None,
        emit: object | None,
        call_id: str,
    ) -> PreparedChild | SubagentOutcome:
        """Validate a follow-up, reserve a run, and emit ``agent.resumed``.

        Order: overrides; ownership and lifecycle (unknown and foreign share one
        message); the stored record; role identity; current authority; budget.
        Nothing is emitted or reserved before every check passed.
        """
        agent_id = str(req.resume)
        label = req.subagent_type or "subagent"

        def refuse(reason: str) -> SubagentOutcome:
            return self._refusal(
                agent=label, request=req,
                error=f"Task: cannot continue {agent_id!r}: {reason}",
            )

        if (
            req.tools is not None
            or req.model is not None
            or req.worktree is not None
            or req.context_read
        ):
            return refuse(
                "a continued subagent keeps its recorded tools, model and workspace; "
                "use the subagent_continue tool with only `agent_id` and `prompt`; to start a "
                "new subagent, use the subagent tool"
            )
        lifecycle = self._lifecycle(agent_id)
        if lifecycle is None or not lifecycle.found:
            return refuse(_UNKNOWN_CHILD)
        if lifecycle.stopped:
            return refuse("it was closed; start a new subagent")
        if lifecycle.background:
            return refuse("it was a background subagent from an older version")
        if agent_id in _LIVE_CHILDREN or lifecycle.open:
            return refuse("it is still running")
        if lifecycle.status != "completed":
            return refuse(
                f"its last run ended {lifecycle.status!r}; only a completed subagent "
                "can be continued, so start a new subagent"
            )
        if lifecycle.external and lifecycle.external_kind != ADAPTER_CLAUDE_CODE:
            if lifecycle.external_kind:
                return refuse(
                    f"it ran as an external {lifecycle.external_label or lifecycle.external_kind} "
                    "worker, which can't be continued; start a new subagent"
                )
        record = self._child_record(agent_id)
        if record is None:
            if lifecycle.external:
                return refuse(
                    "it ran as an external worker started before external workers could be "
                    "continued; start a new subagent"
                )
            return refuse(
                "it has no recorded configuration (started by an older version); "
                "start a new subagent"
            )
        if record.get("worktree"):
            return refuse("worktree subagents cannot be continued; start a new subagent")
        external = record.get("external")
        if lifecycle.external or external:
            if (
                not isinstance(external, Mapping)
                or external.get("kind") != ADAPTER_CLAUDE_CODE
                or not isinstance(external.get("claude_session"), str)
                or not external["claude_session"]
            ):
                return refuse(
                    "it ran as an external worker with no recorded session to resume; "
                    "start a new subagent"
                )
        elif record.get("prompt_redacted"):
            # An external worker's role text was sent on its first run and lives in
            # the resumed Claude session; it is not re-sent, so redaction is moot.
            return refuse("its role prompt contained redacted text; start a new subagent")
        role_name = str(record["role"])
        if req.subagent_type is not None and req.subagent_type != role_name:
            try:  # a legacy alias that resolves to the recorded role is equal
                asked = self._agents.resolve(req.subagent_type, context="subagent").name
            except AgentNotFoundError:
                asked = None
            if asked != role_name:
                return refuse(f"a continued subagent keeps its role {role_name!r}")
        try:
            role = self._agents.resolve(role_name, context="subagent")
        except AgentNotFoundError:
            role = None
        if role is None:
            return refuse(f"its role {role_name!r} is no longer available; start a new subagent")
        try:
            workspace = Path(str(record.get("workspace", "")))
            same_workspace = workspace.resolve() == self._workspace.resolve() and workspace.is_dir()
        except (OSError, ValueError):
            same_workspace = False
        if not same_workspace:
            return refuse("its workspace is no longer this session's workspace")
        try:
            resolution = self._tiers.resolve(
                str(record["tier"]), parent=self._parent_tier, max_tier=self._max_tier
            )
        except Exception:  # noqa: BLE001 - an unknown tier is a refusal, not a crash
            return refuse(f"its tier {record['tier']!r} no longer resolves")
        if external and resolution.clamped:
            # The recorded model is an exact Claude Code route; a tier that moved
            # would swap it, and an external worker is never moved to another model.
            return refuse(
                f"its tier {record['tier']!r} was lowered, which would change the Claude Code "
                "model it ran on; start a new subagent"
            )
        tier = resolution.tier
        allowed = self.allowed_tiers(role)
        if allowed and tier not in allowed:
            return refuse(f"its tier {tier!r} is no longer allowed for role {role.name!r}")
        # A tier lowered below the recorded one moves the child down to it; a
        # concrete recorded model is kept only while the tier did not move.
        child_model = tier if resolution.clamped else record.get("model")
        if not self._runnable(tier):
            # A first run whose tier had no runnable model ran on the parent's
            # model (see ``_tiered_child_model``) and recorded that model. It
            # continues only on that same model, still the parent's; the child is
            # never moved to a different one.
            recorded = child_model if isinstance(child_model, str) else None
            pinned_fallback = (
                recorded is not None
                and recorded not in self._tiers.order
                and bool(self._parent_model)
                and recorded == self._parent_model
            )
            if not pinned_fallback:
                return refuse(f"no model is available for its tier {tier!r} right now")
        recorded_tools = tuple(str(name) for name in record.get("tools") or ())
        selection = self.select_tools(
            role, TaskRequest(prompt=req.prompt, subagent_type=role.name, tools=recorded_tools)
        )
        dropped = tuple(sorted(selection.dropped))

        if _is_cancelled(cancel):
            raise OperationCancelled(_cancel_reason(cancel) or "cancelled")
        # Claim the child before the first await: every check above is
        # synchronous, so two follow-ups to one child in a batch cannot both pass.
        parent_cancel = cancel
        child_cancel = _ChildCancel(parent_cancel)
        _LIVE_CHILDREN[agent_id] = child_cancel
        try:
            reservation, why = await self._budget.reserve_continuation(
                runs=lifecycle.runs, tokens=self._reserved_tokens, cost=self._reserved_cost
            )
        except BaseException:
            _LIVE_CHILDREN.pop(agent_id, None)
            raise
        if reservation is None:
            _LIVE_CHILDREN.pop(agent_id, None)
            return refuse(why)

        try:
            sink = emit if emit is not None else self._event_sink
            run = lifecycle.runs + 1
            task_id = str(record.get("task") or new_id())
            meta = {
                "id": agent_id,
                "parent": self._parent_agent_id or self._parent_session,
                "parent_session": self._parent_session,
                "root_id": (
                    self._sessions.root_id(self._parent_session)
                    if callable(getattr(self._sessions, "root_id", None))
                    else self._parent_session
                ),
                "parent_agent_id": self._parent_agent_id,
                "parent_call_id": call_id if isinstance(call_id, str) else "",
                "root_turn_id": self._root_turn_id,
                "depth": self._depth + 1,
                "type": role.name,
                "task": task_id,
                "agent_id": agent_id,
                "tier": tier,
                "index": record.get("index"),
                "session": agent_id,
            }
            relay = _ChildRelay(sink, meta, agent_id)
            resumed_event: object | None = None
            error: BaseException | None = None
            try:
                resumed_event = await _forward(
                    sink,
                    AGENT_RESUMED,
                    {
                        "agent": dict(meta),
                        **meta,
                        "run": run,
                        "from": "root",
                        "message": _clip_followup(req.prompt),
                        "message_chars": len(req.prompt),
                        "background": False,
                        "model": child_model,
                        "tier": tier,
                        "call_id": call_id if isinstance(call_id, str) else "",
                        "worktree": None,
                    },
                    session=self._parent_session,
                )
            except (Exception, asyncio.CancelledError) as exc:
                error = exc
            return PreparedChild(
                req=req, role=role, depth=self._depth + 1, selection=selection,
                decision=TierDecision(
                    resolution=resolution, requested=str(record.get("requested_tier") or "") or None
                ),
                resolution=resolution, tier=tier, child_model=child_model,
                child_provider=record.get("provider"), clamped=bool(resolution.clamped),
                clamp_note="", dropped=dropped, sink=sink, session_id=agent_id,
                task_id=task_id, agent_id=agent_id, parent_cancel=parent_cancel,
                cancel=child_cancel, meta=meta, relay=relay, reservation=reservation,
                call_id=call_id, worktree=None, worktree_root=None, mark_finished=None,
                child_workspace=workspace, spawn_event=resumed_event, error=error,
                system_prompt=str(record.get("system_prompt") or ""),
                config_record=dict(record),
                reasoning_effort=record.get("reasoning_effort"),
                run=run, resume=True,
            )
        except BaseException:
            # Nothing past this point hands the claim to ``_execute``, whose
            # ``finally`` normally clears it: give both back here.
            _LIVE_CHILDREN.pop(agent_id, None)
            with contextlib.suppress(Exception):
                await self._budget.release_reservation(reservation)
            raise

    def _continuability(
        self, outcome: SubagentOutcome, p: PreparedChild, worktree: Mapping[str, Any] | None
    ) -> tuple[bool | None, str]:
        """Whether ``outcome``'s child can take a follow-up: ``(flag, short reason)``.

        ``None`` means the facade cannot say (no stored records), which keeps the
        report silent rather than wrong.
        """
        if p.run > 1 and outcome.metrics.get("unchanged"):
            # A follow-up refused before it touched the stored transcript: the
            # child is exactly as the previous completed run left it.
            return True, ""
        if outcome.status != "completed" or outcome.is_error:
            return False, outcome.status or "failed"
        if worktree is not None or p.req.worktree or self._worktree_scope:
            return False, "worktree subagent"
        external = outcome.usage_unknown or bool(outcome.metrics.get("authority"))
        if external:
            kind = outcome.metrics.get("external_kind")
            if kind != ADAPTER_CLAUDE_CODE:
                label = outcome.metrics.get("external_label")
                return False, f"external {label} worker" if label else "external worker"
            if not outcome.metrics.get("claude_session"):
                return False, "external Claude Code worker without a recorded session"
        elif p.config_record is not None and p.config_record.get("prompt_redacted"):
            return False, "role prompt contained redacted text"
        if not callable(getattr(self._sessions, "child_config", None)):
            return None, ""
        if self._child_record(p.session_id) is None:
            return False, "no recorded configuration"
        return True, ""

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
        cancellation: OperationCancelled | None = None
        task_cancelled = False
        try:
            if p.error is not None:
                raise p.error
            await self._budget.acquire()
            acquired = True
            if _is_cancelled(cancel):
                raise OperationCancelled(_cancel_reason(cancel) or "cancelled")
            # A follow-up runs on the role prompt, limits and fallbacks the child
            # was created with (the stored record), never on the role file as it
            # is now; authority (tools, tier) was re-intersected in ``_prepare``.
            rec: Mapping[str, Any] = p.config_record if p.resume and p.config_record else {}
            system_prompt = (
                p.system_prompt if (p.resume or p.system_prompt)
                else self._agents.load_body(role)
            )
            spec = ChildSpec(
                task_id=task_id,
                agent_id=agent_id,
                agent=role.name,
                description=req.description or str(rec.get("description") or "") or role.description,
                system_prompt=system_prompt,
                prompt=req.prompt,
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
                requested_model=rec.get("requested_model") if p.resume else req.model,
                parent_model=self._parent_model,
                provider=child_provider,
                reasoning_effort=(
                    rec.get("reasoning_effort") if p.resume
                    else self._reasoning_effort(role, tier, child_model)
                ),
                fallback=tuple(rec.get("fallback") or ()) if p.resume else tuple(role.fallback),
                tier=tier,
                requested_tier=(
                    decision.requested or resolution.reference or self._parent_tier
                ),
                clamped=clamped,
                clamp_note=clamp_note,
                max_iterations=int(rec.get("max_iterations") or 0) if p.resume else (role.max_iterations or 0),
                context_tokens=rec.get("context_tokens") if p.resume else role.context_tokens,
                workspace=child_workspace,
                worktree=worktree,
                worktree_scope=self._worktree_scope or worktree is not None,
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
                resume=p.resume,
                run=p.run,
                config_record=None if p.resume else p.config_record,
                resume_record=(
                    p.config_record
                    if p.resume and p.config_record and p.config_record.get("external")
                    else None
                ),
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
                        _worktree_step(mark_finished, session_id, final_outcome, root=worktree_root)
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
                    merge = finalized_record.get("merge")
                    if outcome is not None and isinstance(merge, Mapping):
                        merge_state = str(merge.get("state", "finished"))
                        merge_detail = _safe(merge.get("detail", ""))
                        integration_note = (
                            f"Child worktree integration: {merge_state}"
                            + (f" — {merge_detail}" if merge_detail else "")
                        )
                        outcome = replace(
                            outcome,
                            text=(outcome.text + "\n" + integration_note).strip(),
                        )
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
        if not outcome.models and relay.models:
            outcome = replace(outcome, models=relay.models)
        continuable, why_not = self._continuability(outcome, p, worktree)
        outcome = replace(
            outcome, agent_id=agent_id, run=p.run,
            continuable=continuable, continuable_note=why_not,
        )
        await self._emit_completed(sink, meta, outcome)
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
                return await self._with_stopped_jobs(
                    _coerce_outcome(run_task.result(), spec), spec
                )
            return await self._with_stopped_jobs(
                _coerce_outcome(await run_task, spec), spec
            )
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
            await self._stopped_jobs(spec)  # a failed or cancelled run too
            with contextlib.suppress(Exception):
                await self._sessions.aclose(spec.session_id)

    async def _stopped_jobs(self, spec: ChildSpec) -> list[str]:
        """Stop the shell jobs the child left running; the ids that were stopped."""
        stop = self._stop_child_jobs
        if stop is None:
            return []
        try:
            ids = await _maybe_await(stop(spec.session_id))
        except Exception:  # noqa: BLE001 - cleanup must not mask the child's result
            return []
        return [str(job_id) for job_id in ids or ()]

    async def _with_stopped_jobs(
        self, outcome: SubagentOutcome, spec: ChildSpec
    ) -> SubagentOutcome:
        """List the jobs stopped at child end in the child's report."""
        stopped = await self._stopped_jobs(spec)
        if not stopped:
            return outcome
        note = "[stopped at child end: " + ", ".join(stopped) + "]"
        return replace(outcome, text=(outcome.text + "\n" + note).strip())

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
                **({"usage_unknown": True} if outcome.usage_unknown else {}),
                **({"run": outcome.run} if outcome.agent_id else {}),
                **({"continuable": outcome.continuable} if outcome.continuable is not None else {}),
                **({"unchanged": True} if outcome.metrics.get("unchanged") else {}),
                **(
                    {
                        "external_kind": str(outcome.metrics["external_kind"]),
                        "external_label": str(outcome.metrics.get("external_label") or ""),
                    }
                    if outcome.usage_unknown and outcome.metrics.get("external_kind")
                    else {}
                ),
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
        "owned_session",
        "merge",
        "committed",
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


async def _worktree_step(operation, *args, **kwargs):
    from ..resources import process_slot
    async with process_slot():
        if inspect.iscoroutinefunction(operation):
            return await operation(*args, **kwargs)
        if getattr(getattr(operation, "__self__", None), "blocking", False):
            return await _maybe_await(await asyncio.to_thread(operation, *args, **kwargs))
        return await _maybe_await(operation(*args, **kwargs))


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
