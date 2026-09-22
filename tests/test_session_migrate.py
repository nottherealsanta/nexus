import json

import pytest

from nexus.errors import SessionError
from nexus.model.message import Message, Text
from nexus.session.manager import SessionManager
from nexus.session.migrate import (
    backup_path,
    jsonl_path,
    legacy_path,
    migrate_session,
    should_migrate,
)
from nexus.session.store import SessionStore


def _write_v1(directory, session, exchanges, *, version=1):
    payload = {"version": version, "exchanges": exchanges}
    path = legacy_path(directory, session)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_real_shape_v1_migrates_to_alternating_messages(tmp_path):
    original = _write_v1(
        tmp_path,
        "main",
        [
            {"user": "hello", "assistant": "hi there"},
            {"user": "second", "assistant": "reply"},
        ],
    )
    original_bytes = original.read_bytes()

    result = migrate_session(tmp_path, "main")
    assert result.migrated is True
    assert result.messages == 4
    assert result.backup_path == backup_path(tmp_path, "main")
    assert result.backup_path.read_bytes() == original_bytes
    assert original.exists()  # legacy file is preserved, not deleted

    records = SessionStore(tmp_path).read("main")
    assert records.next_seq == 4
    assert records.messages() == [
        Message(role="user", content=[Text(text="hello")]),
        Message(role="assistant", content=[Text(text="hi there")]),
        Message(role="user", content=[Text(text="second")]),
        Message(role="assistant", content=[Text(text="reply")]),
    ]


def test_migration_is_idempotent_and_backup_immutable(tmp_path):
    _write_v1(tmp_path, "main", [{"user": "a", "assistant": "b"}])
    first = migrate_session(tmp_path, "main")
    log_after_first = jsonl_path(tmp_path, "main").read_bytes()
    backup_after_first = backup_path(tmp_path, "main").read_bytes()

    second = migrate_session(tmp_path, "main")
    assert second.migrated is False
    assert jsonl_path(tmp_path, "main").read_bytes() == log_after_first
    assert backup_path(tmp_path, "main").read_bytes() == backup_after_first
    assert first.migrated is True


def test_existing_valid_jsonl_is_never_overwritten(tmp_path):
    _write_v1(tmp_path, "main", [{"user": "a", "assistant": "b"}])
    log = jsonl_path(tmp_path, "main")
    log.write_bytes(b"sentinel\n")

    result = migrate_session(tmp_path, "main")
    assert result.migrated is False
    assert log.read_bytes() == b"sentinel\n"
    assert not backup_path(tmp_path, "main").exists()
    assert should_migrate(tmp_path, "main") is False


def test_existing_backup_is_not_overwritten(tmp_path):
    _write_v1(tmp_path, "main", [{"user": "a", "assistant": "b"}])
    backup = backup_path(tmp_path, "main")
    backup.write_bytes(b"older backup")
    migrate_session(tmp_path, "main")
    assert backup.read_bytes() == b"older backup"


def test_empty_exchanges_migrate_to_empty_log(tmp_path):
    _write_v1(tmp_path, "main", [])
    result = migrate_session(tmp_path, "main")
    assert result.migrated is True
    assert result.messages == 0
    assert jsonl_path(tmp_path, "main").read_bytes() == b""


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "exchanges": []},
        {"version": 1, "exchanges": "nope"},
        {"version": 1, "exchanges": [{"user": "a"}]},
        {"version": 1, "exchanges": [{"user": "a", "assistant": 3}]},
        {"version": 1, "exchanges": [{"user": "a", "assistant": "b", "extra": "c"}]},
        ["not", "a", "dict"],
    ],
)
def test_strict_validation_rejects_bad_documents(tmp_path, payload):
    legacy_path(tmp_path, "main").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionError):
        migrate_session(tmp_path, "main")
    assert not jsonl_path(tmp_path, "main").exists()
    assert not backup_path(tmp_path, "main").exists()


def test_invalid_json_rejected(tmp_path):
    legacy_path(tmp_path, "main").write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionError):
        migrate_session(tmp_path, "main")
    assert not jsonl_path(tmp_path, "main").exists()


def test_missing_legacy_and_present_log_is_a_noop(tmp_path):
    assert should_migrate(tmp_path, "main") is False
    result = migrate_session(tmp_path, "main")
    assert result.migrated is False
    assert not jsonl_path(tmp_path, "main").exists()


def test_manager_open_migrates_legacy_session(tmp_path):
    _write_v1(tmp_path, "legacy", [{"user": "old", "assistant": "answer"}])
    manager = SessionManager(tmp_path)
    session = manager.open("legacy")
    assert [m.content[0].text for m in session.messages] == ["old", "answer"]
    assert jsonl_path(tmp_path, "legacy").exists()
    assert backup_path(tmp_path, "legacy").exists()


def test_publish_reports_lost_race_without_overwriting(tmp_path):
    from nexus.session import migrate as migrate_module

    log = migrate_module.jsonl_path(tmp_path, "main")
    log.write_bytes(b"winner\n")
    records = migrate_module._build_records(
        [Message(role="user", content=[Text(text="x")])]
    )
    assert migrate_module._publish(log, records) is False
    assert log.read_bytes() == b"winner\n"

