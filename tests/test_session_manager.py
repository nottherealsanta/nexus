import pytest

from nexus.errors import SessionBusy, SessionError
from nexus.events import Event
from nexus.model.message import Message, Text, ToolResult, ToolUse
from nexus.session.manager import SessionManager
from nexus.session.store import MessageRecord


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
