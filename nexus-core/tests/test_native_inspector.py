import json
import time

import httpx
import pytest
from conftest import remembering

from nexus.inspector_data import encode_cursor, head, inspect, record
from nexus.llmhub.types import Message, Text, ToolCall, ToolResult, Usage
from nexus.server import build_app, make_runtime
from nexus.store import Store


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    store = remembering(Store(tmp_path / "test.db"), "scripted/m")
    rt = make_runtime(store)
    session = store.create_session(rt.open_project(str(tmp_path)), title="Review", agent="build", model="scripted/m")
    yield rt, session.id
    store.close()


def test_empty_and_invalid(data):
    rt, sid = data
    result = inspect(rt.store, sid)
    assert result["cache"]["input"] is None
    assert result["cache"]["hit_percent"] is None
    assert result["lanes"] == []
    for options in ({"view": "bad"}, {"limit": 0}, {"limit": 201}, {"before": "junk"}, {"session": "nope"},
                    {"view": "Steps", "kind": "bogus"}, {"view": "Raw", "before": encode_cursor("c", 3)}):
        with pytest.raises(ValueError):
            inspect(rt.store, sid, **options)


def test_durable_usage_and_parallel_tools(data):
    rt, sid = data
    s = rt.store
    turn = s.start_turn(sid, started=100)
    request = s.start_request(turn["id"], sid, 1, model="scripted/m")
    s.record_usage(request["id"], None, Usage(1000, 750, 20, 5))
    s.record_usage(request["id"], None, Usage(1000, 750, 20, 5))
    for id_ in ("a", "b"):
        call = ToolCall(id_, "bash", '{"command":"true"}')
        s.mark_tool_started(turn["id"], sid, call)
        s.mark_tool_finished(turn["id"], sid, call, ToolResult(id_, "failed", is_error=True))
    result = inspect(s, sid, now=200)
    assert result["cache"]["input"] == 1000
    assert result["cache"]["cached"] == 750
    assert result["cache"]["hit_percent"] == 75
    assert result["cache"]["write"] is None
    assert sum(l["kind"] == "tool" for l in result["lanes"]) == 2
    assert all(l["status"] == "error" for l in result["lanes"] if l["kind"] == "tool")
    assert all(l["running"] for l in result["lanes"] if l["kind"] in ("turn", "model"))
    assert "Select a timing lane" in result["text"]
    s.finish_turn(turn["id"], "interrupted")
    historical = inspect(s, sid)
    assert not any(l["running"] for l in historical["lanes"])
    assert any(l["end_unknown"] for l in historical["lanes"])


def test_paged_canonical_messages(data):
    rt, sid = data
    for i in range(51):
        rt.store.append_message(sid, Message("user", [Text(f"message {i}")]))
    first = inspect(rt.store, sid, view="Conversation")
    older = inspect(rt.store, sid, view="Conversation", before=first["next_before"])
    assert first["version"] == 2 and first["more"] and first["next_before"] and not older["more"]
    assert older["next_before"] is None
    assert "message 50" in first["text"] and "message 0" not in first["text"]
    assert "message 0" in older["text"]
    assert first["records"][-1]["summary"].startswith("user · 1 part · ")


def test_cursors_are_stable_under_writes(data):
    rt, sid = data
    s = rt.store
    for i in range(7):
        s.append_message(sid, Message("user", [Text(f"m{i}")]))
        s.append_event({"session_id": sid, "seq": i, "time": i, "type": "wire_request", "data": {"n": i}})
        s.start_turn(sid, started=100 + i)
    def ids(r):
        return [x["id"] for x in r["records"]]
    for view, extra in (("Conversation", {}), ("Raw", {}), ("Requests", {}), ("Steps", {}), ("Timing", {})):
        first = inspect(s, sid, view=view, limit=3, now=500, **extra)
        reference = inspect(s, sid, view=view, limit=200, now=500)
        # new rows arrive between page 1 and page 2
        s.append_message(sid, Message("user", [Text("late")]))
        s.append_event({"session_id": sid, "seq": 99, "time": 99, "type": "wire_request", "data": {}})
        s.start_turn(sid, started=900)
        seen = ids(first) if view != "Timing" else sorted({l["turn_id"] for l in first["lanes"]})
        cursor = first["next_before"]
        while cursor:
            page = inspect(s, sid, view=view, limit=3, before=cursor, now=500)
            got = ids(page) if view != "Timing" else sorted({l["turn_id"] for l in page["lanes"]})
            assert not set(got) & set(seen)
            seen += got
            cursor = page["next_before"]
        want = ids(reference) if view != "Timing" else sorted({l["turn_id"] for l in reference["lanes"]})
        assert sorted(map(str, seen)) == sorted(map(str, want))
        assert len(seen) == len(set(seen)) == len(want)
        # wipe the late rows so the next view starts from the same fixture
        s.db.execute("delete from message where seq > 7 and session_id = ?", (sid,))
        s.db.execute("delete from event where seq = 99")
        s.db.execute("delete from turn where started = 900")
        s.db.commit()


