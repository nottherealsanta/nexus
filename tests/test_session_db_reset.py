"""Schema 10 is a reset version: old databases are backed up, never migrated."""
from __future__ import annotations

import io
import sqlite3
import stat

import pytest

from nexus.errors import SessionError
from nexus.host import protocol as p
from nexus.host_support.doctor import database_diagnostics
from nexus.model.message import Message, Text
from nexus.session import reset as reset_mod
from nexus.session.db import SCHEMA_VERSION, SqliteSessionStore, StateDatabase


def _old_db(tmp_path, version: int = 9, messages: int = 3):
    """A populated database stamped with an old ``user_version``."""
    path = tmp_path / "nexus.db"
    db = StateDatabase(path)
    store = SqliteSessionStore(db, "proj", "main")
    store.create("s1")
    for index in range(messages):
        store.append_message("s1", Message(role="user", content=[Text(text=f"m{index}")]))
    count = db._connection().execute("SELECT COUNT(*) FROM records").fetchone()[0]
    db.close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    return path, count


def _meta(path):
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def test_schema_version_is_a_reset_version():
    assert SCHEMA_VERSION == 10


def test_old_database_is_backed_up_then_replaced(tmp_path):
    path, count = _old_db(tmp_path)
    db = StateDatabase(path)
    assert db.schema_version() == SCHEMA_VERSION
    assert db._connection().execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
    backups = list((tmp_path / "backups").glob("nexus-v9-*Z.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == count > 0
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    conn.close()


def test_backup_modes_and_meta_entry(tmp_path):
    path, _ = _old_db(tmp_path, version=5)
    StateDatabase(path)
    directory = tmp_path / "backups"
    (backup,) = directory.iterdir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    meta = _meta(path)
    assert meta["reset.backup_path"] == str(backup)
    assert meta["reset.old_version"] == "5"
    assert meta["reset.at"].endswith("Z") and meta["reset.notice_shown"] == "0"


def test_current_database_opens_without_backup(tmp_path):
    path = tmp_path / "nexus.db"
    StateDatabase(path).close()
    StateDatabase(path).close()
    assert not (tmp_path / "backups").exists()
    assert "reset.backup_path" not in _meta(path)


def test_fresh_database_is_created_at_current_version_without_backup(tmp_path):
    db = StateDatabase(tmp_path / "nexus.db")
    assert db.schema_version() == SCHEMA_VERSION
    assert not (tmp_path / "backups").exists()


def test_newer_database_is_refused_untouched(tmp_path):
    path = tmp_path / "nexus.db"
    StateDatabase(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(SessionError):
        StateDatabase(path)
    assert not (tmp_path / "backups").exists()


def test_failed_verification_keeps_original_and_removes_partial_backup(tmp_path, monkeypatch):
    path, count = _old_db(tmp_path)
    monkeypatch.setattr(reset_mod, "_records_count", lambda conn: id(conn) % 1000)  # mismatch
    with pytest.raises(SessionError):
        StateDatabase(path)
    assert list((tmp_path / "backups").iterdir()) == []
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == count
    conn.close()


def test_backup_error_keeps_original(tmp_path, monkeypatch):
    path, count = _old_db(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(reset_mod, "_copy_and_verify", boom)
    with pytest.raises(SessionError, match="nothing was changed"):
        StateDatabase(path)
    assert list((tmp_path / "backups").iterdir()) == []
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == count
    conn.close()


def test_open_handle_blocks_reset(tmp_path, monkeypatch):
    path, count = _old_db(tmp_path)
    monkeypatch.setattr(reset_mod, "OPEN_HANDLE_TIMEOUT_SECONDS", 0.2)
    other = sqlite3.connect(path)  # e.g. an old daemon
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("SELECT COUNT(*) FROM records").fetchall()
    try:
        with pytest.raises(SessionError, match="nexus daemon stop"):
            StateDatabase(path)
        assert not (tmp_path / "backups").exists()
        assert other.execute("SELECT COUNT(*) FROM records").fetchone()[0] == count
    finally:
        other.close()
    StateDatabase(path)  # once released, the reset proceeds
    assert len(list((tmp_path / "backups").iterdir())) == 1


def test_backup_name_never_overwrites(tmp_path, monkeypatch):
    path, _ = _old_db(tmp_path)
    (tmp_path / "backups").mkdir()

    class _Now:
        @staticmethod
        def now(_tz):
            import datetime
            return datetime.datetime(2026, 10, 24, 1, 2, 3, tzinfo=datetime.timezone.utc)

    monkeypatch.setattr(reset_mod, "datetime", _Now)
    taken = tmp_path / "backups" / "nexus-v9-20261024T010203Z.db"
    taken.write_bytes(b"keep me")
    reset_mod.backup_and_remove(path, 9)
    assert taken.read_bytes() == b"keep me"
    assert (tmp_path / "backups" / "nexus-v9-20261024T010203Z-1.db").exists()


def test_doctor_reports_reset_backup(tmp_path):
    path, _ = _old_db(tmp_path)
    db = StateDatabase(path)
    store = SqliteSessionStore(db, "proj", "main")

    class _Sessions:
        pass

    sessions = _Sessions()
    sessions.db = db
    sessions.project_id = "proj"
    sessions.namespace = "main"
    sessions.unattached_count = store.unattached_count
    from nexus.host_support import doctor as doctor_mod
    monkey = doctor_mod._is_sqlite_store
    doctor_mod._is_sqlite_store = lambda _s: True
    try:
        report = database_diagnostics(sessions)
    finally:
        doctor_mod._is_sqlite_store = monkey
    backup = report["reset_backup"]
    assert backup["old_schema_version"] == 9 and backup["size_bytes"] > 0
    assert backup["path"].endswith(".db")

    from nexus import cli
    out = io.StringIO()
    cli._print_doctor({"workspace": "/w", "database": report}, out)
    assert "reset backup: path=" in out.getvalue() and "old_schema=9" in out.getvalue()


def test_notice_is_shown_exactly_once(tmp_path):
    path, _ = _old_db(tmp_path)
    StateDatabase(path)
    first = reset_mod.claim_notice(path)
    assert first.startswith("Nexus started a new session database. Your old sessions are backed up at ")
    assert str(tmp_path / "backups") in first
    assert reset_mod.claim_notice(path) == ""
    assert _meta(path)["reset.notice_shown"] == "1"


def test_no_notice_without_reset(tmp_path):
    path = tmp_path / "nexus.db"
    StateDatabase(path).close()
    assert reset_mod.claim_notice(path) == ""


def test_database_notice_command_is_in_the_protocol():
    assert p.DatabaseNotice().claim is True
    assert p.DatabaseNoticeResult().message == ""
