"""Read-only, shared inspector projection for the native clients (API v2).

Every page is bounded: a cursor-paged query per view, one batched fetch for the
children of the page's turns, a byte budget for details and an explicit
`omitted` list for anything left out. See docs/inspector.md ("Inspector API v2").
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from .store import Store

VERSION = 2
VIEWS = ("Timing", "Conversation", "Steps", "Requests", "Raw", "Questions")
LANE_KINDS = ("turn", "model", "tool", "attempt", "terminal")
RECORD_KINDS = ("message", "event", "turn", "request", "tool", "attempt", "terminal")
PAGE = 50
PAGE_MAX = 200
MAX_BYTES = 2_000_000       # details + text carried by one response
DETAIL_MAX = 8_000          # per-record detail before truncation
MAX_LANES = 2000
MAX_TREE = 200
EXTRA_MAX = 200_000         # snapshot / next-request sections the server prepends


ESC = "escape '\\'"


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _blen(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def encode_cursor(tag: str, *parts: Any) -> str:
    raw = json.dumps([tag, *parts], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, tag: str, width: int) -> list[Any]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (ValueError, TypeError) as exc:
        raise ValueError("malformed inspector cursor") from exc
    if not isinstance(value, list) or len(value) != width + 1 or value[0] != tag:
        raise ValueError("inspector cursor does not belong to this view")
    return value[1:]


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Budget:
    """Byte budget shared by every detail string of one response."""

    def __init__(self, total: int) -> None:
        self.left = total
        self.count = 0
        self.bytes = 0

    def detail(self, text: str) -> dict[str, Any]:
        size = _blen(text)
        allowed = min(DETAIL_MAX, max(0, self.left))
        if size <= allowed:
            self.left -= size
            return {"detail": text, "detail_bytes": size, "detail_truncated": False}
        cut = text.encode("utf-8", "replace")[:allowed].decode("utf-8", "ignore")
        self.left -= _blen(cut)
        self.count += 1
        self.bytes += size - _blen(cut)
        return {"detail": cut, "detail_bytes": size, "detail_truncated": True}


def cache_summary(store: Store, sid: str) -> dict[str, Any]:
    """The selected session's lifetime cache report (never its children's)."""
    models = store.session_usage(sid)["models"]
    with_usage = sum(m["requests"] for m in models.values())
    retained = store.db.execute("select count(*) from turn_request where session_id = ?", (sid,)).fetchone()[0]
    input_ = sum(m["input"] for m in models.values()) if models else None
    cached = sum(m["cached"] for m in models.values()) if models else None
    return {
        "scope": "session lifetime", "input": input_, "cached": cached,
        "uncached": max(0, input_ - cached) if input_ is not None else None,
        "hit_percent": 100 * cached / input_ if input_ else None,
        "denominator": "input tokens reported by provider",
        "coverage": {"requests_with_usage": with_usage, "requests_total": max(retained, with_usage),
                     "complete": with_usage >= retained},
        "models": {name: _model_cache(m) for name, m in models.items()},
        "write": None, "write_known": False,
    }


def _model_cache(m: dict[str, Any]) -> dict[str, Any]:
    return {**m, "uncached": max(0, m["input"] - m["cached"]),
            "hit_percent": 100 * m["cached"] / m["input"] if m["input"] else None}


def _range_cache(requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over exactly the displayed page's requests; one usage per request (attempts already summed)."""
    models: dict[str, dict[str, int]] = {}
    with_usage = 0
    for request in requests:
        usage = request.get("usage")
        if not usage:
            continue
        with_usage += 1
        entry = models.setdefault(request.get("model") or "unknown", {"requests": 0, "input": 0, "cached": 0})
        entry["requests"] += 1
        entry["input"] += usage["input"]
        entry["cached"] += usage["cached"]
    input_ = sum(m["input"] for m in models.values())
    cached = sum(m["cached"] for m in models.values())
    return {"scope": "displayed page", "input": input_ if with_usage else None,
            "cached": cached if with_usage else None,
            "uncached": input_ - cached if with_usage else None,
            "hit_percent": 100 * cached / input_ if input_ else None,
            "denominator": "input tokens reported by provider",
            "coverage": {"requests_with_usage": with_usage, "requests_total": len(requests)},
            "models": {n: {**m, "uncached": m["input"] - m["cached"],
                           "hit_percent": 100 * m["cached"] / m["input"] if m["input"] else None}
                       for n, m in models.items()}}


