"""Doctor's largest-session-logs diagnostic is metadata-only and project scoped."""
from __future__ import annotations

import sqlite3

from nexus.host_support import doctor


class _Db:
    def __init__(self, conn: sqlite3.Connection, path) -> None:
        self.conn = conn
        self.path = path

    def _connection(self) -> sqlite3.Connection:
        return self.conn

    def schema_version(self) -> int:
        return 1

    def quick_check(self) -> str:
        return "ok"


class _Store:
    def __init__(self, db: _Db, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.namespace = "main"

    def tail_bytes(self, *_a, **_k) -> bytes:
        return b""


def _store(tmp_path) -> _Store:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE records (project_id TEXT, namespace TEXT, session_id TEXT, "
        "seq INTEGER, kind TEXT, ts REAL, body BLOB)"
    )
    return _Store(_Db(conn, tmp_path / "x.db"), "p1")


def _add(store: _Store, project: str, ns: str, sid: str, n: int, size: int) -> None:
    store.db.conn.executemany(
        "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
        [(project, ns, sid, i, "event", 0.0, b"x" * size) for i in range(n)],
    )


def test_largest_logs_top_ten_across_namespaces_project_scoped(tmp_path):
    doctor._heavy_cache.clear()
    store = _store(tmp_path)
    for i in range(12):
        _add(store, "p1", "main" if i % 2 else "agents", f"s{i:02d}", 2, 10 + i)
    _add(store, "other", "main", "foreign", 5, 10_000)

    report = doctor.database_diagnostics(store)["largest_session_logs"]

    assert report["scope"] == "project"
    rows = report["sessions"]
    assert len(rows) == 10
    assert [r["session"] for r in rows][:2] == ["s11", "s10"]
    assert rows[0] == {"namespace": "main", "session": "s11", "records": 2, "bytes": 42}
    assert {r["namespace"] for r in rows} == {"main", "agents"}
    assert "foreign" not in str(report)
    assert all(set(r) == {"namespace", "session", "records", "bytes"} for r in rows)


def test_largest_logs_unavailable_without_records_table(tmp_path):
    doctor._heavy_cache.clear()
    conn = sqlite3.connect(":memory:")
    store = _Store(_Db(conn, tmp_path / "x.db"), "p1")
    report = doctor.database_diagnostics(store)["largest_session_logs"]
    assert report["available"] is False
    assert report["sessions"] == []
