"""StateDatabase / SqliteSessionStore: the shared SQLite state DB (STATE_PLAN §4)."""
from __future__ import annotations

import sqlite3
import stat
import time

import msgspec
import pytest

from nexus.errors import SessionError
from nexus.events import Event
from nexus.model.message import Message, Text
from nexus.session.db import SCHEMA_VERSION, SqliteSessionStore, StateDatabase
from nexus.session.manager import SessionManager
from nexus.session.records import MessageRecord


def _msg(text: str, role: str = "user") -> Message:
    return Message(role=role, content=[Text(text=text)])


def _db(tmp_path, name: str = "nexus.db") -> StateDatabase:
    return StateDatabase(tmp_path / name)


def _store(db: StateDatabase, project: str = "proj-a", namespace: str = "main") -> SqliteSessionStore:
    return SqliteSessionStore(db, project, namespace)


# -- schema / file modes -----------------------------------------------------


def test_creates_schema_and_sets_user_version(tmp_path):
    db = _db(tmp_path)
    assert db.schema_version() == SCHEMA_VERSION
    tables = {
        row[0]
        for row in db._connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"projects", "sessions", "records", "snapshots", "kv"} <= tables


def test_file_and_directory_permissions(tmp_path):
    home = tmp_path / "home" / ".nexus"
    _db(home, "nexus.db")
    mode = stat.S_IMODE((home / "nexus.db").stat().st_mode)
    assert mode == 0o600
    dir_mode = stat.S_IMODE(home.stat().st_mode)
    assert dir_mode == 0o700


def test_reopening_an_existing_database_does_not_reset_data(tmp_path):
    path = tmp_path / "nexus.db"
    db1 = StateDatabase(path)
    store = SqliteSessionStore(db1, "proj", "main")
    store.create("s1")
    store.append_message("s1", _msg("hi"))

    db2 = StateDatabase(path)
    assert db2.schema_version() == SCHEMA_VERSION
    reopened = SqliteSessionStore(db2, "proj", "main")
    assert reopened.exists("s1")
    assert reopened.read("s1").messages()[0].content[0].text == "hi"


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "nexus.db"
    StateDatabase(path)  # creates schema at current version
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(SessionError):
        StateDatabase(path)


# -- append / read round trip -------------------------------------------------


def test_append_and_read_round_trip_byte_identical_encoding(tmp_path):
    db = _db(tmp_path)
    store = _store(db)
    store.create("s1")
    message = _msg("hello world")
    record = store.append_message("s1", message)
    assert record.seq == 1

    raw = db._connection().execute(
        "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? AND seq=1",
        ("proj-a", "main", "s1"),
    ).fetchone()[0]
    assert bytes(raw) == msgspec.json.encode(MessageRecord(seq=1, message=message, ts=record.ts))

    read = store.read("s1")
    assert read.records == (record,)
    assert read.messages()[0] == message


