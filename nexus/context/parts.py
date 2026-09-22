"""Composable context parts (plan section 5.2).

A :class:`ContextPart` renders one slice of the prompt. Parts run in a **fixed
order** because prompt caching depends on a stable prefix; that order, and each
part's drop priority, is declared once in :data:`PART_ORDER` and
:data:`PART_PRIORITY` and never inferred from a dict's iteration order.

The ten builtin parts are:

====  ==============  ========  ============================================
#     name            priority  kind
====  ==============  ========  ============================================
1     identity        0         text
2     soul            0         text (``SOUL.md``)
3     environment     1         text (frozen, deterministic field order)
4     tools           0         structured :class:`ToolSchema` list, never text
5     skills_index    1         no-op placeholder (Phase 4)
6     mcp_index       2         no-op placeholder (Phase 5)
7     memory          1         text (``MEMORY.md``)
8     attachments     2         no-op placeholder
9     history         3         message suffix, oldest-droppable
10    user            0         current input + its pinned trailing messages
====  ==============  ========  ============================================

``skills_index``/``mcp_index``/``attachments`` deliberately exist and render
``None``: the fixed assembly order is part of the contract even before those
phases populate them.

Everything here is pure: parts receive an immutable :class:`AssemblyContext`
and return a :class:`PartOutput`; they never touch a session, a provider, or the
filesystem (file reads happen when the context is built, not when a part
renders).
"""
from __future__ import annotations

