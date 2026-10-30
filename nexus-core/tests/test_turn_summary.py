"""Turn summaries: usage, model, reasoning time, the spend ledger and their backfill.

TURN_SUMMARY_PLAN.md steps 1 and 2 at the store and runtime level. HTTP shape
and `turn.summary` events are in test_server.py; recovery in test_recovery.py.
No test calls a provider or the network.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from conftest import remembering

from nexus import runtime as runtime_module
from nexus.bus import Bus
from nexus.codec import to_json
from nexus.llmhub.types import (
    Done,
    PartDone,
    ProviderError,
    Reasoning,
    ReasoningDelta,
    Text,
    TextDelta,
    ToolCall,
    Usage,
    WireRequest,
)
from nexus.server import build_app, make_runtime
from nexus.store import USAGE_COLUMNS, Store

TOKEN = "t0ken"


class Events:
    """Replies are lists of stream events; an exception is raised, a callable is called."""

    name = "scripted"

    def __init__(self):
        self.replies: list[list] = []
        self.hold = asyncio.Event()

    def body(self, req):
        return {"model": req.model}

    async def stream(self, req):
        for item in self.replies.pop(0):
            if isinstance(item, Exception):
                raise item
            if item == "hang":
                await self.hold.wait()
            elif isinstance(item, float):
                await asyncio.sleep(item)
            elif callable(item):
                item()
            else:
                yield item


def reply(*parts, usage=None):
    events = [PartDone(p) for p in parts]
    if usage is not None:
        events.append(usage)
    events.append(Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn"))
    return events


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Events()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime(remembering(Store(tmp_path / "nexus.db"), "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    yield rt, client, provider, workspace
    rt.store.close()


async def _idle(rt, session_id):
    for _ in range(300):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def _turn(rt, client, workspace, *replies, session=None):
    """Run one prompt; returns (session, the turn's requests)."""
    provider = runtime_module.provider_for("scripted/m")[0]
    provider.replies = list(replies)
    if session is None:
        session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "go"})
    await _idle(rt, session["id"])
    turn = rt.store.turns_for_session(session["id"])[-1]
    return session, turn, rt.store.requests_for_turn(turn["id"])


# -- step 1: usage, model, effort, reasoning time --------------------------------


async def test_completed_request_saves_usage_model_reasoning_time_and_turn_end(daemon):
    rt, client, _, workspace = daemon
    events = [ReasoningDelta("thin"), 0.05, ReasoningDelta("king"), PartDone(Reasoning("thinking")),
              TextDelta("hi"), PartDone(Text("hi")), Usage(100, 40, 7, 3), Done("end_turn")]
    session, turn, [request] = await _turn(rt, client, workspace, events)
    assert request["status"] == "completed"
    assert (request["model"], request["effort"]) == ("scripted/m", None)
    assert request["usage"] == {"input": 100, "cached": 40, "output": 7, "reasoning": 3}
    assert 0.04 <= request["reasoning_seconds"] < 2
    assert turn["ended"] == {"stop": "end_turn", "steps": 1}
    [attempt] = rt.store.attempts_for_turn(turn["id"])
    assert [attempt[c] for c in USAGE_COLUMNS] == [100, 40, 7, 3]
    assert rt.store.session_usage(session["id"])["models"] == {
        "scripted/m": {"requests": 1, "input": 100, "cached": 40, "output": 7, "reasoning": 3}}


async def test_failed_request_keeps_its_usage(daemon):
    rt, client, _, workspace = daemon
    events = [TextDelta("par"), Usage(10, 0, 2, 0), ProviderError("stream broke", phase="stream")]
    session, turn, [request] = await _turn(rt, client, workspace, events)
    assert (turn["status"], request["status"]) == ("failed", "failed")
    assert request["usage"] == {"input": 10, "cached": 0, "output": 2, "reasoning": 0}
    assert turn["ended"] is None and request["reasoning_seconds"] is None
    assert rt.store.session_usage(session["id"])["models"]["scripted/m"]["requests"] == 1


