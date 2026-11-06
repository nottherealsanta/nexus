"""Read-only, shared inspector projection for the native clients."""
from __future__ import annotations

import json
import time
from typing import Any

from .store import Store

VIEWS = ("Timing", "Conversation", "Steps", "Requests", "Raw")
PAGE = 50


def cache_summary(store: Store, sid: str) -> dict[str, Any]:
    models = store.session_usage(sid)["models"]
    input_ = sum(m["input"] for m in models.values()) if models else None
    cached = sum(m["cached"] for m in models.values()) if models else None
    return {"input": input_, "cached": cached,
            "uncached": max(0, input_ - cached) if input_ is not None else None,
            "hit_percent": 100 * cached / input_ if input_ else None,
            "write": None, "models": models}


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def inspect(store: Store, sid: str, *, view: str = "Timing", page: int = 0,
            now: float | None = None) -> dict[str, Any]:
    if view not in VIEWS or page < 0:
        raise ValueError("unknown inspector view or negative page")
    now = time.time() if now is None else now
    cache = cache_summary(store, sid)
    compacted = store.compaction(sid)
    notes = ["Read-only diagnostics · Refresh to update · newest page first",
             "Cache: selected session's lifetime provider reports; not a prediction of the next cache hit.",
             "Cache writes: unknown (not retained separately)."]
    if compacted:
        notes.append("Earlier diagnostics compacted: " + _json(compacted))
    lanes: list[dict[str, Any]] = []
    records: list[Any] = []
    more = False
    if view in ("Timing", "Steps"):
        ids = store.tree_ids(sid) if view == "Timing" else [sid]
        if len(ids) > 50:
            notes.append(f"Showing 50 of {len(ids)} sessions in this tree.")
        for session_id in ids[:50]:
            session = store.session(session_id)
            label = session.title or session_id
            turns = store.db.execute(
                "select id from turn where session_id = ? order by started desc, id desc limit ? offset ?",
                (session_id, PAGE + 1, page * PAGE)).fetchall()
            more = more or len(turns) > PAGE
            for row in reversed(turns[:PAGE]):
                turn = store.turn(row["id"])
                requests = store.requests_for_turn(row["id"])
                tools = store.tools_for_turn(row["id"])
                attempts = store.attempts_for_turn(row["id"])
                records.append({"session": label, "turn": turn, "requests": requests,
                                "tools": tools, "attempts": attempts})
                for kind, items in (("turn", [turn]), ("model", requests), ("tool", tools), ("attempt", attempts)):
                    for item in items:
                        start = item.get("started")
                        if start is None:
                            continue
                        end = item.get("finished")
                        status = item.get("status", item.get("state", "unknown"))
                        running = end is None and turn["status"] == "running" and status in (
                            "started", "running", "pending", "scheduled")
                        if item.get("is_error"):
                            status = "error"
                        # Missing end timestamps are not invented for historical rows.
                        finish = now if running else (end if end is not None else start)
                        lanes.append({"id": f'{session_id}:{kind}:{item.get("id", item.get("call_id"))}',
                                      "session": label, "session_id": session_id, "kind": kind,
                                      "label": item.get("name") or item.get("model") or f'{kind} {item.get("step", "")}'.strip(),
                                      "start": start, "end": max(start, finish), "running": running,
                                      "status": status, "end_unknown": end is None and not running, "detail": _json(item)})
        lanes.sort(key=lambda x: (ids.index(x["session_id"]), x["start"], x["kind"]))
        if not records:
            notes.append("No retained execution timings. Older sessions may have messages only.")
        text = "\n\n".join(_json(r) for r in records) if view == "Steps" else ""
        if len(lanes) > 2000:
            notes.append(f"Showing 2000 of {len(lanes)} timing lanes; use Steps for all records on this page.")
            lanes = lanes[:2000]
    elif view == "Conversation":
        rows = store.db.execute(
            "select * from message where session_id = ? order by seq desc limit ? offset ?",
            (sid, PAGE + 1, page * PAGE)).fetchall()
        records = [{"id": r["id"], "seq": r["seq"], "role": r["role"], "parts": json.loads(r["parts"]),
                    "created": r["created"], "origin": r["origin"]} for r in reversed(rows[:PAGE])]
        more = len(rows) > PAGE
        text = "\n\n".join(_json(r) for r in records)
        notes.append("Questions and nested code calls are available in Raw; conversation is canonical model context.")
    else:
        types = " and type in ('wire_request', 'wire_response')" if view == "Requests" else ""
        rows = store.db.execute(
            f"select id from event where session_id = ?{types} order by id desc limit ? offset ?",
            (sid, PAGE + 1, page * PAGE)).fetchall()
        for row in reversed(rows[:PAGE]):
            records.extend(store.events(sid, after=row["id"] - 1, limit=1))
        more = len(rows) > PAGE
        text = "\n\n".join(_json(r) for r in records)
        if not records:
            notes.append("No retained diagnostics on this page. Expired payloads cannot be reconstructed.")
    if more:
        notes.append("More retained records available on the Older page.")
    if view == "Timing" and lanes:
        text = "Select a timing lane for its retained execution details."
    return {"session_id": sid, "view": view, "page": page, "more": more,
            "generated": now, "cache": cache, "notes": notes, "text": text or "No records on this page.",
            "lanes": lanes}
