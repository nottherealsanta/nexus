"""`Bus.publish_threadsafe`: commit on a worker thread, deliver on the owning loop (plan 54, W1)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from nexus.bus import Bus
from nexus.store import Store


def _store(tmp_path):
    store = Store(tmp_path / "nexus.db")
    bus = Bus()
    bus.attach(store)
    return store, bus


class _ManualLoop:
    """A loop stand-in that records callbacks instead of running them."""

    def __init__(self) -> None:
        self.callbacks = []

    def call_soon_threadsafe(self, callback) -> None:
        self.callbacks.append(callback)


async def test_cross_thread_publish_commits_before_return_and_wakes_a_waiter(tmp_path):
    store, bus = _store(tmp_path)
    loop = asyncio.get_running_loop()
    subscription = bus.open()
    result: dict = {}

    def worker() -> None:
        result["event"] = bus.publish_threadsafe(loop, "probe", {"i": 1})
        result["head_at_return"] = store.log_head()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        event = await asyncio.wait_for(subscription.get(), 2)
    finally:
        thread.join(2)

    assert not thread.is_alive()
    assert result["event"] is event
    assert event["id"] is not None
    assert result["head_at_return"] == event["id"]
    assert store.replay(0, store.log_head(), 10)[-1]["id"] == event["id"]


async def test_mixed_loop_and_thread_publish_keeps_id_order(tmp_path):
    store, bus = _store(tmp_path)
    loop = asyncio.get_running_loop()
    subscription = bus.open()
    published = asyncio.Event()
    go = threading.Event()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i in range(3):
                bus.publish_threadsafe(loop, "thread.event", {"i": i})
                loop.call_soon_threadsafe(published.set)
                if not go.wait(2):
                    errors.append(TimeoutError("loop did not release the worker"))
                    return
                go.clear()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        for i in range(3):
            await asyncio.wait_for(published.wait(), 2)
            published.clear()
            bus.publish("loop.event", {"i": i})
            go.set()
    finally:
        thread.join(2)

    assert not errors
    assert not thread.is_alive()
    events = [await asyncio.wait_for(subscription.get(), 2) for _ in range(6)]
    assert [event["type"] for event in events] == ["thread.event", "loop.event"] * 3
    ids = [event["id"] for event in events]
    assert ids == sorted(ids) == list(range(1, 7))
    assert store.log_head() == 6


async def test_event_committed_before_flush_is_not_live_for_a_new_subscriber(tmp_path):
    store, bus = _store(tmp_path)
    loop = _ManualLoop()
    result: dict = {}

    def worker() -> None:
        result["event"] = bus.publish_threadsafe(loop, "probe", {"i": 1})

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)

    assert not thread.is_alive()
    assert len(loop.callbacks) == 1
    event = result["event"]
    # The flush callback has not run yet, but open() drains the committed event and
    # takes its id as the replay head, so it must not arrive as a live event.
    subscription = bus.open()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.get(), 0.05)
    loop.callbacks[0]()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.get(), 0.05)
    assert subscription.head == event["id"]
    assert store.log_head() == event["id"]


async def test_failed_append_raises_in_the_caller_and_delivers_nothing(tmp_path, monkeypatch):
    store, bus = _store(tmp_path)
    loop = _ManualLoop()
    subscription = bus.open()

    def fail(_event):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "append_event", fail)
    result: dict = {}

    def worker() -> None:
        try:
            bus.publish_threadsafe(loop, "probe", {"i": 1})
        except BaseException as exc:  # noqa: BLE001 - this is what the test asserts
            result["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)

    assert not thread.is_alive()
    assert isinstance(result.get("error"), RuntimeError)
    assert loop.callbacks == []
    assert store.log_head() == 0
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.get(), 0.05)


async def test_nondurable_event_is_delivered_but_not_stored(tmp_path):
    store, bus = _store(tmp_path)
    loop = asyncio.get_running_loop()
    subscription = bus.open()

    def worker() -> None:
        bus.publish_threadsafe(loop, "thread.event", {"i": 1})
        bus.publish_threadsafe(loop, "thread.live", {"i": 2}, durable=False)

    await asyncio.to_thread(worker)
    bus.publish("loop.event", {"i": 3})

    events = [await asyncio.wait_for(subscription.get(), 2) for _ in range(3)]
    assert [event["type"] for event in events] == ["thread.event", "thread.live", "loop.event"]
    assert [event["id"] for event in events] == [1, None, 2]
    assert store.log_head() == 2
    assert [row["type"] for row in store.replay(0, store.log_head(), 10)] == ["thread.event", "loop.event"]


def test_closed_loop_durable_publish_does_not_raise_and_is_replayable(tmp_path):
    store, bus = _store(tmp_path)
    loop = asyncio.new_event_loop()
    loop.close()

    event = bus.publish_threadsafe(loop, "probe", {"i": 1})

    assert event["id"] == 1
    assert store.log_head() == 1
    assert [row["type"] for row in store.replay(0, store.log_head(), 10)] == ["probe"]


async def test_closed_loop_nondurable_publish_raises_and_is_not_left_pending(tmp_path):
    store, bus = _store(tmp_path)
    loop = asyncio.new_event_loop()
    loop.close()
    subscription = bus.open()

    durable = bus.publish_threadsafe(loop, "probe", {"i": 1})
    with pytest.raises(RuntimeError, match="live-only event.*event loop is closed"):
        bus.publish_threadsafe(loop, "probe.live", {"i": 2}, durable=False)

    # Neither the durable event nor the rejected live-only one may be delivered
    # stale by a later flush. The durable row remains available to replay.
    bus.flush()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(subscription.get(), 0.05)
    assert durable["id"] == 1
    assert store.log_head() == 1
    assert [row["type"] for row in store.replay(0, store.log_head(), 10)] == ["probe"]
