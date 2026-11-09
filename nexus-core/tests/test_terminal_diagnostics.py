import asyncio
import base64
import json
import sqlite3

import pytest
from conftest import remembering

from nexus.inspector_data import head, inspect, record
from nexus.llmhub.types import ToolCall
from nexus.server import make_runtime
from nexus.store import Store
from nexus.terminals import MAX_CONTROL_CHARS, BoundedByteStream, Screen, TerminalService
from nexus.tools.base import ToolContext


def test_restart_announces_sanitized_linked_terminal_completion_once():
    db = sqlite3.connect(":memory:")
    service = TerminalService(db)
    service.save({"id": "t", "project_id": "p", "session_id": "s", "turn_id": "turn", "tool_call_id": "call",
                  "status": "running", "created": 1, "controller": "private-controller", "workspace": "/private/path"})
    events = []
    TerminalService(db, events.append)
    assert len(events) == 1 and events[0]["status"] == "ended"
    assert events[0]["ended"] is not None
    assert "controller" not in events[0] and "workspace" not in events[0]
    TerminalService(db, events.append)
    assert len(events) == 1


async def test_bounded_parser_preserves_raw_terminal_output(tmp_path):
    service = TerminalService()
    term = service.create(project_id="p", workspace=str(tmp_path), creator="test", session_id=None,
                          rows=24, cols=80,
                          command="printf '\\033]2;'; head -c 12000 /dev/zero | tr '\\0' X; printf '\\007raw-end'")
    try:
        await asyncio.wait_for(term.waiter, 5)
        raw = b"".join(base64.b64decode(frame["data"]) for frame, _ in term.frames if frame["type"] == "output")
        assert raw == b"\x1b]2;" + b"X" * 12000 + b"\x07raw-end"
        assert term.screen.title == ""
        assert "raw-end" in term.screen.display[0]
    finally:
        await service.shutdown()


@pytest.mark.parametrize("prefix,terminator", [(b"\x1b]2;", b"\x07"), (b"\x1b]0;", b"\x1b\\"),
                                               (b"\x1b[", b"m")])
def test_core_parser_bounds_unterminated_control_strings(prefix, terminator):
    screen = Screen(80, 24)
    parser = BoundedByteStream(screen)
    parser.feed(prefix)
    for _ in range(256):
        parser.feed(b"1" * 4096)
    assert not parser._taking_plain_text
    assert parser._parser.gi_frame.f_locals["dropping"]
    assert parser._parser.gi_frame.f_locals["parser"].gi_frame is None
    assert len(screen.title) <= MAX_CONTROL_CHARS
    parser.feed(terminator + b"safe-marker")
    assert parser._taking_plain_text
    assert "safe-marker" in screen.display[0]
    assert len(screen.title) <= MAX_CONTROL_CHARS


async def test_terminal_diagnostics_link_only_real_tools_without_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    store = remembering(Store(tmp_path / "test.db"), "scripted/m")
    runtime = make_runtime(store)
    session = store.create_session(runtime.open_project(str(tmp_path)), title="Diagnostics", agent="build", model="scripted/m")
    service = TerminalService(store.db, lambda data: runtime.bus.publish(
        "terminal.updated", data, session_id=data["session_id"]))
    runtime.jobs.terminal_service = service
    turn = store.start_turn(session.id)
    call = ToolCall("real-call", "bash", '{"command":"read value"}')
    store.mark_tool_started(turn["id"], session.id, call)
    try:
        await runtime._start_job(session, session, {"command": "read value"},
                                 ToolContext(tmp_path, call_id=call.id))
        term = next(iter(service.terminals.values()))
        assert term.turn_id == turn["id"] and term.tool_call_id == call.id
        store.finish_turn(turn["id"], "completed")
        live = inspect(store, session.id, kind="terminal", status="running")
        assert len(live["lanes"]) == 1 and live["lanes"][0]["running"]
        aid = term.attach("private-client-identity")
        term.attachments[aid].connected = True
        before_control = head(store, session.id)["head"]
        term.control(aid, "acquire")
        acquired_head = head(store, session.id)["head"]
        assert acquired_head > before_control
        term.control(aid, "acquire")
        assert head(store, session.id)["head"] == acquired_head
        term.input(aid, 1, b"PRIVATE-TERMINAL-PAYLOAD\n")
        await asyncio.wait_for(term.waiter, 5)
        term.detach(aid)
        assert head(store, session.id)["head"] > acquired_head
        result = inspect(store, session.id)
        lane = next(lane for lane in result["lanes"] if lane["kind"] == "tool")
        spans = json.loads(lane["detail"])["resource_spans"]
        assert len(spans) == 1
        span = spans[0]
        assert span["id"] == term.id and span["kind"] == "terminal" and span["diagnostic_only"]
        assert span["finished"] >= span["started"]
        assert span["control_acquisitions"] == span["control_releases"] == 1
        terminal_lane = next(lane for lane in result["lanes"] if lane["kind"] == "terminal")
        assert terminal_lane["start"] == span["started"] and terminal_lane["end"] == span["finished"]
        assert not terminal_lane["running"] and not terminal_lane["end_unknown"]
        full = record(store, session.id, "terminal", term.id)
        assert full["record"] == span
        assert len(inspect(store, session.id, kind="terminal", status="exited")["lanes"]) == 1
        assert "PRIVATE-TERMINAL-PAYLOAD" not in lane["detail"]
        assert "private-client-identity" not in lane["detail"]
        assert not any(lane["kind"] == "model" for lane in result["lanes"])
        assert inspect(store, session.id, view="Conversation")["records"] == []
        await runtime._start_job(session, session, {"command": "true"}, ToolContext(tmp_path, call_id="not-recorded"))
        unlinked = list(service.terminals.values())[-1]
        assert unlinked.turn_id is None and unlinked.tool_call_id is None
        assert record(store, session.id, "terminal", unlinked.id) is None
        assert len(json.loads(next(lane for lane in inspect(store, session.id)["lanes"]
                                   if lane["kind"] == "tool")["detail"])["resource_spans"]) == 1
    finally:
        await service.shutdown()
        await runtime.jobs.shutdown()
        store.close()
