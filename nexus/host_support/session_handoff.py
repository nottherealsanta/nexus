"""Read-only handoff inspection; never opens a live session or runs recovery."""
from __future__ import annotations

from ..context.handoff import safe_brief
from ..session.db import SqliteSessionStore


def read_handoff(manager, session: str, namespace: str = "main", handoff_id: str = "") -> dict:
    manager.tree(session, namespace=namespace)
    store = SqliteSessionStore(manager.store.db, manager.store.project_id, namespace,
        lock_dir=manager.store.lock_dir, register_project=False)
    head = store.handoff_record(session, handoff_id) if handoff_id else store.context_head(session)
    if head is not None and head.strategy != "handoff":
        _, head = store.active_context_records(session)
    if head is None:
        return {"session": session, "text": "", "head_seq": 0}
    return {"session": session, "text": safe_brief(head.text), "head_seq": head.seq,
            "handoff_id": head.handoff_id, "brief_version": head.brief_version,
            "kept_from_seq": head.kept_from_seq}
