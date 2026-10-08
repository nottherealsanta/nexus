"""SQLite session operations: summaries, archive/trash, export, fork/replay.

The manager owns the read-only projection (:class:`SessionSummary`), the
transactional archive/trash lifecycle, and structured export. These tests pin
the transport-neutral summary contract, the "delete never cancels a turn" rule,
trash retention metadata, export fidelity across JSON / Markdown / JSONL, and
that fork/replay stay consistent with the new surfaces.
"""
from __future__ import annotations

import asyncio
import time

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
from nexus.session.lock import SessionLock


def _manager(tmp_path, **kwargs) -> SessionManager:
    return SessionManager(tmp_path, **kwargs)


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
        "completion_seq",
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


def test_archive_hides_until_open_resumes_session(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("archived"))

    archived = manager.archive("s", reason="user")
    assert archived.session_id == "s"
    assert [item.id for item in manager.list()] == []
    assert [item.id for item in manager.list(include_archived=True)] == ["s"]
    assert manager.archived() == [archived]

    resumed = manager.open("s")
    assert resumed is session
    assert manager.archived() == []
    assert [item.id for item in manager.list()] == ["s"]


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


def test_list_snapshot_aware_and_ignores_corrupt_snapshot(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("snapshotted", ts=1.0))
    session.write_snapshot()
    assert manager.summary("s").title == "snapshotted"

    # Damage the persisted SQLite cache itself, then force a fresh manager
    # handle so open must reject the snapshot and rebuild from records.
    with manager.store.db.transaction() as conn:
        conn.execute(
            "UPDATE snapshots SET body=? WHERE project_id=? AND namespace=? AND session_id=?",
            (b"{broken", manager.store.project_id, manager.store.namespace, "s"),
        )
    assert manager.store.load_snapshot("s", manager.store.read("s")) is None
    manager.evict("s")
    reopened = manager.open("s")
    assert reopened.current.snapshot_seq is None
    assert [message.content[0].text for message in reopened.messages] == ["snapshotted"]

    # The index-backed manager listing remains sound and does not depend on the
    # damaged derived snapshot either.
    listed = manager.list()
    assert [(item.id, item.title) for item in listed] == [("s", "snapshotted")]
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


def test_delete_trashes_sqlite_row_with_retention(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("bye", ts=42.0))
    session.write_snapshot()
    record = manager.delete("s", reason="cleanup")
    assert isinstance(record, TrashRecord)
    assert record.session_id == "s"
    assert record.title == "bye"
    assert record.last_seq == 1
    assert record.reason == "cleanup"
    assert record.delete_after == record.trashed_at + manager.retention_seconds
    assert not record.expired
    assert record.files == ()  # SQLite retains the row and records in place.
    assert manager.list() == []
    assert manager.store.row_exists("s")
    assert manager.store.find_trash(record.trash_id)["id"] == "s"

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
        assert manager.exists("s")
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

    # The refusal touched neither the database row nor the in-memory FIFO.
    assert session.queue_depth == 1
    assert manager.exists("s")

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
    reopened = manager.open("s")
    assert reopened.queue_depth == 1
    with pytest.raises(SessionBusy):
        manager.delete("s", force=True)

    reopened.cancel(drop_queue=True)
    kinds = [event.type for event in reopened.events]
    assert kinds.count("input.queued") == 1
    assert kinds.count("input.dropped") == 1
    assert manager.delete("s").session_id == "s"
    assert not manager.exists("s")


def test_sqlite_trash_id_resolves_to_its_session_row(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("bye"))

    record = manager.delete("s", reason="shape")
    assert manager.store.find_trash(record.trash_id)["id"] == "s"
    assert manager.list_trashed() == [record]


def test_delete_refuses_when_lock_held_by_another_owner(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    lock = SessionLock(manager.store.lock_path("s"))
    lock.acquire(shared=False, blocking=False)
    try:
        with pytest.raises(SessionBusy):
            manager.delete("s", force=True)
        assert manager.exists("s")
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
    assert manager.exists("s")

    record = manager.delete("s", force=True)
    assert record.session_id == "s"
    assert not manager.exists("s")
    await stream.aclose()


async def test_force_delete_then_view_disconnect_does_not_resurrect_log(tmp_path):
    """A late presence cleanup cannot untrash a retired session row."""
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("bye"))
    stream = session.subscribe(from_seq=session.next_seq(), follow=True)
    await stream.__anext__()
    assert session.viewers == 1

    record = manager.delete("s", force=True)
    assert session.retired is True
    assert not manager.exists("s")
    assert manager.store.row_exists("s")

    # Disconnecting the view runs the presence cleanup on the retired handle; it
    # must not change the transactional trash state.
    await stream.aclose()
    assert session.viewers == 0
    assert not manager.exists("s")
    assert manager.store.row_exists("s")
    assert [item.session_id for item in manager.list_trashed()] == ["s"]

    # The authoritative log is safe in the trash and restorable.
    assert manager.restore(record.trash_id) == "s"
    assert manager.exists("s")


def test_failed_delete_rolls_back_retirement(tmp_path, monkeypatch):
    """A delete that fails before the transaction leaves the handle usable."""
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("keep"))

    def boom(*args, **kwargs):
        raise OSError("simulated move failure")

    monkeypatch.setattr(manager.store, "trash", boom)
    with pytest.raises(OSError):
        manager.delete("s", force=True)

    # The refusal/failure rolled the retirement back, so the session remains
    # active and the handle still writes.
    assert session.retired is False
    assert manager.exists("s")
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
    # The fixture's turn never ended; skip recovery so only restore is compared.
    reopened = manager.open("s", recover=False)
    assert reopened.records == before
    assert manager.list_trashed() == []


def test_restore_accepts_session_id_and_refuses_existing(tmp_path):
    manager = _manager(tmp_path)
    session = manager.open("s")
    session.append_message(_msg("x"))
    manager.delete("s")

    # SQLite retains the primary-key row in trash, so a new session cannot
    # reuse the id before restore (and create is idempotent for that row).
    manager.store.create("s")
    assert not manager.exists("s")
    assert manager.store.row_exists("s")
    assert manager.restore("s") == "s"
    assert manager.exists("s")


def test_restore_unknown_id_raises(tmp_path):
    with pytest.raises(SessionError):
        _manager(tmp_path).restore("nope")


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
    (tmp_path / "secret.key").write_text(secret)

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


def test_open_create_false_for_a_genuinely_missing_id_fails_promptly(tmp_path):
    """A missing id with no create in flight still fails without waiting."""
    manager = _manager(tmp_path)
    started = time.monotonic()
    with pytest.raises(SessionError):
        manager.open("nope", create=False)
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
