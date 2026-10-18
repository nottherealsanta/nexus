"""P6 dossier: read-only, metadata-only measurement of real sessions.

Run: python scripts/p6_dossier.py [--database PATH] [--top N] [--projects N]

The default database is ``$NEXUS_HOME/nexus.db`` when ``NEXUS_HOME`` is set (as
``nexus.config.paths.state_db_path`` does), else ``~/.nexus/nexus.db``.

Opens the Nexus SQLite store with ``mode=ro`` and ``query_only``; it never writes.
Prints one JSON document of counts, byte sizes and numeric ranges. It never
emits message or brief text, titles, project roots or any other content; project
ids are replaced by an opaque sha256 prefix. Session ids and namespaces are
printed for the largest logs only.

Scope (also reported under ``scope``): trashed sessions are excluded (active
only), and for forked sessions (``parent_id`` set) the records with
``seq <= fork_seq`` are excluded because they are a copy of the parent's prefix,
not events of the child. Both filters are skipped when the ``sessions`` table
lacks the columns (legacy stores); ``scope`` says which were applied.

Strategy summaries (``kind='summary'``) are classified inside SQLite via
``json_extract(body, '$.strategy')``; only the strategy, the brief's character
length and integer token counts are fetched, never the brief itself, and rows
are streamed rather than loaded at once. Event and message bodies are likewise
never decoded outside SQLite. Between successive handoffs of one session,
``turn_interval`` counts ``turn.started`` events strictly between the two
handoff seqs (true turns), ``message_interval`` counts message records (a
proxy), and ``compact_between`` counts ``compact`` summaries in the same
interval (``mixed_intervals`` is how many intervals contain at least one). No
latency is measured.

Cost: this is an offline full scan of ``records`` (plus one range count per
consecutive handoff pair) with no hard cost cap. ``--top`` and ``--projects``
bound only the size of the output, not the work done.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import sys
from pathlib import Path
from urllib.parse import quote

MAX_LIMIT = 1000

_RECORDS = ("records r LEFT JOIN sessions s ON s.project_id=r.project_id "
            "AND s.namespace=r.namespace AND s.id=r.session_id")


def default_database() -> Path:
    """``$NEXUS_HOME/nexus.db`` or ``~/.nexus/nexus.db`` (mirrors state_db_path)."""
    override = os.environ.get("NEXUS_HOME")
    if override:
        return Path(override).expanduser() / "nexus.db"
    return Path.home() / ".nexus" / "nexus.db"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def opaque(project_id: str) -> str:
    return hashlib.sha256(project_id.encode()).hexdigest()[:10]


def span(values: list[int | float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "min": min(values), "median": statistics.median(values),
            "max": max(values)}


def _scope(conn: sqlite3.Connection, tables: set[str]) -> tuple[str, dict, str | None]:
    """SQL predicate over ``records r`` / ``sessions s``, the scope report, and the
    session column used to exclude trashed sessions (``None`` if unavailable)."""
    cols = ({r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
            if "sessions" in tables else set())
    conds = []
    # ``trash_id`` is authoritative in the real schema; ``trashed_at`` is only a
    # fallback for databases that lack it.
    trash_col = "trash_id" if "trash_id" in cols else "trashed_at" if "trashed_at" in cols else None
    trash = trash_col is not None
    fork = {"parent_id", "fork_seq"} <= cols
    if trash:
        conds.append(f"s.{trash_col} IS NULL")
    if fork:
        conds.append("(COALESCE(s.parent_id,'')='' OR r.seq > COALESCE(s.fork_seq,0))")
    where = " AND ".join(conds) or "1"
    return where, {"include_trashed": False, "trashed_filter_applied": trash,
                   "fork_prefix_excluded": fork}, trash_col


def largest_logs(conn: sqlite3.Connection, where: str, top: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT r.namespace, r.session_id, COUNT(*) n, SUM(length(r.body)) b FROM {_RECORDS} "
        f"WHERE {where} GROUP BY r.project_id, r.namespace, r.session_id "
        "ORDER BY b DESC LIMIT ?", (top,))
    return [{"namespace": ns, "session_id": sid, "records": n, "bytes": b}
            for ns, sid, n, b in rows]


def _count_between(conn: sqlite3.Connection, where: str, key: tuple, lo: int, hi: int,
                   kind: str) -> int:
    """Count records of ``kind`` strictly between two seqs; turns are events whose
    JSON ``$.event.type`` is ``turn.started`` (EventRecord envelope)."""
    sql = (f"SELECT COUNT(*) FROM {_RECORDS} WHERE r.project_id=? AND r.namespace=? AND "
           f"r.session_id=? AND r.seq>? AND r.seq<? AND ({where}) AND ")
    if kind == "turn":
        sql += ("r.kind='event' AND CASE WHEN json_valid(CAST(r.body AS TEXT)) THEN "
                "json_extract(CAST(r.body AS TEXT),'$.event.type')='turn.started' ELSE 0 END")
        return conn.execute(sql, (*key, lo, hi)).fetchone()[0]
    return conn.execute(sql + "r.kind=?", (*key, lo, hi, kind)).fetchone()[0]


def handoff_stats(conn: sqlite3.Connection, where: str, top_projects: int) -> dict:
    per: dict[str, dict] = {}
    # Only strategy, brief length and integer token counts leave SQLite.
    cur = conn.execute(
        "SELECT project_id, namespace, session_id, seq, strategy, blen, tb, ta FROM ("
        "SELECT r.project_id, r.namespace, r.session_id, r.seq, "
        "json_extract(CAST(r.body AS TEXT),'$.strategy') strategy, "
        "CASE WHEN json_type(CAST(r.body AS TEXT),'$.text')='text' "
        "THEN length(json_extract(CAST(r.body AS TEXT),'$.text')) ELSE 0 END blen, "
        "CASE WHEN json_type(CAST(r.body AS TEXT),'$.tokens_before')='integer' "
        "THEN json_extract(CAST(r.body AS TEXT),'$.tokens_before') END tb, "
        "CASE WHEN json_type(CAST(r.body AS TEXT),'$.tokens_after')='integer' "
        "THEN json_extract(CAST(r.body AS TEXT),'$.tokens_after') END ta "
        f"FROM {_RECORDS} WHERE r.kind='summary' AND ({where}) "
        "AND json_valid(CAST(r.body AS TEXT))) "
        "WHERE strategy IN ('handoff','compact') "
        "ORDER BY project_id, namespace, session_id, seq")
    prev_handoff: dict[tuple, int] = {}
    compacts_since: dict[tuple, int] = {}
    totals = {"handoff": 0, "compact": 0}
    for project, ns, sid, seq, strategy, blen, tb, ta in cur:
        agg = per.setdefault(project, {
            "handoff": 0, "compact": 0, "brief_chars": [], "tokens_before": [],
            "tokens_after": [], "seq_gap": [], "message_gap": [], "turn_gap": [],
            "compact_between": []})
        totals[strategy] += 1
        agg[strategy] += 1
        session = (project, ns, sid)
        if strategy == "compact":
            compacts_since[session] = compacts_since.get(session, 0) + 1
            continue
        agg["brief_chars"].append(blen)
        for key, v in (("tokens_before", tb), ("tokens_after", ta)):
            if v is not None:
                agg[key].append(v)
        if session in prev_handoff:
            prev = prev_handoff[session]
            agg["seq_gap"].append(seq - prev)
            agg["message_gap"].append(_count_between(conn, where, session, prev, seq, "message"))
            agg["turn_gap"].append(_count_between(conn, where, session, prev, seq, "turn"))
            agg["compact_between"].append(compacts_since.get(session, 0))
        prev_handoff[session] = seq
        compacts_since[session] = 0
    ranked = sorted(((p, a) for p, a in per.items() if a["handoff"]),
                    key=lambda kv: (-kv[1]["handoff"], -kv[1]["compact"]))
    return {
        "total_handoffs": totals["handoff"],
        "total_compacts": totals["compact"],
        "projects_with_handoffs": len(ranked),
        "projects_with_compacts": sum(1 for a in per.values() if a["compact"]),
        "top_projects": [
            {"project": opaque(p), "handoff_count": a["handoff"],
             "compact_count": a["compact"],
             "brief_chars": span(a["brief_chars"]), "tokens_before": span(a["tokens_before"]),
             "tokens_after": span(a["tokens_after"]), "seq_interval": span(a["seq_gap"]),
             "message_interval": span(a["message_gap"]), "turn_interval": span(a["turn_gap"]),
             "compact_between": span(a["compact_between"]),
             "mixed_intervals": sum(1 for c in a["compact_between"] if c)}
            for p, a in ranked[:top_projects]],
    }


def dossier(path: Path, top: int = 10, projects: int = 20) -> dict:
    conn = connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out: dict = {"user_version": conn.execute("PRAGMA user_version").fetchone()[0]}
        if "records" not in tables:
            return {**out, "error": "no records table"}
        where, out["scope"], trash_col = _scope(conn, tables)
        if "sessions" in tables:
            out["sessions"] = conn.execute(
                "SELECT COUNT(*) FROM sessions" + (
                    f" WHERE {trash_col} IS NULL" if trash_col else ""
                )).fetchone()[0]
        else:
            out["sessions"] = 0
        out["records"], out["record_bytes"] = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(length(r.body)),0) FROM {_RECORDS} "
            f"WHERE {where}").fetchone()
        out["largest_logs"] = largest_logs(conn, where, top)
        out["handoffs"] = handoff_stats(conn, where, projects)
        return out
    finally:
        conn.close()


def _bounded(value: str) -> int:
    n = int(value)
    if not 1 <= n <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_LIMIT}")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database", type=Path, default=default_database())
    ap.add_argument("--top", type=_bounded, default=10, help="largest logs to list (1-1000)")
    ap.add_argument("--projects", type=_bounded, default=20,
                    help="max projects in handoff aggregate (1-1000)")
    args = ap.parse_args(argv)
    if not args.database.is_file():
        print("database not found", file=sys.stderr)
        return 1
    try:
        report = dossier(args.database, args.top, args.projects)
    except sqlite3.Error as exc:
        print(f"database error: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
