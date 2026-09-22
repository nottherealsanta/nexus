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
5     skills_index    1         sanitized ``name: description`` lines, whole-line
6     mcp_index       2         no-op placeholder (Phase 5)
7     memory          1         text (``MEMORY.md``)
8     attachments     2         no-op placeholder
9     history         3         message suffix, oldest-droppable
10    user            0         current input + its pinned trailing messages
====  ==============  ========  ============================================

``skills_index`` renders the frozen, sanitized ``name: description`` lines of the
current iteration's skill snapshot; an absent/empty snapshot renders ``None``, so
the part stays a no-op until skills exist. ``mcp_index``/``attachments``
deliberately exist and render ``None``: the fixed assembly order is part of the
contract even before those phases populate them. The skills index carries only
names and descriptions — never a body, resource, path, bundled-tool candidate, or
provenance — and is emitted as whole lines so a budget can never cut one in half.

Everything here is pure: parts receive an immutable :class:`AssemblyContext`
and return a :class:`PartOutput`; they never touch a session, a provider, or the
filesystem (file reads happen when the context is built, not when a part
renders).
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections.abc import Awaitable, Iterable, Mapping, Sequence
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
    "MAX_SKILLS_INDEX_DESCRIPTION_CHARS",
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
    "freeze_skills_index",
    "render_parts",
]

#: Short, stable identity line. Dates are deliberately excluded: they break
#: prompt-prefix stability, so they are not part of the Phase 3 foundation.
IDENTITY_PREAMBLE = (
    "You are Nexus, a provider-agnostic agent harness working inside a local "
    "workspace. Be direct and accurate. Inspect the workspace when it helps, and "
    "verify your work before reporting success."
)

PartKind = Literal["text", "tools", "history", "user", "skills_index", "noop"]

#: Longest sanitized description placed on one skills-index line. It mirrors the
#: skills layer's own description cap so the context layer can sanitize without
#: importing ``nexus.skills`` (which would couple two same-tier managers).
MAX_SKILLS_INDEX_DESCRIPTION_CHARS = 2_048
_MAX_SKILLS_INDEX_NAME_CHARS = 256

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
# Skills-index freezing (progressive disclosure)
# ---------------------------------------------------------------------------


def _sanitize_index_text(
    text: Any, *, max_chars: int = MAX_SKILLS_INDEX_DESCRIPTION_CHARS
) -> str:
    """Collapse ``text`` to one safe line, bounded by ``max_chars``.

    This is the same transformation the skills layer applies to a description:
    control characters become spaces, whitespace runs collapse, and an over-long
    value is truncated with an ellipsis. It is duplicated here (rather than
    imported) so the context layer never depends on ``nexus.skills``.
    """
    raw = str(text)
    cleaned = "".join(
        " " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in raw
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "\u2026"
    return cleaned


def _compose_skill_line(name: Any, description: Any) -> str:
    safe_name = _sanitize_index_text(name, max_chars=_MAX_SKILLS_INDEX_NAME_CHARS)
    if not safe_name:
        return ""
    safe_description = (
        "" if description is None else _sanitize_index_text(description)
    )
    return f"{safe_name}: {safe_description}"


def _skill_entry_line(entry: Any) -> str:
    """One sanitized line from a duck-typed index entry, or ``""`` when unusable.

    Only ``name``/``description`` (or a ``line()``/``index_line()`` projection of
    them) are read. Bodies, resources, paths, tool candidates, and provenance are
    never touched, so no such data can reach the prompt through this path.
    """
    if isinstance(entry, (str, bytes, bytearray)):
        return _sanitize_index_text(entry)
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return _compose_skill_line(entry[0], entry[1])
    for method_name in ("line", "index_line"):
        method = getattr(entry, method_name, None)
        if callable(method):
            try:
                return _sanitize_index_text(method())
            except Exception:  # noqa: BLE001 - one bad row must not fail assembly
                return ""
    name = getattr(entry, "name", None)
    if name is None:
        return ""
    return _compose_skill_line(name, getattr(entry, "description", ""))


def _skill_entries(snapshot: Any) -> tuple[Any, ...]:
    """Normalize any supported snapshot shape into a tuple of entries."""
    if snapshot is None:
        return ()
    if isinstance(snapshot, (str, bytes, bytearray)):
        return (snapshot,)
    # ``SkillIndex`` exposes ``entries``; ``SkillManager`` exposes ``index``.
    for attribute in ("entries", "index"):
        value = getattr(snapshot, attribute, None)
        if value is not None and not callable(value):
            return tuple(value)
    if isinstance(snapshot, Iterable):
        return tuple(snapshot)
    return (snapshot,)


def freeze_skills_index(snapshot: Any) -> tuple[str, ...]:
    """Freeze a skill snapshot/index into deterministic, sanitized lines.

    Accepts a ``SkillIndex``-like object (``.entries``), an iterable of entries
    (a ``name``/``description`` object, a ``(name, description)`` pair, an entry
    exposing ``line()``/``index_line()``, or a pre-rendered string), or a single
    entry. Only the name and description are read. Each line is sanitized to a
    single line and the set is deduplicated and sorted by ``(casefold, line)``, so
    an equal snapshot always freezes to an equal tuple and an equal prompt.
    """
    lines = [
        line
        for line in (
            _skill_entry_line(entry) for entry in _skill_entries(snapshot)
        )
        if line
    ]
    return tuple(sorted(set(lines), key=lambda line: (line.casefold(), line)))


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
    #: Whole, sanitized lines for the ``skills_index`` kind. Emitted as a unit so
    #: a budget always drops whole entries and never cuts one in half.
    lines: tuple[str, ...] = ()


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
    #: The frozen skills snapshot/index for this iteration. Either already-frozen
    #: lines (the manager's normal form) or a raw snapshot the part will freeze.
    #: Empty/``None`` renders nothing, so the part remains a no-op without skills.
    skills_index: Any = None

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
class _SkillsIndexPart:
    """Renders the frozen skills index as sanitized ``name: description`` lines.

    The snapshot is frozen (and sanitized) here, at render time, so a raw
    snapshot handed straight to an :class:`AssemblyContext` is handled the same
    way as the manager's already-frozen lines. Nothing but the name and
    description is ever read.
    """

    name: str = "skills_index"
    priority: int = 1

    def render(self, ctx: AssemblyContext) -> PartOutput | None:
        lines = freeze_skills_index(ctx.skills_index)
        if not lines:
            return None
        return PartOutput(
            name=self.name,
            priority=self.priority,
            kind="skills_index",
            lines=lines,
        )


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
        _SkillsIndexPart(),
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
