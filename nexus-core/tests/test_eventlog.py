"""Durable event identity and reconnect-safe replay (PLAN.md Step 11).

These tests drive the bus, storage and stream directly (no provider, no live
model) plus a raw ASGI harness for the HTTP framing, cursor errors and
disconnect cleanup. `httpx.ASGITransport` buffers a whole response, so an
infinite SSE stream is exercised through `asgi_get` instead.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import remembering

from nexus import eventlog
from nexus.eventlog import (
    SessionFilter,
    Stream,
    format_cursor,
    parse_cursor,
)
from nexus.server import build_app, make_runtime
from nexus.store import Store

TOKEN = "t0ken"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    store = Store(tmp_path / "nexus.db")
    rt = make_runtime(remembering(store, "scripted/m"))
    app = build_app(rt, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    workspace = tmp_path / "proj"
    workspace.mkdir()
    return SimpleNamespace(rt=rt, store=store, app=app, client=client, workspace=workspace,
                           db=tmp_path / "nexus.db", tmp_path=tmp_path)


def new_session(rt, workspace, **kwargs):
    project = rt.open_project(str(workspace))
    return rt.create_session(project, title=kwargs.pop("title", "test"), model=None, permission=None,
                             agent=kwargs.pop("agent", "build"), **kwargs)


def publish(rt, session_id, n, type_="probe"):
    return rt.bus.publish(type_, {"n": n}, session_id=session_id)


def cursor_of(store, event_id):
    return format_cursor(store.log_generation(), event_id)


async def collect(stream: Stream, count: int, timeout: float = 10.0) -> list[dict]:
    """Take `count` events and close the stream (the live phase would wait forever)."""
    events: list[dict] = []
    generator = stream.events()
    try:
        async with asyncio.timeout(timeout):
            async for event in generator:
                events.append(event)
                if len(events) >= count:
                    break
    finally:
        await generator.aclose()
    return events


async def asgi_get(app, path: str, *, query: str = "", headers: list[tuple[str, str]] | None = None,
                   chunks: int = 1, timeout: float = 5.0) -> tuple[int, dict[str, str], str]:
    """One raw ASGI request; disconnect after `chunks` body messages."""
    status = {"code": 0, "headers": {}}
    body: list[bytes] = []
    first_request = asyncio.Event()
    disconnect_now = asyncio.Event()

    async def receive():
        if not first_request.is_set():
            first_request.set()
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect_now.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            status["headers"] = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            body.append(message.get("body", b""))
            if len(body) >= chunks:
                disconnect_now.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "server": ("test", 80), "client": ("test", 12345),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
    }
    await asyncio.wait_for(app(scope, receive, send), timeout)
    return status["code"], status["headers"], b"".join(body).decode()


def sse_events(body: str) -> list[tuple[str | None, str, dict]]:
    frames = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name, event_id, data = None, None, []
        for line in frame.split("\n"):
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field == "event":
                name = value
            elif field == "id":
                event_id = value
            elif field == "data":
                data.append(value)
        frames.append((event_id, name or "message", json.loads("\n".join(data))))
    return frames


# -- identity across restarts and replacements --------------------------------


async def test_ids_and_the_generation_survive_a_restart(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    generation = store.log_generation()
    assert generation.startswith("log_")

    first = publish(rt, session.id, 1)
    cursor = cursor_of(store, first["id"])
    head = store.log_head()
    assert parse_cursor(cursor, generation=generation, head=head) == first["id"]

    store.close()
    restarted = make_runtime(remembering(Store(env.db), "scripted/m"))
    assert restarted.store.log_generation() == generation  # same database, same log
    assert restarted.store.log_head() == head
    assert any(row["id"] == first["id"] for row in restarted.store.replay(0, head, 100))

    again = restarted.bus.publish("probe", {"n": 2}, session_id=session.id)
    assert again["id"] == head + 1
    assert again["seq"] == 1  # the per-daemon counter restarts; the durable id does not

    stream = Stream.open(restarted.store, restarted.bus, after=parse_cursor(cursor, generation=generation, head=head))
    assert stream.head == head + 1
    [replayed] = await collect(stream, 1)
    assert replayed["id"] == again["id"] and replayed["cursor"] == cursor_of(restarted.store, again["id"])


async def test_a_replaced_database_rejects_an_old_generation(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    old_cursor = cursor_of(store, publish(rt, session.id, 1)["id"])

    replacement = make_runtime(remembering(Store(env.tmp_path / "other.db"), "scripted/m"))
    app = build_app(replacement, token=TOKEN, url="http://test")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"authorization": f"Bearer {TOKEN}"})
    response = await client.get("/api/event", params={"cursor": old_cursor})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "cursor_generation" and body["resync"] is True
    assert body["generation"] == replacement.store.log_generation()
    assert parse_cursor(body["head_cursor"], generation=body["generation"], head=body["head"]) == body["head"]

    info = (await client.get("/api/info")).json()
    assert info["log"]["generation"] == replacement.store.log_generation()
    assert info["log"]["head_cursor"] == format_cursor(info["log"]["generation"], info["log"]["head"])


async def test_events_are_committed_before_a_subscriber_receives_them(env):
    rt = env.rt
    session = new_session(rt, env.workspace)
    subscription = rt.bus.open()
    try:
        event = publish(rt, session.id, 1)
        stored = env.store.replay(event["id"] - 1, event["id"], 1)
        assert stored and stored[0]["id"] == event["id"]
        delivered = await subscription.get()
        assert delivered["id"] == event["id"]
    finally:
        subscription.close()


# -- the replay/live boundary --------------------------------------------------


async def test_a_publication_during_replay_is_delivered_exactly_once(env, monkeypatch):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    start = store.log_head()
    for n in range(5):
        publish(rt, session.id, n)
    head = store.log_head()
    monkeypatch.setattr(eventlog, "REPLAY_PAGE", 2)

    original, calls = store.replay, 0

    def replay(after, upto, limit):
        nonlocal calls
        calls += 1
        if calls == 2:  # between two replay pages
            publish(rt, session.id, 99)  # commits with an id above the boundary
        return original(after, upto, limit)

    monkeypatch.setattr(store, "replay", replay)
    stream = Stream.open(store, rt.bus, after=start)
    assert stream.head == head
    events = await collect(stream, 6)
    ids = [event["id"] for event in events]
    assert ids == sorted(ids) and len(set(ids)) == 6
    assert [event["data"]["n"] for event in events] == [0, 1, 2, 3, 4, 99]
    assert stream.replay_pages == 3


async def test_publication_at_the_live_handoff_is_not_lost(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    start = store.log_head()
    for n in range(2):
        publish(rt, session.id, n)
    stream = Stream.open(store, rt.bus, after=start)
    generator = stream.events()
    try:
        first = [await anext(generator) for _ in range(2)]
        assert [event["data"]["n"] for event in first] == [0, 1]
        live = publish(rt, session.id, 2)  # after the last replay item, before the live read
        nxt = await asyncio.wait_for(anext(generator), 2)
        assert nxt["id"] == live["id"] and nxt["cursor"] == cursor_of(store, live["id"])
    finally:
        await generator.aclose()


async def test_large_replay_histories_are_paged_in_order(env, monkeypatch):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    monkeypatch.setattr(eventlog, "REPLAY_PAGE", 500)
    start = store.log_head()
    count = 5010
    now = time.time()
    with store.db:
        store.db.executemany(
            "insert into event (session_id, seq, time, type, data) values (?, ?, ?, ?, ?)",
            [(session.id, n, now, "probe", json.dumps({"n": n})) for n in range(count)],
        )
    stream = Stream.open(store, rt.bus, after=start)
    events = await collect(stream, count)
    assert [event["data"]["n"] for event in events] == list(range(count))
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert stream.replay_pages == 11  # 10 full pages and a short one
    assert stream.replayed == count


# -- filtering ---------------------------------------------------------------


async def test_a_filtered_stream_includes_the_tree_and_globals_with_gaps(env):
    rt, store = env.rt, env.store
    root = new_session(rt, env.workspace, title="root")
    child = new_session(rt, env.workspace, title="child", parent=root)
    other = new_session(rt, env.workspace, title="other")
    head = store.log_head()
    e1 = publish(rt, root.id, 1)
    e2 = publish(rt, other.id, 2)
    e3 = publish(rt, child.id, 3)
    e4 = rt.bus.publish("project.updated", {"id": root.project_id})
    e5 = publish(rt, root.id, 5)

    stream = Stream.open(store, rt.bus, after=head, session_id=root.id)
    events = await collect(stream, 4)
    assert [event["id"] for event in events] == [e1["id"], e3["id"], e4["id"], e5["id"]]
    assert e2["id"] not in [event["id"] for event in events]  # a normal numeric gap
    assert all(event["session_id"] in (None, root.id, child.id) for event in events)
    assert events[2]["session_id"] is None


async def test_descendant_creation_and_deletion_during_replay_and_live(env):
    rt, store = env.rt, env.store
    root = new_session(rt, env.workspace, title="root")
    stream = Stream.open(store, rt.bus, after=store.log_head(), session_id=root.id)

    child = new_session(rt, env.workspace, title="child", parent=root)
    inside = publish(rt, child.id, 1)
    await rt.delete_session(child.id)  # publishes a minimal global tombstone
    after = rt.bus.publish("probe", {"n": 2}, session_id=child.id)  # no session row: not durable

    events = await collect(stream, 3)
    assert [event["type"] for event in events] == ["session.created", "probe", "session.deleted"]
    assert events[0]["data"]["parent_id"] == root.id
    assert events[1]["id"] == inside["id"]
    assert events[2]["session_id"] is None and events[2]["data"] == {"id": child.id}
    assert after.get("id") is None  # not stored: the session's history is gone
    assert store.log_head() == events[2]["id"]


async def test_a_deleted_descendant_is_no_longer_matched(env):
    rt, store = env.rt, env.store
    root = new_session(rt, env.workspace, title="root")
    child = new_session(rt, env.workspace, title="child", parent=root)
    before = publish(rt, child.id, 1)
    stream = Stream.open(store, rt.bus, after=store.log_head(), session_id=root.id)
    await rt.delete_session(child.id)
    global_event = rt.bus.publish("project.updated", {"id": root.project_id})
    events = await collect(stream, 2)
    assert events[0]["data"] == {"id": child.id} and events[0]["session_id"] is None
    assert events[1]["id"] == global_event["id"]
    assert before["id"] < events[0]["id"]


async def test_filter_membership_from_recorded_creates_and_tombstones(tmp_path):
    store = Store(tmp_path / "filter.db")
    try:
        project, _ = store.project_for_path(str(tmp_path))
        root = store.create_session(project, title="root", agent="build")
        child = store.create_session(project, title="child", agent="build", parent=root)
        other = store.create_session(project, title="other", agent="build")
        membership = SessionFilter(store, root.id)
        assert membership.match({"session_id": root.id, "type": "probe", "data": {}})
        assert membership.match({"session_id": child.id, "type": "probe", "data": {}})
        assert not membership.match({"session_id": other.id, "type": "probe", "data": {}})
        assert membership.match({"session_id": None, "type": "project.updated", "data": {}})
        membership.observe({"type": "session.created", "session_id": "ses_extra", "data": {"parent_id": child.id}})
        assert membership.match({"session_id": "ses_extra", "type": "probe", "data": {}})
        membership.observe({"type": "session.deleted", "session_id": None, "data": {"id": child.id}})
        assert not membership.match({"session_id": child.id, "type": "probe", "data": {}})
        assert not membership.match({"session_id": "ses_extra", "type": "probe", "data": {}})
        assert membership.match({"session_id": root.id, "type": "probe", "data": {}})
    finally:
        store.close()


# -- global lifecycle events and tombstones -----------------------------------


async def test_global_lifecycle_events_and_deletion_tombstones_are_durable(env):
    rt, store = env.rt, env.store
    project = rt.open_project(str(env.workspace))
    rt.rename_project(project.id, "renamed")
    root = new_session(rt, env.workspace, title="root")
    child = new_session(rt, env.workspace, title="child", parent=root)
    publish(rt, child.id, 1)

    await rt.delete_session(root.id)  # cascades to the child
    await rt.delete_project(project.id)

    rows = store.replay(0, store.log_head(), 1000)
    types = [row["type"] for row in rows]
    assert types[:2] == ["project.created", "project.updated"] and "project.deleted" in types
    tombstones = [row for row in rows if row["type"] == "session.deleted"]
    assert [row["data"] for row in tombstones] == [{"id": root.id}, {"id": child.id}]
    assert all(row["session_id"] is None for row in tombstones)
    assert not [row for row in rows if row["session_id"] in (root.id, child.id)]  # history removed
    assert rows[-1]["type"] == "project.deleted" and rows[-1]["session_id"] is None


async def test_a_deletion_tombstone_is_global_even_before_the_row_is_deleted(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    event = rt.bus.publish("session.deleted", {"id": session.id}, session_id=session.id)
    assert event["session_id"] is None and event["data"] == {"id": session.id}
    stored = [row for row in store.replay(0, store.log_head(), 100) if row["type"] == "session.deleted"]
    assert stored[0]["session_id"] is None
    assert not [row for row in store.events(session.id) if row["type"] == "session.deleted"]


async def test_no_cursor_starts_live_at_the_boundary(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    publish(rt, session.id, 0)
    stream = Stream.open(store, rt.bus)  # no cursor: start now, not from the log's start
    assert stream.after == stream.head
    live = publish(rt, session.id, 1)
    [event] = await collect(stream, 1)
    assert event["id"] == live["id"]


async def test_a_cursor_in_deleted_history_still_resumes(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    first = publish(rt, session.id, 1)
    cursor = cursor_of(store, first["id"])
    await rt.delete_session(session.id)  # its rows are gone; ids are not reused
    after = parse_cursor(cursor, generation=store.log_generation(), head=store.log_head())
    assert after == first["id"]  # deleting history is not a continuity failure
    later = rt.bus.publish("project.updated", {"id": session.project_id})
    stream = Stream.open(store, rt.bus, after=after)
    events = await collect(stream, 2)
    assert [event["type"] for event in events] == ["session.deleted", "project.updated"]
    assert events[-1]["id"] == later["id"] and events[-1]["id"] > first["id"]


async def test_a_filtered_stream_receives_global_deletion_tombstones(env):
    rt, store = env.rt, env.store
    root = new_session(rt, env.workspace, title="root")
    stream = Stream.open(store, rt.bus, after=store.log_head(), session_id=root.id)
    await rt.delete_session(root.id)
    [tombstone] = await collect(stream, 1)
    assert tombstone["type"] == "session.deleted" and tombstone["session_id"] is None
    assert tombstone["data"] == {"id": root.id}


# -- invalid cursors ----------------------------------------------------------


async def test_malformed_cursors_are_rejected_actionably(env):
    rt, store = env.rt, env.store
    new_session(rt, env.workspace)
    for cursor in ("nope", "abc", ":" + "1", store.log_generation() + ":", store.log_generation() + ":-1",
                   store.log_generation() + ":1.5", store.log_generation() + ":01", "x" * 200):
        response = await env.client.get("/api/event", params={"cursor": cursor})
        assert response.status_code == 400, cursor
        body = response.json()
        assert body["code"] == "invalid_cursor" and body["resync"] is True
        assert body["generation"] == store.log_generation()
        assert body["head_cursor"] == format_cursor(body["generation"], body["head"])


async def test_a_cursor_ahead_of_the_log_is_rejected(env):
    rt, store = env.rt, env.store
    new_session(rt, env.workspace)
    head = store.log_head()
    response = await env.client.get("/api/event", params={"cursor": format_cursor(store.log_generation(), head + 1)})
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "cursor_ahead" and body["resync"] is True and body["head"] == head


async def test_a_cursor_below_the_floor_reports_unavailable_history(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    first = publish(rt, session.id, 1)
    second = publish(rt, session.id, 2)
    with store.db:
        store.db.execute("update log set floor = ? where id = 1", (second["id"],))
    assert store.log_floor() == second["id"]
    response = await env.client.get("/api/event", params={"cursor": cursor_of(store, first["id"])})
    assert response.status_code == 410
    body = response.json()
    assert body["code"] == "history_unavailable" and body["floor"] == second["id"]
    assert body["floor_cursor"] == cursor_of(store, second["id"])


async def test_the_query_cursor_wins_over_last_event_id(env):
    rt, store = env.rt, env.store
    new_session(rt, env.workspace)
    # An invalid query cursor with a valid header still fails: the query is authoritative.
    response = await env.client.get("/api/event", params={"cursor": "garbage"},
                                    headers={"last-event-id": cursor_of(store, store.log_head())})
    assert response.status_code == 400 and response.json()["code"] == "invalid_cursor"
    # An invalid header alone is used when the query is absent.
    response = await env.client.get("/api/event", headers={"last-event-id": "garbage"})
    assert response.status_code == 400


async def test_an_unknown_session_filter_is_404(env):
    response = await env.client.get("/api/event", params={"session": "ses_missing"})
    assert response.status_code == 404


# -- HTTP framing, Last-Event-ID and disconnect cleanup ------------------------


async def test_the_stream_reports_its_log_and_frames_durable_ids(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    publish(rt, session.id, 1)
    head = store.log_head()

    status, headers, body = await asgi_get(
        env.app, "/api/event", query=f"token={TOKEN}&cursor={format_cursor(store.log_generation(), 0)}", chunks=2)
    assert status == 200 and headers["x-nexus-log-generation"] == store.log_generation()
    frames = sse_events(body)
    assert frames[0][1] == "server.connected"
    assert frames[0][2]["generation"] == store.log_generation() and frames[0][2]["head"] == head
    assert frames[0][2]["head_cursor"] == cursor_of(store, head)
    assert frames[0][2]["resumed_from"] == 0
    [event_id, name, event] = frames[1]
    assert name == "project.created" and event_id == cursor_of(store, event["id"])
    assert event["cursor"] == event_id
    await asyncio.sleep(0)
    assert rt.bus.subscriber_count == 0  # the disconnect closed the subscription


async def test_last_event_id_resumes_and_the_query_cursor_takes_precedence(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    events = [publish(rt, session.id, n) for n in range(3)]

    status, _, body = await asgi_get(env.app, "/api/event", query=f"token={TOKEN}",
                                     headers=[("last-event-id", cursor_of(store, events[0]["id"]))], chunks=3)
    assert status == 200
    frames = sse_events(body)
    assert frames[0][2]["resumed_from"] == events[0]["id"]
    assert [(event_id, event["id"]) for event_id, name, event in frames[1:] if name == "probe"] == [
        (cursor_of(store, events[1]["id"]), events[1]["id"]),
        (cursor_of(store, events[2]["id"]), events[2]["id"]),
    ]

    status, _, body = await asgi_get(env.app, "/api/event",
                                     query=f"token={TOKEN}&cursor={cursor_of(store, events[1]['id'])}",
                                     headers=[("last-event-id", cursor_of(store, events[0]["id"]))], chunks=2)
    frames = sse_events(body)
    assert frames[0][2]["resumed_from"] == events[1]["id"]
    assert frames[1][2]["id"] == events[2]["id"]


async def test_closing_a_stream_unregisters_it(env):
    rt, store = env.rt, env.store
    new_session(rt, env.workspace)
    stream = Stream.open(store, rt.bus, after=store.log_head())
    generator = stream.events()
    task = asyncio.create_task(anext(generator))
    await asyncio.sleep(0)
    assert rt.bus.subscriber_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await generator.aclose()
    assert rt.bus.subscriber_count == 0


# -- overflow and persistence failures ----------------------------------------


async def test_a_slow_subscriber_receives_an_explicit_overflow(env):
    rt = env.rt
    session = new_session(rt, env.workspace)
    rt.bus.queue_max = 3
    subscription = rt.bus.open()
    try:
        published = [publish(rt, session.id, n) for n in range(10)]
        received = []
        while (event := await subscription.get()) is not None:
            received.append(event)
    finally:
        subscription.close()
    assert [event["type"] for event in received] == ["probe", "probe", "probe", "bus.overflow"]
    overflow = received[-1]
    assert overflow["data"]["dropped_after"] == published[2]["id"]
    assert overflow["data"]["head"] == published[3]["id"]
    assert overflow["data"]["generation"] == env.store.log_generation()


async def test_the_stream_ends_with_overflow(env):
    rt = env.rt
    session = new_session(rt, env.workspace)
    rt.bus.queue_max = 2
    stream = Stream.open(env.store, rt.bus, after=env.store.log_head())
    for n in range(5):
        publish(rt, session.id, n)
    events = await collect(stream, 3)
    assert [event["type"] for event in events] == ["probe", "probe", "bus.overflow"]
    assert events[-1]["id"] is None and events[-1]["data"]["dropped_after"] == events[1]["id"]


async def test_a_persistence_failure_is_not_published(env, monkeypatch):
    rt = env.rt
    session = new_session(rt, env.workspace)
    before = env.store.log_head()
    subscription = rt.bus.open()

    def boom(event):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(env.store, "append_event", boom)
    with pytest.raises(sqlite3.OperationalError):
        publish(rt, session.id, 1)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(subscription.get(), 0.05)
    assert env.store.log_head() == before
    subscription.close()


# -- stored paging and exports stay compatible --------------------------------


async def test_stored_paging_and_export_ids_are_unchanged(env):
    rt, store = env.rt, env.store
    session = new_session(rt, env.workspace)
    published = [publish(rt, session.id, n) for n in range(4)]
    rt.bus.publish("project.updated", {"id": session.project_id})
    expected = [row["id"] for row in store.replay(0, store.log_head(), 100) if row["session_id"] == session.id]
    assert published[0]["id"] in expected and expected[-1] == published[-1]["id"]

    first = (await env.client.get(f"/api/session/{session.id}/event", params={"limit": 2})).json()
    rest = (await env.client.get(f"/api/session/{session.id}/event",
                                 params={"after": first["events"][-1]["id"]})).json()
    assert first["more"] and not rest["more"]
    assert [event["id"] for event in first["events"] + rest["events"]] == expected

    response = await env.client.get(f"/api/session/{session.id}/export", params={"format": "json"})
    document = response.json()
    assert document["boundary"]["log_generation"] == store.log_generation()
    assert document["boundary"]["event_id"] == store.log_head()
    assert response.headers["x-nexus-export-boundary"].startswith(f"event:{store.log_head()};seq:")
    assert [event["id"] for event in document["sessions"][0]["events"]] == expected


# -- migration ----------------------------------------------------------------


async def test_the_previous_event_schema_migrates_without_losing_ids(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        create table event (
          id integer primary key autoincrement,
          session_id text not null references session(id) on delete cascade,
          seq integer not null, time real not null, type text not null, data text not null
        );
        create index event_session on event(session_id, id);
        insert into event (id, session_id, seq, time, type, data)
          values (7, 'ses_old', 3, 1.0, 'probe', '{"n": 1}');
    """)
    raw.commit()
    raw.close()

    store = Store(path)
    generation = store.log_generation()
    assert generation.startswith("log_")
    rows = store.replay(0, store.log_head(), 10)
    assert [(row["id"], row["session_id"], row["data"]) for row in rows] == [(7, "ses_old", {"n": 1})]
    assert store.log_floor() == 0

    stored = store.append_event({"seq": 4, "time": 2.0, "type": "project.deleted",
                                 "session_id": None, "data": {"id": "prj_x"}})
    assert stored == 8  # the autoincrement sequence continued past the preserved ids
    store.close()
    assert Store(path).log_generation() == generation