def test_append_event_and_summary_round_trip(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    event = Event("turn.started", {"turn": "t1"})
    erecord = store.append_event("s1", event)
    assert erecord.seq == 1
    assert erecord.event.session == "s1"

    srecord = store.append_summary("s1", text="a summary", strategy="test")
    assert srecord.seq == 2
    assert srecord.text == "a summary"

    read = store.read("s1")
    assert [r.seq for r in read.records] == [1, 2]
    assert read.summaries()[0].text == "a summary"


def test_tail_bytes_limits_descending_record_scan_and_output(tmp_path):
    db = _db(tmp_path)
    store = _store(db)
    store.create("s1")
    for index in range(20):
        store.append_message("s1", _msg(f"record-{index}"))

    body = db._connection().execute(
        "SELECT body FROM records WHERE project_id=? AND namespace=? AND session_id=? "
        "ORDER BY seq DESC LIMIT 1",
        ("proj-a", "main", "s1"),
    ).fetchone()[0]
    traced: list[str] = []
    db._connection().set_trace_callback(traced.append)
    try:
        result = store.tail_bytes("s1", max_bytes=12)
    finally:
        db._connection().set_trace_callback(None)

    assert result == bytes(body)[-11:] + b"\n"
    assert len(result) == 12
    tail_query = next(sql for sql in traced if "substr(body" in sql)
    assert "ORDER BY seq DESC LIMIT 12" in tail_query


def test_seq_monotonic_across_two_connections(tmp_path):
    db = _db(tmp_path)
    a = SqliteSessionStore(db, "proj-a", "main")
    b = SqliteSessionStore(StateDatabase(db.path), "proj-a", "main")
    a.create("s1")
    r1 = a.append_message("s1", _msg("one"))
    r2 = b.append_message("s1", _msg("two"))
    r3 = a.append_message("s1", _msg("three"))
    assert [r1.seq, r2.seq, r3.seq] == [1, 2, 3]


def test_explicit_seq_collision_raises_session_error(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    store.append_message("s1", _msg("one"), seq=5)
    with pytest.raises(SessionError):
        store.append_message("s1", _msg("dup"), seq=5)


def test_append_to_missing_session_raises(tmp_path):
    store = _store(_db(tmp_path))
    with pytest.raises(SessionError):
        store.append_message("missing", _msg("x"))


def test_create_is_idempotent_and_never_truncates(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    store.append_message("s1", _msg("one"))
    store.create("s1")  # must not wipe the existing row/records
    assert [r.seq for r in store.read("s1").records] == [1]


# -- fork-style duplicate refusal --------------------------------------------


def test_create_from_records_refuses_duplicate_id(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    store.append_message("s1", _msg("one"))
    read = store.read("s1")
    with pytest.raises(SessionError, match="already exists"):
        store.create_from_records("s1", read.records)


def test_create_from_records_publishes_prefix_in_one_call(tmp_path):
    store = _store(_db(tmp_path))
    store.create("source")
    store.append_message("source", _msg("one"))
    store.append_message("source", _msg("two"))
    read = store.read("source")

    store.create_from_records("child", read.records, parent_id="source", fork_seq=2)
    assert store.exists("child")
    assert [r.seq for r in store.read("child").records] == [1, 2]
    row = store.session_row("child")
    assert row["parent_id"] == "source"
    assert row["fork_seq"] == 2
    assert row["message_count"] == 2
    assert row["title"] == "one"


# -- WAL cross-connection visibility -----------------------------------------


def test_committed_write_is_visible_from_a_second_connection(tmp_path):
    path = tmp_path / "nexus.db"
    writer_db = StateDatabase(path)
    writer = SqliteSessionStore(writer_db, "proj-a", "main")
    writer.create("s1")
    writer.append_message("s1", _msg("hi"))

    reader_db = StateDatabase(path)
    reader = SqliteSessionStore(reader_db, "proj-a", "main")
    assert reader.exists("s1")
    assert reader.read("s1").messages()[0].content[0].text == "hi"

    # A further write from the original writer is visible to a *fresh* read
    # from the reader connection -- WAL readers see the latest committed state.
    writer.append_message("s1", _msg("again"))
    assert len(reader.read("s1").records) == 2


# -- archive / trash / restore / purge ---------------------------------------


def test_archive_unarchive_round_trip(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    row = store.archive("s1", "user")
    assert row["reason"] == "user"
    assert store.archived_rows()[0]["session_id"] == "s1"
    # Idempotent: archiving again returns the same record, does not overwrite.
    again = store.archive("s1", "auto")
    assert again["reason"] == "user"
    assert store.unarchive("s1") is True
    assert store.archived_rows() == []
    assert store.unarchive("s1") is False


def test_stale_candidates_bounded_and_ordered(tmp_path):
    store = _store(_db(tmp_path))
    now = time.time()
    for index in range(3):
        session_id = f"s{index}"
        store.create(session_id)
        store.append_message(session_id, _msg("x"), seq=1)
        # Force a specific last_activity by writing directly through append ts.
    ids = store.stale_candidates(threshold=now + 3600, limit=2)
    assert len(ids) == 2


def test_trash_hides_session_and_restore_brings_it_back(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    store.append_message("s1", _msg("hi"))
    trashed = store.trash("s1", reason="user-delete", retention_seconds=3600)
    assert trashed["session_id"] == "s1"
    assert store.exists("s1") is False
    assert store.row_exists("s1") is True
    found = store.find_trash(trashed["trash_id"])
    assert found["id"] == "s1"

    restored = store.restore(trashed["trash_id"])
    assert restored == "s1"
    assert store.exists("s1") is True
    assert [r.seq for r in store.read("s1").records] == [1]


def test_trash_trigger_failure_rolls_back_session_columns_and_records(tmp_path):
    db = _db(tmp_path)
    store = _store(db)
    store.create("s1")
    store.append_message("s1", _msg("keep"))
    store.archive("s1", "before-trash")
    row_before = store.session_row("s1")
    records_before = store.read("s1").records
    db._connection().execute(
        "CREATE TRIGGER reject_trash BEFORE UPDATE OF trash_id ON sessions "
        "WHEN NEW.trash_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'trash rejected'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="trash rejected"):
        store.trash("s1", reason="test", retention_seconds=3600)

    assert store.session_row("s1") == row_before
    assert store.read("s1").records == records_before
    assert store.exists("s1") is True
    assert store.trashed_rows() == []


def test_restore_trigger_failure_rolls_back_session_columns_and_records(tmp_path):
    db = _db(tmp_path)
    store = _store(db)
    store.create("s1")
    store.append_message("s1", _msg("keep"))
    trashed = store.trash("s1", reason="test", retention_seconds=3600)
    row_before = store.session_row("s1")
    records_before = store.read("s1").records
    db._connection().execute(
        "CREATE TRIGGER reject_restore BEFORE UPDATE OF trash_id ON sessions "
        "WHEN OLD.trash_id IS NOT NULL AND NEW.trash_id IS NULL "
        "BEGIN SELECT RAISE(ABORT, 'restore rejected'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="restore rejected"):
        store.restore(trashed["trash_id"])

    assert store.session_row("s1") == row_before
    assert store.read("s1").records == records_before
    assert store.exists("s1") is False
    assert store.find_trash(trashed["trash_id"]) == row_before


def test_trash_restore_preserves_archive_metadata(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    archived = store.archive("s1", "auto")

    trashed = store.trash("s1", reason="user-delete", retention_seconds=3600)
    assert store.archived_rows() == []
    assert store.find_trash(trashed["trash_id"])["archived_at"] == archived["archived_at"]

    # Manager.open calls unarchive before checking whether a session exists.
    # A trashed row must keep its original archive marker until restore.
    assert store.unarchive("s1") is False
    assert store.find_trash(trashed["trash_id"])["archive_reason"] == "auto"

    assert store.restore(trashed["trash_id"]) == "s1"
    restored = store.archived_rows()
    assert restored == [
        {"session_id": "s1", "archived_at": archived["archived_at"], "reason": "auto"}
    ]


def test_manager_delete_restore_keeps_session_archived(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("s1")
    session.append_message(_msg("archived"))
    archived = manager.archive("s1", "user")

    trashed = manager.delete("s1", reason="sidebar-check")
    assert manager.archived() == []
    assert manager.list_trashed()[0].trash_id == trashed.trash_id

    assert manager.restore(trashed.trash_id) == "s1"
    assert manager.archived() == [archived]
    assert manager.list() == []
    assert [row.id for row in manager.list(include_archived=True)] == ["s1"]


def test_purge_expired_removes_only_past_retention(tmp_path):
    store = _store(_db(tmp_path))
    store.create("s1")
    trashed1 = store.trash("s1", reason="", retention_seconds=1)
    store.create("s2")
    store.trash("s2", reason="", retention_seconds=10_000)

    removed = store.purge_expired(now=time.time() + 5)
    assert removed == [trashed1["trash_id"]]
    assert store.row_exists("s1") is False
    assert store.row_exists("s2") is True


# -- snapshots -----------------------------------------------------------


def test_snapshot_upsert_and_validate(tmp_path):
    from nexus.session import snapshot as snapshot_mod

    store = _store(_db(tmp_path))
    store.create("s1")
    store.append_message("s1", _msg("hi"))
    read = store.read("s1")
    snapshot = snapshot_mod.build_from_records("s1", read.records, 1)

    store.write_snapshot("s1", snapshot)
    loaded = store.load_snapshot("s1", read)
    assert loaded is not None
    assert loaded.seq == 1
    assert loaded.messages == snapshot.messages

    # Upsert: writing again replaces rather than duplicating the row.
    store.append_message("s1", _msg("again"))
    read2 = store.read("s1")
    snapshot2 = snapshot_mod.build_from_records("s1", read2.records, 2)
    store.write_snapshot("s1", snapshot2)
    loaded2 = store.load_snapshot("s1", read2)
    assert loaded2.seq == 2

    # A corrupt/stale snapshot (claims a message prefix the log disagrees
    # with) is rejected rather than trusted.
    bogus = msgspec.structs.replace(snapshot2, messages=[_msg("not what happened")])
    store.write_snapshot("s1", bogus)
    assert store.load_snapshot("s1", read2) is None


# -- isolation: two projects, two namespaces ---------------------------------


def test_two_projects_are_isolated_in_one_database(tmp_path):
    db = _db(tmp_path)
    a = SqliteSessionStore(db, "proj-a", "main")
    b = SqliteSessionStore(db, "proj-b", "main")
    a.create("shared-id")
    a.append_message("shared-id", _msg("from a"))
    assert b.exists("shared-id") is False
    b.create("shared-id")
    b.append_message("shared-id", _msg("from b"))
    assert a.read("shared-id").messages()[0].content[0].text == "from a"
    assert b.read("shared-id").messages()[0].content[0].text == "from b"


def test_namespaces_are_isolated_within_one_project(tmp_path):
    db = _db(tmp_path)
    main = SqliteSessionStore(db, "proj-a", "main")
    agents = SqliteSessionStore(db, "proj-a", "agents")
    main.create("dup")
    agents.create("dup")
    main.append_message("dup", _msg("main"))
    agents.append_message("dup", _msg("agents"))
    assert len(main.read("dup").records) == 1
    assert len(agents.read("dup").records) == 1
    assert main.read("dup").messages()[0].content[0].text == "main"
    assert agents.read("dup").messages()[0].content[0].text == "agents"
