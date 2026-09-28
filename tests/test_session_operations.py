"""Phase 8a0 session operations: list, delete/trash/restore, export, fork/replay.

The manager owns the read-only projection (:class:`SessionSummary`), the atomic
trash-and-retain delete path, and structured export. These tests pin the
transport-neutral summary contract, the "delete never cancels a turn" rule,
trash retention metadata and crash recovery, export fidelity across JSON /
Markdown / JSONL, and that fork/replay stay consistent with the new surfaces.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import msgspec
import pytest

from nexus.core.loop import ResolvedModel
from nexus.errors import SessionBusy, SessionError
from nexus.events import Event
from nexus.model.message import (
    Image,
    Message,
    MessageMeta,
    Text,
    ToolResult,
    ToolUse,
)
from nexus.model.providers.scripted import (
    ScriptedProvider,
    Wait,
    text_response,
)
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.session import SessionManager, SessionSummary, TrashRecord
from nexus.session import export as export_mod
from nexus.session.lock import SessionLock, TrashLock


def _manager(tmp_path, **kwargs) -> SessionManager:
    return SessionManager(
        tmp_path / "sessions", trash_dir=tmp_path / "trash", **kwargs
    )


def _msg(text, role="user", *, ts=None):
    return Message(role=role, content=[Text(text=text)], meta=MessageMeta(ts=ts))


class _Assembler:
    def assemble(self, session):
        return ModelRequest(
            messages=list(session.messages), provider="scripted", model="m"
        )


class _Resolver:
    def __init__(self, provider):
        self.provider = provider

    def resolve(self, request):
        return ResolvedModel(self.provider, "m", self.provider.capabilities("m"))


def _bind(session, provider):
    session.bind(assemble=_Assembler(), provider_for=_Resolver(provider))
    return session


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _wait_for(path: Path, proc: subprocess.Popen, *, timeout: float = 30.0) -> None:
    """Block until a subprocess touches ``path``, failing if it dies first."""
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            _out, err = proc.communicate()
            raise AssertionError(f"producer exited early rc={proc.returncode}: {err!r}")
        if time.monotonic() > deadline:
            proc.kill()
            proc.communicate()
            raise AssertionError("producer never reached the paused point")
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# SessionSummary contract
# ---------------------------------------------------------------------------


def test_session_summary_is_transport_neutral(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("hello there", ts=10.0))
    session.append_message(_msg("reply", role="assistant", ts=11.0))

    summary = manager.summary("s")
    assert isinstance(summary, SessionSummary)
    assert summary.id == "s"
    assert summary.title == "hello there"
    assert summary.state == "idle"
    assert summary.last_activity == 11.0
    assert summary.last_seq == 2
    assert summary.viewers == 0
    # Only the documented fields cross the boundary, so nothing else can leak.
    assert set(summary.to_dict()) == {
        "id",
        "title",
        "state",
        "last_activity",
        "last_seq",
        "viewers",
        "message_count",
        "created_at",
        "parent_id",
        "fork_seq",
    }
    # msgspec round-trips the frozen struct exactly.
    encoded = msgspec.json.encode(summary)
    assert msgspec.json.decode(encoded, type=SessionSummary) == summary
    assert b"secret" not in encoded


def test_list_is_ordered_by_recent_activity(tmp_path):
    manager = _manager(tmp_path)
    older = manager.open("older")
    older.append_message(_msg("first", ts=100.0))
    newer = manager.open("newer")
    newer.append_message(_msg("second", ts=200.0))

    listed = manager.list()
    assert [item.id for item in listed] == ["newer", "older"]
    assert listed[0].title == "second"
    assert listed[0].last_seq == 1


def test_title_collapses_whitespace_and_truncates(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    long_text = "line one\n\n  line two " + "x" * 200
    session.append_message(_msg(long_text))
    title = manager.summary("s").title
    assert "\n" not in title
    assert title.startswith("line one line two")
    assert len(title) == export_mod.TITLE_LIMIT


def test_title_skips_tool_result_blocks(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(
        Message(
            role="user",
            content=[
                ToolResult(tool_use_id="t1", content=[Text(text="tool output")]),
                Text(text="real prompt"),
            ],
        )
    )
    assert manager.summary("s").title == "real prompt"


def test_title_empty_for_session_without_user_text(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("assistant only", role="assistant"))
    assert manager.summary("s").title == ""


def test_list_includes_unmigrated_legacy_json(tmp_path):
    manager = _manager(tmp_path)
    directory = manager.directory
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "exchanges": [{"user": "old", "assistant": "reply"}]}
    (directory / "legacy.json").write_bytes(msgspec.json.encode(payload))

    ids = [item.id for item in manager.list()]
    assert "legacy" in ids


def test_list_tolerates_malformed_crash_tail(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("valid", ts=5.0))
    with open(session.path, "ab") as handle:
        handle.write(b'{"type":"message","seq":2,"mess')

    summary = manager.summary("s")
    assert summary.last_seq == 1
    assert summary.title == "valid"
    assert [item.id for item in manager.list()] == ["s"]


def test_list_skips_interior_corrupt_log_without_breaking_others(tmp_path):
    manager = _manager(tmp_path)
    good = manager.open("good")
    good.append_message(_msg("ok"))
    bad = manager.open("bad")
    bad.append_message(_msg("first"))
    # A newline-terminated malformed record is genuine corruption, not a tail.
    with open(bad.path, "ab") as handle:
        handle.write(b"{not json}\n")

    assert [item.id for item in manager.list()] == ["good"]


def test_list_snapshot_aware_and_ignores_corrupt_snapshot(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("snapshotted", ts=1.0))
    session.write_snapshot()
    assert manager.summary("s").title == "snapshotted"

    session.snapshot_path.write_bytes(b"{broken")
    assert manager.summary("s").title == "snapshotted"


def test_summary_state_reflects_live_handle(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))

    session.enqueue("queued")
    assert manager.summary("s").state == "awaiting_input"

    lease = session.begin_turn()
    try:
        assert manager.summary("s").state == "running"
        session._observe_event(
            Event(type="permission.requested", data={"id": "req-1"})
        )
        assert manager.summary("s").state == "awaiting_permission"
    finally:
        lease.release()
    session._observe_event(Event(type="turn.completed"))
    assert manager.summary("s").state == "awaiting_input"


async def test_summary_viewers_tracks_subscriptions(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    stream = session.subscribe(from_seq=session.next_seq(), follow=True)
    await stream.__anext__()
    assert session.viewers == 1
    assert manager.summary("s").viewers == 1
    await stream.aclose()
    assert manager.summary("s").viewers == 0


# ---------------------------------------------------------------------------
# delete / trash / restore
# ---------------------------------------------------------------------------


def test_delete_moves_artifacts_to_trash_with_retention(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("bye", ts=42.0))
    session.write_snapshot()
    log_path = session.path
    snapshot_path = session.snapshot_path

    record = manager.delete("s", reason="cleanup")
    assert isinstance(record, TrashRecord)
    assert record.session_id == "s"
    assert record.title == "bye"
    assert record.last_seq == 1
    assert record.reason == "cleanup"
    assert record.delete_after == record.trashed_at + manager.retention_seconds
    assert not record.expired
    assert set(record.files) == {"s.jsonl", "s.snap.json"}

    assert not log_path.exists()
    assert not snapshot_path.exists()
    assert manager.list() == []
    entry = manager.trash_dir / record.trash_id
    assert (entry / "meta.json").exists()
    assert (entry / "s.jsonl").exists()

    trashed = manager.list_trashed()
    assert [item.trash_id for item in trashed] == [record.trash_id]


def test_delete_refuses_active_turn_without_cancelling(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    lease = session.begin_turn()
    try:
        with pytest.raises(SessionBusy):
            manager.delete("s")
        with pytest.raises(SessionBusy):
            manager.delete("s", force=True)
        assert session.active is True
        assert session.cancel_token.cancelled is False
        assert session.path.exists()
    finally:
        lease.release()
    assert manager.delete("s").session_id == "s"


def test_delete_refuses_queued_input_even_with_force(tmp_path):
    """A delete must never strand a durable ``input.queued`` submission.

    Refusing even with ``force`` is what stops a deleted session from
    resurrecting: the submission is durable in the log, so if delete moved the
    artifacts to trash while the queue lived on, the next open would replay it.
    """
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    session.enqueue("queued")
    assert session.queue_depth == 1

    with pytest.raises(SessionBusy):
        manager.delete("s")
    with pytest.raises(SessionBusy):
        manager.delete("s", force=True)

    # The refusal touched neither the log nor the in-memory FIFO.
    assert session.queue_depth == 1
    assert session.path.exists()

    # Dropping the queue explicitly is the only way through.
    session.cancel(drop_queue=True)
    assert session.queue_depth == 0
    assert manager.delete("s").session_id == "s"


def test_queued_input_survives_a_refused_delete_and_drops_on_reload(tmp_path):
    """A refused delete preserves the durable queue; a reload still sees it."""
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    session.enqueue("queued")

    with pytest.raises(SessionBusy):
        manager.delete("s", force=True)

    # A fresh open rehydrates exactly the durable submission, and a second
    # delete is still refused -- the queue cannot be quietly skipped.
    manager.evict("s")
    reopened = manager.open("s", recover=False, migrate=False)
    assert reopened.queue_depth == 1
    with pytest.raises(SessionBusy):
        manager.delete("s", force=True)

    reopened.cancel(drop_queue=True)
    kinds = [event.type for event in reopened.events]
    assert kinds.count("input.queued") == 1
    assert kinds.count("input.dropped") == 1
    assert manager.delete("s").session_id == "s"
    assert not manager.exists("s")


def test_listing_ignores_an_entry_whose_name_is_not_its_trash_id(tmp_path):
    """A record whose ``trash_id`` names a different entry is never trusted."""
    manager = _manager(tmp_path)
    entry = manager.trash_dir / "shadow"
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "s.jsonl").write_text("data", encoding="utf-8")
    (entry / "meta.json").write_bytes(
        msgspec.json.encode(
            TrashRecord(
                trash_id="s-abc123",
                session_id="s",
                trashed_at=1.0,
                delete_after=9_999_999_999.0,
                files=("s.jsonl",),
            )
        )
    )

    assert manager.list_trashed() == []
    assert manager.purge_expired(now=9_999_999_999.0) == []
    with pytest.raises(SessionError):
        manager.restore("s-abc123")
    # The mismatched entry is left completely untouched.
    assert (entry / "s.jsonl").exists()


def test_trash_record_with_no_files_is_ignored_and_refused(tmp_path):
    """A trash record naming no artifacts is never trusted or restored."""
    manager = _manager(tmp_path)
    entry = manager.trash_dir / "empty-abc123"
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "meta.json").write_bytes(
        msgspec.json.encode(
            TrashRecord(
                trash_id="empty-abc123",
                session_id="s",
                trashed_at=1.0,
                delete_after=9_999_999_999.0,
                files=(),
            )
        )
    )

    # This manager refuses a session with no artifacts to move, so it could not
    # have written this; restoring it would silently move nothing.
    assert manager.list_trashed() == []
    assert manager.purge_expired(now=9_999_999_999.0) == []
    with pytest.raises(SessionError):
        manager.restore("empty-abc123")
    assert entry.exists()


def test_published_entry_name_is_its_recorded_trash_id(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("bye"))

    record = manager.delete("s", reason="shape")
    entry = manager.trash_dir / record.trash_id
    assert entry.is_dir()
    assert entry.name == record.trash_id
    assert manager.list_trashed() == [record]


def test_delete_refuses_when_lock_held_by_another_owner(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    lock = SessionLock.for_session(manager.directory, "s")
    lock.acquire(shared=False, blocking=False)
    try:
        with pytest.raises(SessionBusy):
            manager.delete("s", force=True)
        assert session.path.exists()
    finally:
        lock.release()


async def test_delete_refuses_viewed_until_force(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    stream = session.subscribe(from_seq=session.next_seq(), follow=True)
    await stream.__anext__()
    assert session.viewers == 1

    with pytest.raises(SessionBusy):
        manager.delete("s")
    assert session.path.exists()

    record = manager.delete("s", force=True)
    assert record.session_id == "s"
    assert not manager.exists("s")
    await stream.aclose()


async def test_force_delete_then_view_disconnect_does_not_resurrect_log(tmp_path):
    """A disconnecting viewer of a force-deleted session must not recreate it.

    The viewer's ``subscribe`` finally emits presence cleanup through the same
    append path as everything else. If the retired handle could still append,
    ``store.append`` would recreate the moved log as a presence-only file -- a
    resurrected session. Retirement must make every late write a no-op.
    """
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("bye"))
    log_path = session.path

    stream = session.subscribe(from_seq=session.next_seq(), follow=True)
    await stream.__anext__()
    assert session.viewers == 1

    record = manager.delete("s", force=True)
    assert session.retired is True
    assert not manager.exists("s")
    assert not log_path.exists()

    # Disconnecting the view runs the presence cleanup on the retired handle; it
    # must not write. Nothing may reappear in the sessions directory.
    await stream.aclose()
    assert session.viewers == 0
    assert not log_path.exists()
    assert not manager.exists("s")
    assert not list(manager.directory.glob("s.jsonl"))
    assert [item.session_id for item in manager.list_trashed()] == ["s"]

    # The authoritative log is safe in the trash and restorable.
    assert manager.restore(record.trash_id) == "s"
    assert log_path.exists()


def test_failed_delete_rolls_back_retirement(tmp_path, monkeypatch):
    """A delete that fails before the move leaves the handle usable."""
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("keep"))

    def boom(*args, **kwargs):
        raise OSError("simulated move failure")

    monkeypatch.setattr(manager, "_trash", boom)
    with pytest.raises(OSError):
        manager.delete("s", force=True)

    # The refusal/failure rolled the retirement back, so the session is not
    # stranded: the log is authoritative and the handle still writes.
    assert session.retired is False
    assert session.path.exists()
    session.append_message(_msg("still here"))
    assert manager.summary("s").title == "keep"
    monkeypatch.undo()
    assert manager.delete("s").session_id == "s"


async def test_retired_handle_refuses_late_turn_start(tmp_path):
    """A scheduled/again-start turn on a deleted handle must be refused."""
    manager = _manager(tmp_path)
    provider = ScriptedProvider(text_response("nope"))
    session = _bind(manager.open("s"), provider)
    session.append_message(_msg("x"))

    manager.delete("s", force=True)
    assert session.retired is True

    with pytest.raises(SessionError):
        await session.start_turn("late")
    with pytest.raises(SessionError):
        session.enqueue("late")
    with pytest.raises(SessionError):
        session.write_snapshot()
    with pytest.raises(SessionError):
        session.append_message(_msg("late"))
    assert not manager.exists("s")


def test_delete_missing_session_raises(tmp_path):
    with pytest.raises(SessionError):
        _manager(tmp_path).delete("missing")


def test_delete_and_list_work_without_a_live_handle(tmp_path):
    writer = _manager(tmp_path)
    writer.open("s").append_message(_msg("on disk"))

    reader = _manager(tmp_path)
    assert [item.id for item in reader.list()] == ["s"]
    record = reader.delete("s")
    assert not writer.exists("s")
    assert [item.session_id for item in reader.list_trashed()] == ["s"]
    assert reader.restore(record.session_id) == "s"


def test_delete_and_restore_roundtrip_preserves_records(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("one", ts=1.0))
    session.append_event(Event(type="turn.started", data={"i": 1}))
    session.append_message(_msg("two", role="assistant", ts=2.0))
    before = session.records

    record = manager.delete("s")
    assert not manager.exists("s")

    restored_id = manager.restore(record.trash_id)
    assert restored_id == "s"
    reopened = manager.open("s", recover=False, migrate=False)
    assert reopened.records == before
    assert manager.list_trashed() == []


def test_restore_accepts_session_id_and_refuses_existing(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    manager.delete("s")

    # The destination now exists again, so restore must refuse.
    manager.open("s")
    with pytest.raises(SessionError):
        manager.restore("s")

    manager.delete("s")
    assert manager.restore("s") == "s"


def test_restore_unknown_id_raises(tmp_path):
    with pytest.raises(SessionError):
        _manager(tmp_path).restore("nope")


def test_restore_refuses_a_missing_listed_artifact_instead_of_claiming_success(
    tmp_path,
):
    """A record listing an artifact that is gone must refuse, not no-op.

    Restore used to skip a missing/symlinked listed artifact; if every one was
    skipped it removed the entry and returned the session id, reporting success
    while moving nothing and destroying the authoritative trash entry.
    """
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    (entry / "s.jsonl").unlink()

    with pytest.raises(SessionError):
        manager.restore(record.trash_id)
    # The trash entry is preserved so the failure can be investigated/repaired.
    assert entry.exists()
    assert not manager.exists("s")


def test_restore_refuses_a_symlinked_listed_artifact(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    outside = tmp_path / "outside.jsonl"
    outside.write_text("evil", encoding="utf-8")
    (entry / "s.jsonl").unlink()
    (entry / "s.jsonl").symlink_to(outside)

    with pytest.raises(SessionError):
        manager.restore(record.trash_id)
    assert not manager.exists("s")
    assert outside.read_text(encoding="utf-8") == "evil"


def test_a_v1_backup_alone_is_a_deletable_restorable_session(tmp_path):
    """Artifact enumeration must agree: ``.v1.bak`` alone is a session.

    ``_artifacts_exist`` gates summary/export/delete/restore but omitted the
    backup that ``_artifact_paths`` moves and restore trusts, so a session with
    only a ``.v1.bak`` could not be deleted even though delete could move it.
    """
    manager = _manager(tmp_path)
    manager.directory.mkdir(parents=True, exist_ok=True)
    backup = manager.directory / "s.v1.bak"
    backup.write_text('{"version": 1, "exchanges": []}', encoding="utf-8")

    assert manager._artifacts_exist("s")  # by artifact, not the JSONL log alone
    record = manager.delete("s")
    assert record.files == ("s.v1.bak",)
    assert not backup.exists()
    assert manager.restore(record.trash_id) == "s"
    assert backup.exists()


def test_trash_staging_without_meta_is_rolled_back(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    # Simulate a crash between moving the log and publishing the metadata.
    staging = manager.trash_dir / ".staging-interrupted"
    entry.rename(staging)
    (staging / "meta.json").unlink()

    assert manager.list_trashed() == []
    assert manager.exists("s")
    assert not staging.exists()
    # An untrusted metadata document belongs to the staging dir, never to the
    # sessions directory: it must be removed, not moved beside the log.
    assert not (manager.directory / "meta.json").exists()


def _crash_mid_delete(tmp_path):
    """Delete ``s`` then simulate a crash before the staging publish."""
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("authoritative"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    staging = manager.trash_dir / ".staging-crash"
    entry.rename(staging)
    (staging / "meta.json").unlink()
    return staging


def test_open_recovers_a_crash_mid_delete_before_creating_a_fresh_log(tmp_path):
    """A staged authoritative log must not be shadowed by a fresh empty one.

    ``open(..., create=True)`` on a session whose log a crash staged out of the
    sessions directory previously published an empty ``.jsonl`` first, hiding the
    authoritative log (title lost, records gone). Recovery now runs before
    ``create``/``migrate``, so the log is restored and then opened.
    """
    staging = _crash_mid_delete(tmp_path)
    assert not (tmp_path / "sessions" / "s.jsonl").exists()

    # A fresh manager (as after a daemon restart) opens and must recover first.
    reopened = _manager(tmp_path)
    session = reopened.open("s")
    assert not staging.exists()
    assert session.path.exists()
    assert reopened.summary("s").title == "authoritative"
    assert [m.content[0].text for m in session.messages] == ["authoritative"]


def test_list_recovers_a_crash_mid_delete_before_enumerating(tmp_path):
    """``list`` recovers a staged log so it is never missing from the listing."""
    staging = _crash_mid_delete(tmp_path)
    assert staging.exists()

    reopened = _manager(tmp_path)
    assert [item.id for item in reopened.list()] == ["s"]
    assert not staging.exists()


def test_trash_staging_with_meta_is_finalized(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    staging = manager.trash_dir / ".staging-published"
    entry.rename(staging)

    assert [item.trash_id for item in manager.list_trashed()] == [record.trash_id]
    assert manager.restore("s") == "s"
    assert manager.exists("s")


def test_restore_rolls_back_a_partial_move(tmp_path, monkeypatch):
    import os as os_mod

    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    session.write_snapshot()  # a second artifact so the restore is multi-step
    record = manager.delete("s")
    assert not manager.exists("s")

    real_replace = os_mod.replace
    calls = {"n": 0}

    def flaky_replace(source, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated disk failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os_mod, "replace", flaky_replace)
    with pytest.raises(OSError):
        manager.restore(record.trash_id)

    # The already-restored artifact was moved back, so nothing is stranded in
    # the sessions directory and the trash entry is still restorable.
    assert not manager.exists("s")
    assert [item.trash_id for item in manager.list_trashed()] == [record.trash_id]

    monkeypatch.undo()
    assert manager.restore(record.trash_id) == "s"
    assert manager.exists("s")


def test_purge_expired_removes_only_expired_entries(tmp_path):
    manager = _manager(tmp_path, retention_seconds=100.0)
    session = manager.open("s")
    session.append_message(_msg("x"))
    record = manager.delete("s")

    assert manager.purge_expired(now=record.trashed_at + 50) == []
    assert manager.list_trashed()
    removed = manager.purge_expired(now=record.delete_after + 1)
    assert removed == [record.trash_id]
    assert manager.list_trashed() == []


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def _seed_rich_session(session):
    session.append_event(Event(type="turn.started", data={"turn": "t"}))
    session.append_message(
        Message(
            role="user",
            content=[
                Text(text="look"),
                Image(media_type="image/png", data=b"\x00\x01\xff\xfe"),
            ],
        )
    )
    session.append_message(
        Message(
            role="assistant",
            content=[ToolUse(id="call-1", name="Read", input={"path": "a.txt"})],
        )
    )
    session.append_message(
        Message(
            role="user",
            content=[
                ToolResult(
                    tool_use_id="call-1", content=[Text(text="contents")], is_error=False
                )
            ],
        )
    )
    session.append_summary(
        text="condensed", summary_id="sum-1", strategy="summarize", source_to_seq=4
    )


def test_export_json_roundtrip_fidelity(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    _seed_rich_session(session)

    document = msgspec.json.decode(manager.export("s", format="json").encode())
    assert document["version"] == export_mod.EXPORT_VERSION
    assert document["session"]["id"] == "s"
    assert document["session"]["last_seq"] == session.records[-1].seq
    assert document["messages"] == msgspec.json.decode(
        msgspec.json.encode(session.messages)
    )
    assert document["events"] == msgspec.json.decode(
        msgspec.json.encode(session.events)
    )
    assert document["summaries"][0]["summary_id"] == "sum-1"
    # Binary content survives as base64.
    image = document["messages"][0]["content"][1]
    assert image["type"] == "image"
    assert image["data"] == "AAH//g=="


def test_export_markdown_is_readable_and_has_no_base64(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    _seed_rich_session(session)

    markdown = manager.export("s", format="markdown")
    assert markdown.startswith("# look")
    assert "### user" in markdown
    assert "### assistant" in markdown
    assert "**Tool call `Read`** (`call-1`)" in markdown
    assert "**Tool result `call-1`**" in markdown
    assert "[image image/png]" in markdown
    assert "AAH//g==" not in markdown
    assert "## Summaries" in markdown
    assert "condensed" in markdown


def test_export_jsonl_matches_consistent_records(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("a", ts=1.0))
    session.append_event(Event(type="turn.started"))
    with open(session.path, "ab") as handle:
        handle.write(b'{"type":"message","seq":3,"mess')  # crash tail

    lines = [line for line in manager.export("s", format="jsonl").splitlines() if line]
    decoded = [msgspec.json.decode(line.encode()) for line in lines]
    assert [item["seq"] for item in decoded] == [1, 2]
    assert decoded == msgspec.json.decode(msgspec.json.encode(session.records))


def test_export_rejects_unknown_format_and_bad_type(tmp_path):
    manager = _manager(tmp_path)
    manager.open("s")
    with pytest.raises(ValueError):
        manager.export("s", format="xml")
    with pytest.raises(TypeError):
        manager.export("s", format=123)


def test_export_missing_session_raises(tmp_path):
    with pytest.raises(SessionError):
        _manager(tmp_path).export("missing")


def test_export_does_not_leak_non_session_files(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("public content"))
    secret = "sk-super-secret-credential"
    manager.directory.mkdir(parents=True, exist_ok=True)
    (manager.directory / "credentials.json").write_text(f'{{"api_key": "{secret}"}}')
    (manager.directory / "notes.txt").write_text(secret)
    manager.trash_dir.mkdir(parents=True, exist_ok=True)
    (manager.trash_dir / "secret.key").write_text(secret)

    assert [item.id for item in manager.list()] == ["s"]
    for fmt in ("json", "markdown", "jsonl"):
        assert secret not in manager.export("s", format=fmt)


# ---------------------------------------------------------------------------
# fork / replay verification
# ---------------------------------------------------------------------------


def test_fork_child_is_listed_and_exports_independently(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("parent message", ts=1.0))
    child = manager.fork("src", new_id="child")
    child.append_message(_msg("child only", ts=2.0))

    listed = {item.id: item for item in manager.list()}
    assert set(listed) == {"src", "child"}
    assert listed["child"].last_seq == 3
    assert listed["child"].parent_id == "src"
    assert listed["child"].fork_seq == 1

    parent_doc = msgspec.json.decode(manager.export("src", format="json").encode())
    child_doc = msgspec.json.decode(manager.export("child", format="json").encode())
    parent_texts = [block["text"] for m in parent_doc["messages"] for block in m["content"]]
    child_texts = [block["text"] for m in child_doc["messages"] for block in m["content"]]
    assert "child only" not in parent_texts
    assert child_texts == ["parent message", "child only"]


async def test_replay_matches_export_events(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_event(Event(type="turn.started", data={"i": 1}))
    session.append_event(Event(type="totally.unknown", data={"x": [1, 2]}))
    session.append_message(_msg("hi"))

    replayed = [event async for event in manager.replay("s")]
    document = msgspec.json.decode(manager.export("s", format="json").encode())
    assert [event.type for event in replayed] == ["turn.started", "totally.unknown"]
    assert [event["type"] for event in document["events"]] == [
        "turn.started",
        "totally.unknown",
    ]
    assert document["messages"][0]["content"][0]["text"] == "hi"


# ---------------------------------------------------------------------------
# concurrent active turn
# ---------------------------------------------------------------------------


async def test_delete_refuses_running_turn_and_turn_survives(tmp_path):
    manager = _manager(tmp_path)
    provider = ScriptedProvider(
        [
            MessageStart(model="m", provider="scripted"),
            TextDelta(text="working"),
            Wait(),
            MessageStop(stop_reason="stop"),
        ]
    )
    session = _bind(manager.open("s"), provider)

    turn_id = await session.start_turn("go")
    assert session.active is True
    # Let the detached task reach its first event before inspecting the log.
    for _ in range(10):
        if any(event.type == "turn.started" for event in session.events):
            break
        await asyncio.sleep(0)
    # list/export must stay consistent while the exclusive lock is held.
    assert manager.summary("s").state == "running"
    document = msgspec.json.decode(manager.export("s", format="json").encode())
    assert any(event["type"] == "turn.started" for event in document["events"])
    with pytest.raises(SessionBusy):
        manager.delete("s", force=True)
    # The refused delete must not have cancelled the turn.
    assert session.active is True
    assert session.cancel_token.cancelled is False
    assert session.active_turn_id == turn_id

    session.cancel("done")
    await session.wait_turn(turn_id)
    assert session.active is False
    assert manager.delete("s").session_id == "s"


# ---------------------------------------------------------------------------
# malicious on-disk trash metadata
# ---------------------------------------------------------------------------


def _craft_trash_entry(trash_dir, name, meta):
    entry = trash_dir / name
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "meta.json").write_bytes(msgspec.json.encode(meta))
    return entry


def test_purge_ignores_a_traversal_trash_id(tmp_path):
    manager = _manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("safe", encoding="utf-8")
    _craft_trash_entry(
        manager.trash_dir,
        "crafted",
        TrashRecord(
            trash_id="../../outside",
            session_id="s",
            trashed_at=1.0,
            delete_after=0.0,
            files=("s.jsonl",),
        ),
    )

    assert manager.list_trashed() == []
    assert manager.purge_expired(now=9_999_999_999.0) == []
    assert canary.read_text(encoding="utf-8") == "safe"
    assert outside.exists()


def test_restore_refuses_an_untrusted_artifact_even_if_metadata_slips_through(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    entry = manager.trash_dir / "crafted"
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "ok.jsonl").write_text("data", encoding="utf-8")
    outside = tmp_path / "evil.jsonl"
    bad = TrashRecord(
        trash_id="crafted",
        session_id="s",
        trashed_at=1.0,
        delete_after=9_999_999_999.0,
        files=("../evil.jsonl",),
    )
    # Bypass the listing sanitizer to exercise restore's own guard directly.
    monkeypatch.setattr(manager, "_find_trash", lambda _id: bad)

    with pytest.raises(SessionError):
        manager.restore("crafted")
    assert not outside.exists()
    assert (entry / "ok.jsonl").exists()


def test_purge_unlinks_a_symlinked_entry_without_following_it(tmp_path):
    manager = _manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "s.jsonl"
    canary.write_text("safe", encoding="utf-8")
    manager.trash_dir.mkdir(parents=True, exist_ok=True)
    link = manager.trash_dir / "linked"
    link.symlink_to(outside)

    assert manager.list_trashed() == []
    assert manager.purge_expired(now=9_999_999_999.0) == []
    assert canary.read_text(encoding="utf-8") == "safe"


def test_recover_trash_ignores_a_symlinked_staging_entry(tmp_path):
    manager = _manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "s.jsonl"
    canary.write_text("safe", encoding="utf-8")
    manager.trash_dir.mkdir(parents=True, exist_ok=True)
    link = manager.trash_dir / ".staging-linked"
    link.symlink_to(outside)

    assert manager.list_trashed() == []
    assert canary.read_text(encoding="utf-8") == "safe"
    assert not (manager.directory / "s.jsonl").exists()


def test_recovery_is_skipped_while_the_trash_lock_is_held(tmp_path):
    """A held trash lock makes recovery a no-op, never a rollback.

    ``open``/``list`` run recovery non-blocking, so a producer mid-move must not
    have its staging directory rolled back underneath it. This pins that the
    whole sweep is skipped while the lock is held and only runs once it is free.
    """
    from nexus.session.lock import TrashLock

    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("authoritative"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    staging = manager.trash_dir / ".staging-held"
    entry.rename(staging)
    (staging / "meta.json").unlink()  # crash between the move and the meta

    lock = TrashLock.for_dir(manager.trash_dir)
    assert lock.acquire(blocking=False) is True
    try:
        assert manager.list() == []  # skipped: the log is still staged out
        assert staging.exists()
        assert (staging / "s.jsonl").exists()
        assert not (manager.directory / "s.jsonl").exists()
    finally:
        lock.release()

    # Once the lock is free the sweep rolls the authoritative log back.
    assert [item.id for item in manager.list()] == ["s"]
    assert not staging.exists()
    assert (manager.directory / "s.jsonl").exists()


def test_recover_rolls_back_a_corrupt_or_partial_staging_meta(tmp_path):
    """A partially written staging meta must restore, never discard, the log.

    The session writer moves artifacts before it writes metadata, so a corrupt
    or missing ``meta.json`` means the authoritative log is still in staging. The
    sweep must return it to the sessions directory and never move the metadata
    (or its atomic-write temp) beside it.
    """
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("authoritative"))
    record = manager.delete("s")
    entry = manager.trash_dir / record.trash_id
    staging = manager.trash_dir / ".staging-corrupt"
    entry.rename(staging)
    (staging / "meta.json").write_bytes(b'{"trash_id": "s-abc", "sess')
    (staging / "meta.json.tmp").write_bytes(b'{"partial": true')

    assert [item.id for item in manager.list()] == ["s"]
    assert not staging.exists()
    assert (manager.directory / "s.jsonl").exists()
    assert not (manager.directory / "meta.json").exists()
    assert not (manager.directory / "meta.json.tmp").exists()
    assert manager.list_trashed() == []


def test_recover_skips_while_another_process_holds_the_trash_lock(tmp_path):
    """Deterministic two-process check: recovery never races a live producer.

    A real ``delete`` runs in a child process and pauses *after* moving the
    authoritative log into its staging directory, while still holding the
    cross-process trash lock. The parent's ``list``/``list_trashed`` recovery
    must skip (non-blocking) rather than roll the stage back, then resume the
    producer and observe a clean publish.
    """
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("authoritative"))
    manager.evict("s")

    sentinel = tmp_path / "paused"
    resume = tmp_path / "resume"
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.session import SessionManager
        from nexus.session import manager as m

        sessions, trash, sentinel, resume = map(Path, sys.argv[1:5])
        real = m._write_trash_meta

        def paused(path, meta):
            sentinel.write_text("paused")
            deadline = time.monotonic() + 30
            while not resume.exists():
                if time.monotonic() > deadline:
                    raise SystemExit("resume timeout")
                time.sleep(0.005)
            return real(path, meta)

        m._write_trash_meta = paused
        SessionManager(sessions, trash_dir=trash).delete("s")
        """
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(manager.directory),
            str(manager.trash_dir),
            str(sentinel),
            str(resume),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(sentinel, proc)
        staging = list(manager.trash_dir.glob(".staging-*"))
        assert len(staging) == 1
        assert (staging[0] / "s.jsonl").exists()
        assert not (manager.directory / "s.jsonl").exists()

        # Non-blocking recovery must leave the paused stage exactly as it is.
        assert manager.list() == []
        assert manager.list_trashed() == []
        assert (staging[0] / "s.jsonl").exists()
        assert not (manager.directory / "s.jsonl").exists()

        # ``open(create=True)`` must refuse rather than publish a fresh empty
        # log that would shadow the staged authoritative one.
        with pytest.raises(SessionBusy):
            manager.open("s")
        assert not (manager.directory / "s.jsonl").exists()
    finally:
        resume.write_text("go")
        proc.wait(timeout=30)
    assert proc.returncode == 0

    records = manager.list_trashed()
    assert [item.session_id for item in records] == ["s"]
    assert not (manager.directory / "s.jsonl").exists()
    assert manager.restore(records[0].trash_id) == "s"
    assert (manager.directory / "s.jsonl").exists()


