"""Versioned, derived snapshots for fast session resume (plan section 5.1).

A snapshot is **derived state**, never history. SQLite session records remain
authoritative; a snapshot is a cache of the current-state projection of a record
prefix stored in the database's ``snapshots`` table:

.. code-block:: text

    { "v": 1, "id": "<id>", "seq": <n>, "messages": [...],
      "summary": {...} | null, "usage": {...} }

The snapshot is only ever used when it validates against the log it claims to
describe. Validation is explicit and total:

* **version** — ``v`` must equal :data:`SNAPSHOT_VERSION`;
* **schema** — the document must decode into :class:`Snapshot` with unknown
  fields rejected;
* **identity** — ``id`` must match the session being read;
* **range** — ``0 <= seq <=`` the session's last record sequence; a ``seq`` past the end is
  a *future* snapshot and is ignored;
* **record prefix** — the messages the records at or before ``seq`` must equal
  ``messages`` exactly (order, payload, bytes, tagged blocks), and their
  aggregated usage must equal ``usage``. Any mismatch means the snapshot is stale
  or corrupt.

On any failure the snapshot is ignored and the caller falls back to full record
replay. Because records are never rewritten, a snapshot can never lose history;
the public ``records``/``events``/``replay``/``fork`` surfaces always read the
full record history regardless of snapshot validity. Snapshot serialization
and transactional persistence are handled by :mod:`nexus.session.db`.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import msgspec

from ..events import Event
from ..model.message import Message
from .ids import validate_session_id
from .records import MessageRecord, ReadResult, SessionRecord, SummaryRecord

#: Snapshot format version. Bumped whenever the derived shape changes so an old
#: runtime ignores (never misreads) a newer database snapshot.
SNAPSHOT_VERSION = 1


class SnapshotSummary(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """Neutral, serializable summary metadata carried by a snapshot.

    A summary is a *cache* of a durable :class:`~nexus.session.records.SummaryRecord`
    whose authority lives in the records. ``through_seq`` mirrors the source
    range's end; the remaining fields let a snapshot be
    validated against the authoritative summary record it was derived from.
    """

    text: str = ""
    through_seq: int = 0
    source: str = "log"
    summary_id: str = ""
    strategy: str = ""
    source_from_seq: int = 0
    source_messages: int = 0
    tokens_before: int | None = None
    tokens_after: int | None = None
    provider: str | None = None
    model: str | None = None
    ts: float = 0.0

    @classmethod
    def from_record(cls, record: SummaryRecord) -> SnapshotSummary:
        return cls(
            text=record.text,
            through_seq=record.source_to_seq,
            source="context.compacted",
            summary_id=record.summary_id,
            strategy=record.strategy,
            source_from_seq=record.source_from_seq,
            source_messages=record.source_messages,
            tokens_before=record.tokens_before,
            tokens_after=record.tokens_after,
            provider=record.provider,
            model=record.model,
            ts=record.ts,
        )


class Snapshot(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A decoded, versioned snapshot body stored in SQLite."""

    v: int = SNAPSHOT_VERSION
    id: str = ""
    seq: int = 0
    messages: list[Message] = msgspec.field(default_factory=list)
    summary: SnapshotSummary | None = None
    usage: dict[str, int] = msgspec.field(default_factory=dict)


@dataclass(frozen=True)
class CurrentState:
    """Snapshot-aware projection of a session's current state.

    ``records`` and ``events`` are always the full, authoritative log. Only
    ``messages`` and ``usage`` are reconstructed from a valid snapshot plus the
    log tail; when no snapshot is valid they equal the full-log projection too.
    ``snapshot_seq`` is the boundary actually used, or ``None`` for full replay.
    """

    messages: tuple[Message, ...]
    events: tuple[Event, ...]
    usage: dict[str, int]
    seq: int
    snapshot_seq: int | None
    records: tuple[SessionRecord, ...]
    summary: SnapshotSummary | None = None


def accumulate_usage(records: Sequence[SessionRecord]) -> dict[str, int]:
    """Sum per-message persisted usage over ``records``.

    Only integer token counters are aggregated; non-integer or non-mapping
    ``meta.usage`` payloads are ignored so a malformed provider blob cannot make
    the projection nondeterministic. Events are deliberately not counted: the
    per-message ``meta.usage`` is the durable per-call accounting and adding the
    aggregate ``turn.completed`` usage would double-count.
    """
    total: dict[str, int] = {}
    for record in records:
        if not isinstance(record, MessageRecord):
            continue
        usage = record.message.meta.usage
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            total[key] = total.get(key, 0) + value
    return total


