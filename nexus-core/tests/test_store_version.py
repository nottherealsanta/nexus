import sqlite3

import pytest

import nexus.store as store_module
from nexus.llmhub.types import Message, Text
from nexus.store import SCHEMA_VERSION, Store, StoreError


def _legacy(path, version=0):
    """A database from an earlier schema: one table, one row."""
    db = sqlite3.connect(path)
    db.execute("pragma journal_mode = wal")
    db.execute("create table session (id text primary key, title text)")
    db.execute("insert into session values ('old', 'kept')")
    db.execute(f"pragma user_version = {version}")
    db.commit()
    return db


def _backups(tmp_path):
    return sorted(p for p in tmp_path.iterdir() if p.name.endswith(".bak"))


def test_a_new_database_is_versioned_and_not_backed_up(tmp_path):
    store = Store(tmp_path / "nexus.db")
    assert store.backup is None
    assert store.db.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()
    assert _backups(tmp_path) == []


def test_current_database_is_reopened_untouched(tmp_path):
    path = tmp_path / "nexus.db"
    store = Store(path)
    project, _ = store.project_for_path(str(tmp_path))
    session = store.create_session(project, title="mine", agent="build")
    store.append_message(session.id, Message("user", (Text("hi"),)))
    store.close()

    store = Store(path)
    assert store.backup is None and _backups(tmp_path) == []
    assert [m.message.parts[0].text for m in store.messages(session.id)] == ["hi"]


def test_an_earlier_schema_is_backed_up_and_replaced_by_a_new_database(tmp_path):
    version = 0
    path = tmp_path / "nexus.db"
    live = _legacy(path, version)  # left open with its WAL, as a crashed daemon would
    live.execute("insert into session values ('late', 'in the wal')")
    live.commit()

    store = Store(path)
    assert store.backup is not None and store.backup.parent == tmp_path
    assert f".v{version}-" in store.backup.name
    assert store.db.execute("pragma user_version").fetchone()[0] == SCHEMA_VERSION
    assert store.projects() == []
    live.close()

    kept = sqlite3.connect(store.backup)
    assert kept.execute("select id from session order by id").fetchall() == [("late",), ("old",)]
    kept.close()
    store.close()
    assert _backups(tmp_path) == [store.backup]


def test_an_unreadable_file_is_backed_up_byte_for_byte(tmp_path):
    path = tmp_path / "nexus.db"
    path.write_bytes(b"not a database" * 100)
    store = Store(path)
    assert store.backup is not None and ".vunreadable-" in store.backup.name
    assert store.backup.read_bytes() == b"not a database" * 100
    assert store.projects() == []


def test_an_empty_file_is_replaced_without_a_backup(tmp_path):
    path = tmp_path / "nexus.db"
    path.touch()
    store = Store(path)
    assert store.backup is None and _backups(tmp_path) == []
    assert store.projects() == []


def test_each_replacement_keeps_its_own_backup(tmp_path):
    path = tmp_path / "nexus.db"
    for _ in range(2):
        path.unlink(missing_ok=True)
        _legacy(path).close()
        Store(path).close()
    assert len(_backups(tmp_path)) == 2


def test_a_newer_database_is_refused_and_left_alone(tmp_path):
    path = tmp_path / "nexus.db"
    _legacy(path, SCHEMA_VERSION + 1).close()
    before = path.read_bytes()
    with pytest.raises(StoreError, match="schema version"):
        Store(path)
    assert path.read_bytes() == before and _backups(tmp_path) == []


def test_a_failed_build_leaves_the_old_database_in_place(tmp_path, monkeypatch):
    path = tmp_path / "nexus.db"
    _legacy(path).close()
    before = path.read_bytes()
    monkeypatch.setattr(store_module, "SCHEMA", "create table broken (")
    with pytest.raises(sqlite3.Error):
        Store(path)
    assert path.read_bytes() == before
    assert _backups(tmp_path) == []
    assert not (tmp_path / "nexus.db.new").exists()


def test_a_failed_swap_restores_the_old_database(tmp_path, monkeypatch):
    path = tmp_path / "nexus.db"
    _legacy(path).close()
    before = path.read_bytes()
    real = store_module.os.replace

    def replace(source, target):
        if str(source).endswith(".new"):  # the final step: new database into place
            raise OSError("disk full")
        return real(source, target)

    monkeypatch.setattr(store_module.os, "replace", replace)
    with pytest.raises(OSError, match="disk full"):
        Store(path)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert _backups(tmp_path) == []
    assert not (tmp_path / "nexus.db.new").exists()