def test_open_create_refuses_while_a_concurrent_recoverer_holds_the_trash_lock(
    tmp_path,
):
    """A recoverer holding **only** the trash lock cannot be shadowed by a create.

    A crash left the authoritative log staged out of the sessions directory with
    no metadata. A concurrent recoverer holds the cross-process trash lock and
    takes no session lock. ``open(create=True)`` must not publish a fresh empty
    log over the staged authoritative one: under the session lock it takes the
    trash lock non-blocking, finds it busy, and refuses with a clear
    ``SessionBusy``. Once the recoverer sweeps and releases, a retried ``open``
    reads the rolled-back authoritative log.
    """
    staging = _crash_mid_delete(tmp_path)
    manager = _manager(tmp_path)

    held = tmp_path / "recoverer_held"
    resume = tmp_path / "recoverer_resume"
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.session import SessionManager
        from nexus.session.lock import TrashLock

        sessions, trash, held, resume = map(Path, sys.argv[1:5])
        lock = TrashLock.for_dir(trash)
        assert lock.acquire(blocking=False) is True
        held.write_text("1")
        deadline = time.monotonic() + 30
        while not resume.exists():
            if time.monotonic() > deadline:
                raise SystemExit("resume timeout")
            time.sleep(0.005)
        SessionManager(sessions, trash_dir=trash)._recover_trash_locked()
        lock.release()
        """
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(manager.directory),
            str(manager.trash_dir),
            str(held),
            str(resume),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(held, proc)
        with pytest.raises(SessionBusy):
            manager.open("s")
        # No fresh empty log shadowed the staged authoritative one.
        assert not (manager.directory / "s.jsonl").exists()
        assert staging.exists()
        assert (staging / "s.jsonl").exists()
    finally:
        resume.write_text("go")
        proc.wait(timeout=30)
    assert proc.returncode == 0

    session = manager.open("s")
    assert manager.summary("s").title == "authoritative"
    assert [m.content[0].text for m in session.messages] == ["authoritative"]
    assert not staging.exists()


def test_three_process_recovery_create_interleaving_never_shadows(tmp_path):
    """Three real processes: paused recoverer, concurrent creator, verifier.

    The recoverer holds the trash lock while paused mid-sweep; a second process
    concurrently asks ``open(create=True)`` for the same crash-staged session and
    must report a clear busy without creating a shadow log; the parent then
    resumes the recoverer and confirms the authoritative log is intact.
    """
    staging = _crash_mid_delete(tmp_path)
    manager = _manager(tmp_path)
    held = tmp_path / "held"
    resume = tmp_path / "resume"

    recoverer = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.session import SessionManager
        from nexus.session.lock import TrashLock

        sessions, trash, held, resume = map(Path, sys.argv[1:5])
        lock = TrashLock.for_dir(trash)
        assert lock.acquire(blocking=False) is True
        held.write_text("1")
        deadline = time.monotonic() + 30
        while not resume.exists():
            if time.monotonic() > deadline:
                raise SystemExit("resume timeout")
            time.sleep(0.005)
        SessionManager(sessions, trash_dir=trash)._recover_trash_locked()
        lock.release()
        """
    )
    creator = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from nexus.session import SessionManager
        from nexus.errors import SessionBusy

        sessions, trash = map(Path, sys.argv[1:3])
        try:
            SessionManager(sessions, trash_dir=trash).open("s")
        except SessionBusy:
            print("busy")
        else:
            print("created")
        """
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            recoverer,
            str(manager.directory),
            str(manager.trash_dir),
            str(held),
            str(resume),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(held, proc)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                creator,
                str(manager.directory),
                str(manager.trash_dir),
            ],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "busy"
        assert not (manager.directory / "s.jsonl").exists()
        assert staging.exists()
    finally:
        resume.write_text("go")
        proc.wait(timeout=30)
    assert proc.returncode == 0

    assert manager.open("s").id == "s"
    assert manager.summary("s").title == "authoritative"
    assert not staging.exists()


def _migrated_legacy_with_staged_v2(tmp_path):
    """A migrated legacy session whose authoritative v2 log a crash staged out.

    Migration leaves the legacy ``.json`` and its ``.v1.bak`` in place while the
    authoritative ``.jsonl`` carries a later v2 record. A crash mid-delete moved
    *only* the ``.jsonl`` into an untrusted staging directory, so the sessions
    directory still has the legacy ``.json`` and ``store.exists`` is false --
    exactly the shape that used to let a stale re-migration shadow the staged
    authoritative log.
    """
    manager = _manager(tmp_path)
    directory = manager.directory
    directory.mkdir(parents=True, exist_ok=True)
    legacy = {"version": 1, "exchanges": [{"user": "legacy", "assistant": "stale"}]}
    (directory / "s.json").write_bytes(msgspec.json.encode(legacy))
    session = manager.open("s")
    session.append_message(_msg("authoritative v2"))
    manager.evict("s")

    staging = manager.trash_dir / ".staging-v2"
    staging.mkdir(parents=True, exist_ok=True)
    (directory / "s.jsonl").rename(staging / "s.jsonl")
    assert (directory / "s.json").exists()
    assert not (directory / "s.jsonl").exists()
    return manager, staging


def test_migrate_never_shadows_a_staged_v2_log_with_stale_legacy_json(tmp_path):
    """A stale legacy ``.json`` must not be re-migrated over a staged v2 log.

    With the authoritative ``.jsonl`` crash-staged out and the legacy ``.json``
    still present, ``should_migrate`` is true. A concurrent recoverer holds the
    cross-process trash lock, so ``open``'s non-blocking sweep skips. Both
    ``open`` and a direct ``migrate`` must refuse (``SessionBusy``) rather than
    publish a stale re-migration that shadows the staged log; once the recoverer
    resumes, the original v2 log is restored and no shadow remains.
    """
    manager, staging = _migrated_legacy_with_staged_v2(tmp_path)

    held = tmp_path / "recoverer_held"
    resume = tmp_path / "recoverer_resume"
    script = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.session import SessionManager
        from nexus.session.lock import TrashLock

        sessions, trash, held, resume = map(Path, sys.argv[1:5])
        lock = TrashLock.for_dir(trash)
        assert lock.acquire(blocking=False) is True
        held.write_text("1")
        deadline = time.monotonic() + 30
        while not resume.exists():
            if time.monotonic() > deadline:
                raise SystemExit("resume timeout")
            time.sleep(0.005)
        SessionManager(sessions, trash_dir=trash)._recover_trash_locked()
        lock.release()
        """
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(manager.directory),
            str(manager.trash_dir),
            str(held),
            str(resume),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(held, proc)
        # The stale legacy bytes are present and recovery is blocked: ``open``
        # must refuse, and write no shadow log, rather than re-migrate them.
        with pytest.raises(SessionBusy):
            manager.open("s")
        assert not (manager.directory / "s.jsonl").exists()
        assert staging.exists()
        assert (staging / "s.jsonl").exists()

        # A direct migrate is guarded identically, so a caller that migrates
        # without opening is safe too.
        with pytest.raises(SessionBusy):
            manager.migrate("s")
        assert not (manager.directory / "s.jsonl").exists()
        assert staging.exists()
    finally:
        resume.write_text("go")
        proc.wait(timeout=30)
    assert proc.returncode == 0

    # The recoverer restored the authoritative v2 log, and no stale shadow was
    # ever written; the reopened session sees the full v2 history.
    assert not staging.exists()
    session = manager.open("s")
    assert [m.content[0].text for m in session.messages] == [
        "legacy",
        "stale",
        "authoritative v2",
    ]
    assert manager.summary("s").title == "legacy"