def _latest_summary(
    records: Sequence[SessionRecord], through_seq: int
) -> SummaryRecord | None:
    """The newest durable summary covering a record at or before ``through_seq``."""
    latest: SummaryRecord | None = None
    for record in records:
        if not isinstance(record, SummaryRecord):
            continue
        if record.seq <= through_seq:
            latest = record
    return latest


def build_from_records(
    session: str,
    records: Sequence[SessionRecord],
    seq: int,
    *,
    summary: SnapshotSummary | None = None,
) -> Snapshot:
    """Derive a snapshot of the record prefix ``seq`` from authoritative records.

    When ``summary`` is not supplied it is derived from the newest durable
    summary record in the prefix, so a snapshot published after a compaction
    carries (and can later validate) the same artifact.
    """
    session = validate_session_id(session)
    if type(seq) is not int or seq < 0:
        raise ValueError("snapshot seq must be a non-negative integer")
    selected = [record for record in records if record.seq <= seq]
    messages = [
        record.message for record in selected if isinstance(record, MessageRecord)
    ]
    if summary is None:
        latest = _latest_summary(selected, seq)
        if latest is not None:
            summary = SnapshotSummary.from_record(latest)
    return Snapshot(
        v=SNAPSHOT_VERSION,
        id=session,
        seq=seq,
        messages=messages,
        summary=summary,
        usage=accumulate_usage(selected),
    )


def validate(
    snapshot: Snapshot, session: str, log: ReadResult
) -> Snapshot | None:
    """Return ``snapshot`` only when it is a consistent prefix of ``records``.

    Any version/identity/range/prefix mismatch yields ``None``; callers then
    replay the full record history. Malformed snapshot data is rejected by the
    database decoder before it reaches this function.
    """
    if snapshot.v != SNAPSHOT_VERSION:
        return None
    if snapshot.id != session:
        return None
    if type(snapshot.seq) is not int or snapshot.seq < 0:
        return None
    if snapshot.seq > log.next_seq:
        return None  # future snapshot: records do not yet contain this prefix
    prefix_records = [record for record in log.records if record.seq <= snapshot.seq]
    prefix = [
        record.message
        for record in prefix_records
        if isinstance(record, MessageRecord)
    ]
    if prefix != list(snapshot.messages):
        return None  # stale or corrupt: the claimed prefix does not match
    if snapshot.usage != accumulate_usage(prefix_records):
        return None  # corrupt accounting: trust the log, not the cache
    latest = _latest_summary(prefix_records, snapshot.seq)
    if latest is not None and snapshot.summary != SnapshotSummary.from_record(latest):
        return None  # summary disagrees with the durable artifact
    return snapshot


def current_state(log: ReadResult, snapshot: Snapshot | None) -> CurrentState:
    """Project current state, using a valid snapshot plus the record tail.

    ``messages``/``usage`` are rebuilt from the snapshot prefix when one is
    supplied, otherwise from the full log; ``events`` and ``records`` are always
    the full authoritative log so historical events are never omitted.
    """
    events = tuple(log.events())
    records = log.records
    latest = _latest_summary(records, log.next_seq)
    summary = (
        SnapshotSummary.from_record(latest)
        if latest is not None
        else (snapshot.summary if snapshot is not None else None)
    )
    if snapshot is None:
        return CurrentState(
            messages=tuple(log.messages()),
            events=events,
            usage=accumulate_usage(records),
            seq=log.next_seq,
            snapshot_seq=None,
            records=records,
            summary=summary,
        )
    tail = [record for record in records if record.seq > snapshot.seq]
    tail_messages = [
        record.message for record in tail if isinstance(record, MessageRecord)
    ]
    usage = dict(snapshot.usage)
    for key, value in accumulate_usage(tail).items():
        usage[key] = usage.get(key, 0) + value
    return CurrentState(
        messages=tuple(snapshot.messages) + tuple(tail_messages),
        events=events,
        usage=usage,
        seq=log.next_seq,
        snapshot_seq=snapshot.seq,
        records=records,
        summary=summary,
    )


__all__ = [
    "SNAPSHOT_VERSION",
    "CurrentState",
    "Snapshot",
    "SnapshotSummary",
    "accumulate_usage",
    "build_from_records",
    "current_state",
    "validate",
]