def root_of(store: Store, sid: str) -> str:
    seen = set()
    while True:
        row = store.db.execute("select parent_id from session where id = ?", (sid,)).fetchone()
        if row is None:
            raise LookupError(f"no session {sid}")
        if not row[0] or row[0] in seen:
            return sid
        seen.add(sid)
        sid = row[0]


def tree_rows(store: Store, root: str, limit: int = MAX_TREE) -> tuple[list[dict[str, Any]], int]:
    cte = ("with recursive tree(id, depth) as (select id, 0 from session where id = ? union all"
           " select s.id, tree.depth + 1 from session s join tree on s.parent_id = tree.id)")
    total = store.db.execute(cte + " select count(*) from tree", (root,)).fetchone()[0]
    rows = store.db.execute(
        cte + " select s.id, s.title, s.parent_id, tree.depth, s.archived, s.agent, s.model,"
        " exists(select 1 from turn t where t.session_id = s.id and t.status = 'running') running"
        " from tree join session s on s.id = tree.id order by tree.depth, s.created, s.id limit ?",
        (root, limit)).fetchall()
    return [{"id": r["id"], "title": r["title"] or r["id"], "parent": r["parent_id"], "depth": r["depth"],
             "status": "running" if r["running"] else "archived" if r["archived"] else "idle",
             "agent": r["agent"], "model": r["model"]} for r in rows], total


def heads(store: Store, target: str) -> dict[str, Any]:
    ids = store.tree_ids(target)
    marks = ", ".join("?" * len(ids))
    tree_head = store.db.execute(
        f"select coalesce(max(id), 0) from event where session_id in ({marks})", ids).fetchone()[0]
    own = store.db.execute("select coalesce(max(id), 0) from event where session_id = ?", (target,)).fetchone()[0]
    return {"head": tree_head, "session_head": own, "log_head": store.log_head(),
            "generation": store.log_generation(), "floor": store.log_floor()}


def head(store: Store, sid: str, session: str | None = None) -> dict[str, Any]:
    target = _target(store, sid, session)
    return {"session_id": sid, "session": target, **heads(store, target)}


def _target(store: Store, sid: str, session: str | None) -> str:
    if session in (None, "", sid):
        return sid
    if store.session(session) is None:
        raise ValueError(f"no session {session} in this tree")
    if root_of(store, session) != root_of(store, sid):
        raise ValueError(f"session {session} is not in the tree of {sid}")
    return session


def _status_word(item: dict[str, Any]) -> str:
    return str(item.get("status", item.get("state", "unknown")))


def _ms(item: dict[str, Any]) -> str:
    if item.get("started") is None or item.get("finished") is None:
        return "end unknown" if item.get("started") is not None else ""
    return f"{max(0.0, item['finished'] - item['started']) * 1000:.0f} ms"


def _lane_summary(kind: str, label: str, status: str, item: dict[str, Any]) -> str:
    bits = [kind, label, status, _ms(item)]
    if kind == "model" and item.get("ttfb") is not None:
        bits.append(f"ttfb {item['ttfb'] * 1000:.0f} ms")
    return " ".join(b for b in bits if b)


def _event_summary(row: Any, data: Any) -> str:
    model = None
    if isinstance(data, dict):
        body = data.get("body")
        model = data.get("model") or (body.get("model") if isinstance(body, dict) else None)
    size = row["payload_bytes"]
    bits = [row["type"] + (f" {model}" if model else "")]
    if size is not None and size >= 0:
        bits.append(_size(size))
    if row["payload_state"] != "full":
        bits.append(row["payload_state"])
    return " · ".join(bits)


