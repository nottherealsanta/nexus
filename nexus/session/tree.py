"""Bounded read-only ownership tree projection, independent of fork ancestry.

No handle is opened and no lock file is created. A stale turn.started record
is not evidence that a process is still running: only a live handle or a held
OS turn lock can make a node running. Display strings are redacted here, before
the host boundary; unknown legacy facts remain null rather than guessed.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time
from typing import Any

from ..ext.quarantine import sanitize_text
from ..util import redact_secrets
from .db import SqliteSessionStore
from .records import EventRecord


def _text(value: Any, limit: int = 240) -> str:
    return redact_secrets(sanitize_text(str(value or ""), limit=limit))[:limit]


def _lock_state(store: SqliteSessionStore, identity: str) -> bool | None:
    """Probe an existing regular lock only; missing lock means not running."""
    import stat

    path = store.lock_path(identity)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def project_tree(manager: Any, session: str, *, namespace: str, max_depth: int) -> dict[str, Any]:
    """Project at most 500 nodes, with bounded tails and explicit unknowns."""
    rows, unknown, truncated = manager.store.ownership_tree_rows(
        session, namespace=namespace, max_depth=max_depth,
    )
    now = time.time()
    stores: dict[str, SqliteSessionStore] = {}

    def node(row: dict[str, Any]) -> dict[str, Any]:
        ns, identity = row["namespace"], row["id"]
        if ns not in stores:
            stores[ns] = SqliteSessionStore(
                manager.store.db, manager.store.project_id, ns, lock_dir=manager.store.lock_dir, register_project=False,
            )
        store = stores[ns]
        handle = manager._live_handle(identity) if ns == manager.store.namespace else None
        held = _lock_state(store, identity)
        running = bool(handle is not None and handle.active) or held is True
        status = "running" if running else ("unknown" if held is None else "idle")
        model = tier = None
        handoffs = 0
        terminal = None
        task = description = result_text = error = current_tool = None
        branch = worktree = None
        tokens = cost = None
        started_ts = None
        agent_type = None
        started_seq = terminal_seq = 0
        with contextlib.suppress(OSError, ValueError, TypeError):
            for record in store.read_tail(identity, max_records=64).records:
                if not isinstance(record, EventRecord):
                    continue
                event = record.event
                # Only the node's own events are liveness/detail evidence.
                if isinstance(event.data.get("agent"), dict) and event.data["agent"].get("role") == "subagent":
                    continue
                if event.type == "turn.started":
                    started_seq = record.seq
                    started_ts = event.ts
                if event.type == "model.started":
                    model = _text(event.data.get("model")) or model
                if event.type == "tool.started":
                    current_tool = _text(event.data.get("name")) or None
                if event.type in {"tool.completed", "tool.failed"}:
                    current_tool = None
                if event.type == "model.usage":
                    usage = event.data
                    tokens = (usage.get("input", 0) or 0) + (usage.get("output", 0) or 0)
                    cost = usage.get("cost_usd")
                if event.type == "text":
                    result_text = _text(event.data.get("text"), 2000)
                if event.type == "context.handoff":
                    handoffs += 1
                if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                    terminal = event.type.removeprefix("turn.")
                    terminal_seq = record.seq
                    error = _text(event.data.get("error") or event.data.get("reason")) or None
                    current_tool = None
                if event.type == "model.selected" and not isinstance(event.data.get("agent"), dict):
                    model = _text(event.data.get("model")) or model
                meta = event.data.get("agent")
                if isinstance(meta, dict) and meta.get("id") == row["agent_id"]:
                    model = _text(meta.get("resolved_model") or meta.get("model")) or model
                    tier = _text(meta.get("tier")) or tier
        if not running and held is False:
            if started_seq > max(terminal_seq, row["completion_seq"]):
                status = "interrupted"
            elif terminal:
                status = terminal
        # Spawn metadata is authoritative for the child's assignment. Read one
        # owner record, not the entire root transcript or an inferred slug.
        if row["owner_id"] and row["owner_spawn_seq"]:
            owner_ns = row["owner_namespace"]
            if owner_ns not in stores:
                stores[owner_ns] = SqliteSessionStore(
                    manager.store.db, manager.store.project_id, owner_ns,
                    lock_dir=manager.store.lock_dir, register_project=False,
                )
            record = stores[owner_ns].record_at(row["owner_id"], row["owner_spawn_seq"])
            for record in (record,):
                if isinstance(record, EventRecord) and record.event.type == "agent.spawned":
                    meta = record.event.data.get("agent")
                    if isinstance(meta, dict) and meta.get("id") == row["agent_id"]:
                        model = _text(record.event.data.get("model") or meta.get("resolved_model") or meta.get("model")) or model
                        tier = _text(meta.get("tier")) or tier
                        agent_type = _text(meta.get("type")) or None
                        task = _text(meta.get("task"), 2000) or None
                        description = _text(meta.get("description")) or None
                        branch = _text(meta.get("branch")) or None
                        worktree = _text(meta.get("worktree_path")) or None
        result = {
            "id": identity, "namespace": ns, "kind": row["kind"] or "unknown",
            "title": _text(row["title"]), "status": status, "depth": row["depth"],
            "owner_agent_id": _text(row["owner_agent_id"]) or None,
            "resolved_model": model, "tier": tier,
            "agent_type": agent_type,
            "task": task, "description": description,
            "current_tool": current_tool if running else None,
            "elapsed_s": max(0.0, now - started_ts) if running and started_ts else None,
            "tokens": tokens, "cost_usd": cost,
            "usage_bounded": True,
            "branch": branch, "worktree": worktree,
            "result": result_text, "error": error,
            "handoff_count": max(handoffs, row["handoff_count"]),
            "handoff_count_bounded": row["handoff_count"] == 0,
            "last_activity": row["last_activity"],
            "idle_seconds": max(0.0, now - row["last_activity"]),
            "children": [], "read_only": True,
            "actions": {"transcript": True, "request": True, "context": True,
                        "send": False, "resume": False, "fork": False},
            "root_id": row["root_id"] or None,
            "owner_namespace": row["owner_namespace"] or None,
            "owner_id": row["owner_id"] or None,
            "owner_turn_id": _text(row["owner_turn_id"]) or None,
            "owner_spawn_seq": row["owner_spawn_seq"] or None,
            "owner_call_id": _text(row["owner_call_id"]) or None,
            "agent_id": _text(row["agent_id"]) or None,
            "logical_session_id": _text(row["logical_session_id"]) or None,
            "parent_id": row["parent_id"] or None, "fork_seq": row["fork_seq"] or None,
            "last_seq": row["last_seq"], "running_children": 0,
            "context_head_seq": row["context_head_seq"] or None,
        }
        return result

    nodes = {(row["namespace"], row["id"]): node(row) for row in rows}
    roots = []
    for row in rows:
        current = nodes[(row["namespace"], row["id"])]
        owner = nodes.get((row["owner_namespace"], row["owner_id"]))
        if owner is not None and row["depth"] > 0:
            owner["children"].append(current)
        else:
            roots.append(current)
    # Roll-up never crosses a fork relationship and never revives a dead child.
    for row in reversed(rows):
        current = nodes[(row["namespace"], row["id"])]
        current["running_children"] = sum(
            int(child["status"] == "running") + child["running_children"]
            for child in current["children"]
        )
    return {"nodes": roots, "unattached": [node(row) for row in unknown],
            "truncated": truncated, "max_nodes": 500, "max_depth": max_depth,
            "read_only": True}


def running_child_counts(store: SqliteSessionStore) -> dict[str, int]:
    """Bounded live roll-up for lists; a historical event is never liveness.

    At most 500 rows are probed, matching the tree cap. Results are explicitly
    conservative: rows outside that bounded window contribute no live signal.
    """
    rows = store.db._connection().execute(
        "SELECT namespace,id,owner_namespace,owner_id FROM sessions WHERE project_id=? "
        "AND namespace='agents' AND trash_id IS NULL ORDER BY last_activity DESC,id LIMIT 500",
        (store.project_id,),
    ).fetchall()
    child_store = SqliteSessionStore(store.db, store.project_id, "agents", lock_dir=store.lock_dir, register_project=False)
    owners = {(row["namespace"], row["id"]): (row["owner_namespace"], row["owner_id"]) for row in rows}
    counts: dict[str, int] = {}
    for row in rows:
        if _lock_state(child_store, row["id"]) is not True:
            continue
        owner = (row["owner_namespace"], row["owner_id"])
        seen = {(row["namespace"], row["id"])}
        for _ in range(64):
            if not all(owner) or owner in seen:
                break
            seen.add(owner)
            key = owner[1] if owner[0] == store.namespace else ":".join(owner)
            counts[key] = counts.get(key, 0) + 1
            owner = owners.get(owner, ("", ""))
    return counts
