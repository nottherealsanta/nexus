"""Bounded, visible provider retries (Step 11.4), without real sleeps or network."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from nexus import runtime as runtime_module
from nexus.llmhub.types import Done, PartDone, ProviderError, Text, ToolCall, WireRequest
from nexus.loop import Agent, RetryExhausted, RetryScheduled, RetryStarted, TurnEnd
from nexus.retry import RetryPolicy
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay


class Flaky:
    name = "flaky"

    def __init__(self, script) -> None:
        self.script = list(script)
        self.requests = []

    def body(self, req):
        return {"model": req.model}

    async def stream(self, req):
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if item == "prefix":
            yield WireRequest("POST", "http://x", {}, {})
            yield Text("partial")
            raise ProviderError("flaky: HTTP 503", status=503, phase="response")
        for part in item:
            yield PartDone(part)
        yield Done("end_turn", "resp")


def agent(provider, clock, *, policy=None, workspace=None):
    return Agent(provider, model="m", workspace=workspace, retry_policy=policy or RetryPolicy(),
                 retry_clock=clock, retry_sleep=clock.sleep, retry_random=lambda: 0.5)


async def test_transient_response_failure_is_retried_and_visible(tmp_path):
    clock = Clock()
    provider = Flaky([ProviderError("flaky: HTTP 503", status=503, phase="response"), [Text("recovered")]])
    events = [event async for event in agent(provider, clock, workspace=tmp_path).turn("hello")]
    assert len(provider.requests) == 2
    assert [type(e) for e in events if isinstance(e, (RetryScheduled, RetryStarted))] == [
        RetryScheduled, RetryStarted]
    scheduled = next(e for e in events if isinstance(e, RetryScheduled))
    assert scheduled.attempt == 1 and scheduled.next_attempt == 2 and scheduled.status == 503
    assert clock.delays == [scheduled.delay]
    assert isinstance(events[-1], TurnEnd) and events[-1].steps == 1
    assert "".join(p.text for p in events[-2].message.parts if isinstance(p, Text)) == "recovered"


async def test_failure_after_a_stream_event_is_terminal(tmp_path):
    clock = Clock()
    provider = Flaky(["prefix"])

    async def collect():
        return [event async for event in agent(provider, clock, workspace=tmp_path).turn("hello")]

    with pytest.raises(ProviderError):
        await collect()
    assert len(provider.requests) == 1
    assert clock.delays == []


async def test_retry_after_is_honored_or_reported_as_unaffordable(tmp_path):
    clock = Clock()
    error = ProviderError("busy", status=429, phase="response", retry_after=5.0)
    provider = Flaky([error, [Text("ok")]])
    events = [event async for event in agent(provider, clock, workspace=tmp_path).turn("hello")]
    scheduled = next(e for e in events if isinstance(e, RetryScheduled))
    assert scheduled.retry_after == 5.0 and scheduled.delay == 5.0

    clock = Clock()
    provider = Flaky([ProviderError("busy", status=429, phase="response", retry_after=100.0)])
    policy = RetryPolicy(max_attempts=3, max_elapsed=10.0)
    events = []
    with pytest.raises(ProviderError):
        async for event in agent(provider, clock, policy=policy, workspace=tmp_path).turn("hello"):
            events.append(event)
    assert isinstance(events[-1], RetryExhausted)
    assert events[-1].attempts == 1 and events[-1].retry_after == 100.0
    assert clock.delays == [] and len(provider.requests) == 1


async def test_authentication_is_not_retried(tmp_path):
    clock = Clock()
    provider = Flaky([ProviderError("unauthorized", status=401, phase="response")])
    events = []
    with pytest.raises(ProviderError):
        async for event in agent(provider, clock, workspace=tmp_path).turn("hello"):
            events.append(event)
    assert len(provider.requests) == 1 and not any(isinstance(e, RetryScheduled) for e in events)


async def test_a_transient_stream_error_is_not_retried(tmp_path):
    clock = Clock()
    provider = Flaky([ProviderError("stream error", phase="stream")])
    with pytest.raises(ProviderError):
        async for _ in agent(provider, clock, workspace=tmp_path).turn("hello"):
            pass
    assert len(provider.requests) == 1


class Scripted:
    name = "scripted"

    def __init__(self) -> None:
        self.replies = []
        self.requests = []

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages)}

    async def stream(self, req):
        self.requests.append(req)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        parts = item
        for part in parts:
            yield PartDone(part)
        yield Done("tool_calls" if any(isinstance(p, ToolCall) for p in parts) else "end_turn")


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    provider = Scripted()
    monkeypatch.setattr(runtime_module, "provider_for", lambda ref: (provider, ref.partition("/")[2]))
    rt = make_runtime("scripted/m", Store(tmp_path / "nexus.db"))
    rt.retry_policy = RetryPolicy(max_attempts=3, base_delay=0.0, max_wait=0.0, max_elapsed=1.0)
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return rt, client, provider, workspace


async def _idle(rt, session_id):
    for _ in range(300):
        if session_id not in rt.turns:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


async def test_runtime_persists_attempts_and_retry_events_once(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [ProviderError("busy", status=503, phase="response"), [Text("done")]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    response = await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    assert response.status_code == 202
    await _idle(rt, session["id"])
    messages = (await client.get(f"/api/session/{session['id']}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert len(provider.replies) == 0
    events = (await client.get(f"/api/session/{session['id']}/event")).json()["events"]
    retries = [e for e in events if e["type"] in ("retry_scheduled", "retry_started", "retry_exhausted")]
    assert [e["type"] for e in retries] == ["retry_scheduled", "retry_started"]
    turns = rt.store.turns_for_session(session["id"])
    attempts = rt.store.attempts_for_turn(turns[0]["id"])
    assert [(a["attempt"], a["status"]) for a in attempts] == [(1, "failed"), (2, "completed")]
    assert rt.store.requests_for_turn(turns[0]["id"])[0]["status"] == "completed"


async def test_interruption_during_backoff_stops_without_another_attempt(daemon):
    rt, client, provider, workspace = daemon
    rt.retry_policy = RetryPolicy(max_attempts=3, base_delay=10.0, max_wait=10.0, max_elapsed=60.0)
    provider.replies = [ProviderError("busy", status=503, phase="response")]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "hello"})
    for _ in range(300):
        if rt.store.events(session["id"], types=["retry_scheduled"]):
            break
        await asyncio.sleep(0.01)
    assert rt.store.events(session["id"], types=["retry_scheduled"])
    assert (await client.post(f"/api/session/{session['id']}/interrupt")).status_code == 204
    await _idle(rt, session["id"])
    assert len(provider.requests) == 1
    assert not rt.store.events(session["id"], types=["retry_started"])
    turns = rt.store.turns_for_session(session["id"])
    assert turns[0]["status"] == "interrupted"
    attempts = rt.store.attempts_for_turn(turns[0]["id"])
    assert attempts[0]["status"] == "failed"
    assert all(a["status"] != "started" for a in attempts)


async def test_retries_never_repeat_a_completed_tool(daemon):
    rt, client, provider, workspace = daemon
    provider.replies = [[ToolCall("c1", "write", '{"path": "x.txt", "content": "hi"}')],
                        ProviderError("down", status=502, phase="response"),
                        [Text("after")]]
    session = (await client.post("/api/session", json={"directory": str(workspace)})).json()
    await client.post(f"/api/session/{session['id']}/prompt", json={"text": "go"})
    await _idle(rt, session["id"])
    assert (workspace / "x.txt").read_text() == "hi"
    assert len(provider.replies) == 0
    messages = (await client.get(f"/api/session/{session['id']}/message")).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert sum(1 for p in messages[2]["parts"] if p["type"] == "tool_result") == 1


async def test_retry_keeps_the_original_model_when_settings_change_during_backoff(tmp_path):
    clock = Clock()
    provider = Flaky([ProviderError("busy", status=503, phase="response"), [Text("ok")]])
    active = agent(provider, clock, workspace=tmp_path)
    async def change(delay):
        active.model = "new-model"
        await clock.sleep(delay)
    active._retry_sleep = change
    events = [event async for event in active.turn("hello")]
    assert all(request.model == "m" for request in provider.requests)
    assert isinstance(events[-1], TurnEnd)


async def test_slow_backoff_cannot_start_an_attempt_past_its_budget(tmp_path):
    clock = Clock()
    provider = Flaky([ProviderError("busy", status=503, phase="response")])
    active = agent(provider, clock, workspace=tmp_path)
    async def oversleep(delay):
        clock.now += 1000
    active._retry_sleep = oversleep
    with pytest.raises(ProviderError):
        async for _ in active.turn("hello"):
            pass
    assert len(provider.requests) == 1
