"""Allowlisted, bounded projection of session lifecycle events for host logs."""
from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import msgspec

from ..session.store import (
    SESSION_LOG_VERSION,
    EventRecord,
    SessionRecord,
)

MAX_SESSION_LOG_PAGE = 100
# A poll never inspects more than this many persisted records, even when a
# session has a very long history or the supplied cursor is far behind.
SESSION_LOG_SCAN_WINDOW = 4096
SESSION_LOG_READ_BYTES = 1024 * 1024
SESSION_LOG_MAX_RECORD_BYTES = 64 * 1024

# Event fields are deliberately not interpolated. In particular, tool names,
# error text, permission previews, and all message/thinking payloads stay private.
_PROJECTIONS: dict[str, tuple[str, str, str]] = {
    "turn.started": ("info", "turn.started", "A turn started."),
    "turn.completed": ("info", "turn.completed", "A turn completed."),
    "turn.failed": ("error", "turn.failed", "A turn failed."),
    "turn.cancelled": ("warning", "turn.cancelled", "A turn was cancelled."),
    "tool.requested": ("info", "tool.requested", "A tool was requested."),
    "tool.started": ("info", "tool.started", "A tool started."),
    "tool.completed": ("info", "tool.completed", "A tool completed."),
    "tool.failed": ("error", "tool.failed", "A tool failed."),
    "permission.requested": (
        "warning", "permission.requested", "A permission request is awaiting a decision."
    ),
    "permission.resolved": (
        "info", "permission.resolved", "A permission request was resolved."
    ),
    "model.retrying": ("warning", "model.retrying", "A model request is being retried."),
    "error": ("error", "error", "An operation failed."),
}


