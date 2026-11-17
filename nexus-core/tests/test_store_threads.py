"""Storage threading (plan 50, phase 1a): the event loop never waits for SQLite.

A slow read or write is held in the store while other requests are served, a large read does not stall the loop,
a caller that writes then reads sees its write, and a write queued on the writer thread survives a cancelled
caller. See docs/daemon.md § Storage threading.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Self

import httpx
import pytest
from conftest import remembering

from nexus import store as store_module
from nexus.llmhub.types import Message, Text
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
    project = rt.open_project(str(workspace))
    session = rt.create_session(project, title="t", model=None, permission=None, agent="build")
    yield rt, store, client, session
    store.close()


def user(text: str) -> Message:
    return Message("user", (Text(text),))


async def timed(client, path: str) -> float:
    started = time.perf_counter()
    response = await client.get(path)
    assert response.status_code == 200, response.text
    return time.perf_counter() - started


class Lateness:
    """How late a 5 ms ticker wakes: the event loop's stall, measured directly."""

    def __init__(self) -> None:
        self.max = 0.0
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> Self:
        async def tick() -> None:
            loop = asyncio.get_running_loop()
            while True:
                before = loop.time()
                await asyncio.sleep(0.005)
                self.max = max(self.max, loop.time() - before - 0.005)
        self._task = asyncio.create_task(tick())
        await asyncio.sleep(0.05)
        self.max = 0.0
        return self

    async def __aexit__(self, *exc) -> None:
        self._task.cancel()


SLOW = 0.8


async def test_a_slow_read_does_not_hold_other_requests(env, monkeypatch):
    _, store, client, session = env
    store.append_message(session.id, user("hello"))
    original = Store.messages_page

    def slow(self, *args, **kwargs):
        time.sleep(SLOW)  # a slow query inside the store
        return original(self, *args, **kwargs)

    monkeypatch.setattr(store_module.Store, "messages_page", slow)
    slow_request = asyncio.create_task(client.get(f"/api/session/{session.id}/message?limit=10"))
    await asyncio.sleep(0.1)  # the read is now in progress on a reader thread
    assert not slow_request.done()
    assert await timed(client, "/api/info") < SLOW / 2
    assert await timed(client, "/api/bash") < SLOW / 2
    assert await timed(client, f"/api/session/{session.id}") < SLOW / 2  # sessions list shape, also a read
    response = await slow_request
    assert response.status_code == 200 and len(response.json()["messages"]) == 1


async def test_a_slow_write_does_not_hold_other_requests(env):
    _rt, store, client, session = env
    store.append_message(session.id, user("hello"))
    def hold_the_write_lock(s):
        with s.db:  # the writer thread is inside a transaction for a while
            s.db.execute("insert into preference (name, value) values ('probe', '1')")
            time.sleep(SLOW)

    slow_write = asyncio.create_task(store.w.run(hold_the_write_lock))
    await asyncio.sleep(0.1)  # the writer thread holds the write connection
    assert not slow_write.done()
    assert await timed(client, "/api/info") < SLOW / 2
    assert await timed(client, "/api/bash") < SLOW / 2
    assert await timed(client, f"/api/session/{session.id}/message?limit=10") < SLOW / 2  # reads use the pool
    assert await timed(client, "/api/session") < SLOW / 2
    assert await timed(client, f"/api/session/{session.id}/turn") < SLOW / 2
    assert await timed(client, f"/api/snapshot?session={session.id}") < SLOW / 2
    await slow_write
    assert store.preference("probe") == "1"


async def test_a_large_read_does_not_stall_the_loop(env):
    _rt, store, client, session = env
    big = "x" * 4000
    for n in range(3000):
        store.append_message(session.id, user(f"{n} {big}"))
    async with Lateness() as lateness:
        for path in (f"/api/session/{session.id}/message",  # all 3000, ~12 MB
                     f"/api/session/{session.id}/message?limit=500",
                     f"/api/snapshot?session={session.id}&message_limit=1000"):
            response = await client.get(path)
            assert response.status_code == 200
    # The encode of the 12 MB body is still on the loop (plan 50 phase 1b); the SQLite read and row conversion are not.
    assert lateness.max < 0.25, f"loop stalled {lateness.max * 1000:.0f} ms"