import hashlib
import sys
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from ..model.message import (
    Document,
    Image,
    Message,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from ..model.request import ToolSchema
from .cache import canonical_json

__all__ = [
    "IDENTITY_PREAMBLE",
    "PART_ORDER",
    "PART_PRIORITY",
    "AssemblyContext",
    "ContextPart",
    "EnvironmentInfo",
    "PartKind",
    "PartOutput",
    "builtin_parts",
    "canonical_block_text",
    "canonical_message_text",
    "canonical_tool_text",
    "capture_environment",
    "current_user_index",
    "render_parts",
]

#: Short, stable identity line. Dates are deliberately excluded: they break
#: prompt-prefix stability, so they are not part of the Phase 3 foundation.
IDENTITY_PREAMBLE = (
    "You are Nexus, a provider-agnostic agent harness working inside a local "
    "workspace. Be direct and accurate. Inspect the workspace when it helps, and "
    "verify your work before reporting success."
)

PartKind = Literal["text", "tools", "history", "user", "noop"]

#: Fixed assembly order. The order is the contract; do not reorder.
PART_ORDER: tuple[str, ...] = (
    "identity",
    "soul",
    "environment",
    "tools",
    "skills_index",
    "mcp_index",
    "memory",
    "attachments",
    "history",
    "user",
)

#: Drop priority per part, 0 = never drop. Matches :data:`PART_ORDER`.
PART_PRIORITY: Mapping[str, int] = {
    "identity": 0,
    "soul": 0,
    "environment": 1,
    "tools": 0,
    "skills_index": 1,
    "mcp_index": 2,
    "memory": 1,
    "attachments": 2,
    "history": 3,
    "user": 0,
}


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentInfo:
    """Frozen, deterministically ordered description of the workspace.

    Fields render in a fixed order and any caller-supplied ``extra`` entries are
    rendered after them in sorted key order, so two equal environments always
    render byte-for-byte identically.
    """

    workspace: str
    platform: str
    profile: str | None = None
    git_branch: str | None = None
    git_status: str | None = None
    extra: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.extra:
            normalised = tuple(
                sorted((str(key), str(value)) for key, value in self.extra)
            )
            object.__setattr__(self, "extra", normalised)

    def fields(self) -> tuple[tuple[str, str], ...]:
        pairs: list[tuple[str, str]] = [
            ("workspace", self.workspace),
            ("platform", self.platform),
        ]
        if self.profile:
            pairs.append(("profile", self.profile))
        if self.git_branch:
            pairs.append(("git_branch", self.git_branch))
        if self.git_status:
            pairs.append(("git_status", self.git_status))
        pairs.extend(sorted(self.extra))
        return tuple(pairs)
    def render(self, header: str = "environment") -> str:
        lines = [f"<{header}>"]
        lines.extend(f"{key}: {value}" for key, value in self.fields())
        lines.append(f"</{header}>")
        return "\n".join(lines)


def capture_environment(
    workspace: str | Path,
    *,
    profile: str | None = None,
    git_branch: str | None = None,
    git_status: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> EnvironmentInfo:
    """Capture a deterministic environment snapshot.

    Git fields are only populated when explicitly supplied; detection is left to
    the caller so this foundation stays free of subprocesses and non-determinism.
    """
    return EnvironmentInfo(
        workspace=str(Path(workspace)),
        platform=sys.platform,
        profile=profile,
        git_branch=git_branch,
        git_status=git_status,
        extra=tuple(sorted((extra or {}).items())),
    )


# ---------------------------------------------------------------------------
# Canonical serialization (used for token accounting, never sent to a model)
# ---------------------------------------------------------------------------


def canonical_block_text(block: Any) -> str:
    """A stable textual projection of one content block for counting."""
    if isinstance(block, Text):
        return f"text:{block.text}"
    if isinstance(block, Thinking):
        return f"thinking:{block.text}"
    if isinstance(block, ToolUse):
        return f"tool_use:{block.name}:{canonical_json(block.input)}"
    if isinstance(block, ToolResult):
        body = "|".join(canonical_block_text(part) for part in block.content)
        return f"tool_result:{block.tool_use_id}:error={block.is_error}:{body}"
    if isinstance(block, Image):
        digest = (
            hashlib.sha256(block.data).hexdigest()
            if block.data is not None
            else ""
        )
        return f"image:{block.media_type}:{digest}:{block.url or ''}"
    if isinstance(block, Document):
        digest = hashlib.sha256(block.data).hexdigest()
        return f"document:{block.media_type}:{digest}:{block.title or ''}"
    return f"{type(block).__name__}"


def canonical_message_text(message: Message) -> str:
    blocks = "\n".join(canonical_block_text(block) for block in message.content)
    return f"role={message.role}\n{blocks}"


def canonical_tool_text(schema: ToolSchema) -> str:
    return (
        f"tool:{schema.name}:{schema.description}:"
        f"{canonical_json(dict(schema.input_schema))}"
    )


# ---------------------------------------------------------------------------
# Outputs and protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartOutput:
    """One part's contribution to the assembled request.

    Only the field matching ``kind`` is meaningful; the rest stay at their empty
    defaults. ``None`` is not used here — parts that contribute nothing return
    ``None`` from :meth:`ContextPart.render` instead.
    """

    name: str
    priority: int
    kind: PartKind
    text: str = ""
    tools: tuple[ToolSchema, ...] = ()
    messages: tuple[Message, ...] = ()


@dataclass(frozen=True)
class AssemblyContext:
    """The frozen per-turn environment a part renders against."""

    workspace: Path
    config: Any
    identity: str
    soul_text: str
    memory_text: str
    environment: EnvironmentInfo
    tool_schemas: tuple[ToolSchema, ...]
    messages: tuple[Message, ...]
    current_user_index: int | None
    capabilities: Any
    model: str | None
    provider: str | None
    max_file_bytes: int
    note_resolver: Any = None
    summarizer: Any = None
    #: The session handle for this assembly (duck-typed). Used only to append a
    #: durable summary artifact before a summary is placed in an assembled copy;
    #: it is never imported as a concrete type here.
    session: Any = None

    def history(self) -> tuple[Message, ...]:
        """Droppable prefix: everything before the current user turn."""
        cut = len(self.messages) if self.current_user_index is None else self.current_user_index
        return self.messages[:cut]

    def user_tail(self) -> tuple[Message, ...]:
        """Pinned tail: the current user turn and anything after it."""
        if self.current_user_index is None:
            return ()
        return self.messages[self.current_user_index:]


@runtime_checkable
class ContextPart(Protocol):
    """One composable slice of the prompt."""

    name: str
    priority: int

    def render(
        self, ctx: AssemblyContext
    ) -> PartOutput | None | Awaitable[PartOutput | None]:
        """Render against ``ctx``; ``None`` means "contribute nothing"."""
        ...


# ---------------------------------------------------------------------------
# Builtin parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TextPart:
    name: str
    priority: int
    source: str  # attribute on AssemblyContext

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        text = getattr(ctx, self.source)
        if not text or not text.strip():
            return None
        return PartOutput(name=self.name, priority=self.priority, kind="text", text=text)


@dataclass(frozen=True)
class _EnvironmentPart:
    name: str = "environment"
    priority: int = 1

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        text = ctx.environment.render()
        if not text.strip():
            return None
        return PartOutput(name=self.name, priority=self.priority, kind="text", text=text)


@dataclass(frozen=True)
class _ToolsPart:
    name: str = "tools"
    priority: int = 0

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        if not ctx.tool_schemas:
            return None
        return PartOutput(
            name=self.name,
            priority=self.priority,
            kind="tools",
            tools=tuple(ctx.tool_schemas),
        )


@dataclass(frozen=True)
class _NoOpPart:
    name: str
    priority: int

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        return None


@dataclass(frozen=True)
class _HistoryPart:
    name: str = "history"
    priority: int = 3

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        history = ctx.history()
        if not history:
            return None
        return PartOutput(
            name=self.name, priority=self.priority, kind="history", messages=history
        )


@dataclass(frozen=True)
class _UserPart:
    name: str = "user"
    priority: int = 0

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        tail = ctx.user_tail()
        if not tail:
            return None
        return PartOutput(
            name=self.name, priority=self.priority, kind="user", messages=tail
        )


def builtin_parts() -> tuple[ContextPart, ...]:
    """The ten builtin parts in fixed assembly order."""
    return (
        _TextPart("identity", 0, "identity"),
        _TextPart("soul", 0, "soul_text"),
        _EnvironmentPart(),
        _ToolsPart(),
        _NoOpPart("skills_index", 1),
        _NoOpPart("mcp_index", 2),
        _TextPart("memory", 1, "memory_text"),
        _NoOpPart("attachments", 2),
        _HistoryPart(),
        _UserPart(),
    )


def current_user_index(messages: Sequence[Message]) -> int | None:
    """Index of the last user message, or ``None`` when there is no user turn."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return None


def render_parts(
    parts: Sequence[ContextPart], ctx: AssemblyContext
) -> tuple[PartOutput | None, ...]:
    """Render every part in order. Awaitable outputs are **not** awaited here.

    Parts shipped in Phase 3 are synchronous; a future async part is rejected by
    the manager rather than silently dropped. This function is pure.
    """
    outputs: list[PartOutput | None] = []
    for part in parts:
        result = part.render(ctx)
        if isinstance(result, Awaitable):  # pragma: no cover - defensive
            raise TypeError(
                f"part {part.name!r} returned an awaitable; async parts are not "
                "supported by the synchronous renderer"
            )
        if result is None:
            outputs.append(None)
            continue
        if not isinstance(result, PartOutput):  # pragma: no cover - defensive
            raise TypeError(f"part {part.name!r} returned {type(result).__name__}")
        outputs.append(result)
    return tuple(outputs)
