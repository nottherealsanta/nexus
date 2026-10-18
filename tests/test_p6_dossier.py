"""Offline tests for scripts/p6_dossier.py (read-only, metadata-only measurement)."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "p6_dossier.py"
_SPEC = importlib.util.spec_from_file_location("_p6_dossier_test_target", _PATH)
p6 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(p6)

SCHEMA = (
    "CREATE TABLE sessions(project_id,namespace,id);"
    "CREATE TABLE records(project_id,namespace,session_id,seq INTEGER,kind,ts,body BLOB);"
)


def _event(etype: str) -> bytes:
    return json.dumps({"seq": 0, "event": {"type": etype, "secret": "SECRET-EVENT"}}).encode()


def _handoff(**extra) -> bytes:
    return json.dumps({"strategy": "handoff", "text": "SECRET-BRIEF", **extra}).encode()


def _make(tmp_path: Path, rows: list[tuple]) -> Path:
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO sessions VALUES('p1','main','s1'),('p1','main','s2'),('p2','main','s3')")
    conn.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?)",
                     [(p, ns, s, seq, kind, 0.0, body) for p, ns, s, seq, kind, body in rows])
    conn.commit()
    conn.close()
    return db


def _project(out: dict) -> dict:
    return out["handoffs"]["top_projects"][0]


def test_turn_interval_counts_turn_started_between_handoffs(tmp_path):
    rows = [("p1", "main", "s1", 1, "event", _event("turn.started")),  # before first handoff
            ("p1", "main", "s1", 2, "summary", _handoff(tokens_before=100, tokens_after=10)),
            ("p1", "main", "s1", 3, "event", _event("turn.started")),
            ("p1", "main", "s1", 4, "message", b"{}"),
            ("p1", "main", "s1", 5, "message", b"{}"),
            ("p1", "main", "s1", 6, "message", b"{}"),
            ("p1", "main", "s1", 7, "event", _event("turn.completed")),
            ("p1", "main", "s1", 8, "event", _event("turn.started")),
            ("p1", "main", "s1", 9, "summary", _handoff(tokens_before=200, tokens_after=20)),
            ("p1", "main", "s1", 10, "event", _event("turn.started")),
            ("p1", "main", "s1", 11, "summary", _handoff()),
            # other session must not leak into s1's intervals
            ("p1", "main", "s2", 4, "event", _event("turn.started"))]
    out = p6.dossier(_make(tmp_path, rows))
    p = _project(out)
    assert p["handoff_count"] == 3
    assert p["turn_interval"] == {"n": 2, "min": 1, "median": 1.5, "max": 2}
    assert p["message_interval"] == {"n": 2, "min": 0, "median": 1.5, "max": 3}
    assert p["seq_interval"] == {"n": 2, "min": 2, "median": 4.5, "max": 7}
    assert p["tokens_before"] == {"n": 2, "min": 100, "median": 150, "max": 200}


def test_mixed_summaries_and_malformed_bodies_are_ignored(tmp_path):
    rows = [("p1", "main", "s1", 1, "summary", _handoff()),
            ("p1", "main", "s1", 2, "summary", b'{"strategy":"compact","text":"handoff?"}'),
            ("p1", "main", "s1", 3, "summary", b"not json handoff"),
            ("p1", "main", "s1", 4, "summary", b'["handoff"]'),
            ("p1", "main", "s1", 5, "event", b"{broken"),  # malformed event body
            ("p1", "main", "s1", 6, "event", b'{"event":{"type":["turn.started"]}}'),
            ("p1", "main", "s1", 7, "event", b'{"type":"turn.started"}'),  # wrong shape
            ("p1", "main", "s1", 8, "event", _event("turn.started")),
            ("p1", "main", "s1", 9, "summary", _handoff())]
    out = p6.dossier(_make(tmp_path, rows))
    h = out["handoffs"]
    assert h["total_handoffs"] == 2 and h["projects_with_handoffs"] == 1
    assert _project(out)["turn_interval"] == {"n": 1, "min": 1, "median": 1, "max": 1}


def test_no_handoffs_reports_zero(tmp_path):
    out = p6.dossier(_make(tmp_path, [("p1", "main", "s1", 1, "message", b"{}")]))
    assert out["handoffs"] == {"total_handoffs": 0, "total_compacts": 0,
                               "projects_with_handoffs": 0, "projects_with_compacts": 0,
                               "top_projects": []}


def _make_full(tmp_path: Path, rows: list[tuple], sessions: list[tuple]) -> Path:
    db = tmp_path / "full.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE sessions(project_id,namespace,id,parent_id TEXT NOT NULL DEFAULT '',"
        "fork_seq INTEGER NOT NULL DEFAULT 0,trashed_at REAL);"
        "CREATE TABLE records(project_id,namespace,session_id,seq INTEGER,kind,ts,body BLOB);")
    conn.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?)", sessions)
    conn.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?)",
                     [(p, ns, s, seq, kind, 0.0, body) for p, ns, s, seq, kind, body in rows])
    conn.commit()
    conn.close()
    return db


def test_fork_prefix_and_trashed_are_excluded(tmp_path):
    rows = [("p1", "main", "parent", 1, "summary", _handoff()),
            ("p1", "main", "parent", 2, "event", _event("turn.started")),
            ("p1", "main", "parent", 3, "summary", _handoff()),
            # child inherits seq 1..3 of the parent, then has its own 4..6
            ("p1", "main", "child", 1, "summary", _handoff()),
            ("p1", "main", "child", 2, "event", _event("turn.started")),
            ("p1", "main", "child", 3, "summary", _handoff()),
            ("p1", "main", "child", 4, "event", _event("turn.started")),
            ("p1", "main", "child", 5, "summary", _handoff()),
            ("p2", "main", "gone", 1, "summary", _handoff()),
            ("p2", "main", "gone", 2, "message", b"{}")]
    sessions = [("p1", "main", "parent", "", 0, None), ("p1", "main", "child", "parent", 3, None),
                ("p2", "main", "gone", "", 0, 5.0)]
    out = p6.dossier(_make_full(tmp_path, rows, sessions))
    assert out["scope"] == {"include_trashed": False, "trashed_filter_applied": True,
                            "fork_prefix_excluded": True}
    assert out["sessions"] == 2
    assert out["records"] == 5  # 3 parent + 2 child own (seq 4,5); trashed session dropped
    assert out["handoffs"]["total_handoffs"] == 3
    assert out["handoffs"]["projects_with_handoffs"] == 1
    assert _project(out)["handoff_count"] == 3


def _make_trash_id(tmp_path: Path, trash_cols: str, rows: list[tuple], sessions: list[tuple]) -> Path:
    db = tmp_path / "trash.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        f"CREATE TABLE sessions(project_id,namespace,id,{trash_cols});"
        "CREATE TABLE records(project_id,namespace,session_id,seq INTEGER,kind,ts,body BLOB);")
    marks = ",".join("?" * (3 + len(trash_cols.split(","))))
    conn.executemany(f"INSERT INTO sessions VALUES({marks})", sessions)
    conn.executemany("INSERT INTO records VALUES(?,?,?,?,?,?,?)",
                     [(p, ns, s, seq, kind, 0.0, body) for p, ns, s, seq, kind, body in rows])
    conn.commit()
    conn.close()
    return db


def test_trash_id_is_authoritative_over_trashed_at(tmp_path):
    rows = [("p1", "main", "live", 1, "summary", _handoff()),
            ("p1", "main", "gone", 1, "summary", _handoff()),
            ("p1", "main", "gone", 2, "message", b"{}")]
    # "gone" has a non-null trash_id but a null trashed_at: it must be excluded.
    sessions = [("p1", "main", "live", None, None), ("p1", "main", "gone", "t1", None)]
    out = p6.dossier(_make_trash_id(tmp_path, "trash_id,trashed_at", rows, sessions))
    assert out["scope"]["trashed_filter_applied"] is True
    assert out["sessions"] == 1
    assert out["records"] == 1
    assert out["handoffs"]["total_handoffs"] == 1


def test_trash_id_alone_is_used_without_trashed_at(tmp_path):
    rows = [("p1", "main", "live", 1, "message", b"{}"),
            ("p1", "main", "gone", 1, "message", b"{}")]
    sessions = [("p1", "main", "live", None), ("p1", "main", "gone", "t1")]
    out = p6.dossier(_make_trash_id(tmp_path, "trash_id", rows, sessions))
    assert (out["sessions"], out["records"]) == (1, 1)


def test_mixed_compact_and_handoff_intervals(tmp_path):
    rows = [("p1", "main", "s1", 1, "summary", _handoff()),
            ("p1", "main", "s1", 2, "summary", b'{"strategy":"compact","text":"X"}'),
            ("p1", "main", "s1", 3, "event", _event("turn.started")),
            ("p1", "main", "s1", 4, "summary", b'{"strategy":"compact"}'),
            ("p1", "main", "s1", 5, "summary", _handoff()),
            ("p1", "main", "s1", 6, "event", _event("turn.started")),
            ("p1", "main", "s1", 7, "summary", _handoff(tokens_before=True))]
    out = p6.dossier(_make(tmp_path, rows))
    p = _project(out)
    assert out["handoffs"]["total_compacts"] == 2
    assert (p["handoff_count"], p["compact_count"]) == (3, 2)
    assert p["compact_between"] == {"n": 2, "min": 0, "median": 1.0, "max": 2}
    assert p["mixed_intervals"] == 1
    assert p["turn_interval"] == {"n": 2, "min": 1, "median": 1.0, "max": 1}
    assert p["tokens_before"] is None  # bool is not a token count
    assert out["scope"]["trashed_filter_applied"] is False


def test_default_database_honors_nexus_home(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "h"))
    assert p6.default_database() == tmp_path / "h" / "nexus.db"
    monkeypatch.delenv("NEXUS_HOME")
    assert p6.default_database() == Path.home() / ".nexus" / "nexus.db"


def test_output_is_metadata_only_and_database_untouched(tmp_path, capsys):
    rows = [("p1", "main", "s1", 1, "summary", _handoff()),
            ("p1", "main", "s1", 2, "event", _event("turn.started")),
            ("p1", "main", "s1", 3, "summary", _handoff()),
            ("p2", "main", "s3", 1, "message", b"{}" * 500)]
    db = _make(tmp_path, rows)
    before = db.read_bytes()
    assert p6.main(["--database", str(db), "--top", "1"]) == 0
    text = capsys.readouterr().out
    assert db.read_bytes() == before
    assert "SECRET" not in text
    assert str(tmp_path) not in text
    out = json.loads(text)
    assert [log["session_id"] for log in out["largest_logs"]] == ["s3"]
    assert _project(out)["project"] == p6.opaque("p1")
    assert "p1" not in _project(out)["project"]


def test_connection_is_read_only(tmp_path):
    db = _make(tmp_path, [])
    conn = p6.connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO sessions VALUES('x','x','x')")
    finally:
        conn.close()


def test_missing_records_table(tmp_path):
    db = tmp_path / "e.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other(x)")
    conn.commit()
    conn.close()
    assert p6.dossier(db)["error"] == "no records table"


def test_cli_bounds_and_missing_database(tmp_path, capsys):
    db = _make(tmp_path, [])
    for flag in ("--top", "--projects"):
        for bad in ("0", "1001", "x"):
            with pytest.raises(SystemExit) as exc:
                p6.main(["--database", str(db), flag, bad])
            assert exc.value.code == 2
    capsys.readouterr()
    assert p6.main(["--database", str(tmp_path / "missing.db")]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and str(tmp_path) not in captured.err


def test_corrupt_database_exits_cleanly(tmp_path, capsys):
    db = tmp_path / "bad.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert p6.main(["--database", str(db)]) == 1
    assert str(tmp_path) not in capsys.readouterr().err