async def test_writing_then_reading_sees_the_write(env):
    _rt, store, _client, session = env
    for n in range(30):
        await store.w.append_message(session.id, user(f"m{n}"))
        page = await store.r.messages_page(session.id, limit=100)
        assert len(page["messages"]) == n + 1
        assert page["messages"][-1]["parts"][0]["text"] == f"m{n}"
    await store.w.set_preference("x", "y")
    assert await store.r.preference("x") == "y"


async def test_a_unit_run_on_the_writer_is_one_transaction(env):
    _rt, store, _client, _session = env

    def two_writes_then_fail(s):
        with s.db:
            s.db.execute("insert into preference (name, value) values ('a', '1')")
            s.db.execute("insert into preference (name, value) values ('b', '2')")
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await store.w.run(two_writes_then_fail)
    assert store.preference("a") is None and store.preference("b") is None


async def test_readers_are_read_only_and_one_snapshot(env):
    _rt, store, _client, session = env
    with pytest.raises(sqlite3.OperationalError):
        await store.r.run(lambda s: s.db.execute("delete from session"))
    with pytest.raises(AttributeError):
        await store.r.append_message(session.id, user("no"))  # not a read method

    def twice(s):
        first = s.db.execute("select count(*) from message").fetchone()[0]
        time.sleep(0.2)  # a write commits while this read is open
        return first, s.db.execute("select count(*) from message").fetchone()[0]

    read = asyncio.create_task(store.r.run(twice))
    await asyncio.sleep(0.05)
    await store.w.append_message(session.id, user("during"))
    first, second = await read
    assert first == second  # one snapshot for the whole call


async def test_a_cancelled_caller_does_not_drop_its_queued_write(env):
    _rt, store, _client, _session = env
    gate = asyncio.Event()
    blocker = asyncio.create_task(store.w.run(lambda s: time.sleep(0.3)))
    await asyncio.sleep(0.05)
    write = asyncio.create_task(store.w.set_preference("queued", "yes"))
    await asyncio.sleep(0.05)
    write.cancel()  # the caller goes away while its write is still queued behind the blocker
    await blocker
    gate.set()
    await asyncio.sleep(0.3)
    assert store.preference("queued") == "yes"


async def test_apublish_is_durable_on_return_and_delivered_in_id_order(env):
    rt, store, _client, session = env
    subscription = rt.bus.open()
    seen = []

    async def consume():
        while (event := await subscription.get()) is not None:
            if event["type"] == "probe":
                seen.append(event)
                if len(seen) == 60:
                    return

    consumer = asyncio.create_task(consume())

    async def writer(base):
        for n in range(20):
            event = await rt.bus.apublish("probe", {"n": base + n}, session_id=session.id)
            assert store.replay(event["id"] - 1, event["id"], 1)[0]["id"] == event["id"]  # durable on return

    async def sync_publisher():
        for n in range(20):
            rt.bus.publish("probe", {"n": 1000 + n}, session_id=session.id)
            await asyncio.sleep(0)

    await asyncio.gather(writer(0), writer(100), sync_publisher())
    await asyncio.wait_for(consumer, 5)
    ids = [e["id"] for e in seen]
    assert ids == sorted(ids) and len(set(ids)) == 60
    stored = [e["data"]["n"] for e in store.events(session.id, types=["probe"])]
    assert stored == [e["data"]["n"] for e in seen]  # delivery order is log order
    subscription.close()


async def test_retention_pass_runs_off_the_loop(env):
    rt, _store, _client, _session = env
    summary = await rt.retention.run_once_async()
    assert "events" in summary and rt.retention.last_summary is summary