def test_cache_coverage_per_model_range_and_ttfb(data):
    rt, sid = data
    s = rt.store
    turn = s.start_turn(sid, started=100)
    for step, (model, usage) in enumerate((("scripted/m", Usage(1000, 600, 5, 0)), ("scripted/n", Usage(200, 0, 5, 0)),
                                           ("scripted/m", None)), 1):
        request = s.start_request(turn["id"], sid, step, model=model)
        if usage:
            s.record_usage(request["id"], None, usage)
    s.note_wire(request["id"], {"type": "wire_request"})
    s.note_wire(request["id"], {"type": "wire_event"})
    result = inspect(s, sid, view="Steps")
    cache = result["cache"]
    assert cache["scope"] == "session lifetime" and cache["input"] == 1200 and cache["cached"] == 600
    assert cache["denominator"] == "input tokens reported by provider"
    assert cache["coverage"] == {"requests_with_usage": 2, "requests_total": 3, "complete": False}
    assert cache["models"]["scripted/m"]["hit_percent"] == 60 and cache["models"]["scripted/n"]["hit_percent"] == 0
    assert cache["write"] is None and cache["write_known"] is False
    assert result["cache_range"]["scope"] == "displayed page" and result["cache_range"]["input"] == 1200
    assert result["cache_range"]["coverage"] == {"requests_with_usage": 2, "requests_total": 3}
    assert inspect(s, sid, view="Raw")["cache_range"] is None
    model_lanes = [l for l in result["lanes"] if l["kind"] == "model"]
    assert [("ttfb" in l) for l in model_lanes] == [False, False, True]


def test_filters_tree_session_and_questions(data):
    rt, sid = data
    s = rt.store
    child = s.create_session(rt.open_project(s.session(sid).directory), title="Scout", agent="build", parent=s.session(sid))
    turn = s.start_turn(sid, started=100)
    call = ToolCall("c1", "bash", '{"command":"echo needle"}')
    s.mark_tool_started(turn["id"], sid, call)
    s.mark_tool_finished(turn["id"], sid, call, ToolResult("c1", "ok"))
    s.finish_turn(turn["id"], "completed")
    other = s.start_turn(sid, started=200)
    s.finish_turn(other["id"], "failed")
    s.append_message(child.id, Message("user", [Text("child only")]))
    s.append_message(sid, Message("assistant", [Text("hello needle")]))
    s.append_event({"session_id": sid, "seq": 1, "time": 1, "type": "question.asked",
                    "data": {"id": "q1", "call_id": "c1", "questions": [{"q": "ok?"}]}})
    s.append_event({"session_id": sid, "seq": 2, "time": 2, "type": "question.asked",
                    "data": {"id": "q2", "call_id": "zz", "questions": [{"q": "again?"}]}})
    s.append_event({"session_id": sid, "seq": 3, "time": 3, "type": "question.replied",
                    "data": {"id": "q1", "call_id": "c1", "outcome": "answered", "answers": []}})
    s.append_event({"session_id": sid, "seq": 4, "time": 4, "type": "tool.x", "data": {"v": "abc"}})
    steps = inspect(s, sid, view="Steps", q="needle")
    assert [r["id"] for r in steps["records"]] == [turn["id"]]
    assert [r["id"] for r in inspect(s, sid, view="Steps", status="failed")["records"]] == [other["id"]]
    assert {l["kind"] for l in inspect(s, sid, view="Timing", kind="tool")["lanes"]} == {"tool"}
    assert [t["id"] for t in steps["tree"]] == [sid, child.id]
    assert steps["tree"][1] == {**steps["tree"][1], "parent": sid, "depth": 1, "status": "idle"}
    assert [r["role"] for r in inspect(s, sid, view="Conversation", q="needle")["records"]] == ["assistant"]
    assert inspect(s, sid, view="Conversation", session=child.id)["records"][0]["session_id"] == child.id
    raw = inspect(s, sid, view="Raw", types=["tool.x"], q="abc")
    assert [r["type"] for r in raw["records"]] == ["tool.x"]
    assert inspect(s, sid, view="Requests", types=["tool.x"])["records"] == []
    qs = inspect(s, sid, view="Questions")
    by_id = {r["id"]: r for r in qs["records"]}
    assert by_id["q1"]["status"] == "answered" and by_id["q1"]["turn_id"] == turn["id"]
    assert by_id["q1"]["links"] == {"turn": turn["id"], "tool": "c1"}
    assert by_id["q2"]["status"] == "pending" and by_id["q2"]["turn_id"] is None
    assert [r["id"] for r in inspect(s, sid, view="Questions", status="pending")["records"]] == ["q2"]
    # a non-tree session is refused
    stranger = s.create_session(rt.open_project(s.session(sid).directory), title="X", agent="build")
    with pytest.raises(ValueError):
        inspect(s, sid, session=stranger.id)


