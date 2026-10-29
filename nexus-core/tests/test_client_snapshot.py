"""Consistent snapshot and reconnecting client stream (Step 11.5)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from nexus import runtime as runtime_module
from nexus.client import Client
from nexus.llmhub.types import Done, PartDone, Text, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Scripted:
    name = "scripted"

    def __init__(self) -> None:
        self.replies = []
        self.requests = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages)}

    async def stream(self, req):
        self.requests.append(req)
        for part in self.replies.pop(0):
            yield PartDone(part)
        yield Done("end_turn")


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    app = build_app(rt, token=TOKEN, url="http://test")
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://test",
                             headers={"authorization": f"Bearer {TOKEN}"})
    client = Client("http://test", TOKEN)
    client.http = http
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace


async def _idle(rt, session_id):
    for _ in range(300):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def test_snapshot_has_a_watermark_state_and_a_bounded_session_view(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[Text("one")], [Text("two")]]
    project = await client.call("POST", "/api/project", {"path": str(workspace)})
    session = await client.call("POST", "/api/session", {"project": project["id"]})
    await client.call("POST", f"/api/session/{session['id']}/prompt", {"text": "first"})
    await _idle(rt, session["id"])
    await client.call("POST", f"/api/session/{session['id']}/prompt", {"text": "second"})
    await _idle(rt, session["id"])

    snapshot = await client.snapshot(session["id"], message_limit=2)
    assert snapshot["watermark"]["generation"] == rt.store.log_generation()
    assert snapshot["watermark"]["head"] == rt.store.log_head()
    assert snapshot["watermark"]["head_cursor"].endswith(f":{rt.store.log_head()}")
    assert [p["id"] for p in snapshot["projects"]] == [project["id"]]
    assert [s["id"] for s in snapshot["sessions"]] == [session["id"]]
    current = await client.call("GET", f"/api/session/{session['id']}")
    assert snapshot["sessions"][0]["revision"] == current["revision"]
    assert snapshot["running"] == [] and snapshot["approvals"] == [] and snapshot["questions"] == []
    selected = snapshot["selected"]
    assert selected["session_id"] == session["id"] and selected["messages_more"] is True
    assert len(selected["messages"]) == 2 and selected["messages"][-1]["role"] == "assistant"
    assert selected["messages"][-1]["parts"][0]["type"] == "text"


async def test_snapshot_reports_a_running_turn_and_a_pending_approval(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[ToolCall("c1", "bash", '{"command": "true"}')], [Text("done")]]
    session = await client.call("POST", "/api/session", {"directory": str(workspace), "permission": "ask"})
    await client.call("POST", f"/api/session/{session['id']}/prompt", {"text": "go"})
    for _ in range(200):
        if rt.pending_approvals(session["id"]):
            break
        await asyncio.sleep(0.01)
    snapshot = await client.snapshot(session["id"])
    assert [turn["session_id"] for turn in snapshot["running"]] == [session["id"]]
    assert [a["session_id"] for a in snapshot["approvals"]] == [session["id"]]
    await client.call("POST", f"/api/session/{session['id']}/interrupt")
    await _idle(rt, session["id"])


class FakeClient(Client):
    """Scripted event streams; the watch logic is tested without an HTTP socket."""

    def __init__(self, streams, snapshot_state):
        super().__init__("http://fake", "t")
        self.streams = list(streams)
        self.state = snapshot_state
        self.snapshot_calls = 0
        self.cursors = []

    async def events(self, session_id=None, *, cursor=None):
        self.cursors.append(cursor)
        item = self.streams.pop(0)
        if isinstance(item, Exception):
            raise item
        for event in item:
            yield event

    async def snapshot(self, session_id=None, **kwargs):
        self.snapshot_calls += 1
        return self.state


def _state(generation, head):
    return {"watermark": {"generation": generation, "head": head, "floor": 0,
                          "head_cursor": f"{generation}:{head}"}, "running": [], "sessions": []}


async def test_watch_reconnects_deduplicates_and_advances_after_consumption():
    generation = "log_test"
    streams = [
        [{"type": "server.connected", "head": 10, "resumed_from": 5},
         {"type": "probe", "id": 6, "cursor": f"{generation}:6"},
         {"type": "probe", "id": 7, "cursor": f"{generation}:7"}],
        [{"type": "server.connected", "head": 12, "resumed_from": 7},
         {"type": "probe", "id": 7, "cursor": f"{generation}:7"},  # overlap: skipped
         {"type": "probe", "id": 8, "cursor": f"{generation}:8"}],
    ]
    client = FakeClient(streams, _state(generation, 8))
    events = client.watch("ses", cursor=f"{generation}:5", backoff=(0, 0))
    got = []
    async for event in events:
        got.append(event)
        if event.get("id") == 8:
            break
    await events.aclose()
    assert [e.get("id") for e in got if e["type"] == "probe"] == [6, 7, 8]
    assert client.cursors == [f"{generation}:5", f"{generation}:7"]
    assert client.snapshot_calls == 0  # an ordinary reconnect is not a resync


async def test_watch_resync_fetches_one_snapshot_then_resumes_at_its_head():
    from nexus.client import ResyncRequired

    generation = "log_new"
    client = FakeClient(
        [ResyncRequired(409, "gone", code="cursor_generation"), [{"type": "server.connected"}]],
        _state(generation, 20))
    seen = []
    events = client.watch("ses", cursor="log_old:1", backoff=(0,))
    first = await anext(events)
    assert first["type"] == "client.resync" and first["data"]["reason"] == "cursor_generation"
    second = await anext(events)
    assert second["type"] == "server.connected"
    await events.aclose()
    assert client.snapshot_calls == 1
    assert client.cursors[1] == f"{generation}:20"
    assert seen == []


async def test_watch_resyncs_when_the_selected_session_was_deleted(daemon):
    _, client, provider, workspace = daemon
    session = await client.call("POST", "/api/session", {"directory": str(workspace)})
    await client.call("DELETE", f"/api/session/{session['id']}")
    events = client.watch(session["id"], cursor="log_other:1")
    first = await asyncio.wait_for(anext(events), 3)
    assert first["type"] == "client.resync"
    assert first["data"]["snapshot"]["deleted_session"] == session["id"]
    assert first["data"]["snapshot"]["selected"] is None
    await events.aclose()
    assert provider.requests == []


async def test_watch_resyncs_from_a_snapshot_on_a_foreign_cursor(daemon):
    rt, client, provider, workspace = daemon
    session = await client.call("POST", "/api/session", {"directory": str(workspace)})
    resynced = []

    async def on_resync(state):
        resynced.append(state["watermark"])

    events = client.watch(session["id"], cursor="log_other:1", on_resync=on_resync)
    first = await asyncio.wait_for(anext(events), 3)
    assert first["type"] == "client.resync"
    assert first["data"]["reason"] == "cursor_generation"
    assert first["data"]["snapshot"]["watermark"]["head"] == rt.store.log_head()
    assert resynced and resynced[0]["generation"] == rt.store.log_generation()
    await events.aclose()
    assert provider.requests == []  # resyncing never submits a prompt


async def test_watch_resyncs_on_overflow_instead_of_skipping_lost_events():
    client = FakeClient([[{"type": "server.connected", "head": 2, "head_cursor": "log_test:2"},
                          {"type": "bus.overflow", "id": None}],
                         [{"type": "server.connected"}]], _state("log_test", 20))
    events = client.watch(cursor="log_test:2", backoff=(0,))
    assert (await anext(events))["type"] == "server.connected"
    resync = await anext(events)
    assert resync["type"] == "client.resync" and resync["data"]["reason"] == "bus.overflow"
    assert client.snapshot_calls == 1
    assert (await anext(events))["type"] == "server.connected"
    assert client.cursors == ["log_test:2", "log_test:20"]
    await events.aclose()


async def test_disconnect_before_first_event_resumes_from_connection_head():
    client = FakeClient([[{"type": "server.connected", "head": 10, "head_cursor": "log_test:10"}],
                         [{"type": "server.connected", "head": 11, "resumed_from": 10},
                          {"type": "probe", "id": 11, "cursor": "log_test:11"}]], _state("log_test", 11))
    events = client.watch(backoff=(0,))
    for _ in range(3):
        event = await anext(events)
    assert event["id"] == 11
    assert client.cursors == [None, "log_test:10"]
    await events.aclose()


async def test_repeated_empty_streams_exhaust_backoff():
    from nexus.client import ApiError
    client = FakeClient([[{"type": "server.connected", "head": 0}]] * 3, _state("log_test", 0))
    events = client.watch(backoff=(0, 0))
    with pytest.raises(ApiError, match="ended repeatedly"):
        async for _ in events:
            pass
    await events.aclose()


async def test_local_client_refreshes_rotated_token_without_resending_a_mutation(monkeypatch):
    from nexus import daemon as discovery
    from nexus.client import ApiError
    client = FakeClient([ApiError(401, "old token"),
                         [{"type": "server.connected", "head": 10}]], _state("log_test", 10))
    client._local_discovery = True
    monkeypatch.setattr(discovery, "discover", lambda: {"url": "http://fake", "token": "new-token"})
    events = client.watch(cursor="log_test:5", backoff=(0,))
    assert (await anext(events))["type"] == "server.connected"
    assert client.http.headers["authorization"] == "Bearer new-token"
    assert client.cursors == ["log_test:5", "log_test:5"]
    await events.aclose()
    await client.close()


async def test_cli_resync_ignores_waits_and_running_turns_outside_its_tree(monkeypatch):
    import io

    from nexus import cli
    state = _state("log_test", 20)
    state.update({"sessions": [{"id": "ses", "parent_id": None}, {"id": "other", "parent_id": None}],
                  "running": [{"session_id": "other"}],
                  "approvals": [{"id": "wrong", "session_id": "other"}], "questions": []})
    class Watching:
        async def watch(self, session_id):
            yield {"type": "server.connected"}
            yield {"type": "client.resync", "data": {"reason": "history_unavailable", "snapshot": state}}
            raise AssertionError("CLI should have ended its completed tree")
    async def start():
        return {"turn_id": "turn"}
    async def wrong(*args):
        raise AssertionError("must not ask another tree's question")
    monkeypatch.setattr(cli, "_ask", wrong)
    await cli._stream_turn(Watching(), "ses", cli.Renderer(wire=False, wire_file=None, out=io.StringIO()),
                           start, label="prompt")