async def test_interrupted_request_keeps_its_usage_and_reasoning_time(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[ReasoningDelta("hm"), Usage(5, 1, 1, 1), "hang"]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "go"})
    for _ in range(300):
        if rt.store.events(session["id"], types=["usage"]):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)
    assert (await client.post(f"/api/session/{session['id']}/interrupt")).status_code == 204
    await _idle(rt, session["id"])
    [turn] = rt.store.turns_for_session(session["id"])
    [request] = rt.store.requests_for_turn(turn["id"])
    assert (turn["status"], request["status"]) == ("interrupted", "interrupted")
    assert request["usage"] == {"input": 5, "cached": 1, "output": 1, "reasoning": 1}
    assert request["reasoning_seconds"] is not None and request["reasoning_seconds"] > 0


async def test_model_change_between_steps_is_recorded_per_request(daemon):
    rt, client, _, workspace = daemon
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()

    def switch():
        rt.update_session(session["id"], model="scripted/n", effort="high")

    first = [switch, *reply(ToolCall("c1", "read", json.dumps({"path": "."})), usage=Usage(1, 0, 1, 0))]
    _, _, requests = await _turn(rt, client, workspace, first, reply(Text("done"), usage=Usage(2, 1, 1, 0)),
                                 session=session)
    assert [(r["step"], r["model"], r["effort"]) for r in requests] == [
        (1, "scripted/m", None), (2, "scripted/n", "high")]
    models = rt.store.session_usage(session["id"])["models"]
    assert models["scripted/m"]["input"] == 1 and models["scripted/n"]["input"] == 2


async def test_request_failing_before_output_has_its_model_and_unknown_usage(daemon):
    rt, client, _, workspace = daemon
    session, turn, [request] = await _turn(
        rt, client, workspace, [ProviderError("bad request", status=400, phase="response")])
    assert (turn["status"], request["status"], request["model"]) == ("failed", "failed", "scripted/m")
    assert request["usage"] is None  # unknown, never zeros
    [attempt] = rt.store.attempts_for_turn(turn["id"])
    assert all(attempt[c] is None for c in USAGE_COLUMNS)
    assert rt.store.session_usage(session["id"]) == {"since": None, "models": {}}


async def test_completed_request_without_reported_usage_stays_unknown(daemon):
    rt, client, _, workspace = daemon
    session, _, [request] = await _turn(rt, client, workspace, reply(Text("hi")))
    assert request["usage"] is None
    summary = rt.store.turn_summaries(session["id"])["turns"][0]["requests"][0]
    assert summary["usage"] is None and summary["attempts"][0]["usage"] is None


# -- record_usage and the ledger ----------------------------------------------------


def _request(store, workspace, model="scripted/m"):
    project, _ = store.project_for_path(str(workspace))
    session = store.create_session(project, title="t", agent="build")
    turn = store.start_turn(session.id)
    request = store.start_request(turn["id"], session.id, 1, model=model, effort="low")
    return session, turn, request


