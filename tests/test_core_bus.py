import asyncio

from nexus.core.bus import DROP_NEWEST, DROP_OLDEST, Bus
from nexus.errors import BusClosed


async def test_delivers_to_all_subscribers_in_order():
    bus = Bus(maxsize=8)
    first, second = bus.subscribe(), bus.subscribe()
    bus.publish("x")
    bus.publish("y")
    assert await first.get() == "x"
    assert await second.get() == "x"
    assert await first.get() == "y"
    assert await second.get() == "y"


async def test_drop_newest_keeps_oldest_and_counts():
    bus = Bus(maxsize=2, policy=DROP_NEWEST)
    sub = bus.subscribe()
    for item in (1, 2, 3):
        bus.publish(item)
    assert sub.dropped == 1
    assert [await sub.get(), await sub.get()] == [1, 2]


async def test_drop_oldest_keeps_newest_and_counts():
    bus = Bus(maxsize=2, policy=DROP_OLDEST)
    sub = bus.subscribe()
    for item in (1, 2, 3):
        bus.publish(item)
    assert sub.dropped == 1
    assert [await sub.get(), await sub.get()] == [2, 3]


async def test_close_drains_then_stops():
    bus = Bus(maxsize=4)
    sub = bus.subscribe()
    bus.publish("a")
    await bus.aclose()
    assert await sub.get() == "a"
    try:
        await sub.get()
        assert False
    except StopAsyncIteration:
        pass


async def test_close_is_idempotent_and_publish_rejects():
    bus = Bus()
    await bus.aclose()
    await bus.aclose()
    try:
        bus.publish("x")
        assert False
    except BusClosed:
        pass


async def test_subscribe_after_close_raises():
    bus = Bus()
    await bus.aclose()
    try:
        bus.subscribe()
        assert False
    except BusClosed:
        pass


async def test_async_iteration_waits_for_publish():
    bus = Bus()
    sub = bus.subscribe()

    async def produce():
        await asyncio.sleep(0)
        bus.publish(42)

    task = asyncio.create_task(produce())
    assert await asyncio.wait_for(sub.get(), timeout=1) == 42
    await task


async def test_unsubscribe_stops_delivery():
    bus = Bus()
    sub = bus.subscribe()
    bus.unsubscribe(sub)
    bus.publish(1)
    assert bus.subscribers == 0
    assert sub.dropped == 0


def test_invalid_configuration():
    for kwargs in ({"maxsize": 0}, {"policy": "nope"}):
        try:
            Bus(**kwargs)
            assert False
        except ValueError:
            pass
