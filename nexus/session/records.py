"""Versioned records persisted by the SQLite session store (STATE_PLAN §5.1)."""
from __future__ import annotations

from dataclasses import dataclass

import msgspec

from ..events import Event
from ..model.message import Message

#: Envelope version. Bumping this is how a future runtime refuses old logs
#: explicitly instead of misreading them.
SESSION_LOG_VERSION = 1


class EventRecord(msgspec.Struct, tag="event", frozen=True):
    seq: int
    event: Event
    ts: float = 0.0
    v: int = SESSION_LOG_VERSION


class MessageRecord(msgspec.Struct, tag="message", frozen=True):
    seq: int
    message: Message
    ts: float = 0.0
    v: int = SESSION_LOG_VERSION


class SummaryRecord(msgspec.Struct, tag="summary", frozen=True):
    """An append-only, versioned compaction/summary artifact.

    This is deliberately **not** an assistant/user transcript message: it is a
    separate record kind so a summary can never be mistaken for model output or
    replay as a conversational turn. The full original history stays
    authoritative; this record makes the compaction reproducible.
    """

    seq: int
    text: str = ""
    summary_id: str = ""
    strategy: str = ""
    #: Deterministic digest of the summarizer's semantic inputs; identical
    #: ``(strategy, inputs)`` reuse the existing artifact instead of re-running.
    input_digest: str = ""
    #: Inclusive record-sequence range the summary covers (``0`` = unknown).
    source_from_seq: int = 0
    source_to_seq: int = 0
    source_messages: int = 0
    tokens_before: int | None = None
    tokens_after: int | None = None
    provider: str | None = None
    model: str | None = None
    ts: float = 0.0
    v: int = SESSION_LOG_VERSION


SessionRecord = EventRecord | MessageRecord | SummaryRecord


@dataclass(frozen=True)
class ReadResult:
    """A parsed log plus crash-tail bookkeeping."""

    records: tuple[SessionRecord, ...] = ()
    truncated_tail: bool = False
    #: Byte length of the valid prefix; ``< len(file)`` when a tail was discarded.
    valid_bytes: int = 0
    #: Whether the file's final byte is a newline. ``False`` with records present
    #: means a complete record is missing its terminator and must be fixed before
    #: the next append.
    ends_with_newline: bool = True

    @property
    def next_seq(self) -> int:
        return self.records[-1].seq if self.records else 0

    def messages(self) -> list[Message]:
        return [r.message for r in self.records if isinstance(r, MessageRecord)]

    def events(self) -> list[Event]:
        return [r.event for r in self.records if isinstance(r, EventRecord)]

    def summaries(self) -> list[SummaryRecord]:
        return [r for r in self.records if isinstance(r, SummaryRecord)]


__all__ = [
    "SESSION_LOG_VERSION",
    "EventRecord",
    "MessageRecord",
    "ReadResult",
    "SessionRecord",
    "SummaryRecord",
]
