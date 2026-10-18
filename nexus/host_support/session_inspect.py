"""Bounded durable child inspection without recovery, handles, or turn locks."""
from __future__ import annotations

import unicodedata
from typing import Any

from ..ext.quarantine import sanitize_text
from ..session.db import SqliteSessionStore
from ..session.records import EventRecord
from ..util import redact_secrets
from ..view import apply, initial_state


def inspect_session(manager: Any, session: str, namespace: str) -> dict[str, Any]:
    # Validate identity, namespace and project visibility using the tree contract.
    manager.tree(session, namespace=namespace)
    store = SqliteSessionStore(
        manager.store.db, manager.store.project_id, namespace,
        lock_dir=manager.store.lock_dir, register_project=False,
    )
    tail = store.read_tail(session, max_records=4000)
    view = initial_state(session)
    request: dict[str, Any] = {}
    for record in tail.records:
        if isinstance(record, EventRecord):
            view = apply(view, record.event)
            if record.event.type == "context.assembled" and isinstance(record.event.data.get("request"), dict):
                request = {**record.event.data["request"],
                           "model": record.event.data.get("model"),
                           "provider": record.event.data.get("provider")}

    budget = [256_000]
    clipped = [len(tail.records) == 4000]

    def safe(value: Any, depth: int = 0) -> Any:
        if depth > 24:
            clipped[0] = True
            return "[truncated]"
        if isinstance(value, str):
            if budget[0] <= 0:
                clipped[0] = True
                return "[truncated]"
            # Preserve readable code/paragraphs, but never terminal controls or bidi.
            text = "".join(ch if ch in "\n\t" or not unicodedata.category(ch).startswith("C") else " " for ch in value)
            text = redact_secrets(text)
            limit = min(16_000, budget[0])
            if len(text) > limit:
                clipped[0] = True
                text = text[:limit] + "…"
            budget[0] -= len(text)
            return text
        if isinstance(value, dict):
            clipped[0] |= len(value) > 500
            return {sanitize_text(str(k), limit=200): safe(v, depth + 1) for k, v in list(value.items())[:500]}
        if isinstance(value, (list, tuple)):
            clipped[0] |= len(value) > 500
            return [safe(v, depth + 1) for v in value[:500]]
        return value

    result = safe({"view": view.to_dict(), "request": request})
    result["truncated"] = clipped[0]
    return result