def _legacy_only_crash_mid_delete(tmp_path):
    """A legacy-only session whose ``.json`` a crash staged out of sessions.

    The legacy ``.json`` is the only authoritative artifact; no ``.jsonl`` was
    ever written. A crash mid-delete moved it into a staging directory with no
    metadata, so ``store.exists`` is false and ``should_migrate`` is false while
    it is staged -- the shape that used to let ``open(create=True)`` publish an
    empty ``.jsonl`` that shadowed the restored history.
    """
    manager = _manager(tmp_path)
    directory = manager.directory
    directory.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 1,
        "exchanges": [
            {"user": "hello", "assistant": "world"},
            {"user": "second", "assistant": "reply"},
        ],
    }
    (directory / "s.json").write_bytes(msgspec.json.encode(legacy))
    manager.trash_dir.mkdir(parents=True, exist_ok=True)
    staging = manager.trash_dir / ".staging-legacy"
    staging.mkdir()
    (directory / "s.json").rename(staging / "s.json")
    assert not (directory / "s.json").exists()
    assert not (directory / "s.jsonl").exists()
    return manager, staging


_LEGACY_ONLY_MESSAGES = ["hello", "world", "second", "reply"]


def test_open_create_migrates_a_crash_restored_legacy_json_without_shadowing(
    tmp_path,
):
    """A crash-restored legacy-only ``.json`` is migrated, never shadowed.

    The outer best-effort sweep can be skipped while a concurrent recoverer holds
    the trash lock and then find it free by the time the create path takes it.
    ``_create_session_log`` must therefore re-evaluate ``should_migrate`` under
    the session and trash locks: it migrates the restored legacy bytes to the
    authoritative ``.jsonl`` instead of publishing an empty shadow.
    """
    manager, staging = _legacy_only_crash_mid_delete(tmp_path)
    # Simulate the concurrent recoverer holding the lock across the outer sweep
    # and the migrate decision, then releasing before the create path.
    manager._recover_trash = lambda: None

    session = manager.open("s")

    assert not staging.exists()
    assert (manager.directory / "s.jsonl").exists()
    assert [m.content[0].text for m in session.messages] == _LEGACY_ONLY_MESSAGES
    assert manager.summary("s").title == "hello"
    assert (manager.directory / "s.v1.bak").exists()