def test_bounds_omissions_and_record_fetch(data):
    rt, sid = data
    s = rt.store
    big = "x" * 50_000
    for i in range(6):
        s.append_message(sid, Message("user", [Text(f"{i}{big}")]))
    result = inspect(s, sid, view="Conversation", max_bytes=40_000)
    detail_bytes = sum(len(r["detail"]) for r in result["records"])
    assert detail_bytes <= 20_000 and all(r["detail_truncated"] for r in result["records"])
    assert all(r["detail_bytes"] > 50_000 for r in result["records"])
    omission = next(o for o in result["omitted"] if o["kind"] == "detail")
    assert omission["count"] == 6 and omission["bytes"] > 250_000 and omission["reason"]
    assert any("clipped" in n for n in result["notes"])
    full = record(s, sid, "message", result["records"][0]["id"])
    assert len(json.dumps(full["record"])) > 50_000 and full["detail_bytes"] >= result["records"][0]["detail_bytes"] - 1
    assert record(s, sid, "message", "missing") is None
    # lane cap
    turn = s.start_turn(sid, started=100)
    for i in range(10):
        s.start_request(turn["id"], sid, i, model="scripted/m")
    capped = inspect(s, sid, view="Timing", max_lanes=4)
    assert len(capped["lanes"]) == 4
    assert {"kind": "lanes", "count": 7} == {k: v for k, v in next(o for o in capped["omitted"] if o["kind"] == "lanes").items() if k != "reason"}
    # tree cap
    for i in range(3):
        s.create_session(rt.open_project(s.session(sid).directory), title=f"c{i}", agent="build", parent=s.session(sid))
    small = inspect(s, sid, max_tree=2)
    assert len(small["tree"]) == 2 and any(o["kind"] == "tree" and o["count"] == 2 for o in small["omitted"])


def test_head_changes_with_new_events(data):
    rt, sid = data
    s = rt.store
    first = head(s, sid)
    assert set(first) >= {"head", "session_head", "generation", "floor"}
    s.append_event({"session_id": sid, "seq": 1, "time": 1, "type": "test.event", "data": {}})
    second = head(s, sid)
    assert second["head"] > first["head"] and second["generation"] == first["generation"]
    assert inspect(s, sid)["head"] == second["head"]


