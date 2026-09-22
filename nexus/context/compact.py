"""Deterministic context compaction strategies (plan section 5.2).

Every strategy here is **pure**: it takes messages and returns a result whose
``messages`` is a fresh tuple. It never mutates an input message, a content
block, or the session log. Persisting a summary belongs to the session log, and
calling a model belongs to a summarizer seam the caller injects; neither happens
in this module.

Strategies
----------
``drop_oldest``
    Keep the last ``keep`` messages, drop the rest. Whole messages only.
``evict_tool_results``
    Replace the *content* of old, eligible ``ToolResult`` blocks with a
    non-empty ``context_note``. The block keeps its id and error flag, so
    tool_use/tool_result pairing is preserved.
``summarize``
    Summarize the dropped prefix into one pinned ``Text`` message. Requires an
    injected :class:`Summarizer`; without one it fails actionably.
``hybrid``
    Evict, then summarize, then drop.

``context_note`` availability
------------------------------
The durable note lives on the IR ``ToolResult.context_note`` itself and is read
directly, so eviction works with no external resolver. An external
:class:`NoteResolver` keyed by ``tool_use_id`` (see
:class:`MappingNoteResolver`) is still honoured as a compatibility fallback for
results persisted before the field existed.
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..errors import NexusError
from ..model.message import Message, Text, ToolResult

__all__ = [
    "CompactionAction",
    "CompactionError",
    "CompactionResult",
    "DropOldestAction",
    "EvictAction",
    "EvictedResult",
    "MappingNoteResolver",
    "NoteResolver",
    "SummarizeAction",
    "Summarizer",
    "SummaryArtifact",
    "compact",
    "drop_oldest",
    "evict_tool_results",
    "hybrid",
    "summarize",
]


class CompactionError(NexusError):
    """A compaction strategy could not run as requested."""


@runtime_checkable
class NoteResolver(Protocol):
    """Resolves a non-empty context note for a tool result, keyed by call id."""

    def note_for(self, tool_use_id: str) -> str | None: ...


class MappingNoteResolver:
    """A :class:`NoteResolver` backed by a plain ``{tool_use_id: note}`` map."""

    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._notes = {
            str(key): str(value)
            for key, value in dict(mapping or {}).items()
            if isinstance(value, str) and value.strip()
        }

    def note_for(self, tool_use_id: str) -> str | None:
        note = self._notes.get(tool_use_id)
        return note if note and note.strip() else None


@dataclass(frozen=True)
class SummaryArtifact:
    """The result of one summarizer call. Content-bearing; never persisted here."""

    text: str
    source_count: int
    tokens: int | None = None
    meta: Mapping[str, Any] | None = None


@runtime_checkable
class Summarizer(Protocol):
    """The integration seam that turns a message prefix into a summary.

    A concrete implementation may call a model. It may be synchronous or
    asynchronous; callers use ``inspect.isawaitable``/maybe-await so both work
    without blocking the event loop. This module never calls a model itself.

    Failure policy (decided at the manager layer): an explicit ``summarize``
    strategy propagates a non-cancellation summarizer failure as an actionable
    error; ``hybrid`` degrades to eviction/drop with accurate metadata; and
    ``asyncio.CancelledError`` always propagates.
    """

    def summarize(
        self, messages: Sequence[Message]
    ) -> SummaryArtifact | Awaitable[SummaryArtifact]: ...


# ---------------------------------------------------------------------------
# Typed actions and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DropOldestAction:
    dropped_messages: int
    retained_messages: int


@dataclass(frozen=True)
class EvictedResult:
    tool_use_id: str
    note: str
    original_chars: int
    replacement_chars: int


@dataclass(frozen=True)
class EvictAction:
    evicted: tuple[EvictedResult, ...] = ()


@dataclass(frozen=True)
class SummarizeAction:
    summarized_messages: int
    summary_chars: int
    tokens: int | None = None


CompactionAction = DropOldestAction | EvictAction | SummarizeAction


@dataclass(frozen=True)
class CompactionResult:
    """The compacted message tuple plus typed, content-free accounting."""

    messages: tuple[Message, ...]
    actions: tuple[CompactionAction, ...] = ()
    strategy: str = ""
    dropped: int = 0
    evicted: int = 0
    #: Set when a durable summary artifact backs this result.
    summary_id: str | None = None
    #: ``True`` when an existing artifact was reused (nothing new was persisted).
    summary_reused: bool = False
    tokens_before: int | None = None
    tokens_after: int | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "dropped": self.dropped,
            "evicted": self.evicted,
            "summary_id": self.summary_id,
            "summary_reused": self.summary_reused,
            "actions": [dict(_action_metadata(action)) for action in self.actions],
        }


def _action_metadata(action: CompactionAction) -> dict[str, Any]:
    if isinstance(action, DropOldestAction):
        return {
            "type": "drop_oldest",
            "dropped_messages": action.dropped_messages,
            "retained_messages": action.retained_messages,
        }
    if isinstance(action, EvictAction):
        return {"type": "evict_tool_results", "evicted": len(action.evicted)}
    if isinstance(action, SummarizeAction):
        return {
            "type": "summarize",
            "summarized_messages": action.summarized_messages,
            "summary_chars": action.summary_chars,
            "tokens": action.tokens,
        }
    return {"type": type(action).__name__}  # pragma: no cover - exhaustive


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def drop_oldest(messages: Sequence[Message], *, keep: int) -> CompactionResult:
    """Keep the newest ``keep`` messages; drop the rest."""
    if keep < 0:
        raise ValueError("keep must be non-negative")
    original = tuple(messages)
    if keep >= len(original):
        return CompactionResult(messages=original, strategy="drop_oldest")
    kept = original[len(original) - keep :] if keep else ()
    dropped = len(original) - len(kept)
    action = DropOldestAction(dropped_messages=dropped, retained_messages=len(kept))
    return CompactionResult(
        messages=kept,
        actions=(action,),
        strategy="drop_oldest",
        dropped=dropped,
    )


def _replacement_content(
    block: ToolResult, note: str
) -> list[Text] | None:
    if (
        len(block.content) == 1
        and isinstance(block.content[0], Text)
        and block.content[0].text == note
    ):
        return None
    return [Text(text=note)]


def _note_for(
    block: ToolResult, resolver: NoteResolver | None
) -> str | None:
    """Prefer the durable persisted note; fall back to an external resolver."""
    note = block.context_note
    if isinstance(note, str) and note.strip():
        return note
    if resolver is not None:
        resolved = resolver.note_for(block.tool_use_id)
        if isinstance(resolved, str) and resolved.strip():
            return resolved
    return None


def evict_tool_results(
    messages: Sequence[Message],
    *,
    note_resolver: NoteResolver | Mapping[str, str] | None,
    keep_recent: int = 0,
    min_content_chars: int = 0,
) -> CompactionResult:
    """Replace old eligible tool-result content with its context note.

    ``keep_recent`` messages at the end are never touched. Only successful
    results (``is_error`` false) at or above ``min_content_chars`` are eligible.
    A missing or empty note means the result is left as-is. Inputs are never
    mutated: changed messages and blocks are freshly constructed.
    """
    if keep_recent < 0:
        raise ValueError("keep_recent must be non-negative")
    resolver = _coerce_resolver(note_resolver)
    original = tuple(messages)
    boundary = max(0, len(original) - keep_recent)
    evicted: list[EvictedResult] = []
    out: list[Message] = []
    changed = False
    for index, message in enumerate(original):
        if index >= boundary or not message.content:
            out.append(message)
            continue
        new_blocks = None
        for position, block in enumerate(message.content):
            if not isinstance(block, ToolResult) or block.is_error:
                continue
            original_chars = sum(
                len(part.text) for part in block.content if isinstance(part, Text)
            )
            if original_chars < min_content_chars:
                continue
            note = _note_for(block, resolver)
            if not note:
                continue
            replacement = _replacement_content(block, note)
            if replacement is None:
                continue
            if new_blocks is None:
                new_blocks = list(message.content)
            new_blocks[position] = ToolResult(
                tool_use_id=block.tool_use_id,
                content=replacement,
                is_error=block.is_error,
                context_note=note,
            )
            evicted.append(
                EvictedResult(
                    tool_use_id=block.tool_use_id,
                    note=note,
                    original_chars=original_chars,
                    replacement_chars=len(note),
                )
            )
        if new_blocks is not None:
            changed = True
            out.append(Message(role=message.role, content=new_blocks, meta=message.meta))
        else:
            out.append(message)
    result_messages = tuple(out) if changed else original
    actions = (EvictAction(evicted=tuple(evicted)),) if evicted else ()
    return CompactionResult(
        messages=result_messages,
        actions=actions,
        strategy="evict_tool_results",
        evicted=len(evicted),
    )


def summarize(
    messages: Sequence[Message],
    *,
    summarizer: Summarizer | None,
    keep: int,
    on_artifact: Any | None = None,
) -> CompactionResult | Awaitable[CompactionResult]:
    """Summarize the dropped prefix into one pinned ``Text`` message.

    ``on_artifact`` is invoked with ``(artifact, prefix)`` **before** the summary
    message is built, so a caller can durably persist the artifact before it is
    ever placed in an assembled copy. An asynchronous ``summarizer`` returns an
    awaitable; a synchronous one returns the result directly.
    """
    if summarizer is None:
        raise CompactionError(
            "compaction strategy 'summarize' requires a summarizer callback; "
            "none was provided. Inject a Summarizer seam or choose "
            "'drop_oldest'/'evict_tool_results'."
        )
    if keep < 0:
        raise ValueError("keep must be non-negative")
    original = tuple(messages)
    if keep >= len(original):
        return CompactionResult(messages=original, strategy="summarize")
    prefix = original[: len(original) - keep]
    suffix = original[len(original) - keep :]
    if not prefix:
        return CompactionResult(messages=original, strategy="summarize")
    produced = summarizer.summarize(list(prefix))
    if inspect.isawaitable(produced):
        return _summarize_awaitable(produced, prefix, suffix, on_artifact)
    return _build_summary_result(produced, prefix, suffix, on_artifact)


async def _summarize_awaitable(
    produced: Any,
    prefix: tuple[Message, ...],
    suffix: tuple[Message, ...],
    on_artifact: Any | None,
) -> CompactionResult:
    artifact = await produced
    return _build_summary_result(artifact, prefix, suffix, on_artifact)


def _build_summary_result(
    artifact: Any,
    prefix: tuple[Message, ...],
    suffix: tuple[Message, ...],
    on_artifact: Any | None,
) -> CompactionResult:
    text = artifact.text if isinstance(artifact, SummaryArtifact) else str(artifact)
    if not text or not text.strip():
        # Fail before persisting anything, so no partial artifact is created.
        raise CompactionError("summarizer returned an empty summary")
    if on_artifact is not None:
        on_artifact(artifact, prefix)
    summary_message = Message(
        role="user",
        content=[Text(text=f"[summary of {len(prefix)} earlier messages]\n{text}")],
    )
    action = SummarizeAction(
        summarized_messages=len(prefix),
        summary_chars=len(text),
        tokens=artifact.tokens if isinstance(artifact, SummaryArtifact) else None,
    )
    return CompactionResult(
        messages=(summary_message, *suffix),
        actions=(action,),
        strategy="summarize",
        dropped=len(prefix),
        tokens_after=artifact.tokens
        if isinstance(artifact, SummaryArtifact)
        else None,
    )


def hybrid(
    messages: Sequence[Message],
    *,
    note_resolver: NoteResolver | Mapping[str, str] | None = None,
    summarizer: Summarizer | None = None,
    keep: int = 0,
    keep_recent: int = 0,
    min_content_chars: int = 0,
    on_artifact: Any | None = None,
) -> CompactionResult | Awaitable[CompactionResult]:
    """Evict, then summarize, then drop — falling back gracefully."""
    evicted = evict_tool_results(
        messages,
        note_resolver=note_resolver,
        keep_recent=keep_recent,
        min_content_chars=min_content_chars,
    )
    if summarizer is not None:
        produced = summarize(
            evicted.messages,
            summarizer=summarizer,
            keep=keep,
            on_artifact=on_artifact,
        )
        if inspect.isawaitable(produced):
            return _hybrid_awaitable(produced, evicted)
        return _hybrid_combine(produced, evicted)
    dropped = drop_oldest(evicted.messages, keep=keep)
    return _hybrid_combine(dropped, evicted)


async def _hybrid_awaitable(
    produced: Any, evicted: CompactionResult
) -> CompactionResult:
    summarized = await produced
    return _hybrid_combine(summarized, evicted)


def _hybrid_combine(
    summarized: CompactionResult, evicted: CompactionResult
) -> CompactionResult:
    return CompactionResult(
        messages=summarized.messages,
        actions=(*evicted.actions, *summarized.actions),
        strategy="hybrid",
        dropped=summarized.dropped,
        evicted=evicted.evicted,
        summary_id=summarized.summary_id,
        summary_reused=summarized.summary_reused,
        tokens_before=summarized.tokens_before,
        tokens_after=summarized.tokens_after,
    )


def compact(
    messages: Sequence[Message],
    strategy: str,
    *,
    note_resolver: NoteResolver | Mapping[str, str] | None = None,
    summarizer: Summarizer | None = None,
    keep: int = 0,
    keep_recent: int = 0,
    min_content_chars: int = 0,
    on_artifact: Any | None = None,
) -> CompactionResult:
    """Dispatch to one strategy by name."""
    if strategy == "drop_oldest":
        return drop_oldest(messages, keep=keep)
    if strategy == "evict_tool_results":
        return evict_tool_results(
            messages,
            note_resolver=note_resolver,
            keep_recent=keep_recent,
            min_content_chars=min_content_chars,
        )
    if strategy == "summarize":
        return summarize(
            messages, summarizer=summarizer, keep=keep, on_artifact=on_artifact
        )
    if strategy == "hybrid":
        return hybrid(
            messages,
            note_resolver=note_resolver,
            summarizer=summarizer,
            keep=keep,
            keep_recent=keep_recent,
            min_content_chars=min_content_chars,
            on_artifact=on_artifact,
        )
    raise ValueError(f"unknown compaction strategy: {strategy!r}")


def _coerce_resolver(
    resolver: NoteResolver | Mapping[str, str] | None,
) -> NoteResolver | None:
    if resolver is None or isinstance(resolver, Mapping):
        return MappingNoteResolver(resolver) if resolver is not None else None
    note_for = getattr(resolver, "note_for", None)
    if not callable(note_for):
        raise TypeError("note_resolver must expose note_for(tool_use_id)")
    return resolver