def test_open_create_migrate_false_still_migrates_a_restored_legacy_json(tmp_path):
    """``migrate=False`` must not license an empty log over a restored legacy file.

    The caller asked not to *pre*-migrate, but the create path still owns the
    decision: publishing an empty ``.jsonl`` beside a restored legacy ``.json``
    would permanently hide the authoritative history.
    """
    manager, staging = _legacy_only_crash_mid_delete(tmp_path)

    session = manager.open("s", create=True, migrate=False)

    assert not staging.exists()
    assert [m.content[0].text for m in session.messages] == _LEGACY_ONLY_MESSAGES


def test_open_create_refuses_a_staged_legacy_json_while_trash_locked(tmp_path):
    """A held trash lock must fail closed, then a retry restores and migrates.

    While a concurrent recoverer holds the cross-process trash lock, the create
    path refuses (``SessionBusy``) and writes no shadow. Once it is released the
    crash-staged legacy ``.json`` is rolled back and migrated, preserving the
    full original content.
    """
    manager, staging = _legacy_only_crash_mid_delete(tmp_path)
    lock = TrashLock.for_dir(manager.trash_dir)
    assert lock.acquire(blocking=False) is True
    try:
        with pytest.raises(SessionBusy):
            manager.open("s")
        assert not (manager.directory / "s.jsonl").exists()
        assert staging.exists()
        assert (staging / "s.json").exists()
    finally:
        lock.release()

    session = manager.open("s")
    assert not staging.exists()
    assert [m.content[0].text for m in session.messages] == _LEGACY_ONLY_MESSAGES


