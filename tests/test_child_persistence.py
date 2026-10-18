"""Required child state must survive both persistence and relay boundaries."""
from types import SimpleNamespace

import pytest

from nexus.agents.runner import _ChildRelay
from nexus.events import Event
from nexus.runtime import _ChildEventSink


@pytest.mark.parametrize("event_type", ["text.delta", "todo.updated"])
async def test_failed_child_append_relays_diagnostic_without_exception_text(event_type):
    relayed = []

    def fail(_event):
        raise OSError("secret path or credential")

    async def relay(kind, data):
        relayed.append((kind, data))

    sink = _ChildEventSink(SimpleNamespace(append_event=fail), relay)
    event = Event(type=event_type, data={"revision": 1})
    if event_type == "todo.updated":
        with pytest.raises(OSError, match="secret path"):
            await sink.emit(event)
    else:
        await sink.emit(event)
    assert relayed[0] == (event_type, event.data)
    assert relayed[1][0] == "error"
    assert relayed[1][1]["code"] == "child_log_persist_failed"
    assert "secret" not in str(relayed)


@pytest.mark.parametrize("event_type", ["text.delta", "todo.updated"])
async def test_owner_failure_propagates_only_for_required_state_through_nested_relays(event_type):
    written = []

    async def fail(_event):
        raise OSError("owner append failed")

    root = _ChildRelay(SimpleNamespace(emit=fail), {"id": "child"}, "child-session")
    nested = _ChildRelay(root.emit, {"id": "grandchild"}, "grandchild-session")
    sink = _ChildEventSink(SimpleNamespace(append_event=written.append), nested.emit)
    event = Event(type=event_type, data={"revision": 1})
    if event_type == "todo.updated":
        with pytest.raises(OSError, match="owner append failed"):
            await sink.emit(event)
    else:
        await sink.emit(event)
    assert written == [event]


async def test_successful_todo_reaches_child_and_owner():
    written, relayed = [], []
    sink = _ChildEventSink(
        SimpleNamespace(append_event=written.append),
        lambda kind, data: relayed.append((kind, data)),
    )
    event = Event(type="todo.updated", data={"revision": 1, "todos": []})
    assert await sink.emit(event) is event
    assert written == [event]
    assert relayed == [(event.type, event.data)]
