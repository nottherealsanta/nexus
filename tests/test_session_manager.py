import pytest

from nexus.errors import SessionBusy, SessionError
from nexus.events import Event
from nexus.model.message import Message, MessageMeta, Text, ToolResult, ToolUse
from nexus.session import snapshot as snapshot_mod
from nexus.session.ids import is_valid_session_id
from nexus.session.manager import SessionManager
from nexus.session.records import MessageRecord


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
    before = session.records

    assert session.recover_dangling_tool_uses() == []
    reopened = SessionManager(tmp_path).open("main")
    assert reopened.recover_dangling_tool_uses() == []
    assert reopened.records == before
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
    session.append_message(_msg("hello"))
    session.append_message(_tool_use("call-1"))

    reopened = SessionManager(tmp_path).open("main")
    assert len(reopened.recovered) == 1
    assert reopened.messages[-1].content[0].tool_use_id == "call-1"


def test_open_can_skip_recovery(tmp_path):
    session = SessionManager(tmp_path).open("main")
    session.append_message(_msg("hello"))
    session.append_message(_tool_use("call-1"))
    reopened = SessionManager(tmp_path).open("main", recover=False)
    assert reopened.recovered == ()
    assert len(reopened.messages) == 2


def test_completion_watermark_ignores_presence_and_advances_for_next_turn(tmp_path):
    from nexus.ui_support.session_status import session_status

    manager = SessionManager(tmp_path)
    session = manager.open("completion-watermark", recover=False)
    session.append_message(_msg("hello"))
    session.append_event(Event(type="turn.completed"))
    completed = manager.summary(session.id)
    assert completed.completion_seq == completed.last_seq

    # Acknowledging completion, then a view detaching writes presence events;
    # these must not change the durable identity of the completed turn.
    acknowledged = completed.completion_seq
    seen = {session.id: acknowledged}
    session.append_event(Event(type="presence.left", data={"viewers": 0}))
    session.append_event(Event(type="presence.changed", data={"viewers": 0}))
    after_presence = manager.summary(session.id)
    assert after_presence.last_seq > completed.last_seq
    assert after_presence.completion_seq == acknowledged
    assert session_status(after_presence, seen, "other") == "idle"

    session.append_event(Event(type="turn.failed"))
    next_turn = manager.summary(session.id)
    assert next_turn.completion_seq > acknowledged
    assert next_turn.completion_seq == next_turn.last_seq
    assert session_status(next_turn, seen, "other") == "done"


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
    parent_before = session.records

    child = manager.fork("src")
    appended = child.append_message(_msg("child-only"))
    assert appended.seq == 4
    assert child.next_seq() == 5

    assert session.records == parent_before
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
    assert manager.store.exists(child.id)
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
    loaded = manager.store.load_snapshot(child.id, child.read(force=True))
    assert loaded is not None
    assert loaded.seq == session.read(force=True).next_seq


