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


def test_restart_marks_running_terminals_ended_and_unlinks_job_terminals():
    db = sqlite3.connect(":memory:")
    service = TerminalService(db)
    service.save({"id": "t", "project_id": "p", "session_id": "s", "status": "running", "created": 1,
                  "controller": "private-controller"})
    service.save({"id": "old", "project_id": "p", "session_id": "s", "turn_id": "turn", "tool_call_id": "call",
                  "background_job_id": "job_1", "status": "exited", "created": 1, "ended": 2, "controller": None})
    service = TerminalService(db)
    by_id = {t["id"]: t for t in service.list("p")}
    assert by_id["t"]["status"] == "ended" and by_id["t"]["controller"] is None
    assert by_id["old"]["status"] == "closed"
    assert "background_job_id" not in by_id["old"] and "turn_id" not in by_id["old"]


async def test_bounded_parser_preserves_raw_terminal_output(tmp_path, monkeypatch):
    service = TerminalService()
    shell = tmp_path / "shell.sh"
    shell.write_text("#!/bin/bash\nprintf '\\033]2;'; head -c 12000 /dev/zero | tr '\\0' X; printf '\\007raw-end'\n")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    term = service.create(project_id="p", workspace=str(tmp_path), creator="test", session_id=None, rows=24, cols=80)
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


async def test_background_job_leaves_inspector_without_terminal_lanes(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    store = remembering(Store(tmp_path / "test.db"), "scripted/m")
    runtime = make_runtime(store)
    session = store.create_session(runtime.open_project(str(tmp_path)), title="Diagnostics", agent="build", model="scripted/m")
    turn = store.start_turn(session.id)
    call = ToolCall("real-call", "bash", '{"command":"sleep 30"}')
    store.mark_tool_started(turn["id"], session.id, call)
    try:
        await runtime._start_job(session, session, {"command": "sleep 30"}, ToolContext(tmp_path, call_id=call.id))
        job = runtime.jobs.list()[0]
        assert job.turn_id == turn["id"] and job.tool_call_id == call.id
        store.finish_turn(turn["id"], "completed")
        result = inspect(store, session.id)
        assert not any(lane["kind"] == "terminal" for lane in result["lanes"])
        lane = next(lane for lane in result["lanes"] if lane["kind"] == "tool")
        assert "resource_spans" not in json.loads(lane["detail"])
        with pytest.raises(ValueError):
            inspect(store, session.id, kind="terminal")
        with pytest.raises(ValueError):
            record(store, session.id, "terminal", "x")
        assert head(store, session.id)["head"] is not None
    finally:
        await runtime.jobs.shutdown()
        store.close()