def test_retried_request_sums_its_attempts_and_the_ledger_counts_both(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    store = Store(tmp_path / "nexus.db")
    try:
        session, turn, request = _request(store, workspace)
        first, second = f"{request['id']}:1", "att_2"
        store.start_attempt(turn["id"], session.id, request["id"], first, 1)
        store.record_usage(request["id"], first, Usage(10, 2, 3, 1))
        store.finish_attempt(first, "failed", error="cut off")
        store.start_attempt(turn["id"], session.id, request["id"], second, 2)
        store.record_usage(request["id"], second, Usage(12, 4, 5, 0))
        store.record_usage(request["id"], second, Usage(12, 4, 5, 0))  # a repeat changes nothing
        assert store.request(request["id"])["usage"] == {"input": 22, "cached": 6, "output": 8, "reasoning": 1}
        assert store.session_usage(session.id)["models"]["scripted/m"] == {
            "requests": 1, "input": 22, "cached": 6, "output": 8, "reasoning": 1}
        # The last report for an attempt wins; the ledger moves by the difference.
        store.record_usage(request["id"], second, Usage(20, 4, 9, 0))
        assert store.request(request["id"])["usage"] == {"input": 30, "cached": 6, "output": 12, "reasoning": 1}
        ledger = store.session_usage(session.id)
        assert ledger["models"]["scripted/m"] == {"requests": 1, "input": 30, "cached": 6, "output": 12,
                                                  "reasoning": 1}
        assert ledger["since"] is not None
        attempts = store.turn_summaries(session.id)["turns"][0]["requests"][0]["attempts"]
        assert [(a["n"], a["usage"]["input"]) for a in attempts] == [(1, 10), (2, 20)]
    finally:
        store.close()


def test_ledger_survives_pruned_turns_and_the_floor_announces_them(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    store = Store(tmp_path / "nexus.db")
    try:
        session, turn, request = _request(store, workspace)
        store.start_attempt(turn["id"], session.id, request["id"], "a1", 1)
        store.record_usage(request["id"], "a1", Usage(50, 0, 5, 0))
        store.finish_request(request["id"], stop="end_turn", response_id=None, parts=[])
        store.append_message(session.id, to_message("old"))
        store.finish_turn(turn["id"], "completed", steps=1)
        later = store.start_turn(session.id)
        store.finish_turn(later["id"], "completed")
        store.db.execute("update turn set recovery = 'none'")
        store.db.execute("update turn set started = 1, finished = 2 where id = ?", (turn["id"],))
        store.db.commit()
        assert store.turn_summaries(session.id)["floor"] is None
        assert store.prune_turns(now=5000, age_seconds=1000, keep_per_session=1)["turns_removed"] == 1
        page = store.turn_summaries(session.id)
        assert [t["id"] for t in page["turns"]] == [later["id"]]
        assert page["floor"] == {"seq": 2}  # message 1 belonged to the pruned turn
        assert page["usage"]["models"]["scripted/m"]["input"] == 50
    finally:
        store.close()


def to_message(text):
    from nexus.llmhub.types import Message

    return Message("user", (Text(text),))


# -- migration and backfill ---------------------------------------------------------


def _old_schema(store: Store) -> None:
    """Drop what this step added, as a database from before it looks."""
    for column in ("model", "effort", *USAGE_COLUMNS, "reasoning_seconds"):
        store.db.execute(f"alter table turn_request drop column {column}")
    for column in USAGE_COLUMNS:
        store.db.execute(f"alter table turn_attempt drop column {column}")
    store.db.execute("alter table turn drop column ended")
    store.db.execute("drop table session_usage")
    store.db.commit()


def test_backfill_fills_unambiguous_requests_once_and_leaves_the_rest_unknown(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    db = tmp_path / "nexus.db"
    store = Store(db)
    bus = Bus()
    bus.attach(store)
    project, _ = store.project_for_path(str(workspace))
    session = store.create_session(project, title="t", agent="build")
    turn = store.start_turn(session.id)

    def publish(event):
        data = to_json(event)
        bus.publish(data["type"], data, session_id=session.id)

    one = store.start_request(turn["id"], session.id, 1)
    store.start_attempt(turn["id"], session.id, one["id"], "a1", 1)
    store.start_attempt(turn["id"], session.id, one["id"], "a2", 2)
    publish(WireRequest("POST", "https://x", {}, {"model": "gpt-x", "reasoning": {"effort": "medium"}}))
    publish(Usage(100, 10, 20, 5))
    two = store.start_request(turn["id"], session.id, 2)
    publish(WireRequest("POST", "https://x", {}, {"model": "gpt-x", "reasoning_effort": "low"}))
    publish(Usage(1, 0, 1, 0))
    publish(Usage(2, 0, 1, 0))  # two reports in one range: ambiguous
    three = store.start_request(turn["id"], session.id, 3)  # no events at all
    _old_schema(store)
    store.close()

    store = Store(db)
    try:
        first, second, third = (store.request(r["id"]) for r in (one, two, three))
        assert (first["model"], first["effort"]) == ("gpt-x", "medium")
        assert first["usage"] == {"input": 100, "cached": 10, "output": 20, "reasoning": 5}
        attempts = {a["id"]: a for a in store.attempts_for_turn(turn["id"])}
        assert attempts["a2"]["input_tokens"] == 100 and attempts["a1"]["input_tokens"] is None
        assert (second["model"], second["effort"], second["usage"]) == ("gpt-x", "low", None)
        assert (third["model"], third["effort"], third["usage"]) == (None, None, None)
        assert store.turn(turn["id"])["ended"] is None
        ledger = store.session_usage(session.id)
        assert ledger["models"] == {"gpt-x": {"requests": 1, "input": 100, "cached": 10, "output": 20,
                                              "reasoning": 5}}
        assert ledger["since"] == first["started"]
    finally:
        store.close()

    again = Store(db)  # the columns exist now: no second backfill, no doubled ledger
    try:
        assert again.session_usage(session.id) == ledger
        assert again.request(one["id"])["usage"] == first["usage"]
    finally:
        again.close()


def test_new_database_has_the_columns_and_runs_no_backfill(tmp_path):
    store = Store(tmp_path / "nexus.db")
    try:
        columns = {r["name"] for r in store.db.execute("pragma table_info(turn_request)")}
        assert {"model", "effort", *USAGE_COLUMNS, "reasoning_seconds"} <= columns
        assert "ended" in {r["name"] for r in store.db.execute("pragma table_info(turn)")}
    finally:
        store.close()
    Store(tmp_path / "nexus.db").close()  # reopening is a no-op


# -- step 2: paging and query count ---------------------------------------------------


def _turns(store, workspace, count):
    project, _ = store.project_for_path(str(workspace))
    session = store.create_session(project, title="t", agent="build")
    ids = []
    for index in range(count):
        turn = store.start_turn(session.id, started=1000.0 + index)
        request = store.start_request(turn["id"], session.id, 1, model="scripted/m")
        store.start_attempt(turn["id"], session.id, request["id"], f"{request['id']}:1", 1)
        store.finish_turn(turn["id"], "completed", steps=1)
        ids.append(turn["id"])
    return session, ids


def test_a_long_session_pages_fifty_at_a_time_both_ways(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    store = Store(tmp_path / "nexus.db")
    try:
        session, ids = _turns(store, workspace, 500)
        page = store.turn_summaries(session.id)
        assert [t["id"] for t in page["turns"]] == ids[-50:]
        assert (page["more_before"], page["more_after"]) == (True, False)
        seen = page["turns"]
        while page["more_before"]:
            page = store.turn_summaries(session.id, before=seen[0]["id"])
            seen = page["turns"] + seen
        assert [t["id"] for t in seen] == ids
        forward = store.turn_summaries(session.id, after=ids[0], limit=200)
        assert [t["id"] for t in forward["turns"]] == ids[1:201] and forward["more_after"]
        tail = store.turn_summaries(session.id, after=ids[-3])
        assert [t["id"] for t in tail["turns"]] == ids[-2:] and not tail["more_after"]
        assert store.turn_summaries(session.id, after=ids[-1])["turns"] == []
        with pytest.raises(ValueError):
            store.turn_summaries(session.id, before="trn_nope")
        with pytest.raises(ValueError):
            store.turn_summaries(session.id, limit=201)
    finally:
        store.close()


def test_a_page_costs_the_same_queries_for_five_turns_or_fifty(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    store = Store(tmp_path / "nexus.db")
    try:
        small, _ = _turns(store, workspace, 5)
        large, _ = _turns(store, workspace, 50)
        counts = []
        for session in (small, large):
            statements: list[str] = []
            store.db.set_trace_callback(statements.append)
            try:
                page = store.turn_summaries(session.id)
            finally:
                store.db.set_trace_callback(None)
            assert all(t["requests"][0]["attempts"] for t in page["turns"])
            children = [s for s in statements if any(f"from {table} " in s for table in
                                                     ("turn_request", "turn_attempt", "turn_tool"))]
            assert len(children) == 3
            counts.append(len(statements))
        assert counts[0] == counts[1]
    finally:
        store.close()