def validate_logs_read(limit: object, cursor: object) -> tuple[int, int | None]:
    """Validate the shared page/cursor bounds before touching session state."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_SESSION_LOG_PAGE
    ):
        raise ValueError(f"limit must be between 1 and {MAX_SESSION_LOG_PAGE}")
    if cursor is not None and (
        isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
    ):
        raise ValueError("session cursor must be a non-negative integer")
    return limit, cursor if cursor is None else int(cursor)


def validate_daemon_cursor(cursor: object) -> str | None:
    """Validate the generation-qualified daemon cursor envelope."""
    if cursor is None:
        return None
    if not isinstance(cursor, str) or len(cursor) > 128:
        raise ValueError("invalid daemon cursor")
    generation, separator, position = cursor.partition(":")
    if (
        not separator
        or len(generation) != 32
        or any(char not in "0123456789abcdef" for char in generation)
        or not position
        or not position.isascii()
        or not position.isdecimal()
    ):
        raise ValueError("invalid daemon cursor")
    return cursor


def read_session_page(
    records: Sequence[Any],
    *,
    cursor: int | None,
    limit: int,
    latest_seq: int | None = None,
) -> dict[str, Any]:
    """Project a tail or bounded forward page from already-loaded session records."""
    count = len(records)
    if cursor is None:
        start = max(0, count - SESSION_LOG_SCAN_WINDOW)
        stop = count
        truncated = count > SESSION_LOG_SCAN_WINDOW
        tail = True
        if not count and latest_seq:
            return {
                "entries": [],
                "next_cursor": latest_seq,
                "truncated": True,
                "has_more": False,
            }
    else:
        end_seq = (
            latest_seq
            if latest_seq is not None
            else (_record_seq(records[-1]) if count else 0)
        )
        if cursor > end_seq:
            raise ValueError("session cursor is ahead of the session log")
        window_start = max(0, count - SESSION_LOG_SCAN_WINDOW)
        oldest = _record_seq(records[window_start]) if count else 0
        truncated = bool(count and cursor < oldest - 1)
        if not count and latest_seq is not None and cursor < latest_seq:
            # Every record in the scanned window may have been filtered as
            # invalid/oversized. Advance over the known range rather than
            # asking the poller to scan that same empty range forever.
            return {
                "entries": [],
                "next_cursor": latest_seq,
                "truncated": True,
                "has_more": False,
            }
        if latest_seq is not None and count and cursor >= _record_seq(records[-1]):
            return {
                "entries": [],
                "next_cursor": latest_seq,
                "truncated": False,
                "has_more": False,
            }
        if truncated:
            start = window_start
        else:
            start = _first_after(records, cursor)
        stop = min(count, start + SESSION_LOG_SCAN_WINDOW)
        tail = False

    entries: list[dict[str, Any]] = []
    scanned_to = cursor or 0
    has_more = False
    for index in range(start, stop):
        record = records[index]
        scanned_to = _record_seq(record)
        if not isinstance(record, EventRecord):
            continue
        event = record.event
        projection = _PROJECTIONS.get(event.type)
        if projection is None:
            continue
        if len(entries) >= limit:
            has_more = True
            # Do not advance beyond the last returned diagnostic. The next page
            # can safely rescan intervening non-diagnostic records.
            scanned_to = entries[-1]["seq"]
            break
        level, kind, summary = projection
        timestamp = event.ts
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            timestamp = record.ts if math.isfinite(record.ts) else 0.0
        entries.append(
            {
                "source": "session",
                "seq": _record_seq(record),
                "ts": float(timestamp),
                "level": level,
                "kind": kind,
                "summary": summary,
            }
        )

    if not tail and stop < count and not has_more:
        has_more = True
    if tail:
        # Tail reads return the newest diagnostics in the bounded window, in
        # sequence order. Callers begin following at the current record sequence.
        tail_entries: list[dict[str, Any]] = []
        for index in range(stop - 1, start - 1, -1):
            record = records[index]
            if not isinstance(record, EventRecord):
                continue
            projection = _PROJECTIONS.get(record.event.type)
            if projection is None:
                continue
            level, kind, summary = projection
            timestamp = record.event.ts
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(timestamp)
            ):
                timestamp = record.ts if math.isfinite(record.ts) else 0.0
            tail_entries.append(
                {
                    "source": "session",
                    "seq": _record_seq(record),
                    "ts": float(timestamp),
                    "level": level,
                    "kind": kind,
                    "summary": summary,
                }
            )
            if len(tail_entries) > limit:
                break
        entries = list(reversed(tail_entries[:limit]))
        scanned_to = (
            latest_seq
            if latest_seq is not None
            else (_record_seq(records[-1]) if count else 0)
        )
        has_more = False
    return {
        "entries": entries,
        "next_cursor": scanned_to,
        "truncated": truncated,
        "has_more": has_more,
    }


def session_records(
    handle: Any | None, *, store: Any | None = None, session_id: str | None = None
) -> tuple[Sequence[Any], bool, int | None]:
    """Return an already-loaded log or a bounded tail from a validated handle.

    ``Session.read()`` may reload and parse an arbitrarily large JSONL file after
    each append invalidates its cache. Prefer its current cache; otherwise read a
    bounded byte tail through the session manager's store path. The facade
    validates existence and obtains any live handle from that manager without
    opening, creating, migrating, or recovering the session.
    """
    cached = getattr(handle, "_read", None)
    if cached is not None and isinstance(getattr(cached, "records", None), tuple):
        return (
            cached.records,
            bool(getattr(cached, "truncated_tail", False)),
            _record_seq(cached.records[-1]) if cached.records else 0,
        )
    store = store if store is not None else getattr(handle, "_store", None)
    session_id = session_id if session_id is not None else getattr(handle, "id", None)
    log_path = getattr(store, "log_path", None)
    if not isinstance(session_id, str) or not callable(log_path):
        # A deliberately injected fake/session adapter may expose only records.
        records = getattr(handle, "records", ()) if handle is not None else ()
        records = records if isinstance(records, (tuple, list)) else ()
        return records, False, _record_seq(records[-1]) if records else 0
    path = Path(log_path(session_id))
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - SESSION_LOG_READ_BYTES)
            stream.seek(start)
            payload = stream.read(SESSION_LOG_READ_BYTES)
    except OSError:
        return (), False, 0
    clipped = start > 0
    lines = payload.splitlines()
    if clipped and lines:
        lines = lines[1:]  # discard a possible partial first JSONL record
    unterminated_tail = bool(payload and not payload.endswith(b"\n"))
    if unterminated_tail and lines:
        # SessionStore treats an invalid final fragment as a crash tail. A
        # complete record without its newline remains a valid record.
        try:
            _decode_record(lines[-1])
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
            lines.pop()
            clipped = True
    records: list[Any] = []
    skipped_window = len(lines) > SESSION_LOG_SCAN_WINDOW
    for line in lines[-SESSION_LOG_SCAN_WINDOW:]:
        if not line or len(line) > SESSION_LOG_MAX_RECORD_BYTES:
            if line and len(line) > SESSION_LOG_MAX_RECORD_BYTES:
                clipped = True
            continue
        try:
            record = _decode_record(line)
            records.append(record)
        except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
            clipped = True
            continue
    latest_seq: int | None = None
    if size == 0:
        latest_seq = 0
    elif not payload or unterminated_tail and not lines:
        # No valid boundary is available after a discarded crash tail.
        latest_seq = _record_seq(records[-1]) if records else None
    else:
        final_line = payload.rstrip(b"\n").rsplit(b"\n", 1)[-1]
        if len(final_line) <= SESSION_LOG_MAX_RECORD_BYTES:
            try:
                # Cursor metadata may still be trustworthy when a supported
                # typed decode rejects only the envelope version.
                latest_seq = _record_seq(
                    msgspec.json.decode(final_line, type=SessionRecord)
                )
            except (msgspec.DecodeError, msgspec.ValidationError, ValueError):
                latest_seq = None
    if unterminated_tail and latest_seq is None and records:
        # The unterminated fragment was discarded, but prior decoded records
        # still provide a safe cursor boundary for the valid prefix.
        latest_seq = _record_seq(records[-1])
    return records, bool(clipped or skipped_window or unterminated_tail and latest_seq is None), latest_seq


def _decode_record(raw: bytes) -> SessionRecord:
    """Decode only envelopes supported by this session-log reader."""
    record = msgspec.json.decode(raw, type=SessionRecord)
    if record.v != SESSION_LOG_VERSION:
        raise ValueError("unsupported session log version")
    return record


def _record_seq(record: Any) -> int:
    seq = getattr(record, "seq", 0)
    return seq if type(seq) is int and seq >= 0 else 0


def _first_after(records: Sequence[Any], cursor: int) -> int:
    """Binary-search monotonically increasing record sequence numbers."""
    low, high = 0, len(records)
    while low < high:
        middle = (low + high) // 2
        if _record_seq(records[middle]) <= cursor:
            low = middle + 1
        else:
            high = middle
    return low


__all__ = [
    "MAX_SESSION_LOG_PAGE",
    "SESSION_LOG_SCAN_WINDOW",
    "read_session_page",
    "session_records",
    "validate_daemon_cursor",
    "validate_logs_read",
]