def test_large_tree_is_bounded(data):
    rt, sid = data
    s = rt.store
    root = s.session(sid)
    project = rt.open_project(root.directory)
    sessions = [sid] + [s.create_session(project, title=f"c{i}", agent="build", parent=root).id for i in range(29)]
    with s.db:
        for si, session in enumerate(sessions):
            for t in range(40):
                tid = f"t{si}_{t}"
                s.db.execute("insert into turn (id, session_id, status, started, finished) values (?, ?, 'completed', ?, ?)",
                             (tid, session, 1000 + t * 10 + si / 100, 1005 + t * 10))
                s.db.execute("insert into turn_request (id, turn_id, session_id, step, status, started, finished, model)"
                             " values (?, ?, ?, 1, 'completed', ?, ?, 'scripted/m')", ("r" + tid, tid, session, 1000 + t * 10, 1004 + t * 10))
                s.db.execute("insert into turn_tool (turn_id, session_id, call_id, name, state, started, finished)"
                             " values (?, ?, 'c', 'bash', 'finished', ?, ?)", (tid, session, 1001 + t * 10, 1002 + t * 10))
    statements: list[str] = []
    s.db.set_trace_callback(statements.append)
    start = time.perf_counter()
    for view in ("Timing", "Steps", "Conversation", "Raw", "Requests", "Questions"):
        result = inspect(s, sid, view=view, limit=50)
        assert len(result["tree"]) == 30
    timing = inspect(s, sid, view="Timing", limit=200)
    s.db.set_trace_callback(None)
    assert len(timing["lanes"]) <= 2000 and timing["more"]
    assert time.perf_counter() - start < 3
    assert len(statements) < 120, len(statements)  # constant per page, not per turn or session