def test_open_create_waits_out_a_transient_unrelated_trash_lock(tmp_path, monkeypatch):
    """A short, unrelated trash lock must not turn a new open into a busy error.

    The create path retries the trash lock for a bounded window, so a lock
    released inside that window (an unrelated delete/purge finishing) is waited
    out and the open proceeds.
    """
    import nexus.session.manager as manager_mod

    monkeypatch.setattr(manager_mod, "_TRASH_LOCK_WAIT_SECONDS", 0.5)
    manager = _manager(tmp_path)
    lock = TrashLock.for_dir(manager.trash_dir)
    assert lock.acquire(blocking=False) is True

    import threading

    def release_soon():
        time.sleep(0.05)
        lock.release()

    thread = threading.Thread(target=release_soon)
    thread.start()
    try:
        session = manager.open("brand-new")
        assert session.id == "brand-new"
        assert (manager.directory / "brand-new.jsonl").exists()
    finally:
        thread.join()


def test_open_create_fails_closed_when_trash_lock_stays_held(tmp_path, monkeypatch):
    """The trash-lock retry is bounded: a stuck lock still refuses, never waits.

    A lock held past the (shortened) bound must produce a clear ``SessionBusy``
    and no log, so a transient-wait optimization can never become an unbounded
    hang or a shadowing create.
    """
    import nexus.session.manager as manager_mod

    monkeypatch.setattr(manager_mod, "_TRASH_LOCK_WAIT_SECONDS", 0.1)
    manager = _manager(tmp_path)
    lock = TrashLock.for_dir(manager.trash_dir)
    assert lock.acquire(blocking=False) is True
    try:
        with pytest.raises(SessionBusy):
            manager.open("brand-new")
        assert not (manager.directory / "brand-new.jsonl").exists()
    finally:
        lock.release()