def _rows_in(store: Store, table: str, column: str, ids: list[str]) -> list[Any]:
    if not ids:
        return []
    return store.db.execute(
        f"select * from {table} where {column} in ({', '.join('?' * len(ids))})", ids).fetchall()


def inspect(store: Store, sid: str, *, view: str = "Timing", before: str | None = None, limit: int = PAGE,
            session: str | None = None, q: str | None = None, kind: str | None = None,
            status: str | None = None, types: list[str] | None = None, now: float | None = None,
            max_bytes: int = MAX_BYTES, max_lanes: int = MAX_LANES, max_tree: int = MAX_TREE) -> dict[str, Any]:
    if view not in VIEWS:
        raise ValueError("unknown inspector view")
    if not 1 <= limit <= PAGE_MAX:
        raise ValueError(f"limit must be between 1 and {PAGE_MAX}")
    if kind and view in ("Timing", "Steps") and kind not in LANE_KINDS:
        raise ValueError("kind must be one of " + ", ".join(LANE_KINDS))
    now = time.time() if now is None else now
    target = _target(store, sid, session)
    root = root_of(store, sid)
    q = q or None
    kind = kind or None
    status = status or None
    types = [t for t in (types or []) if t]
    notes = ["Read-only diagnostics · newest page first · cursor paging is stable under new writes",
             "Cache: lifetime provider reports of the selected session; not a prediction of the next cache hit.",
             "Cache writes: unknown (providers fold them into input and the store does not retain them)."]
    omitted: list[dict[str, Any]] = []
    budget = Budget(max_bytes // 2)  # text repeats the details, so the pair stays under max_bytes
    compacted = store.compaction(target)
    if compacted:
        notes.append("Earlier diagnostics compacted: " + _json(compacted))
    tree, tree_total = tree_rows(store, root, max_tree)
    if tree_total > len(tree):
        omitted.append({"kind": "tree", "count": tree_total - len(tree), "reason": f"tree list limited to {max_tree} sessions"})
        notes.append(f"Showing {len(tree)} of {tree_total} sessions in the tree selector.")
    lanes: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    cache_range: dict[str, Any] | None = None
    more = False
    next_before: str | None = None
    text = ""

    def attach(record: dict[str, Any], full: str) -> dict[str, Any]:
        return record | budget.detail(full)

    if view in ("Timing", "Steps"):
        ids = store.tree_ids(target) if view == "Timing" else [target]
        if len(ids) > max_tree:
            omitted.append({"kind": "sessions", "count": len(ids) - max_tree,
                            "reason": f"timing covers the first {max_tree} sessions of the subtree"})
            notes.append(f"Showing {max_tree} of {len(ids)} sessions in this tree.")
            ids = ids[:max_tree]
        names = {r["id"]: r["title"] or r["id"] for r in _rows_in(store, "session", "id", ids)}
        cond = [f"t.session_id in ({', '.join('?' * len(ids))})"]
        params: list[Any] = list(ids)
        if before:
            started, tid = decode_cursor(before, "t", 2)
            cond.append("(t.started < ? or (t.started = ? and t.id < ?))")
            params += [started, started, tid]
        if status and kind != "terminal" and status not in ("exited", "closed", "ended"):
            cond.append("t.status = ?")
            params.append(status)
        if q:
            clause = (f"(t.id like ? {ESC} or t.error like ? {ESC} or t.data like ? {ESC}"
                      f" or exists (select 1 from turn_tool x where x.turn_id = t.id and (x.name like ? {ESC}"
                      f" or x.arguments like ? {ESC} or x.output like ? {ESC}))"
                      f" or exists (select 1 from turn_request r where r.turn_id = t.id and r.model like ? {ESC}))")
            cond.append(clause)
            params += [_like(q)] * clause.count("?")
        rows = store.db.execute(
            "select t.* from turn t where " + " and ".join(cond) + " order by t.started desc, t.id desc limit ?",
            (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        if more:
            next_before = encode_cursor("t", page[-1]["started"], page[-1]["id"])
        turns = {r["id"]: store._turn_row(r) for r in page}
        turn_ids = list(turns)
        by_turn: dict[str, dict[str, list[dict[str, Any]]]] = {t: {"requests": [], "tools": [], "attempts": []}
                                                                for t in turn_ids}
        for r in sorted(_rows_in(store, "turn_request", "turn_id", turn_ids), key=lambda r: (r["step"], r["started"])):
            by_turn[r["turn_id"]]["requests"].append(store._request_row(r))
        for r in sorted(_rows_in(store, "turn_tool", "turn_id", turn_ids), key=lambda r: (r["started"] or 0, r["call_id"])):
            by_turn[r["turn_id"]]["tools"].append(store._tool_row(r))
        for r in sorted(_rows_in(store, "turn_attempt", "turn_id", turn_ids),
                        key=lambda r: (r["started"] or 0, r["attempt"], r["id"])):
            by_turn[r["turn_id"]]["attempts"].append(dict(r))
        if turn_ids and store.db.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'terminal_resource'").fetchone():
            resources = store.db.execute(
                "select metadata from terminal_resource where json_extract(metadata, '$.turn_id') in ("
                + ", ".join("?" * len(turn_ids)) + ") order by id limit 201", turn_ids).fetchall()
            if len(resources) > 200:
                omitted.append({"kind": "resources", "count": 1, "reason": "terminal span limit 200; at least one omitted"})
            for resource in resources[:200]:
                metadata = json.loads(resource["metadata"])
                tid = metadata["turn_id"]
                if metadata.get("session_id") != turns[tid]["session_id"]:
                    continue
                for tool in by_turn[tid]["tools"]:
                    if tool["call_id"] != metadata.get("tool_call_id"):
                        continue
                    span = _terminal_span(metadata)
                    tool.setdefault("resource_spans", []).append(span)
        cache_range = _range_cache([x for t in turn_ids for x in by_turn[t]["requests"]])
        ordered = list(reversed(page))
        if view == "Steps":
            for row in ordered:
                turn = turns[row["id"]]
                parts = by_turn[row["id"]]
                bundle = {"session": names.get(row["session_id"], row["session_id"]), "turn": turn, **parts}
                summary = (f"turn {turn['status']} · {len(parts['requests'])} requests · {len(parts['tools'])} tools"
                           f" · {_ms(turn) or 'running'}")
                records.append(attach({"kind": "turn", "id": row["id"], "session_id": row["session_id"],
                                       "turn_id": row["id"], "started": row["started"], "status": turn["status"],
                                       "summary": summary}, _json(bundle)))
            text = "\n\n".join(f"{r['summary']}\n{r['detail']}" for r in records)
        built: list[dict[str, Any]] = []
        for row in ordered:
            turn = turns[row["id"]]
            parts = by_turn[row["id"]]
            for lane_kind, items in (("turn", [turn]), ("model", parts["requests"]), ("tool", parts["tools"]),
                                     ("attempt", parts["attempts"])):
                if kind and kind != lane_kind or status in ("exited", "closed", "ended"):
                    continue
                for item in items:
                    start = item.get("started")
                    if start is None:
                        continue
                    end = item.get("finished")
                    state = _status_word(item)
                    running = end is None and turn["status"] == "running" and state in (
                        "started", "running", "pending", "scheduled")
                    if item.get("is_error"):
                        state = "error"
                    finish = now if running else (end if end is not None else start)  # never invent an end
                    ident = item.get("id", item.get("call_id"))
                    label = item.get("name") or item.get("model") or f'{lane_kind} {item.get("step", "")}'.strip()
                    ref_id = f'{row["id"]}:{ident}' if lane_kind == "tool" else ident
                    lane = {"id": f'{row["session_id"]}:{lane_kind}:{ident}', "session": names.get(row["session_id"]),
                            "session_id": row["session_id"], "kind": lane_kind, "label": label, "turn_id": row["id"],
                            "start": start, "end": max(start, finish), "running": running, "status": state,
                            "end_unknown": end is None and not running,
                            "ref": {"kind": "request" if lane_kind == "model" else lane_kind, "id": ref_id},
                            "summary": _lane_summary(lane_kind, label, state, item)}
                    if lane_kind == "model" and item.get("ttfb") is not None:
                        lane["ttfb"] = item["ttfb"]  # recorded seconds from wire start to the first event
                    built.append((lane, item))
        if view == "Timing" and (not kind or kind == "terminal"):
            for row in ordered:
                for tool in by_turn[row["id"]]["tools"]:
                    for span in tool.get("resource_spans", []):
                        if status and span["status"] != status:
                            continue
                        start, end = span["started"], span["finished"]
                        running = span["status"] == "running" and end is None
                        lane = {"id": f'{row["session_id"]}:terminal:{span["id"]}',
                                "session": names.get(row["session_id"]), "session_id": row["session_id"],
                                "kind": "terminal", "label": "Terminal", "turn_id": row["id"],
                                "start": start, "end": max(start, now if running else end or start),
                                "running": running, "end_unknown": end is None and not running,
                                "status": span["status"], "ref": {"kind": "terminal", "id": span["id"]},
                                "summary": f'Terminal · {span["status"]} · diagnostic only'}
                        built.append((lane, span))
        if len(built) > max_lanes:
            built.sort(key=lambda p: p[0]["start"])
            dropped = len(built) - max_lanes
            built = built[dropped:]
            omitted.append({"kind": "lanes", "count": dropped, "reason": f"lane limit {max_lanes}; oldest on this page dropped"})
            notes.append(f"Showing {max_lanes} of {max_lanes + dropped} timing lanes; narrow with kind/status/q or page older.")
        position = {s: i for i, s in enumerate(ids)}
        built.sort(key=lambda p: (position.get(p[0]["session_id"], 0), p[0]["start"], p[0]["kind"]))
        for lane, item in built:
            lanes.append(attach(lane, _json(item)))
        if not rows and not before and not (q or status or kind):
            notes.append("No retained execution timings. Older sessions may have messages only.")
        if view == "Timing":
            text = "Select a timing lane for its retained execution details." if lanes else ""
    elif view == "Conversation":
        cond, params = ["session_id = ?"], [target]
        if before:
            cond.append("seq < ?")
            params.append(decode_cursor(before, "c", 1)[0])
        if kind:
            cond.append("role = ?")
            params.append(kind)
        if q:
            cond.append("parts like ? escape '\\'")
            params.append(_like(q))
        rows = store.db.execute("select * from message where " + " and ".join(cond) + " order by seq desc limit ?",
                                (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        if more:
            next_before = encode_cursor("c", rows[limit - 1]["seq"])
        for r in reversed(rows[:limit]):
            parts = json.loads(r["parts"])
            full = _json({"id": r["id"], "seq": r["seq"], "role": r["role"], "parts": parts,
                          "created": r["created"], "origin": r["origin"]})
            summary = f"{r['role']} · {len(parts)} part{'s' * (len(parts) != 1)} · {_size(_blen(r['parts']))}"
            records.append(attach({"kind": "message", "id": r["id"], "seq": r["seq"], "role": r["role"],
                                   "created": r["created"], "origin": r["origin"], "session_id": target,
                                   "summary": summary}, full))
        text = "\n\n".join(r["detail"] for r in records)
        notes.append("Questions and nested code calls are in the Questions and Raw views; conversation is canonical model context.")
    elif view == "Questions":
        store._migrate_session_lifecycle(target)
        cond, params = ["session_id = ?", "kind = 'question'"], [target]
        order = "coalesce(asked_at, replied_at, 0)"
        if before:
            at, ident = decode_cursor(before, "q", 2)
            cond.append(f"({order} < ? or ({order} = ? and ident < ?))")
            params += [at, at, ident]
        if status in ("pending", "replied"):
            cond.append("replied is null" if status == "pending" else "replied is not null")
        elif status:
            raise ValueError("Questions status must be pending or replied")
        if q:
            cond.append(f"(asked like ? {ESC} or replied like ? {ESC})")
            params += [_like(q), _like(q)]
        rows = store.db.execute(
            f"select *, {order} as at from wait_record where " + " and ".join(cond)
            + f" order by {order} desc, ident desc limit ?", (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        if more:
            next_before = encode_cursor("q", page[-1]["at"], page[-1]["ident"])
        call_ids = [json.loads(r["asked"]).get("call_id") for r in page if r["asked"]]
        turn_of: dict[str, str] = {}
        call_ids = [c for c in call_ids if c]
        if call_ids:
            for t in store.db.execute(
                    f"select call_id, turn_id from turn_tool where session_id = ? and call_id in ({', '.join('?' * len(call_ids))})"
                    " order by rowid", (target, *call_ids)):
                turn_of[t["call_id"]] = t["turn_id"]
        for r in reversed(page):
            asked = json.loads(r["asked"]) if r["asked"] else None
            replied = json.loads(r["replied"]) if r["replied"] else None
            call_id = (asked or replied or {}).get("call_id")
            state = "pending" if replied is None else (replied.get("outcome") or "replied")
            n = len((asked or {}).get("questions") or [])
            summary = f"question {state} · {n} item{'s' * (n != 1)}" + (f" · tool call {call_id}" if call_id else "")
            records.append(attach({"kind": "question", "id": r["ident"], "session_id": target, "status": state,
                                   "asked_at": r["asked_at"], "replied_at": r["replied_at"], "call_id": call_id,
                                   "turn_id": turn_of.get(call_id),
                                   "links": {"turn": turn_of.get(call_id), "tool": call_id},
                                   "summary": summary}, _json({"asked": asked, "replied": replied})))
        text = "\n\n".join(f"{r['summary']}\n{r['detail']}" for r in records)
    else:  # Requests and Raw
        wire = ["wire_request", "wire_response"]
        use = [t for t in types if t in wire] if view == "Requests" and types else (wire if view == "Requests" else types)
        cond, params = ["session_id = ?"], [target]
        if view == "Requests" or types:
            if view == "Requests" and types and not use:
                cond.append("0")
            elif use:
                cond.append(f"type in ({', '.join('?' * len(use))})")
                params += use
        if before:
            cond.append("id < ?")
            params.append(decode_cursor(before, "e", 1)[0])
        if q:
            cond.append("(type like ? escape '\\' or data like ? escape '\\')")
            params += [_like(q), _like(q)]
            notes.append("q matches event type and the inline payload; bodies stored out of line match by type only.")
        rows = store.db.execute("select * from event where " + " and ".join(cond) + " order by id desc limit ?",
                                (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        if more:
            next_before = encode_cursor("e", rows[limit - 1]["id"])
        for r in reversed(rows[:limit]):
            ev = store._event_row(r)
            records.append(attach({"kind": "event", "id": r["id"], "seq": r["seq"], "type": r["type"],
                                   "time": r["time"], "session_id": target, "turn_id": r["turn_id"],
                                   "payload_state": r["payload_state"], "summary": _event_summary(r, ev["data"])},
                                  _json(ev)))
        text = "\n\n".join(r["detail"] for r in records)
        if not records:
            notes.append("No retained diagnostics on this page. Expired payloads cannot be reconstructed.")
    if budget.count:
        omitted.append({"kind": "detail", "count": budget.count, "bytes": budget.bytes,
                        "reason": "detail text clipped (per-record or response byte limit); fetch /inspect/record for the full record"})
        notes.append(f"{budget.count} detail(s) clipped, {_size(budget.bytes)} omitted; fetch the full record on demand.")
    if more:
        notes.append("More retained records are available with next_before.")
    filters = {k: v for k, v in (("q", q), ("kind", kind), ("status", status), ("type", types or None)) if v}
    return {"version": VERSION, "session_id": sid, "session": target, "view": view, "limit": limit,
            "before": before, "next_before": next_before, "more": more, "generated": now,
            **heads(store, target), "filters": filters,
            "cache": cache_summary(store, target), "cache_range": cache_range,
            "tree": tree, "notes": notes, "omitted": omitted,
            "text": text or "No records on this page.", "records": records, "lanes": lanes}


def clip(text: str, limit: int = EXTRA_MAX) -> tuple[str, int]:
    """Cut a prepended section to `limit` bytes; returns (text, omitted_bytes)."""
    size = _blen(text)
    if size <= limit:
        return text, 0
    cut = text.encode("utf-8", "replace")[:limit].decode("utf-8", "ignore")
    return cut + "\n... clipped", size - _blen(cut)


def _terminal_span(metadata: dict[str, Any]) -> dict[str, Any]:
    span = {key: metadata.get(key) for key in (
        "id", "session_id", "turn_id", "tool_call_id", "background_job_id", "status",
        "exit_code", "signal", "control_acquisitions", "control_releases")}
    return span | {"kind": "terminal", "started": metadata["created"],
                   "finished": metadata.get("ended"), "diagnostic_only": True}


def record(store: Store, sid: str, kind: str, ident: str) -> dict[str, Any] | None:
    """One full record (no truncation) of the tree containing `sid`, or None."""
    if kind not in RECORD_KINDS:
        raise ValueError("kind must be one of " + ", ".join(RECORD_KINDS))
    root = root_of(store, sid)
    value: Any = None
    owner: str | None = None
    if kind == "message":
        r = store.db.execute("select * from message where id = ?", (ident,)).fetchone()
        if r:
            owner = r["session_id"]
            value = {"id": r["id"], "seq": r["seq"], "role": r["role"], "parts": json.loads(r["parts"]),
                     "created": r["created"], "origin": r["origin"]}
    elif kind == "event":
        try:
            event_id = int(ident)
        except ValueError as exc:
            raise ValueError("event id must be an integer") from exc
        r = store.db.execute("select * from event where id = ?", (event_id,)).fetchone()
        if r:
            owner = r["session_id"]
            value = store._event_row(r)
    elif kind == "turn":
        value = store.turn(ident)
        owner = value["session_id"] if value else None
    elif kind == "request":
        value = store.request(ident)
        owner = value["session_id"] if value else None
    elif kind == "terminal":
        exists = store.db.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'terminal_resource'").fetchone()
        r = store.db.execute("select metadata from terminal_resource where id = ?", (ident,)).fetchone() if exists else None
        if r:
            metadata = json.loads(r["metadata"])
            linked = store.db.execute(
                "select 1 from turn_tool where session_id = ? and turn_id = ? and call_id = ?",
                (metadata.get("session_id"), metadata.get("turn_id"), metadata.get("tool_call_id"))).fetchone()
            if linked:
                owner, value = metadata["session_id"], _terminal_span(metadata)
    elif kind == "attempt":
        r = store.db.execute("select * from turn_attempt where id = ?", (ident,)).fetchone()
        if r:
            owner, value = r["session_id"], dict(r)
    else:
        turn_id, _, call_id = ident.partition(":")
        r = store.db.execute("select * from turn_tool where turn_id = ? and call_id = ?", (turn_id, call_id)).fetchone()
        if r:
            owner, value = r["session_id"], store._tool_row(r)
    if value is None or owner is None or store.session(owner) is None or root_of(store, owner) != root:
        return None
    text = _json(value)
    return {"version": VERSION, "kind": kind, "id": ident, "session_id": owner,
            "detail_bytes": _blen(text), "record": value}