async def test_authenticated_endpoint_and_validation(data, monkeypatch):
    rt, sid = data
    async def context(_sid):
        return {"body": {"model": "scripted/m"}}
    monkeypatch.setattr(rt, "context_usage", context)
    app = build_app(rt, token="secret", url="http://test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/session/{sid}/inspect"
        assert (await client.get(path)).status_code == 401
        headers = {"authorization": "Bearer secret"}
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["session_id"] == sid
        assert (await client.get(path + "?view=wrong", headers=headers)).status_code == 400
        assert (await client.get(path + "?page=1", headers=headers)).status_code == 400
        assert (await client.get(path + "?limit=999", headers=headers)).status_code == 400
        assert (await client.get(path + "?before=zzz", headers=headers)).status_code == 400
        assert response.json()["version"] == 2
        h = await client.get(path + "/head", headers=headers)
        assert h.status_code == 200 and set(h.json()) >= {"head", "generation", "floor"}
        assert (await client.get(path + "/record?kind=message&id=nope", headers=headers)).status_code == 404
        assert (await client.get(path + "/record?kind=bad&id=1", headers=headers)).status_code == 400
        assert (await client.get(path + "/record", headers=headers)).status_code == 400
        rt.store.append_message(sid, Message("user", [Text("hi")]))
        mid = (await client.get(path + "?view=Conversation", headers=headers)).json()["records"][0]["id"]
        rec = await client.get(path + f"/record?kind=message&id={mid}", headers=headers)
        assert rec.status_code == 200 and rec.json()["record"]["role"] == "user"
        assert (await client.get("/api/session/missing/inspect", headers=headers)).status_code == 404
        requests = await client.get(path + "?view=Requests", headers=headers)
        assert requests.status_code == 200
        assert "Next request" in requests.json()["text"]
        raw = await client.get(path + "?view=Raw", headers=headers)
        assert raw.status_code == 200
        assert "Session record" in raw.json()["text"]


def test_tree_lanes_share_absolute_time_but_cache_is_session_scoped(data):
    rt, sid = data
    s = rt.store
    child = s.create_session(rt.open_project(s.session(sid).directory), title="Scout", agent="build", parent=s.session(sid))
    for session, start in ((sid, 100), (child.id, 103)):
        turn = s.start_turn(session, started=start)
        request = s.start_request(turn["id"], session, 1, model="scripted/m")
        s.record_usage(request["id"], None, Usage(1000, 750, 20, 5))
    result = inspect(s, sid, now=110)
    turns = [l for l in result["lanes"] if l["kind"] == "turn"]
    assert [l["start"] for l in turns] == [100, 103]
    assert all(l["end"] == 110 for l in turns)
    assert result["cache"]["input"] == 1000  # no double counting children


def test_requests_filter_and_raw_pagination(data):
    rt, sid = data
    for i in range(51):
        rt.store.append_event({"session_id": sid, "seq": i, "time": i,
                               "type": "wire_response" if i == 0 else "test.event", "data": {"marker": f"event-{i}"}})
    requests = inspect(rt.store, sid, view="Requests")
    assert "event-0" in requests["text"] and "event-50" not in requests["text"]
    assert requests["records"][0]["summary"].startswith("wire_response")
    raw = inspect(rt.store, sid, view="Raw")
    assert raw["more"] and "event-50" in raw["text"]
    assert "event-0" in inspect(rt.store, sid, view="Raw", before=raw["next_before"])["text"]


def test_questions_paging_is_stable_when_new_questions_arrive(data):
    rt, sid = data
    s = rt.store
    for i in range(7):
        s.append_event({"session_id": sid, "seq": i, "time": 10 + i, "type": "question.asked",
                        "data": {"id": f"q{i}", "call_id": f"c{i}", "questions": [{"q": "?"}]}})
    first = inspect(s, sid, view="Questions", limit=3)
    s.append_event({"session_id": sid, "seq": 50, "time": 999, "type": "question.asked",
                    "data": {"id": "late", "call_id": "cl", "questions": [{"q": "?"}]}})
    seen = [r["id"] for r in first["records"]]
    cursor = first["next_before"]
    while cursor:
        page = inspect(s, sid, view="Questions", limit=3, before=cursor)
        seen += [r["id"] for r in page["records"]]
        cursor = page["next_before"]
    # pages run newest first (rows within a page read oldest first); nothing repeats and the late one is not paged in
    assert sorted(seen) == [f"q{i}" for i in range(7)] and len(seen) == len(set(seen))
    assert seen[:3] == ["q4", "q5", "q6"]


def test_paging_survives_retention_deleting_rows_between_pages(data):
    rt, sid = data
    s = rt.store
    for i in range(12):
        s.append_event({"session_id": sid, "seq": i, "time": i, "type": "probe", "data": {"n": i}})
    first = inspect(s, sid, view="Raw", limit=4)
    seen = [r["id"] for r in first["records"]]
    # retention drops the oldest rows (and one the cursor already passed) between page 1 and page 2
    with s.db:
        s.db.execute("delete from event where session_id = ? and type = 'probe' and seq < 3", (sid,))
        s.db.execute("delete from event where session_id = ? and type = 'probe' and seq = 11", (sid,))
    cursor = first["next_before"]
    while cursor:
        page = inspect(s, sid, view="Raw", limit=4, before=cursor)
        assert not {r["id"] for r in page["records"]} & set(seen)
        seen += [r["id"] for r in page["records"]]
        cursor = page["next_before"]
    assert len(seen) == len(set(seen))
    remaining = {r["id"] for r in inspect(s, sid, view="Raw", limit=200)["records"]}
    boundary = min(r["id"] for r in first["records"])
    # every surviving row older than page 1 was reached exactly once through the cursors
    assert {i for i in remaining if i < boundary} == set(seen) - {r["id"] for r in first["records"]}


def test_failures_read_in_the_list_with_when_and_where(data):
    rt, sid = data
    s = rt.store
    turn = s.start_turn(sid, started=100)
    request = s.start_request(turn["id"], sid, 1, model="scripted/m")
    s.start_attempt(turn["id"], sid, request["id"], "att_1", 1)
    s.finish_attempt("att_1", "failed", error="copilot: network error: ConnectTimeout")
    s.start_attempt(turn["id"], sid, request["id"], "att_2", 2, delay=0.7, error="copilot: network error: ConnectTimeout")
    s.finish_attempt("att_2", "failed", error="copilot: network error: ConnectTimeout")
    s.append_event({"session_id": sid, "seq": 1, "time": 101, "type": "retry_scheduled",
                    "data": {"attempt": 1, "max_attempts": 2, "delay": 0.7, "reason": "copilot: network error: ConnectTimeout"}})
    s.append_event({"session_id": sid, "seq": 2, "time": 102, "type": "session.error",
                    "data": {"kind": "provider", "message": "copilot: network error: ConnectTimeout"}})
    lanes = inspect(s, sid, view="Timing", kind="attempt", now=200)["lanes"]
    assert [l["label"] for l in lanes] == ["#1", "#2"]
    assert all(l["summary"].endswith("· copilot: network error: ConnectTimeout") for l in lanes)
    raw = inspect(s, sid, view="Raw")["records"]
    assert [r["summary"].split(" · ")[:2] for r in raw] == [
        ["retry_scheduled", "copilot: network error: ConnectTimeout"],
        ["session.error", "copilot: network error: ConnectTimeout"]]
