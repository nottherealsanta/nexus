import time
import uuid

from nexus.errors import (
    BusClosed,
    ConfigError,
    MalformedToolCall,
    NexusError,
    OperationCancelled,
    ProviderError,
    SessionBusy,
    SessionError,
    ToolError,
)
from nexus.events import EVENT_TYPES, Event
from nexus.util import new_id


def test_error_hierarchy():
    assert issubclass(ConfigError, ValueError)
    assert issubclass(ConfigError, NexusError)
    assert issubclass(ProviderError, RuntimeError)
    assert issubclass(SessionBusy, RuntimeError)
    assert issubclass(SessionBusy, SessionError)
    assert issubclass(BusClosed, RuntimeError)
    assert issubclass(OperationCancelled, NexusError)
    assert issubclass(ToolError, NexusError)


def test_malformed_tool_call_details():
    error = MalformedToolCall("call_1", "bad json", raw="{")
    assert isinstance(error, ProviderError)
    assert error.tool_call_id == "call_1"
    assert error.raw == "{"


def test_event_envelope_defaults():
    before = time.time()
    event = Event("started", {"session": "default"})
    after = time.time()
    assert event.type == "started"
    assert event.data == {"session": "default"}
    assert event.seq == 0
    assert event.session is None
    assert event.turn is None
    assert before <= event.ts <= after
    assert event.to_dict() == {
        "type": "started",
        "data": {"session": "default"},
        "seq": 0,
        "ts": event.ts,
        "session": None,
        "turn": None,
        "id": event.id,
    }


def test_event_ids_are_unique_uuid7_shaped():
    first, second = Event("x"), Event("x")
    assert first.id != second.id
    parsed = uuid.UUID(first.id)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_new_id_is_uuid7_shaped():
    assert uuid.UUID(new_id()).version == 7


def test_event_explicit_envelope_fields():
    event = Event(
        "text.delta", {"text": "x"}, seq=3, session="s", turn="t", id="fixed"
    )
    data = event.to_dict()
    assert data["seq"] == 3
    assert data["session"] == "s"
    assert data["turn"] == "t"
    assert data["id"] == "fixed"


def test_event_catalogue_includes_transitional_legacy_types():
    # The pre-Phase-1 path still emits these; the catalogue must not reject them.
    for name in ("started", "message", "provider", "completed", "error"):
        assert name in EVENT_TYPES


def test_event_catalogue_covers_plan_types():
    for name in (
        "session.opened",
        "turn.completed",
        "context.degraded",
        "model.started",
        "tool.completed",
        "permission.requested",
        "ext.loaded",
        "mcp.connected",
        "skill.invoked",
        "agent.spawned",
        "hook.fired",
        "provider.raw",
        "error",
    ):
        assert name in EVENT_TYPES
