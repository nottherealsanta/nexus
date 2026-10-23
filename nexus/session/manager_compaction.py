"""Lazy, durable Manager compaction at turn admission (plan 10, docs/manager.md).

Called by ``Session`` right before a turn is admitted, so it needs no timer and no
background work: the idle clock is the newest durable activity record of the
Manager's own log, which survives a daemon restart. When the gap reaches the
configured limit and there is retirable context, it

1. extracts the deterministic machine memory (:mod:`nexus.context.manager_memory`),
2. writes it to the ``kv`` table (``manager_machine_memory``): **first**, and
   compaction stops here if the write fails, so context is never retired without
   its memory being durable,
3. performs an ordinary pair-safe same-session handoff with the Manager brief,
4. records ``manager.memory_updated`` (digest, size, retired range).

Any failure leaves the log untouched and the turn runs on the uncompacted context;
a failed handoff keeps the memory note and the next admission retries. Presence,
input-queue and audit records do not count as activity, otherwise opening the
Manager screen or queueing the very message being admitted would reset the clock.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

from ..context.manager_memory import MEMORY_KEY, build_manager_brief, extract_manager_memory
from .records import EventRecord, MessageRecord, SummaryRecord

__all__ = ["IDLE_RECORD_WINDOW", "compact_manager", "last_activity"]

log = logging.getLogger("nexus.manager")

#: Newest records inspected for the idle clock.
IDLE_RECORD_WINDOW = 200
_ACTIVITY_PREFIXES = ("turn.", "manager.", "session.linked", "context.handoff")


def _counts(record: Any) -> bool:
    if isinstance(record, (MessageRecord, SummaryRecord)):
        return True
    if isinstance(record, EventRecord):
        kind = record.event.type
        return kind != "manager.memory_updated" and kind.startswith(_ACTIVITY_PREFIXES)
    return False


def last_activity(handle: Any) -> float | None:
    """Timestamp of the newest activity record in the Manager log, or ``None`` if empty."""
    records = handle._store.read_tail(handle.id, max_records=IDLE_RECORD_WINDOW).records
    if not records:
        return None
    for record in reversed(records):
        if _counts(record):
            return float(record.ts)
    return float(records[0].ts)  # only quiet records in the window: activity is older still


def compact_manager(
    handle: Any, policy: Callable[[], tuple[bool, float]], *, now: float | None = None
) -> bool:
    """Compact ``handle`` if it has been idle long enough; ``True`` when a handoff happened."""
    try:
        enabled, idle_seconds = policy()
        if not enabled:
            return False
        return _compact(handle, float(idle_seconds), time.time() if now is None else now)
    except Exception as exc:  # noqa: BLE001 - compaction never blocks or fails a turn
        log.warning("manager compaction skipped: %s: %s", type(exc).__name__, exc)
        return False


def _compact(handle: Any, idle_seconds: float, now: float) -> bool:
    last = last_activity(handle)
    if last is None or now - last < idle_seconds:
        return False
    messages, seqs = handle.active_messages()
    retirable = [seq for seq in seqs if seq > 0]
    if not retirable:
        return False
    read = handle.read(force=True)
    store = handle._store
    note = extract_manager_memory(read.records, store.kv_get(MEMORY_KEY) or "")
    try:
        store.kv_set(MEMORY_KEY, note)  # durable before anything is retired
    except Exception as exc:  # noqa: BLE001
        log.warning("manager memory write failed; compaction skipped: %s: %s", type(exc).__name__, exc)
        return False
    try:
        brief = build_manager_brief(messages, handle, read.next_seq + 1, note)
        receipt = handle.handoff(summary=brief, reason="idle_timeout")
    except Exception as exc:  # noqa: BLE001
        log.warning("manager handoff failed; memory kept, context retained: %s: %s", type(exc).__name__, exc)
        return False
    if not receipt.get("fresh"):
        return False  # an earlier handoff already covers this range
    try:
        handle.emit_session_event("manager.memory_updated", {
            "chars": len(note),
            "digest": hashlib.sha256(note.encode("utf-8")).hexdigest()[:16],
            "from_seq": min(retirable),
            "through_seq": read.next_seq,
            "reason": "idle_timeout",
        })
    except Exception:  # noqa: BLE001 - the audit record is best effort once memory and handoff are durable
        log.debug("manager.memory_updated not recorded", exc_info=True)
    return True
