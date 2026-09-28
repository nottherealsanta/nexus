import os
from pathlib import Path

import pytest

from nexus.errors import SessionBusy, SessionError
from nexus.events import Event
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.session import snapshot as snapshot_mod
from nexus.session.ids import is_valid_session_id
from nexus.session.manager import SessionManager
from nexus.session.store import MessageRecord


def _msg(text, role="user"):
    return Message(role=role, content=[Text(text=text)])


def _tool_use(call_id, name="Read"):
    return Message(role="assistant", content=[ToolUse(id=call_id, name=name, input={"path": "x"})])


def _tool_result(call_id, text="done"):
    return Message(role="user", content=[ToolResult(tool_use_id=call_id, content=[Text(text=text)])])


def test_manager_validates_ids(tmp_path):
    manager = SessionManager(tmp_path)
    for bad in ("", "../escape", "has space", "x" * 81, None):
        with pytest.raises(ValueError):
            manager.open(bad)
    assert manager.open("good-id_1").id == "good-id_1"


def test_open_create_false_requires_existing_log(tmp_path):
    manager = SessionManager(tmp_path)
    with pytest.raises(SessionError):
        manager.open("missing", create=False)
    manager.open("missing")
    assert manager.open("missing", create=False).id == "missing"


def test_history_and_event_apis_round_trip(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("main")
    session.append_event(Event(type="turn.started", data={"turn": "t"}))
    session.append_message(Message(role="user", content=[Text(text="hi")]))
    session.append_message(Message(role="assistant", content=[Text(text="hello")]))

    reopened = SessionManager(tmp_path).open("main")
    assert [m.content[0].text for m in reopened.messages] == ["hi", "hello"]
    assert [e.type for e in reopened.events] == ["turn.started"]
    assert reopened.next_seq() == 4
    assert reopened.events[0].session == "main"


def test_exclusive_active_turn_ownership(tmp_path):
    session = SessionManager(tmp_path).open("main")
    lease = session.begin_turn()
    assert session.active is True
    assert session.active_turn_id == lease.turn_id
    with pytest.raises(SessionBusy):
        session.begin_turn()
    lease.release()
    assert session.active is False
    lease.release()  # idempotent

    second = session.begin_turn()
    assert second.turn_id != lease.turn_id
    second.release()


def test_turn_lease_context_manager_and_state(tmp_path):
    session = SessionManager(tmp_path).open("main")
    with session.begin_turn(turn_id="turn-1") as lease:
        assert lease.turn_id == "turn-1"
        assert lease.state.session_id == "main"
        assert lease.state.phase == "awaiting_model"
    assert session.active is False


def test_cancellation_token_is_per_turn(tmp_path):
    session = SessionManager(tmp_path).open("main")
    first = session.begin_turn()
    session.cancel("stop")
    assert first.cancel_token.cancelled is True
    first.release()

    second = session.begin_turn()
    assert second.cancel_token.cancelled is False
    assert second.cancel_token is session.cancel_token
    second.release()


def test_dangling_tool_use_recovery_appends_error_result(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("main")
    session.append_message(Message(role="user", content=[Text(text="read it")]))
    session.append_message(_tool_use("call-1"))
    session.append_message(_tool_use("call-2", name="Write"))

    recovered = session.recover_dangling_tool_uses()
    assert len(recovered) == 1
    message = recovered[0].message
    assert message.role == "user"
    assert [type(block).__name__ for block in message.content] == [
        "ToolResult",
        "ToolResult",
    ]
    assert [block.tool_use_id for block in message.content] == ["call-1", "call-2"]
    assert all(block.is_error for block in message.content)
    assert "call-1" in message.content[0].content[0].text
    assert "Write" in message.content[1].content[0].text
    assert session.recovered == tuple(recovered)


def test_recovery_persists_after_assistant_tool_use(tmp_path):
    session = SessionManager(tmp_path).open("main")
    assistant = session.append_message(_tool_use("call-1"))
    result = session.recover_dangling_tool_uses()[0]
    assert assistant.seq < result.seq
    assert isinstance(result, MessageRecord)
    assert result.seq == 2


def test_recovery_is_idempotent_across_reopen(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("main")
    session.append_message(_tool_use("call-1"))
    first = session.recover_dangling_tool_uses()
    assert len(first) == 1
    before = session.path.read_bytes()

    assert session.recover_dangling_tool_uses() == []
    reopened = SessionManager(tmp_path).open("main")
    assert reopened.recover_dangling_tool_uses() == []
    assert reopened.path.read_bytes() == before
    assert len([m for m in reopened.messages if m.role == "user"]) == 1


def test_resolved_tool_use_is_not_recovered(tmp_path):
    session = SessionManager(tmp_path).open("main")
    session.append_message(_tool_use("done"))
    session.append_message(_tool_result("done"))
    session.append_message(_tool_use("pending"))
    recovered = session.recover_dangling_tool_uses()
    assert len(recovered) == 1
    blocks = recovered[0].message.content
    assert [block.tool_use_id for block in blocks] == ["pending"]
    assert all(block.is_error for block in blocks)


def test_recovery_skips_when_turn_is_active(tmp_path):
    session = SessionManager(tmp_path).open("main")
    session.append_message(_tool_use("call-1"))
    lease = session.begin_turn()
    try:
        assert session.recover_dangling_tool_uses() == []
    finally:
        lease.release()
    assert len(session.recover_dangling_tool_uses()) == 1


def test_open_runs_recovery_by_default(tmp_path):
    session = SessionManager(tmp_path).open("main")
    session.append_message(_tool_use("call-1"))

    reopened = SessionManager(tmp_path).open("main")
    assert len(reopened.recovered) == 1
    assert reopened.messages[-1].content[0].tool_use_id == "call-1"


def test_open_can_skip_recovery(tmp_path):
    session = SessionManager(tmp_path).open("main")
    session.append_message(_tool_use("call-1"))
    reopened = SessionManager(tmp_path).open("main", recover=False)
    assert reopened.recovered == ()
    assert len(reopened.messages) == 1


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------


def test_fork_at_end_preserves_records_exactly(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    event = session.append_event(Event(type="turn.started", data={"i": 1}))
    session.append_message(_msg("hi"))
    session.append_message(_msg("there", role="assistant"))

    child = manager.fork("src")

    assert child.id != "src"
    assert child.records[: len(session.records)] == session.records
    assert [record.seq for record in child.records[:-1]] == [record.seq for record in session.records]
    assert child.events[-1].type == "session.forked"
    assert child.events[0].id == event.event.id
    assert child.events[0].ts == event.event.ts
    assert child.events[0].data == {"i": 1}


def test_fork_preserves_bytes_and_tagged_blocks(tmp_path):
    from nexus.model.message import Image

    manager = SessionManager(tmp_path)
    session = manager.open("src")
    message = Message(
        role="user",
        content=[
            Text(text="see"),
            Image(media_type="image/png", data=b"\x00\x01\xff\xfe"),
        ],
    )
    session.append_message(message)
    child = manager.fork("src")
    assert child.messages[0] == message
    assert child.messages[0].content[1].data == b"\x00\x01\xff\xfe"


def test_fork_future_append_is_monotonic_and_diverges(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("one"))
    session.append_message(_msg("two"))
    parent_before = session.path.read_bytes()

    child = manager.fork("src")
    appended = child.append_message(_msg("child-only"))
    assert appended.seq == 4
    assert child.next_seq() == 5

    assert session.path.read_bytes() == parent_before
    assert session.next_seq() == 3
    assert [m.content[0].text for m in child.messages][-1] == "child-only"


def test_fork_at_mid_boundary_copies_only_through_boundary(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.append_message(_msg("b"))
    session.append_message(_msg("c"))

    child = manager.fork("src", at_seq=2)

    assert [m.content[0].text for m in child.messages] == ["a", "b"]
    assert child.next_seq() == 4
    assert child.append_message(_msg("d")).seq == 4
    assert [m.content[0].text for m in session.messages] == ["a", "b", "c"]


def test_fork_of_empty_session(tmp_path):
    manager = SessionManager(tmp_path)
    manager.open("empty")
    child = manager.fork("empty")
    assert len(child.records) == 1
    assert child.events[0].type == "session.forked"
    assert child.next_seq() == 2


def test_fork_missing_source_is_rejected(tmp_path):
    with pytest.raises(SessionError):
        SessionManager(tmp_path).fork("missing")


@pytest.mark.parametrize("bad", [0, -1, "x", 1.5, True])
def test_fork_rejects_invalid_boundaries(tmp_path, bad):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    with pytest.raises((ValueError, SessionError)):
        manager.fork("src", at_seq=bad)


def test_fork_boundary_past_end_is_rejected(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    with pytest.raises(SessionError):
        manager.fork("src", at_seq=99)


def test_fork_boundary_between_records_is_rejected(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.append_message(_msg("b"), seq=5)  # deliberate gap
    with pytest.raises(SessionError):
        manager.fork("src", at_seq=3)
    assert manager.fork("src", at_seq=5).next_seq() == 7


def test_fork_generates_safe_collision_resistant_child_id(tmp_path):
    manager = SessionManager(tmp_path)
    manager.open("src")
    child = manager.fork("src")
    assert is_valid_session_id(child.id)
    assert child.id.startswith("src-")
    assert (tmp_path / f"{child.id}.jsonl").exists()
    assert manager.fork("src").id != child.id


def test_fork_generated_id_retries_on_collision(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path)
    manager.open("src")
    manager.open("src-deadbeef0000")
    tokens = iter(["deadbeef0000", "cafebabe0000"])
    monkeypatch.setattr(
        "nexus.session.manager.secrets.token_hex", lambda n: next(tokens)
    )
    child = manager.fork("src")
    assert child.id == "src-cafebabe0000"


def test_fork_at_event_boundary(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_event(Event(type="turn.started"))
    session.append_event(Event(type="turn.completed"))
    session.append_message(_msg("after"))

    child = manager.fork("src", at_seq=2)
    assert [e.type for e in child.events] == ["turn.started", "turn.completed", "session.forked"]
    assert child.messages == []
    assert child.next_seq() == 4
    summary = manager.summary(child.id)
    assert summary.parent_id == "src"
    assert summary.fork_seq == 2


def test_fork_explicit_new_id_collision_is_rejected(tmp_path):
    manager = SessionManager(tmp_path)
    manager.open("src")
    manager.open("taken")
    with pytest.raises(SessionError):
        manager.fork("src", new_id="taken")
    with pytest.raises(ValueError):
        manager.fork("src", new_id="bad id")
    # The existing session is untouched.
    assert manager.open("taken", create=False).records == []


def test_fork_inherits_valid_snapshot_only(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.append_message(_msg("b"))
    session.write_snapshot()

    child = manager.fork("src")
    loaded = snapshot_mod.load(tmp_path, child.id, child.read(force=True))
    assert loaded is not None
    assert loaded.seq == session.read(force=True).next_seq


def test_fork_ignores_invalid_source_snapshot(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.write_snapshot()
    session.snapshot_path.write_bytes(b"{broken")
    child = manager.fork("src")
    assert not child.snapshot_path.exists()
    assert [m.content[0].text for m in child.messages] == ["a"]


def test_fork_does_not_apply_snapshot_past_boundary(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.append_message(_msg("b"))
    session.write_snapshot()  # seq == 2, after the requested boundary
    child = manager.fork("src", at_seq=1)
    assert not child.snapshot_path.exists()
    assert [m.content[0].text for m in child.messages] == ["a"]


def test_fork_does_not_recover_dangling_tool_use(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_tool_use("call-1"))
    before = session.path.read_bytes()
    child = manager.fork("src")
    # The source prefix stays exact; a durable provenance event follows it.
    assert child.records[: len(session.records)] == session.records
    assert child.events[-1].type == "session.forked"
    assert child.events[-1].data == {"parent": "src", "at_seq": session.next_seq() - 1}
    assert session.path.read_bytes() == before


def test_archive_sidecar_hides_and_open_unarchives(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("archived")
    session.append_message(_msg("hello"))
    record = manager.archive("archived", "user")
    assert record.reason == "user"
    assert manager.archive_path.exists()
    assert "archived" not in [row.id for row in manager.list()]
    assert "archived" in [row.id for row in manager.list(include_archived=True)]

    reopened = SessionManager(tmp_path).open("archived", create=False)
    assert reopened.id == "archived"
    assert manager.archived() == []
    assert manager.summary("archived").message_count == 1


def test_archive_index_corruption_recovers_without_losing_session(tmp_path, caplog):
    manager = SessionManager(tmp_path)
    session = manager.open("safe")
    session.append_message(_msg("still here"))
    manager.archive_path.write_text("{broken", encoding="utf-8")
    assert manager.archived() == []
    assert "corrupt session archive index" in caplog.text
    manager.archive("safe")
    assert manager.summary("safe").message_count == 1
    assert manager.archived()[0].session_id == "safe"


def test_archive_index_replacement_is_atomic_and_restart_durable(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path)
    session = manager.open("durable")
    session.append_message(_msg("kept"))
    original_replace = os.replace
    replacements = []

    def checked_replace(source, destination):
        source, destination = Path(source), Path(destination)
        replacements.append((source.parent, destination))
        return original_replace(source, destination)

    monkeypatch.setattr("nexus.session.manager.os.replace", checked_replace)
    manager.archive("durable")
    assert replacements == [(tmp_path, manager.archive_path)]
    assert list(tmp_path.glob(".archive-*.tmp")) == []
    restarted = SessionManager(tmp_path)
    assert [row.session_id for row in restarted.archived()] == ["durable"]


def test_archive_stale_skips_live_handles_and_archives_only_old_idle_sessions(tmp_path):
    first = SessionManager(tmp_path)
    live = first.open("live")
    live.append_event(Event(type="turn.completed", ts=1))
    old = first.open("old")
    old.append_event(Event(type="turn.completed", ts=2))
    # The second manager has no local open handles, as a daemon startup sweep
    # would. The `live` record's lock is not held, so mark it as open only in
    # the first manager and exercise the manager's in-process exclusion there.
    assert first.archive_stale(now=1000, older_than=100) == []
    sweep = SessionManager(tmp_path)
    rows = sweep.archive_stale(now=1000, older_than=100)
    assert [row.session_id for row in rows] == ["live", "old"]


def test_archive_stale_rotates_bounded_cursor_past_recent_sessions(tmp_path, monkeypatch):
    from nexus.session.manager import ArchiveRecord, SessionSummary

    manager = SessionManager(tmp_path / "sessions")
    manager.open("seed")
    ids = [f"session-{index:04d}" for index in range(501)]
    monkeypatch.setattr(manager, "_session_ids", lambda: ids)
    monkeypatch.setattr(
        manager,
        "summary",
        lambda session_id: SessionSummary(
            id=session_id, last_activity=950 if session_id != ids[-1] else 1
        ),
    )
    monkeypatch.setattr(manager, "_has_pending_work", lambda _session_id: False)
    monkeypatch.setattr(
        manager,
        "archive",
        lambda session_id, reason: ArchiveRecord(
            session_id=session_id, archived_at=1000, reason=reason
        ),
    )

    assert manager.archive_stale(now=1000, older_than=100) == []
    archived = manager.archive_stale(now=1000, older_than=100)
    assert [row.session_id for row in archived] == [ids[-1]]


@pytest.mark.parametrize(
    "event_type,data",
    [
        ("turn.started", {}),
        ("input.queued", {"queued_id": "q1"}),
        ("permission.requested", {"id": "p1"}),
    ],
)
def test_archive_stale_skips_durable_pending_work(tmp_path, event_type, data):
    first = SessionManager(tmp_path)
    session = first.open("pending")
    session.append_event(Event(type=event_type, data=data, ts=1))
    sweep = SessionManager(tmp_path)
    assert sweep.archive_stale(now=1000, older_than=100) == []


def test_open_archive_does_not_touch_purgeable_trash(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("trashable")
    session.append_message(_msg("x"))
    manager.delete("trashable")
    assert manager.purge_expired(now=10**12)
    assert not manager.archived()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


async def test_replay_fidelity_and_unknown_events(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("r")
    session.append_event(Event(type="turn.started", data={"i": 1}))
    session.append_event(Event(type="totally.unknown.type", data={"weird": [1, 2, 3]}))
    session.append_message(_msg("hi"))
    session.append_event(Event(type="turn.completed", data={}))

    replayed = [event async for event in manager.replay("r")]
    persisted = session.events

    assert replayed == persisted
    assert [e.seq for e in replayed] == [e.seq for e in persisted]
    assert [e.id for e in replayed] == [e.id for e in persisted]
    assert [e.ts for e in replayed] == [e.ts for e in persisted]
    assert [e.type for e in replayed] == [
        "turn.started",
        "totally.unknown.type",
        "turn.completed",
    ]


async def test_replay_is_read_only_without_side_effects(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("r")
    session.append_message(_msg("a"))
    before = session.path.read_bytes()

    events = [event async for event in manager.replay("r")]

    assert events == []
    assert session.path.read_bytes() == before
    assert not session.snapshot_path.exists()
    assert session.next_seq() == 2


async def test_replay_does_not_recover_dangling_tool_use(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("r")
    session.append_message(_tool_use("call-1"))
    before = session.path.read_bytes()
    async for _ in manager.replay("r"):
        pass
    assert session.path.read_bytes() == before


async def test_replay_missing_session_is_rejected(tmp_path):
    with pytest.raises(SessionError):
        async for _ in SessionManager(tmp_path).replay("missing"):
            pass


async def test_replay_orders_by_persisted_sequence(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("r")
    session.append_event(Event(type="a"))
    session.append_event(Event(type="b"))
    session.append_message(_msg("x"))
    session.append_event(Event(type="c"))
    replayed = [event async for event in manager.replay("r")]
    seqs = [event.seq for event in replayed]
    assert seqs == sorted(seqs)
    assert [event.type for event in replayed] == ["a", "b", "c"]