def test_fork_ignores_invalid_source_snapshot(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.write_snapshot()
    # Write a snapshot the log no longer agrees with (stale/corrupt).
    manager.store.write_snapshot(
        "src",
        snapshot_mod.Snapshot(id="src", seq=1, messages=[_msg("not what happened")]),
    )
    child = manager.fork("src")
    assert manager.store.load_snapshot(child.id, child.read(force=True)) is None
    assert [m.content[0].text for m in child.messages] == ["a"]


def test_fork_does_not_apply_snapshot_past_boundary(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_msg("a"))
    session.append_message(_msg("b"))
    session.write_snapshot()  # seq == 2, after the requested boundary
    child = manager.fork("src", at_seq=1)
    assert manager.store.load_snapshot(child.id, child.read(force=True)) is None
    assert [m.content[0].text for m in child.messages] == ["a"]


def test_fork_does_not_recover_dangling_tool_use(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("src")
    session.append_message(_tool_use("call-1"))
    before = session.records
    child = manager.fork("src")
    # The source prefix stays exact; a durable provenance event follows it.
    assert child.records[: len(session.records)] == session.records
    assert child.events[-1].type == "session.forked"
    assert child.events[-1].data == {"parent": "src", "at_seq": session.next_seq() - 1}
    assert session.records == before


def test_archive_hides_from_list_and_open_unarchives(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("archived")
    session.append_message(_msg("hello"))
    record = manager.archive("archived", "user")
    assert record.reason == "user"
    assert any(row.session_id == "archived" for row in manager.archived())
    assert "archived" not in [row.id for row in manager.list()]
    assert "archived" in [row.id for row in manager.list(include_archived=True)]

    reopened = SessionManager(tmp_path).open("archived", create=False)
    assert reopened.id == "archived"
    assert manager.archived() == []
    assert manager.summary("archived").message_count == 1


def test_archive_is_durable_across_a_restarted_manager(tmp_path):
    # Archive state is a plain SQL column update in the same shared database,
    # so there is no file-based sidecar left to corrupt or replace
    # non-atomically (the pre-STATE_PLAN JSON sidecar's failure modes).
    manager = SessionManager(tmp_path)
    session = manager.open("durable")
    session.append_message(_msg("kept"))
    manager.archive("durable")
    restarted = SessionManager(tmp_path)
    assert [row.session_id for row in restarted.archived()] == ["durable"]


def test_archive_stale_skips_live_handles_and_archives_only_old_idle_sessions(tmp_path):
    first = SessionManager(tmp_path)
    live = first.open("live")
    first.store.append_message("live", Message(role="user", content=[Text(text="hello")], meta=MessageMeta(ts=0)))
    live.append_event(Event(type="turn.completed", ts=1))
    old = first.open("old")
    first.store.append_message("old", Message(role="user", content=[Text(text="hello")], meta=MessageMeta(ts=0)))
    old.append_event(Event(type="turn.completed", ts=2))
    # The second manager has no local open handles, as a daemon startup sweep
    # would. The `live` record's lock is not held, so mark it as open only in
    # the first manager and exercise the manager's in-process exclusion there.
    assert first.archive_stale(now=1000, older_than=100) == []
    sweep = SessionManager(tmp_path)
    rows = sweep.archive_stale(now=1000, older_than=100)
    assert [row.session_id for row in rows] == ["live", "old"]


def test_archive_stale_is_bounded_per_call_oldest_first(tmp_path, monkeypatch):
    # The bounded, ordered ``ORDER BY last_activity ASC LIMIT`` query replaces
    # the old file-store's persisted sweep cursor (STATE_PLAN §5.1): each call
    # still only ever archives up to the cap, oldest activity first.
    monkeypatch.setattr("nexus.session.manager._ARCHIVE_MAX_SWEEP", 2)
    manager = SessionManager(tmp_path)
    for index in range(5):
        session = manager.open(f"session-{index}")
        manager.store.append_message(session.id, Message(role="user", content=[Text(text="hello")], meta=MessageMeta(ts=0)))
        session.append_event(Event(type="turn.completed", data={}, ts=float(index)))
        manager.evict(f"session-{index}")  # a live handle would exclude it from the sweep

    archived = manager.archive_stale(now=1000, older_than=100)
    assert [row.session_id for row in archived] == ["session-0", "session-1"]

    archived_again = manager.archive_stale(now=1000, older_than=100)
    assert [row.session_id for row in archived_again] == ["session-2", "session-3"]


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
    before = session.records

    events = [event async for event in manager.replay("r")]

    assert events == []
    assert session.records == before
    assert manager.store.load_snapshot("r", session.read(force=True)) is None
    assert session.next_seq() == 2


async def test_replay_does_not_recover_dangling_tool_use(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("r")
    session.append_message(_tool_use("call-1"))
    before = session.records
    async for _ in manager.replay("r"):
        pass
    assert session.records == before


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


def test_draft_is_unsaved_and_hidden_until_first_user_input(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("draft")
    session.append_event(Event(type="session.model", data={}))
    session.append_message(_msg("not user input", role="assistant"))
    assert manager.list() == []
    assert manager.store.db.project_sessions() == []
    conn = manager.store.db._connection()
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 0
    assert len(session.read().records) == 2
    assert not SessionManager(tmp_path).store.exists("draft")
    session.append_message(_msg("hello"))
    assert [row.id for row in manager.list()] == ["draft"]
    assert len(SessionManager(tmp_path).open("draft", create=False).read().records) == 3


def test_queued_user_submission_promotes_draft(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.open("draft")
    session.append_event(Event(type="input.queued", data={"queued_id": "q1", "text": "hi"}))
    assert [row.id for row in manager.list()] == ["draft"]
    assert SessionManager(tmp_path).store.exists("draft")


def test_existing_zero_user_sessions_hidden_without_purge(tmp_path):
    manager = SessionManager(tmp_path)
    manager.store.create("empty")
    manager.store.append_message("empty", _msg("assistant only", role="assistant"))
    assert manager.list(include_archived=True) == []
    assert manager.store.db.project_sessions() == []
    assert manager.store.exists("empty")
    assert len(manager.store.read("empty").records) == 1