async def test_migration_keeps_deleted_high_watermark(tmp_path):
    path = tmp_path / "deleted.db"
    raw = sqlite3.connect(path)
    raw.executescript("""
        create table event (
          id integer primary key autoincrement, session_id text not null,
          seq integer not null, time real not null, type text not null, data text not null
        );
        insert into event values (900, 'gone', 1, 1, 'probe', '{}');
        delete from event;
    """)
    raw.close()
    store = Store(path)
    try:
        assert store.append_event({"session_id": None, "seq": 1, "time": 1,
                                   "type": "project.deleted", "data": {}}) == 901
    finally:
        store.close()


async def test_empty_query_cursor_is_invalid_even_with_valid_header(env):
    status, _, body = await asgi_get(env.app, "/api/event", query=f"token={TOKEN}&cursor=",
                                   headers=[("last-event-id", cursor_of(env.store, 0))])
    assert status == 400
    assert json.loads(body)["code"] == "invalid_cursor"


def test_inspector_load_and_replay_have_independent_deduplication():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for inspector state regression")
    source = (Path(__file__).parents[1] / "nexus/inspector/index.html").read_text()
    loading = source[source.index("let loadSeq = 0;"):source.index("// -- rendering")]
    applying = source[source.index("function applyEvent(e)"):source.index('document.addEventListener("keydown"')]
    script = r"""
const assert = require('node:assert/strict');
const S = {sel: 's', session: null, events: [], messages: []};
const live = {lastId: 10, cursor: 'log_x:10', generation: 'log_x', pending: []};
const MAX_EVENTS = 50000, PAGE = 5000, RAW_ROWS = 1500;
const document = {getElementById: () => ({replaceChildren() {}})};
const h = () => null, setHash = () => {}, renderSidebar = () => {}, render = () => {}, renderSoon = () => {};
const isLifecycle = (type) => type.startsWith('session.');
const event = {id: 11, cursor: 'log_x:11', session_id: 's', type: 'message.added', data: {id: 'msg'}};
async function api(path) {
  if (path.endsWith('/message')) return []; // fetched before the message was committed
  if (path.endsWith('/question')) return [];
  if (path.includes('/event?')) return {events: [event], more: false}; // fetched afterwards
  return {id: 's'};
}
""" + loading + applying + r"""
(async () => {
  await select('s');
  assert.equal(live.lastId, 10);
  applyEvent(event);
  assert.equal(S.events.length, 1);
  assert.equal(S.messages.length, 1);
  applyEvent(event);
  assert.equal(S.messages.length, 1);
  live.lastId = 10; // replay after a full reload that already has the message
  applyEvent(event);
  assert.equal(S.messages.length, 1);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