def test_concurrent_open_of_a_brand_new_session_both_succeed(tmp_path):
    """Two brand-new openers must both succeed, not one spurious ``SessionBusy``.

    The winner pauses while holding the session lock *before* it publishes the
    empty log; the loser must wait out the short contention and then observe the
    created log. This is the case a non-blocking session-lock guard alone turned
    into a spurious refusal.
    """
    manager = _manager(tmp_path)
    a_creating = tmp_path / "a_creating"
    b_waiting = tmp_path / "b_waiting"
    resume_a = tmp_path / "resume_a"

    winner = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.session import SessionManager
        from nexus.session import store as store_mod

        sessions, trash, a_creating, resume_a = map(Path, sys.argv[1:5])
        real_create = store_mod.SessionStore.create

        def paused_create(self, session):
            a_creating.write_text("1")
            deadline = time.monotonic() + 30
            while not resume_a.exists():
                if time.monotonic() > deadline:
                    raise SystemExit("resume timeout")
                time.sleep(0.005)
            return real_create(self, session)

        store_mod.SessionStore.create = paused_create
        print(SessionManager(sessions, trash_dir=trash).open("brand-new").id)
        """
    )
    loser = textwrap.dedent(
        """
        import sys, time
        from pathlib import Path
        from nexus.session import SessionManager
        import nexus.session.manager as manager_mod

        sessions, trash, b_waiting = map(Path, sys.argv[1:4])
        # Keep the loser waiting past any scheduling jitter until the winner
        # releases; the wait only bounds a genuine delete in production.
        manager_mod._CREATE_LOCK_WAIT_SECONDS = 30.0
        real_sleep = time.sleep

        def signalling_sleep(seconds):
            b_waiting.write_text("1")
            real_sleep(seconds)

        manager_mod.time.sleep = signalling_sleep
        print(SessionManager(sessions, trash_dir=trash).open("brand-new").id)
        """
    )
    p_win = subprocess.Popen(
        [
            sys.executable,
            "-c",
            winner,
            str(manager.directory),
            str(manager.trash_dir),
            str(a_creating),
            str(resume_a),
        ],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p_lose = None
    try:
        _wait_for(a_creating, p_win)
        p_lose = subprocess.Popen(
            [
                sys.executable,
                "-c",
                loser,
                str(manager.directory),
                str(manager.trash_dir),
                str(b_waiting),
            ],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for(b_waiting, p_lose)
        resume_a.write_text("go")
        out_win, err_win = p_win.communicate(timeout=30)
        out_lose, err_lose = p_lose.communicate(timeout=30)
    finally:
        resume_a.write_text("go")
        for proc in (p_win, p_lose):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
    assert p_win.returncode == 0, err_win
    assert p_lose.returncode == 0, err_lose
    assert out_win.strip() == "brand-new"
    assert out_lose.strip() == "brand-new"
    assert (manager.directory / "brand-new.jsonl").exists()


def test_open_create_false_waits_for_a_concurrent_in_process_create(
    tmp_path, monkeypatch
):
    """``create=False`` must not report a spurious missing id mid-create.

    A creator is paused inside the create after it registered its in-flight
    marker (and released ``_handles_lock``). A concurrent ``create=False`` opener
    must wait for that create and then return the same live handle instead of
    raising ``SessionError``. This is the deterministic, same-process case the
    manager-wide lock alone could not cover once the slow create moved outside
    it.
    """
    manager = _manager(tmp_path)
    started = threading.Event()
    release = threading.Event()
    real = SessionManager._create_or_migrate_locked

    def paused(self, session_id):
        started.set()
        assert release.wait(30.0), "create was never released"
        return real(self, session_id)

    monkeypatch.setattr(SessionManager, "_create_or_migrate_locked", paused)

    created: dict[str, object] = {}
    creator = threading.Thread(
        target=lambda: created.__setitem__("s", manager.open("s"))
    )
    creator.start()
    try:
        assert started.wait(10.0), "creator never reached the paused create"
        # Release shortly, from a different thread, so the waiting open proceeds.
        threading.Timer(0.1, release.set).start()
        non_creator = manager.open("s", create=False)
    finally:
        release.set()
        creator.join(timeout=10.0)
    assert not creator.is_alive()
    assert non_creator.id == "s"
    assert non_creator is created["s"]


def test_open_create_false_for_a_genuinely_missing_id_fails_promptly(tmp_path):
    """A missing id with no create in flight still fails without waiting."""
    manager = _manager(tmp_path)
    started = time.monotonic()
    with pytest.raises(SessionError):
        manager.open("nope", create=False)
    assert time.monotonic() - started < 1.0


def test_open_create_false_after_a_failed_create_is_not_wedged(tmp_path, monkeypatch):
    """A failed create clears its in-flight marker, so the id fails promptly.

    The pending-create event is cleaned up in a ``finally``: a creator that
    raises must not leave a later ``create=False`` opener waiting on a create
    that will never complete.
    """
    manager = _manager(tmp_path)

    def boom(self, session_id):
        raise SessionBusy("create refused")

    monkeypatch.setattr(SessionManager, "_create_session_log", boom)
    with pytest.raises(SessionBusy):
        manager.open("s")
    assert "s" not in manager._pending_creates
    started = time.monotonic()
    with pytest.raises(SessionError):
        manager.open("s", create=False)
    assert time.monotonic() - started < 1.0


async def test_export_while_turn_running_is_consistent(tmp_path):
    manager = _manager(tmp_path)
    provider = ScriptedProvider(text_response("quick"))
    session = _bind(manager.open("s"), provider)

    turn_id = await session.start_turn("hello")
    await session.wait_turn(turn_id)
    summary = manager.summary("s")
    assert summary.state == "idle"
    assert summary.title == "hello"
    assert [m["role"] for m in msgspec.json.decode(
        manager.export("s", format="json").encode()
    )["messages"]] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# lock order: an open waiting on a session flock must not hold the manager-wide
# ``_handles_lock`` (otherwise ``delete`` inverts and stalls every operation)
# ---------------------------------------------------------------------------


def _open_quietly(manager: SessionManager, session_id: str) -> None:
    """Open, swallowing the bounded ``SessionBusy`` the held lock produces."""
    try:
        manager.open(session_id)
    except SessionBusy:
        pass


def _park_open_on_a_held_session_lock(tmp_path, monkeypatch, session_id):
    """Park an ``open`` deterministically inside its session-flock wait.

    The create path's poll sleep is patched to block on an event, so the opener
    is unambiguously waiting on a session lock we hold. Returns the held lock,
    an event set once the opener is waiting, and the release event (plus the
    lock itself) the caller must release in ``finally``.
    """
    import nexus.session.manager as manager_mod

    monkeypatch.setattr(manager_mod, "_CREATE_LOCK_WAIT_SECONDS", 0.3)
    held = SessionLock.for_session(tmp_path / "sessions", session_id)
    held.acquire(shared=False, blocking=False)

    waiting = threading.Event()
    release = threading.Event()

    def blocking_sleep(_seconds):
        waiting.set()
        release.wait(10.0)

    monkeypatch.setattr(manager_mod.time, "sleep", blocking_sleep)
    return held, waiting, release


def test_open_does_not_hold_the_handle_lock_while_waiting_on_a_session_flock(
    tmp_path, monkeypatch
):
    """The manager-wide ``_handles_lock`` stays free during an open's wait.

    ``open`` polls the session flock in its create path. Holding
    ``_handles_lock`` across that bounded wait would stall every other manager
    operation (``open``/``evict``/``delete``/``list``) and invert the
    session-flock -> ``_handles_lock`` order ``delete`` uses. A deterministic
    same-process two-thread check.
    """
    manager = _manager(tmp_path)
    held, waiting, release = _park_open_on_a_held_session_lock(
        tmp_path, monkeypatch, "brand-new"
    )
    opener = threading.Thread(target=_open_quietly, args=(manager, "brand-new"))
    opener.start()
    try:
        assert waiting.wait(5.0), "opener never reached the session-lock wait"
        # While the opener is parked on the session flock, the global handle
        # lock must be immediately available to an unrelated operation.
        probe_done = threading.Event()

        def probe():
            manager.evict("unrelated")
            probe_done.set()

        probe_thread = threading.Thread(target=probe)
        probe_thread.start()
        assert probe_done.wait(2.0), (
            "manager-wide _handles_lock was held across the session-lock wait"
        )
        probe_thread.join()
    finally:
        release.set()
        opener.join(timeout=5.0)
        held.release()


def test_delete_is_not_stalled_by_an_unrelated_open_waiting_on_a_session_lock(
    tmp_path, monkeypatch
):
    """A delete of an unrelated session proceeds while another open waits.

    The global stall from the old lock order: one ``open`` parked on a session
    flock held ``_handles_lock``, so ``delete``'s first ``_live_handle`` (which
    takes ``_handles_lock``) blocked even for a different session. With the
    create wait moved outside the lock the delete completes promptly.
    """
    manager = _manager(tmp_path)
    live = manager.open("live")
    live.append_message(_msg("x"))

    held, waiting, release = _park_open_on_a_held_session_lock(
        tmp_path, monkeypatch, "brand-new"
    )
    opener = threading.Thread(target=_open_quietly, args=(manager, "brand-new"))
    opener.start()
    try:
        assert waiting.wait(5.0), "opener never reached the session-lock wait"
        done = threading.Event()
        record: dict[str, TrashRecord] = {}

        def deleter():
            record["value"] = manager.delete("live")
            done.set()

        delete_thread = threading.Thread(target=deleter)
        delete_thread.start()
        assert done.wait(2.0), (
            "delete of an unrelated session stalled behind an open's wait"
        )
        delete_thread.join()
        assert record["value"].session_id == "live"
        assert not manager.exists("live")
    finally:
        release.set()
        opener.join(timeout=5.0)
        held.release()
