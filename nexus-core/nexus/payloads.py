"""Wire payload storage: lossless compression, deduplication and expiry markers.

Only diagnostic wire traffic (`wire_request`, `wire_response`, `wire_event`) is
stored through this module. Conversation messages, snapshots, turn/request/tool
execution rows and semantic lifecycle events stay in their own columns: they are
the context and recovery evidence and are not made dependent on a diagnostic
payload.

A wire payload is content-addressed by its SHA-256 and compressed as one
`event_body` row. Every event row keeps its durable id, type, time, sequence and
session; `body_sha` points at the shared body while the payload is live. Expiry
never deletes the envelope or renumbers anything: it clears `body_sha`, writes
an explicit marker into `data` and sets `payload_state = 'expired'`. Replay,
the HTTP event endpoint and exports therefore show the same event id and type
and an obvious statement that the payload expired, rather than an empty event
or a silent hole.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import Any

WIRE_TYPES = frozenset({"wire_request", "wire_response", "wire_event"})
STORED_MARKER = "$nexus_payload"
EXPIRED = "expired"
FULL = "full"


def has_column(row: sqlite3.Row, name: str) -> bool:
    return name in tuple(row.keys())
RAW = "raw"
ZLIB = "zlib"


def payload_bytes(data: Any) -> int:
    return len(json.dumps(data, separators=(",", ":")).encode("utf-8"))


def encode_body(raw: bytes) -> tuple[str, bytes]:
    """Compress a payload, or keep it raw when compression does not pay."""
    compressed = zlib.compress(raw, 6)
    # Small bodies and already-compressed content lose to zlib's header; do not
    # inflate the database just to claim every wire body was compressed.
    if len(compressed) + 16 < len(raw):
        return ZLIB, compressed
    return RAW, raw


def decode_body(encoding: str, body: bytes) -> bytes:
    if encoding == ZLIB:
        return zlib.decompress(body)
    if encoding == RAW:
        return body
    raise ValueError(f"unknown event body encoding {encoding!r}")


def body_sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def stored_marker(sha: str) -> str:
    return json.dumps({STORED_MARKER: "stored", "sha": sha}, separators=(",", ":"))


def compact_expired_marker(type_: str, original_bytes: int, *, expired_at: float,
                           deadline: float | None = None) -> dict[str, Any]:
    """The small at-rest marker. Human explanation is synthesized on read."""
    return {
        STORED_MARKER: EXPIRED,
        "state": EXPIRED,
        "event_type": type_,
        "original_bytes": max(0, int(original_bytes)),
        "expired_at": expired_at,
        **({"deadline": deadline} if deadline is not None else {}),
    }


def expand_marker(marker: dict[str, Any]) -> dict[str, Any]:
    """Add the descriptive note/resync contract without storing it per event."""
    if marker.get(STORED_MARKER) == "unavailable":
        marker = dict(marker)
        marker.setdefault("reason", "the stored event body record is missing")
        marker["note"] = (
            "This event's diagnostic payload is unavailable; its body record is missing. The event envelope "
            "and durable id are retained. The conversation, snapshots and turn execution records are stored "
            "separately."
        )
        marker.setdefault("resync", "/api/snapshot")
        return marker
    if marker.get(STORED_MARKER) == EXPIRED:
        marker = dict(marker)
        marker.setdefault("reason", "diagnostic wire payload retention")
        marker["note"] = (
            "This diagnostic wire payload expired by retention. The event envelope, durable id, type, "
            "sequence and session are retained, so replay and cursors have no invisible hole. The full "
            "payload is no longer recoverable; the conversation, snapshots and turn execution records are "
            "stored separately and remain complete."
        )
        marker.setdefault("resync", "/api/snapshot")
    return marker


def expired_marker(type_: str, original_bytes: int, *, expired_at: float, reason: str,
                   deadline: float | None = None) -> dict[str, Any]:
    """The full explicit marker returned to readers."""
    marker = compact_expired_marker(type_, original_bytes, expired_at=expired_at, deadline=deadline)
    marker["reason"] = reason
    return expand_marker(marker)


def unavailable_marker(type_: str, original_bytes: int, *, at: float, reason: str) -> dict[str, Any]:
    """A payload whose body is missing, reported explicitly instead of as an empty event."""
    marker = expired_marker(type_, original_bytes, expired_at=at, reason=reason)
    marker["state"] = "unavailable"
    marker[STORED_MARKER] = "unavailable"
    marker["note"] = (
        "This event's diagnostic payload is unavailable; its body record is missing. The event envelope "
        "and durable id are retained. The conversation, snapshots and turn execution records are stored "
        "separately."
    )
    return marker


def event_data(db: sqlite3.Connection, row: sqlite3.Row) -> Any:
    """Decode one stored event's payload, including an explicit expiry marker."""
    state = row["payload_state"] if has_column(row, "payload_state") else FULL
    if state == EXPIRED:
        return expand_marker(json.loads(row["data"]))
    sha = row["body_sha"] if has_column(row, "body_sha") else None
    if sha:
        body = db.execute("select encoding, body, raw_bytes from event_body where sha = ?", (sha,)).fetchone()
        if body is None:
            return unavailable_marker(row["type"], row["payload_bytes"] if has_column(row, "payload_bytes") else 0,
                                      at=row["time"], reason="the stored event body record is missing")
        return json.loads(decode_body(body["encoding"], body["body"]))
    return json.loads(row["data"])


def is_expired(data: Any) -> bool:
    return isinstance(data, dict) and data.get(STORED_MARKER) in (EXPIRED, "unavailable")
