"""Legacy ``.nexus/sessions``/``.nexus/trash`` -> state database import (STATE_PLAN §5.1)."""
from __future__ import annotations

import json
import time

import msgspec
import pytest

from nexus.config.paths import project_key
from nexus.model.message import Message, Text
from nexus.session import snapshot as snapshot_mod
from nexus.session.db import SqliteSessionStore, StateDatabase
from nexus.session.import_legacy import import_workspace_sessions
from nexus.session.store import JsonlSessionStore


def _msg(text: str, role: str = "user") -> Message:
    return Message(role=role, content=[Text(text=text)])


def _workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    return workspace


def test_no_legacy_dir_marks_project_imported_without_scanning(tmp_path):
    workspace = _workspace(tmp_path)
    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)

    result = import_workspace_sessions(db, project_id, workspace / ".nexus")
    assert result.ran is False
    assert result.imported == 0
    row = db.project_row(project_id)
    assert row is not None and row["legacy_imported_at"]


def test_imports_jsonl_sessions_and_renames_directory(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    legacy_store = JsonlSessionStore(sessions_dir)
    legacy_store.append_message("s1", _msg("hello"))
    legacy_store.append_message("s1", _msg("world", role="assistant"))

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    result = import_workspace_sessions(db, project_id, legacy)

    assert result.ran is True
    assert result.imported == 1
    assert not sessions_dir.exists()
    renamed = [p for p in legacy.iterdir() if p.name.startswith("sessions.imported-")]
    assert len(renamed) == 1
    assert (renamed[0] / "s1.jsonl").exists()  # never deleted, only renamed

    store = SqliteSessionStore(db, project_id, "main")
    assert store.exists("s1")
    messages = store.read("s1").messages()
    assert [m.content[0].text for m in messages] == ["hello", "world"]


def test_import_is_idempotent(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    JsonlSessionStore(legacy / "sessions").append_message("s1", _msg("hi"))
    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)

    first = import_workspace_sessions(db, project_id, legacy)
    assert first.ran is True and first.imported == 1

    second = import_workspace_sessions(db, project_id, legacy)
    assert second.ran is False

    store = SqliteSessionStore(db, project_id, "main")
    assert len(store.read("s1").records) == 1  # not double-appended


def test_imports_child_agent_sessions_into_agents_namespace(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    JsonlSessionStore(sessions_dir).append_message("main", _msg("parent"))
    JsonlSessionStore(sessions_dir / "agents").append_message("child-1", _msg("child work"))

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    result = import_workspace_sessions(db, project_id, legacy)
    assert result.imported == 2

    main_store = SqliteSessionStore(db, project_id, "main")
    agents_store = SqliteSessionStore(db, project_id, "agents")
    assert main_store.exists("main")
    assert not main_store.exists("child-1")
    assert agents_store.exists("child-1")
    assert not agents_store.exists("main")


def test_crash_tail_is_dropped_not_repaired_on_disk(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    JsonlSessionStore(sessions_dir).append_message("s1", _msg("good"))
    log_path = sessions_dir / "s1.jsonl"
    with open(log_path, "ab") as handle:
        handle.write(b'{"type": "message", "v": 1, "seq": 2, "broken"')  # no trailing newline, invalid JSON

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    result = import_workspace_sessions(db, project_id, legacy)
    assert result.imported == 1

    store = SqliteSessionStore(db, project_id, "main")
    records = store.read("s1").records
    assert len(records) == 1  # the crash tail never made it into the database


def test_corrupt_log_is_left_in_place_and_reported(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    sessions_dir.mkdir(parents=True)
    # A genuinely malformed (newline-terminated, still invalid) record is
    # interior corruption, not a crash tail: the store refuses to read it.
    (sessions_dir / "bad.jsonl").write_text('{"type": "message", "v": 1, "seq": 1, "nope"}\n')
    JsonlSessionStore(sessions_dir).append_message("good", _msg("fine"))

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    result = import_workspace_sessions(db, project_id, legacy)

    assert result.imported == 1
    assert "bad" in result.corrupt
    store = SqliteSessionStore(db, project_id, "main")
    assert store.exists("good")
    assert not store.exists("bad")
    # Never deleted: the corrupt log survives under the renamed directory.
    renamed = next(p for p in legacy.iterdir() if p.name.startswith("sessions.imported-"))
    assert (renamed / "bad.jsonl").exists()


def test_imports_trashed_session_with_historical_metadata(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    trash_entry = legacy / "trash" / "old-1-abcdef"
    JsonlSessionStore(trash_entry).append_message("old", _msg("trashed content"))
    meta = {
        "trash_id": "old-1-abcdef",
        "session_id": "old",
        "trashed_at": time.time() - 100,
        "delete_after": time.time() + 100,
        "files": ["old.jsonl"],
        "title": "trashed content",
        "last_seq": 1,
        "reason": "user",
        "v": 1,
    }
    (trash_entry / "meta.json").write_text(json.dumps(meta))

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    result = import_workspace_sessions(db, project_id, legacy)
    assert result.imported == 1

    store = SqliteSessionStore(db, project_id, "main")
    assert store.exists("old") is False  # trashed sessions stay hidden from `exists`
    found = store.find_trash("old-1-abcdef")
    assert found is not None
    assert found["id"] == "old"
    assert found["trash_reason"] == "user"
    restored = store.restore("old-1-abcdef")
    assert restored == "old"
    assert store.read("old").messages()[0].content[0].text == "trashed content"


def test_imports_archive_metadata_from_archive_json(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    JsonlSessionStore(sessions_dir).append_message("archived-one", _msg("old news"))
    archive_index = {"archived-one": {"archived_at": time.time() - 10, "reason": "auto"}}
    (sessions_dir / "archive.json").write_text(json.dumps(archive_index))

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    import_workspace_sessions(db, project_id, legacy)

    store = SqliteSessionStore(db, project_id, "main")
    rows = store.archived_rows()
    assert rows and rows[0]["session_id"] == "archived-one"
    assert rows[0]["reason"] == "auto"


def test_imports_snapshot_sidecar(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    JsonlSessionStore(sessions_dir).append_message("s1", _msg("hi"))
    read = JsonlSessionStore(sessions_dir).read("s1")
    snapshot = snapshot_mod.build_from_records("s1", read.records, 1)
    snapshot_mod.write(sessions_dir, "s1", snapshot)

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    import_workspace_sessions(db, project_id, legacy)

    store = SqliteSessionStore(db, project_id, "main")
    loaded = store.load_snapshot("s1", store.read("s1"))
    assert loaded is not None
    assert loaded.seq == 1


def test_concurrent_import_is_guarded_by_flock(tmp_path):
    """Two imports racing for the same project never double-import."""
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    JsonlSessionStore(legacy / "sessions").append_message("s1", _msg("hi"))
    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)

    results = [
        import_workspace_sessions(db, project_id, legacy, home=tmp_path / "home"),
        import_workspace_sessions(db, project_id, legacy, home=tmp_path / "home"),
    ]
    assert sum(1 for r in results if r.ran) == 1
    store = SqliteSessionStore(db, project_id, "main")
    assert len(store.read("s1").records) == 1


def test_migrates_v1_json_session_during_import(tmp_path):
    workspace = _workspace(tmp_path)
    legacy = workspace / ".nexus"
    sessions_dir = legacy / "sessions"
    sessions_dir.mkdir(parents=True)
    v1_doc = {"version": 1, "exchanges": [{"user": "hi there", "assistant": "hello!"}]}
    (sessions_dir / "legacy-v1.json").write_text(json.dumps(v1_doc))

    db = StateDatabase(tmp_path / "nexus.db")
    project_id = project_key(workspace)
    result = import_workspace_sessions(db, project_id, legacy)
    assert result.imported == 1

    store = SqliteSessionStore(db, project_id, "main")
    messages = store.read("legacy-v1").messages()
    assert [m.content[0].text for m in messages] == ["hi there", "hello!"]
