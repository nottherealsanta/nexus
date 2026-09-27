"""Bounded, in-memory diagnostics for one daemon generation.

Only reviewed daemon event names are accepted. Summaries are generated from
fixed templates and selected bounded integers; caller-provided text and other
fields are never copied into the diagnostics packet.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import Any

# Store and response bounds are part of the diagnostics contract.
MAX_DIAGNOSTIC_ENTRIES = 512
MAX_DIAGNOSTIC_BYTES = 128 * 1024
MAX_DIAGNOSTIC_SUMMARY_BYTES = 512
MAX_DIAGNOSTIC_PAGE = 100

# Account for the fixed JSON keys/values and sequence/timestamp when enforcing
# the aggregate store bound, not just the variable summary text.
_ENTRY_OVERHEAD_BYTES = 96
_MAX_SAFE_INTEGER = 1_000_000_000


class DaemonDiagnostics:
    """A bounded diagnostics ring belonging to one daemon process instance."""

    def __init__(self) -> None:
        self._generation = secrets.token_hex(16)
        self._entries: deque[tuple[dict[str, Any], int]] = deque()
        self._bytes = 0
        self._seq = 0
        self._lock = threading.Lock()

    def capture(self, category: str, fields: dict[str, Any] | None = None) -> None:
        """Capture a reviewed category, dropping all unreviewed field values."""
        reviewed = _reviewed_summary(category, fields or {})
        if reviewed is None:
            return
        level, summary = reviewed
        summary_size = len(summary.encode("utf-8"))
        if summary_size > MAX_DIAGNOSTIC_SUMMARY_BYTES:
            return
        encoded_size = len(category.encode("utf-8")) + summary_size
        entry_size = encoded_size + _ENTRY_OVERHEAD_BYTES
        if entry_size > MAX_DIAGNOSTIC_BYTES:
            return
        with self._lock:
            self._seq += 1
            entry = {
                "source": "daemon",
                "seq": self._seq,
                "ts": time.time(),
                "level": level,
                "kind": category,
                "summary": summary,
            }
            self._entries.append((entry, entry_size))
            self._bytes += entry_size
            while (
                len(self._entries) > MAX_DIAGNOSTIC_ENTRIES
                or self._bytes > MAX_DIAGNOSTIC_BYTES
            ):
                _, removed_size = self._entries.popleft()
                self._bytes -= removed_size

    def read(self, cursor: str | None, limit: int) -> dict[str, Any]:
        """Read a bounded page; a missing cursor intentionally reads the tail."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")  # noqa: TRY004 - consistent page-validation errors
        if not 1 <= limit <= MAX_DIAGNOSTIC_PAGE:
            raise ValueError(f"limit must be between 1 and {MAX_DIAGNOSTIC_PAGE}")
        parsed_generation: str | None = None
        position = 0
        if cursor is not None:
            parsed_generation, position = _parse_cursor(cursor)

        with self._lock:
            current_seq = self._seq
            entries = list(self._entries)
            mismatch = parsed_generation is not None and parsed_generation != self._generation
            oldest_seq = entries[0][0]["seq"] if entries else current_seq + 1
            truncated = bool(mismatch)
            if cursor is None or mismatch:
                selected = entries[-limit:]
            else:
                if position > current_seq:
                    raise ValueError("cursor is ahead of the diagnostics stream")
                if position < oldest_seq - 1:
                    truncated = True
                selected = [item for item in entries if item[0]["seq"] > position][:limit]

            result_entries = [dict(item[0]) for item in selected]
            if result_entries:
                next_position = result_entries[-1]["seq"]
            elif mismatch or cursor is None:
                next_position = current_seq
            else:
                next_position = position
            if not mismatch and cursor is not None and position < oldest_seq - 1:
                # A stale cursor advances to just before the retained range so
                # the caller can consume every surviving entry in order.
                next_position = result_entries[-1]["seq"] if result_entries else position
            has_more = bool(
                result_entries
                and result_entries[-1]["seq"] < current_seq
                and not mismatch
            )
            # Tail reads are snapshots of the current end, not a request to
            # replay retained history page-by-page.
            if cursor is None:
                has_more = False
            return {
                "entries": result_entries,
                "next_cursor": f"{self._generation}:{next_position}",
                "truncated": truncated,
                "has_more": has_more,
            }


def _parse_cursor(cursor: str) -> tuple[str, int]:
    if not isinstance(cursor, str) or len(cursor) > 128:
        raise ValueError("invalid diagnostics cursor")
    generation, separator, raw_position = cursor.partition(":")
    if not separator or len(generation) != 32 or any(
        char not in "0123456789abcdef" for char in generation
    ):
        raise ValueError("invalid diagnostics cursor")
    if not raw_position or not raw_position.isascii() or not raw_position.isdecimal():
        raise ValueError("invalid diagnostics cursor")
    return generation, int(raw_position)


def _count(fields: dict[str, Any], name: str) -> int | None:
    value = fields.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= _MAX_SAFE_INTEGER:
        return None
    return value


def _reviewed_summary(
    category: str, fields: dict[str, Any]
) -> tuple[str, str] | None:
    """Return a fixed safe summary for the explicitly reviewed event set."""
    if category == "daemon.started":
        pid = _count(fields, "pid")
        return "info", f"Daemon started (pid {pid})." if pid is not None else "Daemon started."
    if category == "daemon.stopping":
        return "info", "Daemon shutdown started."
    if category == "daemon.stopped":
        return "info", "Daemon stopped."
    if category == "daemon.idle_shutdown":
        return "warning", "Daemon is shutting down after becoming idle."
    if category == "daemon.http_started":
        port = _count(fields, "port")
        return "info", f"HTTP listener started (port {port})." if port is not None else "HTTP listener started."
    if category == "daemon.session_queued":
        depth = _count(fields, "depth")
        return "info", f"A session turn was queued (depth {depth})." if depth is not None else "A session turn was queued."
    if category == "daemon.session_scheduled":
        return "info", "A session turn was scheduled."
    if category == "daemon.session_cancelled":
        dropped = _count(fields, "dropped")
        return "warning", f"A session turn was cancelled ({dropped} queued items dropped)." if dropped is not None else "A session turn was cancelled."
    if category == "daemon.session_failed":
        return "error", "A session turn failed."
    if category in {"connection_error", "subscription_error"}:
        return "error", "A daemon client operation failed."
    if category in {"connection_task_timeout", "connection_close_timeout"}:
        return "warning", "A daemon client connection timed out during shutdown."
    return None
