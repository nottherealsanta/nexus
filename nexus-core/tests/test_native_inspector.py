import httpx
import pytest
from conftest import remembering

from nexus.inspector_data import inspect
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
    for options in ({"view": "bad"}, {"page": -1}):
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
    older = inspect(rt.store, sid, view="Conversation", page=1)
    assert first["more"] and not older["more"]
    assert "message 50" in first["text"] and "message 0" not in first["text"]
    assert "message 0" in older["text"]


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
        assert (await client.get(path + "?page=-1", headers=headers)).status_code == 400
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
    raw = inspect(rt.store, sid, view="Raw")
    assert raw["more"] and "event-50" in raw["text"]
    assert "event-0" in inspect(rt.store, sid, view="Raw", page=1)["text"]
