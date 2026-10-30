"""Durable recovery for turns interrupted by a daemon crash or restart.

The crash is made deterministic by writing exactly the durable state each crash
window leaves behind (turn, request, tool and event rows) and then starting a new
runtime over the same database. `Recovery(crash_after=...)` interrupts recovery
itself at named points. No test calls a provider or the network.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from nexus import runtime as runtime_module
from nexus.bus import Bus
from nexus.codec import to_json
from nexus.eventlog import Stream
from nexus.llmhub.types import Done, Message, PartDone, Text, ToolCall, ToolResult
from nexus.observations import FileGuard, content_revision
from nexus.questions import result_text
from nexus.recovery import CRASH_POINTS, Recovery, SimulatedCrash
from nexus.runtime import Runtime, message_json
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.requests = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "instructions": req.instructions,
                "tools": [t.name for t in req.tools]}

    async def stream(self, req):
        self.requests.append(req)
        parts = self.replies.pop(0)
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


class Crash:
    """Writes the durable rows a crash at one execution window would leave behind."""

    def __init__(self, db: Path, workspace: Path) -> None:
        self.store = Store(db)
        self.bus = Bus()
        self.bus.attach(self.store)
        self.project, _ = self.store.project_for_path(str(workspace))

    def session(self, *, parent=None, title="crash"):
        return self.store.create_session(self.project, title=title, agent="build", parent=parent)

    def turn(self, session):
        return self.store.start_turn(session.id)

    def request(self, turn, step=1):
        return self.store.start_request(turn["id"], turn["session_id"], step)

    def event(self, session, type_, data):
        return self.bus.publish(type_, data, session_id=session.id)

    def publish_message(self, session, stored):
        event = self.bus.publish("message.added", message_json(stored), session_id=session.id)
        if event.get("id") is not None:
            self.store.link_message_event(stored.id, event["id"])
        return event

    def assistant(self, session, turn, calls, *, request=None):
        stored = self.store.save_assistant_message(
            session.id, Message("assistant", tuple(calls)), turn_id=turn["id"],
            request_id=request["id"] if request else None)
        self.publish_message(session, stored)
        return stored

    def close(self):
        self.store.close()


def restart(db: Path, model="scripted/m") -> Runtime:
    runtime = Runtime(Store(db), Bus(), model)
    runtime.recover()
    return runtime


def texts(store: Store, session_id: str, role: str | None = None) -> list[str]:
    out = []
    for stored in store.messages(session_id):
        if role is not None and stored.message.role != role:
            continue
        for part in stored.message.parts:
            if isinstance(part, Text):
                out.append(part.text)
    return out


def tool_results(store: Store, session_id: str) -> list[ToolResult]:
    return [part for stored in store.messages(session_id) for part in stored.message.parts
            if isinstance(part, ToolResult)]


def event_types(store: Store, session_id: str) -> list[str]:
    return [event["type"] for event in store.events(session_id, limit=5000)]


def newcomer(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir(exist_ok=True)
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    return workspace, provider


async def _idle(rt: Runtime, session_id: str):
    for _ in range(500):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"still running: {list(rt.turns)}")


# -- the durable windows ------------------------------------------------------


def test_accepted_turn_before_its_first_provider_request(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [notice] = texts(rt.store, session.id)
        assert "No provider request was recorded" in notice
        assert "prompt itself was not saved" in notice
        [recovered] = rt.recovered
        assert recovered["request"] is None and recovered["assistant_message"] is None
        stored_turn = rt.store.turn(turn["id"])
        assert (stored_turn["status"], stored_turn["recovery"]) == ("interrupted", "done")
        assert rt.store.unfinished_turns() == []
        assert rt.turns == {}
        events = rt.store.events(session.id)
        assert [e["type"] for e in events].count("turn.recovered") == 1
        idle = [e for e in events if e["type"] == "session.status" and e["data"].get("turn_id") == turn["id"]]
        assert len(idle) == 1 and idle[0]["data"]["status"] == "idle"
    finally:
        rt.store.close()


def test_interrupted_stream_keeps_output_as_diagnostic_history_only(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    notes = [
        ("wire_response", {"type": "wire_response", "status": 200, "headers": {}}),
        ("text_delta", {"type": "text_delta", "text": "hello"}),
        ("reasoning_delta", {"type": "reasoning_delta", "text": "hm"}),
        ("tool_call_started", {"type": "tool_call_started", "id": "c1", "name": "bash"}),
        ("tool_args_delta", {"type": "tool_args_delta", "id": "c1", "delta": '{"c'}),
        ("part_done", {"type": "part_done", "part": {"type": "text", "text": "hel",
                                                     "origin": "codex", "raw": {"type": "message"}}}),
    ]
    ids = [crash.event(session, name, data)["id"] for name, data in notes]
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        # Only the new recovery notice is conversation; the partial stream is not.
        assert [m.message.role for m in rt.store.messages(session.id)] == ["user"]
        assert not tool_results(rt.store, session.id)
        [notice] = texts(rt.store, session.id)
        assert "interrupted while streaming" in notice
        assert "5 text characters" in notice and "2 reasoning characters" in notice
        assert "3 tool-argument characters" in notice and "`bash`" in notice
        assert "not added to the conversation" in notice
        stored = rt.store.request(request["id"])
        assert stored["status"] == "interrupted" and "daemon restarted" in stored["data"]["reason"]
        # The original durable events, ids and raw part survive untouched.
        kept = {e["id"]: e for e in rt.store.events(session.id)}
        assert all(event_id in kept for event_id in ids)
        part = kept[ids[-1]]["data"]["part"]
        assert part["raw"] == {"type": "message"} and part["origin"] == "codex"
        [recovered] = rt.recovered
        assert recovered["request"]["status"] == "interrupted"
        assert recovered["assistant_message"] is None
        assert recovered["partial_output"]["diagnostic_only"] is True
        assert recovered["partial_output"]["counts"]["text_delta"]["chars"] == 5
    finally:
        rt.store.close()


def test_completed_response_is_restored_before_its_message_is_lost(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    parts = [
        to_json(Text("the answer", origin="codex", raw={"type": "message", "role": "assistant"})),
        to_json(ToolCall("c1", "read", '{"path": "a.txt"}', origin="codex",
                         raw={"type": "function_call", "call_id": "c1"})),
    ]
    crash.store.finish_request(request["id"], stop="tool_calls", response_id="resp_1", parts=parts)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    messages = rt.store.messages(session.id)
    assert [m.message.role for m in messages] == ["assistant", "tool", "user"]
    assert message_json(messages[0])["parts"][0] == parts[0]
    assert message_json(messages[0])["parts"][1] == parts[1]
    [result] = tool_results(rt.store, session.id)
    assert result.is_error and "before this tool ran" in result.output
    [notice] = texts(rt.store, session.id, "user")
    assert "had completed" in notice and "restored as assistant message" in notice
    assert "was not run" in notice or "did not run" in notice
    stored = rt.store.request(request["id"])
    assert stored["message_id"] == messages[0].id
    [recovered] = rt.recovered
    assert recovered["assistant_message"] == messages[0].id
    # A second startup changes nothing: the request link is durable.
    before = (len(rt.store.messages(session.id)), len(rt.store.events(session.id)))
    rt.store.close()
    again = restart(tmp_path / "nexus.db")
    try:
        assert again.recovered == []
        assert (len(again.store.messages(session.id)), len(again.store.events(session.id))) == before
    finally:
        again.store.close()


def test_started_tool_without_result_is_reported_unknown(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "bash", '{"command": "touch changed"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [result] = tool_results(rt.store, session.id)
        assert result.is_error
        assert "result is unknown" in result.output and "may have changed the workspace" in result.output
        assert not (workspace / "changed").exists()
        [notice] = texts(rt.store, session.id, "user")
        assert "it had started; its result is unknown" in notice
        [recovered] = rt.recovered
        assert recovered["tools"][0]["outcome"] == "unknown"
    finally:
        rt.store.close()


def test_completed_result_missing_from_history_is_inserted_once(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    a = workspace / "a.txt"
    a.write_text("one\ntwo\n")
    b = workspace / "b.txt"
    b.write_text("bee\n")
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    calls = [ToolCall("c1", "read", '{"path": "a.txt"}'), ToolCall("c2", "read", '{"path": "b.txt"}')]
    assistant = crash.assistant(session, turn, calls, request=request)

    # c1 recorded a real observation; c2 produced no observation (as a listing or a
    # pre-lifecycle read would not).
    guard = FileGuard(crash.store, session.id, None, pending=[])
    observation = guard.record_read(a, a.read_bytes(), lines=2, ranges=[(1, 2)], call_id="c1")
    crash.store.mark_tool_started(turn["id"], session.id, calls[0])
    crash.store.mark_tool_finished(turn["id"], session.id, calls[0], ToolResult("c1", "     1\tone\n     2\ttwo"),
                                   [observation.json() | {"path": observation.path}])
    crash.store.mark_tool_started(turn["id"], session.id, calls[1])
    crash.store.mark_tool_finished(turn["id"], session.id, calls[1], ToolResult("c2", "     1\tbee"))
    crash.close()
    a.write_text("changed after the crash\n")
    b.write_text("changed after the crash\n")

    rt = restart(tmp_path / "nexus.db")
    try:
        [first, second] = tool_results(rt.store, session.id)
        assert first.output == "     1\tone\n     2\ttwo" and not first.is_error
        assert second.output == "     1\tbee" and not second.is_error
        [tool_message] = [m for m in rt.store.messages(session.id) if m.message.role == "tool"]
        assert tool_message.id and len(tool_message.message.parts) == 2
        # Only the read whose output was restored grants coverage, at its recorded revision.
        stored = rt.store.file_observation(session.id, str(a))
        assert stored is not None and stored["revision"] == content_revision(b"one\ntwo\n")
        assert rt.store.file_observation(session.id, str(b)) is None
        [assistant_stored] = [m for m in rt.store.messages(session.id) if m.message.role == "assistant"]
        assert assistant_stored.id == assistant.id
        [notice] = texts(rt.store, session.id, "user")
        assert "completed result was preserved exactly" in notice
    finally:
        rt.store.close()


def test_durable_turn_end_is_not_reported_as_interrupted(tmp_path):
    # A turn left open before `turn.ended` existed: the log's `turn_end` is the fallback evidence.
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    user = crash.store.append_message(session.id, Message("user", (Text("hi"),)))
    crash.publish_message(session, user)
    assistant = crash.store.save_assistant_message(
        session.id, Message("assistant", (Text("done"),)), turn_id=turn["id"], request_id=request["id"])
    crash.publish_message(session, assistant)
    crash.event(session, "turn_end", {"type": "turn_end", "stop": "end_turn", "steps": 1})
    # Crash before finish_turn wrote `completed`.
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    [summary] = rt.recovered
    assert summary["outcome"] == "completed" and summary["notice"] is None
    [stored] = rt.store.turns_for_session(session.id)
    assert (stored["status"], stored["recovery"]) == ("completed", "done")
    # No recovery notice: nothing was interrupted from the model's point of view.
    assert [m.message.role for m in rt.store.messages(session.id)] == ["user", "assistant"]
    events = rt.store.events(session.id)
    assert [e["type"] for e in events].count("turn.recovered") == 1
    idle = [e for e in events if e["type"] == "session.status" and e["data"].get("turn_id") == turn["id"]]
    assert len(idle) == 1 and idle[0]["data"]["status"] == "idle"
    rt.store.close()
    again = restart(tmp_path / "nexus.db")
    try:
        assert again.recovered == []
    finally:
        again.store.close()


def test_turn_ended_row_proves_completion_without_the_event_log(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    user = crash.store.append_message(session.id, Message("user", (Text("hi"),)))
    crash.publish_message(session, user)
    crash.assistant(session, turn, [Text("done")], request=request)
    crash.event(session, "turn_end", {"type": "turn_end", "stop": "end_turn", "steps": 1})
    crash.store.mark_turn_ended(turn["id"], "end_turn", 1)
    # Crash before finish_turn; retention or anything else removed every event but the outbox.
    crash.store.db.execute("delete from event where type != 'message.added'")
    crash.store.db.commit()
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [summary] = rt.recovered
        assert summary["outcome"] == "completed" and summary["notice"] is None
        [stored] = rt.store.turns_for_session(session.id)
        assert (stored["status"], stored["steps"], stored["recovery"]) == ("completed", 1, "done")
        assert [m.message.role for m in rt.store.messages(session.id)] == ["user", "assistant"]
        [event] = rt.store.events(session.id, types=["turn.summary"])
        assert event["data"] == rt.store.turn_summaries(session.id)["turns"][0]
        assert event["data"]["stop"] == "end_turn" and event["data"]["ended"] == {"stop": "end_turn", "steps": 1}
    finally:
        rt.store.close()


def test_recovered_interrupted_turn_publishes_its_summary_once(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    crash.store.start_request(turn["id"], session.id, 1, model="scripted/m", effort="low")
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        assert rt.recovered[0]["outcome"] == "interrupted"
        [event] = rt.store.events(session.id, types=["turn.summary"])
        [entry] = rt.store.turn_summaries(session.id)["turns"]
        assert event["data"] == entry
        assert (entry["status"], entry["recovery"], entry["requests"][0]["status"]) == (
            "interrupted", "done", "interrupted")
        assert (entry["requests"][0]["model"], entry["requests"][0]["usage"]) == ("scripted/m", None)
    finally:
        rt.store.close()
    again = restart(tmp_path / "nexus.db")
    try:
        assert len(again.store.events(session.id, types=["turn.summary"])) == 1
    finally:
        again.store.close()


def test_saved_tool_calls_before_execution_do_not_run(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    calls = [ToolCall("c1", "bash", '{"command": "touch one"}'),
             ToolCall("c2", "write", '{"path": "two", "content": "no"}')]
    crash.assistant(session, turn, calls, request=request)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        results = tool_results(rt.store, session.id)
        assert [result.call_id for result in results] == ["c1", "c2"]
        assert all(result.is_error and "before this tool ran" in result.output for result in results)
        assert not (workspace / "one").exists() and not (workspace / "two").exists()
        [recovered] = rt.recovered
        assert [item["outcome"] for item in recovered["tools"]] == ["not_run", "not_run"]
    finally:
        rt.store.close()


def test_mixed_multi_call_batch_is_closed_by_evidence(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    done = ToolCall("c1", "write", '{"path": "x.txt", "content": "hi"}')
    running = ToolCall("c2", "bash", '{"command": "touch y"}')
    unstarted = ToolCall("c3", "read", '{"path": "x.txt"}')
    crash.assistant(session, turn, [done, running, unstarted], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, done)
    crash.store.mark_tool_finished(turn["id"], session.id, done, ToolResult("c1", "created x.txt"))
    crash.store.mark_tool_started(turn["id"], session.id, running)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        results = tool_results(rt.store, session.id)
        assert [r.call_id for r in results] == ["c1", "c2", "c3"]
        assert (results[0].output, results[0].is_error) == ("created x.txt", False)
        assert "result is unknown" in results[1].output and results[1].is_error
        assert "before this tool ran" in results[2].output and results[2].is_error
        [tool_message] = [m for m in rt.store.messages(session.id) if m.message.role == "tool"]
        assert [p.call_id for p in tool_message.message.parts] == ["c1", "c2", "c3"]
        states = {row["call_id"]: row["state"] for row in rt.store.tools_for_turn(turn["id"])}
        assert states == {"c1": "inserted", "c2": "inserted", "c3": "inserted"}
        [recovered] = rt.recovered
        assert [item["outcome"] for item in recovered["tools"]] == ["restored", "unknown", "not_run"]
    finally:
        rt.store.close()


async def test_pending_approval_and_question_are_cancelled_and_unusable(tmp_path, monkeypatch):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    bash = ToolCall("c1", "bash", '{"command": "true"}')
    ask = ToolCall("c2", "ask", '{"questions": [{"id": "q", "question": "Which?"}]}')
    crash.assistant(session, turn, [bash, ask], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, bash)
    crash.store.mark_tool_started(turn["id"], session.id, ask)
    crash.event(session, "permission.asked", {
        "id": "per_1", "session_id": session.id, "tool": "bash", "call_id": "c1", "args": {}})
    crash.event(session, "question.asked", {
        "id": "qst_1", "session_id": session.id, "call_id": "c2", "created": time.time(),
        "questions": [{"id": "q", "question": "Which?", "options": [],
                       "multiple": False, "allow_free_text": True}]})
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        results = tool_results(rt.store, session.id)
        assert "waiting for approval" in results[0].output and "did not run" in results[0].output
        assert "waiting for the user's answer" in results[1].output and results[1].is_error
        events = rt.store.events(session.id)
        permission_replies = [e for e in events if e["type"] == "permission.replied"]
        question_replies = [e for e in events if e["type"] == "question.replied"]
        assert len(permission_replies) == 1
        assert permission_replies[0]["data"]["outcome"] == "cancelled"
        assert permission_replies[0]["data"]["allow"] is False
        assert len(question_replies) == 1 and question_replies[0]["data"]["outcome"] == "cancelled"
        [notice] = texts(rt.store, session.id, "user")
        assert "Cancelled pending waits" in notice and "per_1" in notice and "qst_1" in notice
        [recovered] = rt.recovered
        assert recovered["waits"] == {"questions": ["qst_1"], "approvals": ["per_1"]}

        app = build_app(rt, token=TOKEN, url="http://test")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                   headers={"authorization": f"Bearer {TOKEN}"})
        late_question = await client.post(
            f"/api/session/{session.id}/question/qst_1",
            json={"answers": [{"question_id": "q", "text": "late"}]})
        late_approval = await client.post(
            f"/api/session/{session.id}/permission/per_1", json={"reply": "allow"})
        assert late_question.status_code == 409
        assert late_approval.status_code == 404
        await client.aclose()
        assert rt.pending_questions(session.id) == [] and rt.pending_approvals(session.id) == []
        assert len([e for e in rt.store.events(session.id) if e["type"] == "question.replied"]) == 1
    finally:
        rt.store.close()


def test_message_saved_before_its_event_is_announced_once(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    crash.request(turn)
    # The message row committed; the event did not.
    user = crash.store.append_message(session.id, Message("user", (Text("hello"),)))
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    announcements = [e for e in rt.store.events(session.id, limit=5000)
                     if e["type"] == "message.added" and e["data"].get("id") == user.id]
    assert len(announcements) == 1
    assert rt.store.message_event_id(user.id) == announcements[0]["id"]
    # Context was not duplicated: the original user message exists once, then the notice.
    assert [m.message.role for m in rt.store.messages(session.id)] == ["user", "user"]
    assert next(m.id for m in rt.store.messages(session.id)) == user.id
    before = (len(rt.store.messages(session.id)), len(rt.store.events(session.id, limit=5000)))
    rt.store.close()
    again = restart(tmp_path / "nexus.db")
    try:
        assert again.recovered == []
        assert (len(again.store.messages(session.id)),
                len(again.store.events(session.id, limit=5000))) == before
    finally:
        again.store.close()


def test_durable_question_answer_is_restored_instead_of_cancelled(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    ask = ToolCall("c1", "ask", '{"questions": [{"id": "db", "question": "Which?"}]}')
    crash.assistant(session, turn, [ask], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, ask)
    questions = [{"id": "db", "question": "Which?", "options": [], "multiple": False,
                  "allow_free_text": True}]
    crash.event(session, "question.asked", {
        "id": "qst_1", "session_id": session.id, "call_id": "c1", "created": time.time(),
        "questions": questions})
    crash.event(session, "question.replied", {
        "id": "qst_1", "session_id": session.id, "call_id": "c1", "outcome": "answered",
        "answers": [{"question_id": "db", "selected": [], "text": "PostgreSQL"}]})
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [result] = tool_results(rt.store, session.id)
        assert not result.is_error
        assert result.output == result_text(
            questions, [{"question_id": "db", "selected": [], "text": "PostgreSQL"}])
        types = event_types(rt.store, session.id)
        assert types.count("question.replied") == 1
        [notice] = texts(rt.store, session.id, "user")
        assert "durable answer was restored" in notice
        assert rt.recovered[0]["tools"][0]["outcome"] == "question_answered"
    finally:
        rt.store.close()


def test_a_replied_denial_is_not_reported_as_a_lost_wait(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "bash", '{"command": "true"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.event(session, "permission.asked", {
        "id": "per_1", "session_id": session.id, "tool": "bash", "call_id": "c1", "args": {}})
    crash.event(session, "permission.replied", {
        "id": "per_1", "session_id": session.id, "call_id": "c1", "allow": False,
        "outcome": "denied"})
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [result] = tool_results(rt.store, session.id)
        assert "approval was denied" in result.output and "did not run" in result.output
        [recovered] = rt.recovered
        assert recovered["waits"] == {"questions": [], "approvals": []}
        assert recovered["tools"][0]["outcome"] == "approval_denied"
        [notice] = texts(rt.store, session.id, "user")
        assert "Cancelled pending waits" not in notice
        assert [e for e in rt.store.events(session.id) if e["type"] == "permission.replied"
                and e["data"].get("outcome") == "cancelled"] == []
    finally:
        rt.store.close()


def test_nested_code_and_runtime_losses_are_reported(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("code1", "code", '{"code": "x = 41"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.event(session, "code.call", {
        "kernel_id": "krn_1", "cell": 1, "index": 1, "parent_call_id": "code1",
        "tool": "bash", "arguments": {"command": "touch z"}, "output": "", "error": None})
    crash.event(session, "code.cell", {
        "kernel_id": "krn_1", "number": 1, "session_id": session.id, "call_id": "code1",
        "code": "x = 41", "stdout": "", "value": None, "duration_s": 0.1, "error": None})
    crash.event(session, "bash.started", {
        "id": "job_1", "session_id": session.id, "root": session.id, "command": "sleep 100",
        "directory": str(workspace), "started": time.time(), "status": "running"})
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [result] = tool_results(rt.store, session.id)
        assert result.is_error and "code cell" in result.output
        assert "kernel state did not survive" in result.output
        assert "result is unknown" in result.output and "not invented" in result.output
        [notice] = texts(rt.store, session.id, "user")
        assert "Kernels do not survive a daemon restart" in notice
        assert "job_1" in notice and "may still be running" in notice
        assert "was not killed by PID" in notice
        [recovered] = rt.recovered
        assert recovered["losses"] == {"kernels": True, "bash_jobs": ["job_1"]}
        # Nested evidence is untouched diagnostic history, never a fabricated result.
        assert {"code.call", "code.cell"} <= set(event_types(rt.store, session.id))
        assert not (workspace / "z").exists()
    finally:
        rt.store.close()


def test_code_with_a_durable_result_is_preserved_but_kernel_is_still_lost(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("code1", "code", '{"code": "x = 41"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.store.mark_tool_finished(
        turn["id"], session.id, call, ToolResult("code1", "kernel krn_1 · In [1] · 0.01 s · 0 calls\nOut [1]: 41"))
    crash.event(session, "code.cell", {"kernel_id": "krn_1", "number": 1, "session_id": session.id,
                                      "call_id": "code1", "code": "x = 41", "stdout": "", "value": "41",
                                      "duration_s": 0.01, "error": None})
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        [result] = tool_results(rt.store, session.id)
        assert not result.is_error and result.output.endswith("Out [1]: 41")
        [notice] = texts(rt.store, session.id, "user")
        assert "Kernels do not survive" in notice and "completed result was preserved exactly" in notice
        [recovered] = rt.recovered
        assert recovered["losses"]["kernels"] is True
    finally:
        rt.store.close()


def test_parent_and_child_recover_independently(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    parent = crash.session(title="parent")
    child = crash.session(parent=parent, title="child")
    parent_turn = crash.turn(parent)
    child_turn = crash.turn(child)
    parent_request = crash.request(parent_turn)
    child_request = crash.request(child_turn)
    parent_call = ToolCall("p1", "bash", '{"command": "true"}')
    child_call = ToolCall("k1", "bash", '{"command": "true"}')
    crash.assistant(parent, parent_turn, [parent_call], request=parent_request)
    crash.assistant(child, child_turn, [child_call], request=child_request)
    crash.store.mark_tool_started(parent_turn["id"], parent.id, parent_call)
    crash.store.mark_tool_started(child_turn["id"], child.id, child_call)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        assert len(rt.recovered) == 2
        assert texts(rt.store, parent.id, "user") and texts(rt.store, child.id, "user")
        assert "result is unknown" in tool_results(rt.store, parent.id)[0].output
        assert "result is unknown" in tool_results(rt.store, child.id)[0].output
        assert rt.turns == {} and rt.notify == {}
        assert not any(e["type"] == "session.notified" for e in rt.store.events(parent.id, limit=5000))
        assert not any(e["type"] == "session.notified" for e in rt.store.events(child.id, limit=5000))
        # The child's recovery notice never becomes the parent's context.
        assert not any("sub agent" in text for text in texts(rt.store, parent.id))
    finally:
        rt.store.close()


def test_archived_session_stays_archived_and_idle(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    crash.store.set_tree_archived(session.id, True)
    turn = crash.turn(session)
    call = ToolCall("c1", "write", '{"path": "x", "content": "no"}')
    crash.assistant(session, turn, [call], request=None)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        assert rt.store.session(session.id).archived == 1
        assert rt.turns == {}
        [notice] = texts(rt.store, session.id, "user")
        assert "remains archived and idle" in notice
        assert "session.unarchived" not in event_types(rt.store, session.id)
        [recovered] = rt.recovered
        assert recovered["turn_id"] == turn["id"]
    finally:
        rt.store.close()


@pytest.mark.parametrize("point", sorted(CRASH_POINTS))
def test_recovery_interrupted_midway_is_repeatable(tmp_path, point):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    write = ToolCall("c1", "write", '{"path": "x", "content": "hi"}')
    bash = ToolCall("c2", "bash", '{"command": "true"}')
    ask = ToolCall("c3", "ask", '{"questions": [{"id": "q", "question": "Which?"}]}')
    crash.assistant(session, turn, [write, bash, ask], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, write)
    crash.store.mark_tool_finished(turn["id"], session.id, write, ToolResult("c1", "created x"))
    crash.store.mark_tool_started(turn["id"], session.id, bash)
    crash.store.mark_tool_started(turn["id"], session.id, ask)
    crash.event(session, "permission.asked", {
        "id": "per_1", "session_id": session.id, "tool": "bash", "call_id": "c2", "args": {}})
    crash.event(session, "question.asked", {
        "id": "qst_1", "session_id": session.id, "call_id": "c3", "created": time.time(),
        "questions": [{"id": "q", "question": "Which?", "options": [],
                       "multiple": False, "allow_free_text": True}]})
    crash.close()

    rt = Runtime(Store(tmp_path / "nexus.db"), Bus(), "scripted/m")
    with pytest.raises(SimulatedCrash):
        Recovery(rt, crash_after=point).run()
    rt.store.close()
    rt = restart(tmp_path / "nexus.db")  # a new daemon over the same database
    try:
        assert rt.recovered and rt.store.turn(turn["id"])["recovery"] == "done"

        async def counts():
            messages = rt.store.messages(session.id)
            return {
                "assistant": sum(1 for m in messages if m.message.role == "assistant"),
                "tool": sum(1 for m in messages if m.message.role == "tool"),
                "notice": sum(1 for m in messages if m.message.role == "user"),
                "tool_results": len(tool_results(rt.store, session.id)),
            }

        first = asyncio.run(counts())
        assert first == {"assistant": 1, "tool": 1, "notice": 1, "tool_results": 3}
        types = event_types(rt.store, session.id)
        assert types.count("turn.recovered") == 1
        assert types.count("question.replied") == 1 and types.count("permission.replied") == 1
        idle = [e for e in rt.store.events(session.id) if e["type"] == "session.status"
                and e["data"].get("turn_id") == turn["id"]]
        assert len(idle) == 1
        # Every recovery message has exactly one announcement.
        for stored in rt.store.messages(session.id):
            announced = [e for e in rt.store.events(session.id)
                         if e["type"] == "message.added" and e["data"].get("id") == stored.id]
            assert len(announced) == 1

        rt.recover()  # nothing unfinished: a repeated startup appends nothing
        assert asyncio.run(counts()) == first
        assert len(rt.store.events(session.id)) == len(types)
    finally:
        rt.store.close()


def test_repeated_startup_appends_no_duplicate_context_or_events(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "bash", '{"command": "true"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    messages = [m.id for m in rt.store.messages(session.id)]
    events = [e["id"] for e in rt.store.events(session.id, limit=5000)]
    rt.store.close()
    for _ in range(3):
        again = restart(tmp_path / "nexus.db")
        try:
            assert again.recovered == []
            assert [m.id for m in again.store.messages(session.id)] == messages
            assert [e["id"] for e in again.store.events(session.id, limit=5000)] == events
        finally:
            again.store.close()


def test_migration_from_the_current_schema_keeps_history(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    user = crash.store.append_message(session.id, Message("user", (Text("before"),)))
    crash.store.link_message_event(user.id, crash.event(session, "message.added", message_json(user))["id"])
    assistant = crash.store.save_assistant_message(
        session.id, Message("assistant", (Text("answer"),)), turn_id=turn["id"], request_id=request["id"])
    crash.store.link_message_event(assistant.id, crash.event(session, "message.added", message_json(assistant))["id"])
    generation = crash.store.log_generation()
    message_ids = [m.id for m in crash.store.messages(session.id)]
    event_ids = [e["id"] for e in crash.store.events(session.id, limit=5000)]
    # Simulate the schema before this increment.
    crash.store.db.execute("drop table turn_attempt")
    crash.store.db.execute("drop table turn_tool")
    crash.store.db.execute("drop table turn_request")
    crash.store.db.execute("drop table turn")
    crash.store.db.execute("drop index if exists message_event")
    crash.store.db.execute("alter table message drop column event_id")
    crash.store.db.commit()
    crash.close()

    store = Store(tmp_path / "nexus.db")
    try:
        assert store.log_generation() == generation
        assert [m.id for m in store.messages(session.id)] == message_ids
        assert [e["id"] for e in store.events(session.id, limit=5000)] == event_ids
        assert store.unfinished_turns() == []
        rt = Runtime(store, Bus(), "scripted/m")
        rt.recover()
        assert rt.recovered == []
        # No invented execution state, no rewritten context.
        assert [m.id for m in store.messages(session.id)] == message_ids
        assert [e["id"] for e in store.events(session.id, limit=5000)] == event_ids
    finally:
        store.close()


def test_legacy_dangling_context_is_labelled_unknown_once(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    user = crash.store.append_message(session.id, Message("user", (Text("do it"),)))
    crash.publish_message(session, user)
    call = ToolCall("old1", "bash", '{"command": "touch old"}')
    assistant = crash.store.append_message(session.id, Message("assistant", (call,)))
    crash.publish_message(session, assistant)
    message_ids = [m.id for m in crash.store.messages(session.id)]
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    [recovered] = rt.recovered
    assert recovered["legacy"] is True
    assert recovered["request"] is None
    assert [m.message.role for m in rt.store.messages(session.id)] == ["user", "assistant", "tool", "user"]
    [result] = tool_results(rt.store, session.id)
    assert result.is_error and "outcome is unknown" in result.output and "not re-run" in result.output
    notices = [text for text in texts(rt.store, session.id, "user") if text.startswith("[Nexus recovery:")]
    assert len(notices) == 1
    notice = notices[0]
    assert "legacy interrupted turn" in notice
    assert "cannot tell which of them started" in notice
    assert not (workspace / "old").exists()
    [turn] = rt.store.turns_for_session(session.id)
    assert turn["data"]["legacy"] is True and turn["recovery"] == "done"
    assert [m.id for m in rt.store.messages(session.id)][:2] == message_ids
    before = (len(rt.store.messages(session.id)), len(rt.store.events(session.id, limit=5000)))
    rt.store.close()
    again = restart(tmp_path / "nexus.db")
    try:
        assert again.recovered == []
        assert (len(again.store.messages(session.id)),
                len(again.store.events(session.id, limit=5000))) == before
    finally:
        again.store.close()


async def test_recovery_events_replay_through_a_durable_cursor(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "bash", '{"command": "true"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.close()

    rt = restart(tmp_path / "nexus.db")
    try:
        stream = Stream.open(rt.store, rt.bus, after=0)
        replayed = []
        async for event in stream.events():
            replayed.append(event)
            if event["type"] == "turn.recovered":
                break
        stream.close()
        assert any(e["type"] == "turn.recovered" and e["session_id"] == session.id for e in replayed)
        recovered = next(e for e in replayed if e["type"] == "turn.recovered")
        assert isinstance(recovered["id"], int) and recovered["cursor"].startswith(rt.store.log_generation())
        assert any(e["type"] == "message.added" and e["data"]["role"] == "user" for e in replayed)
        assert any(e["type"] == "session.status" and e["data"]["status"] == "idle" for e in replayed)
        assert [e["id"] for e in replayed] == sorted(e["id"] for e in replayed)
    finally:
        rt.store.close()


def test_recovery_calls_no_provider_and_runs_no_tool(tmp_path, monkeypatch):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "bash", '{"command": "touch marker"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.close()

    def forbidden(ref):
        raise AssertionError("recovery must not resolve a provider")

    monkeypatch.setattr(runtime_module, "provider_for", forbidden)
    rt = restart(tmp_path / "nexus.db")
    try:
        assert rt.recovered and rt.turns == {}
        assert not (workspace / "marker").exists()
        assert not any(e["type"] in ("session.error", "wire_request") for e in rt.store.events(session.id))
    finally:
        rt.store.close()


async def test_normal_turn_lifecycle_and_clean_interrupt_need_no_recovery(tmp_path, monkeypatch):
    workspace, provider = newcomer(tmp_path, monkeypatch)
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime("scripted/m", store)
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    try:
        provider.replies = [[ToolCall("w1", "write", json.dumps({"path": "x.txt", "content": "hi"}))], [Text("done")]]
        session = (await client.post("/api/session", json={"directory": str(workspace), "permission": "ask"})).json()
        await client.post(f"/api/session/{session['id']}/prompt", json={"text": "make it"})
        await _idle(rt, session["id"])
        [turn] = store.turns_for_session(session["id"])
        assert (turn["status"], turn["recovery"]) == ("completed", "none")
        requests = store.requests_for_turn(turn["id"])
        assert [r["status"] for r in requests] == ["completed", "completed"]
        assert all(r["message_id"] for r in requests)
        assert {row["call_id"]: row["state"] for row in store.tools_for_turn(turn["id"])} == {"w1": "inserted"}
        assert store.unfinished_turns() == []

        # A clean interrupt closes the call and is not recovered as a crash later.
        provider.replies = [[ToolCall("b1", "bash", '{"command": "sleep 30"}')]]
        await client.post(f"/api/session/{session['id']}/prompt", json={"text": "wait"})
        for _ in range(500):
            if rt.pending_approvals(session["id"]):
                break
            await asyncio.sleep(0.01)
        assert rt.pending_approvals(session["id"])
        assert (await client.post(f"/api/session/{session['id']}/interrupt")).status_code == 204
        await _idle(rt, session["id"])
        [interrupted] = [t for t in store.turns_for_session(session["id"]) if t["status"] == "interrupted"]
        assert interrupted["recovery"] == "none"
        assert store.tools_for_turn(interrupted["id"])[0]["state"] == "inserted"
    finally:
        await client.aclose()
        await rt.shutdown()
        rt.store.close()

    rt2 = Runtime(Store(tmp_path / "nexus.db"), Bus(), "scripted/m")
    try:
        rt2.recover()
        assert rt2.recovered == []
    finally:
        rt2.store.close()


def test_subprocess_crash_and_restart_smoke(tmp_path):
    """Start the real daemon twice over one isolated home; recovery is idempotent."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = home / "nexus.db"
    crash = Crash(db, workspace)
    session = crash.session(title="smoke")
    turn = crash.turn(session)
    request = crash.request(turn)
    call = ToolCall("c1", "bash", '{"command": "true"}')
    crash.assistant(session, turn, [call], request=request)
    crash.store.mark_tool_started(turn["id"], session.id, call)
    crash.close()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = dict(os.environ)
    env.update({
        "NEXUS10_HOME": str(home),
        "NEXUS_PORT": str(port),
        "NEXUS_MODEL": "codex/gpt-6.1-terra",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    })

    def start():
        process = subprocess.Popen(
            [sys.executable, "-m", "nexus", "serve"], cwd=str(home), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                info = json.loads((home / "daemon.json").read_text())
                response = httpx.get(f"{info['url']}/api/info",
                                     headers={"authorization": f"Bearer {info['token']}"}, timeout=1)
                if response.status_code == 200:
                    return process, info
            except (OSError, ValueError, KeyError, httpx.HTTPError):
                pass
            if process.poll() is not None:
                raise AssertionError("daemon exited during startup")
            time.sleep(0.1)
        process.kill()
        process.wait(timeout=10)
        raise AssertionError("daemon did not start")

    def snapshot(info):
        headers = {"authorization": f"Bearer {info['token']}"}
        base = info["url"]
        messages = httpx.get(f"{base}/api/session/{session.id}/message", headers=headers, timeout=5).json()
        events = httpx.get(f"{base}/api/session/{session.id}/event", headers=headers, timeout=5).json()["events"]
        return messages, events

    process, info = start()
    try:
        assert info["pid"] == process.pid
        messages, events = snapshot(info)
        assert [m["role"] for m in messages] == ["assistant", "tool", "user"]
        assert any(p["type"] == "tool_result" and "result is unknown" in p["output"]
                   for m in messages for p in m["parts"])
        assert any(e["type"] == "turn.recovered" for e in events)
        first = ([m["id"] for m in messages], [e["id"] for e in events])
        first_pid = info["pid"]
    finally:
        process.kill()
        process.wait(timeout=10)

    # A hard kill left daemon.json behind; the next start must recover nothing twice.
    process, info = start()
    try:
        assert info["pid"] != first_pid
        messages, events = snapshot(info)
        assert ([m["id"] for m in messages], [e["id"] for e in events]) == first
    finally:
        process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize("idle_published", [False, True])
def test_closed_turn_before_cleanup_is_recovered_once(tmp_path, idle_published):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    crash = Crash(tmp_path / "nexus.db", workspace)
    session = crash.session()
    turn = crash.turn(session)
    crash.store.finish_turn(turn["id"], "completed")
    assert crash.store.turn(turn["id"])["recovery"] == "pending"
    if idle_published:
        crash.event(session, "session.status", {"status": "idle", "turn_id": turn["id"]})
    crash.close()
    rt = restart(tmp_path / "nexus.db")
    try:
        assert rt.recovered[0]["outcome"] == "completed"
        assert not rt.store.unfinished_turns()
        rt.recover()
        idle = [e for e in rt.store.events(session.id) if e["type"] == "session.status"
                and e["data"].get("turn_id") == turn["id"]]
        assert len(idle) == 1
        assert not texts(rt.store, session.id)
    finally:
        rt.store.close()


async def test_permission_storage_failure_does_not_release_tool(tmp_path, monkeypatch):
    store = Store(tmp_path / "nexus.db")
    rt = Runtime(store, Bus(), "scripted/m")
    future = asyncio.get_running_loop().create_future()
    rt.approvals["per_test"] = ("ses_test", future, {})

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "append_event", fail)
    try:
        with pytest.raises(OSError, match="disk unavailable"):
            rt.reply("ses_test", "per_test", True)
        await asyncio.sleep(0)
        assert not future.done()
    finally:
        future.cancel()
        store.close()


def test_message_link_migration_rolls_back_column_with_legacy_markers(tmp_path, monkeypatch):
    path = tmp_path / "nexus.db"
    store = Store(path)
    store.db.execute("drop index message_event")
    store.db.execute("alter table message drop column event_id")
    store.db.commit()
    store.close()
    connect = sqlite3.connect
    opened = []

    def failing_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)

        def authorize(action, table, column, database, trigger):
            if action == sqlite3.SQLITE_UPDATE and table == "message" and column == "event_id":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", failing_connect)
        with pytest.raises(sqlite3.DatabaseError):
            Store(path)
    for connection in opened:
        connection.close()
    check = connect(path)
    try:
        assert "event_id" not in {row[1] for row in check.execute("pragma table_info(message)")}
    finally:
        check.close()
    store = Store(path)
    store.close()
