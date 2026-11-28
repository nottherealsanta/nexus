"""Recorder host (plan 54, B3): worker affinity, loop safety, detach bridge, sweeper and lifecycle."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections import deque
from typing import Any

import pytest

from nexus.bus import Bus
from nexus.recorder import RecorderStore
from nexus.recorder import store as store_module
from nexus.recorder.host import SWEEP_INTERVAL, RecorderHost, RecorderUnavailable
from nexus.recorder.service import LEASE_TTL_SECONDS, RecorderService
from nexus.server import ClientViews
from nexus.store import Store

RECOVERY = secrets.token_urlsafe(32)


class Clock:
    def __init__(self, now: float = 1_700_000_000.0, mono: float = 10_000.0) -> None:
        self.now = now
        self.mono = mono

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.mono += seconds


class FakeSubscription:
    def __init__(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self._queue = queue
        self.closed = False

    async def get(self) -> dict[str, Any] | None:
        return await self._queue.get()

    def close(self) -> None:
        self.closed = True


class FakeBus:
    """The W1 contract: commit off-loop, append under the lock, flush on the loop in id order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._queues: list[asyncio.Queue[dict[str, Any] | None]] = []
        self._next_id = 0
        self.events: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self.publish_threads: list[int] = []
        self.threadsafe_threads: list[int] = []
        self.threadsafe_loops: list[asyncio.AbstractEventLoop] = []
        self.delivery_threads: list[int] = []
        self.closed_loops: list[asyncio.AbstractEventLoop] = []

    def _event(self, type_: str, data: Any, session_id: str | None, durable: bool) -> dict[str, Any]:
        self._next_id += 1
        event = {"id": self._next_id, "seq": self._next_id, "time": time.time(), "type": type_,
                 "session_id": session_id, "data": data, "durable": durable}
        self.events.append(event)
        return event

    def publish(self, type_: str, data: Any = None, *, session_id: str | None = None,
                turn_id: str | None = None, durable: bool = True) -> dict[str, Any]:
        self.publish_threads.append(threading.get_ident())
        with self._lock:
            event = self._event(type_, data, session_id, durable)
            self._drain_locked()
            self._deliver_locked(event)
        return event

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, type_: str, data: Any = None, *,
                           session_id: str | None = None, turn_id: str | None = None,
                           durable: bool = True) -> dict[str, Any]:
        self.threadsafe_threads.append(threading.get_ident())
        self.threadsafe_loops.append(loop)
        with self._lock:
            event = self._event(type_, data, session_id, durable)
            self._pending.append(event)
        try:
            loop.call_soon_threadsafe(self.flush)
        except RuntimeError:
            self.closed_loops.append(loop)
        return event

    def flush(self) -> None:
        with self._lock:
            self._drain_locked()

    def _drain_locked(self) -> None:
        while self._pending:
            self._deliver_locked(self._pending.popleft())

    def _deliver_locked(self, event: dict[str, Any]) -> None:
        self.delivered.append(event)
        self.delivery_threads.append(threading.get_ident())
        for queue in self._queues:
            queue.put_nowait(event)

    def open(self) -> FakeSubscription:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        with self._lock:
            self._drain_locked()
            self._queues.append(queue)
        return FakeSubscription(queue)


class FakeViews:
    def __init__(self, clients: dict[str, str] | None = None) -> None:
        self.connected: set[str] = set(clients or {"s1": "tui", "s2": "nsh"})
        self.clients = dict(clients or {"s1": "tui", "s2": "nsh"})
        self.listeners: list[Any] = []

    def has_stream(self, stream_id: str) -> bool:
        return stream_id in self.connected

    def view_for(self, stream_id: str) -> dict[str, str] | None:
        if stream_id not in self.connected:
            return None
        client_id = self.clients.get(stream_id)
        return None if client_id is None else {"stream_id": stream_id, "client_id": client_id}

    def on_detach(self, listener: Any) -> None:
        self.listeners.append(listener)

    def bind(self, stream_id: str, client_id: str) -> None:
        self.connected.add(stream_id)
        self.clients[stream_id] = client_id

    def detach(self, stream_id: str) -> None:
        self.connected.discard(stream_id)
        for listener in list(self.listeners):
            listener(stream_id)


class FakeScheduler:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host: RecorderHost | None = None
        self.attach_thread: int | None = None
        self.close_thread: int | None = None
        self.closed = 0
        self.calls: list[tuple[str, tuple, int]] = []

    def attach(self, loop: asyncio.AbstractEventLoop, host: RecorderHost) -> None:
        self.loop = loop
        self.host = host
        self.attach_thread = threading.get_ident()

    async def close(self) -> None:
        self.closed += 1
        self.close_thread = threading.get_ident()

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args, threading.get_ident()))

    def recovered(self, ids: list[str]) -> None:
        self._record("recovered", ids)

    def audio_committed(self, recording_id: str, generation: int, durable_offset: int) -> None:
        self._record("audio_committed", recording_id, generation, durable_offset)

    def capture_ended(self, recording_id: str, generation: int, end_offset: int, state: str) -> None:
        self._record("capture_ended", recording_id, generation, end_offset, state)

    def cancel(self, recording_id: str) -> None:
        self._record("cancel", recording_id)


class ClosingScheduler:
    """Scheduler close that checkpoints through the bounded background lane."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host: RecorderHost | None = None
        self.close_started: asyncio.Event | None = None
        self.release = threading.Event()
        self.closed = 0
        self.checkpoint_result: str | None = None
        self.checkpoint_thread: int | None = None
        self.executor_present = False
        self.order: list[str] = []

    def attach(self, loop: asyncio.AbstractEventLoop, host: RecorderHost) -> None:
        self.loop = loop
        self.host = host
        self.close_started = asyncio.Event()

    async def close(self) -> None:
        self.closed += 1
        assert self.host is not None and self.close_started is not None
        self.close_started.set()

        def checkpoint() -> str:
            self.checkpoint_thread = threading.get_ident()
            self.executor_present = self.host._executor is not None
            self.order.append("checkpoint-start")
            assert self.release.wait(2.0)
            self.order.append("checkpoint-end")
            return "checkpointed"

        self.checkpoint_result = await self.host.call_background(checkpoint)
        self.order.append("close-end")


class FakeStream:
    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self.listeners: list[Any] = []
        self.closed = False

    def add_close_listener(self, listener: Any) -> None:
        self.listeners.append(listener)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for listener in list(self.listeners):
            listener(self)


async def wait_until(predicate: Any, *, timeout: float = 2.0, message: str = "condition timed out") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.005)


@pytest.fixture
async def make_host(tmp_path):
    hosts: list[RecorderHost] = []
    stores: list[Store] = []
    count = 0

    def make(*, bus: Any = None, views: Any = None, clock: Clock | None = None,
             sweep_interval: float = SWEEP_INTERVAL, max_pending_calls: int = 32,
             max_pending_background: int = 8, max_pending_detach: int = 256,
             scheduler: Any = None) -> RecorderHost:
        nonlocal count
        count += 1
        if bus is None:
            store = Store(tmp_path / f"nexus-{count}.db")
            bus = Bus()
            bus.attach(store)
            stores.append(store)
        host = RecorderHost(
            bus,
            views if views is not None else FakeViews(),
            store=RecorderStore(tmp_path / f"recordings-{count}"),
            scheduler=scheduler,
            clock=clock,
            monotonic=clock.monotonic if clock is not None else None,
            sweep_interval=sweep_interval,
            max_pending_calls=max_pending_calls,
            max_pending_background=max_pending_background,
            max_pending_detach=max_pending_detach,
        )
        hosts.append(host)
        return host

    yield make
    for host in hosts:
        await host.close()
    for store in stores:
        store.close()


async def test_service_calls_run_on_one_worker_in_submission_order(make_host):
    host = make_host()
    await host.start()
    loop_thread = threading.get_ident()
    seen: list[tuple[str, int]] = []

    def wrap(name: str, fn: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            seen.append((name, threading.get_ident()))
            return fn(*args, **kwargs)
        return call

    host.service.create = wrap("create", host.service.create)
    host.service.get = wrap("get", host.service.get)
    host.service.list = wrap("list", host.service.list)

    created = await host.call(host.service.create, "s1", "tui", "key-1", recovery=RECOVERY)
    recording_id = created["id"]

    order: list[str] = []

    def job(name: str, delay: float) -> str:
        order.append(f"{name}:start")
        time.sleep(delay)
        order.append(f"{name}:end")
        return name

    results = await asyncio.gather(host.call(job, "a", 0.05), host.call(job, "b", 0.0))
    await asyncio.gather(host.call(host.service.get, recording_id), host.call(host.service.list))

    assert results == ["a", "b"]
    assert order == ["a:start", "a:end", "b:start", "b:end"]
    worker_threads = {thread for _, thread in seen}
    assert len(worker_threads) == 1
    assert loop_thread not in worker_threads


async def test_service_fsync_never_blocks_the_loop(make_host, monkeypatch):
    host = make_host()
    await host.start()
    await host.call(lambda: None)  # startup first, so only create's fsyncs are measured

    real_fsync = store_module.os.fsync

    def slow_fsync(fd: int) -> None:
        time.sleep(0.3)
        real_fsync(fd)

    monkeypatch.setattr(store_module.os, "fsync", slow_fsync)
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        created = await host.call(host.service.create, "s1", "tui", "key-loop", recovery=RECOVERY)
    finally:
        stop.set()
        await task
    assert created["active"] is True
    assert gaps and max(gaps) < 0.1, f"loop stalled for {max(gaps):.3f}s"


async def test_detach_listener_is_queued_not_run_on_the_loop(make_host):
    views = FakeViews()
    host = make_host(views=views)
    await host.start()
    created = await host.call(host.service.create, "s1", "tui", "key-detach", recovery=RECOVERY)
    recording_id = created["id"]

    locked = threading.Event()
    release = threading.Event()

    def hold_service_lock() -> str:
        with host.service._lock:  # the lock the service really uses
            locked.set()
            release.wait(2)
        return "held"

    holder = asyncio.create_task(host.call(hold_service_lock))
    try:
        await wait_until(locked.is_set)
        started = time.perf_counter()
        views.detach("s1")
        elapsed = time.perf_counter() - started
        assert elapsed < 0.1, "detach listener ran on the loop behind the service lock"
        assert not release.is_set()
    finally:
        release.set()
    assert await holder == "held"
    await host.call(lambda: None)  # drain the queued detach listener

    state = await host.call(host.service.get, recording_id)
    assert state["stream_detached"] is True


async def test_real_client_views_are_read_from_the_worker_and_detach_is_bridged(make_host):
    views = ClientViews()
    stream = FakeStream("stream_real")
    views.attach(stream)
    views.report("stream_real", "tui", project=None, session=None, panel=None)
    host = make_host(views=views)
    await host.start()

    created = await host.call(host.service.create, "stream_real", "tui", "key-views", recovery=RECOVERY)
    recording_id = created["id"]
    assert created["owner"] == {"client_id": "tui"}

    stream.close()  # ClientViews._detach runs on the loop and must only queue recorder work
    await host.call(lambda: None)
    state = await host.call(host.service.get, recording_id)
    assert state["stream_detached"] is True


async def test_sweeper_expires_a_lease_and_delivers_state_on_the_loop(make_host):
    clock = Clock()
    host = make_host(clock=clock, sweep_interval=0.02)
    await host.start()
    created = await host.call(host.service.create, "s1", "tui", "key-sweep", recovery=RECOVERY)
    recording_id = created["id"]

    subscription = host.bus.bus.open()
    clock.advance(LEASE_TTL_SECONDS + 1)

    event = None
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            candidate = await asyncio.wait_for(subscription.get(), 2)
            if (candidate is not None and candidate["type"] == "recorder.state"
                    and (candidate["data"] or {}).get("reason") == "lease_expired"):
                event = candidate
                break
    finally:
        subscription.close()
    assert event is not None, "the sweeper did not publish lease_expired"
    assert event["id"] is not None and event["session_id"] is None

    state = await host.call(host.service.get, recording_id)
    assert state["capture_state"] == "interrupted" and state["active"] is False


async def test_close_waits_for_an_inflight_call_then_refuses_new_ones(make_host):
    host = make_host()
    await host.start()
    await host.call(lambda: None)

    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(2)
        return "committed"

    inflight = asyncio.create_task(host.call(blocking))
    try:
        await wait_until(started.is_set)
        closing = asyncio.create_task(host.close())
        await asyncio.sleep(0.05)
        assert not closing.done()
        with pytest.raises(RecorderUnavailable):
            await host.call(lambda: None)
        release.set()
        assert await inflight == "committed"
        await asyncio.wait_for(closing, 3)
    finally:
        release.set()

    with pytest.raises(RecorderUnavailable):
        await host.call(lambda: None)
    assert host.closed is True


async def test_cancelled_call_keeps_its_work_running(make_host):
    host = make_host()
    await host.start()

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def job() -> int:
        started.set()
        release.wait(2)
        finished.set()
        return 42

    task = asyncio.create_task(host.call(job))
    try:
        await wait_until(started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set()
    finally:
        release.set()

    await wait_until(finished.is_set)
    assert await host.call(lambda: 7) == 7


async def test_cancelled_closer_does_not_signal_completion_and_concurrent_close_waits(make_host):
    host = make_host()
    await host.start()
    await host.call(lambda: None)

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(2)
        finished.set()
        return "committed"

    inflight = asyncio.create_task(host.call(blocking))
    first = second = None
    try:
        await wait_until(started.is_set)
        first = asyncio.create_task(host.close())
        await wait_until(lambda: host._shutdown_task is not None)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert host.closed is True
        assert host._close_event is not None and not host._close_event.is_set()

        second = asyncio.create_task(host.close())
        await asyncio.sleep(0.05)
        assert not second.done(), "a concurrent close returned before the executor drained"
        assert not finished.is_set()
    finally:
        release.set()

    assert await inflight == "committed"
    assert second is not None
    await asyncio.wait_for(second, 3)
    assert host._close_event is not None and host._close_event.is_set()
    with pytest.raises(RecorderUnavailable):
        await host.call(lambda: None)


async def test_normal_calls_get_retryable_backpressure_when_admission_is_full(make_host):
    host = make_host(max_pending_calls=1)
    await host.start()
    await host.call(lambda: None)

    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(2)
        return "done"

    inflight = asyncio.create_task(host.call(blocking))
    try:
        await wait_until(started.is_set)
        with pytest.raises(RecorderUnavailable) as rejected:
            await host.call(lambda: None)
        assert rejected.value.code == "recorder_backpressure"
        assert rejected.value.status == 503
        assert rejected.value.retry_after == 1
    finally:
        release.set()
    assert await inflight == "done"
    assert await host.call(lambda: "admitted") == "admitted"


async def test_detach_is_reserved_and_coalesced_under_normal_saturation(make_host, monkeypatch):
    calls: list[tuple[str, int]] = []
    original = RecorderService.on_stream_detached

    def counted(self: RecorderService, stream_id: str) -> bool:
        calls.append((stream_id, threading.get_ident()))
        return original(self, stream_id)

    monkeypatch.setattr(RecorderService, "on_stream_detached", counted)
    views = FakeViews()
    host = make_host(views=views, max_pending_calls=1)
    await host.start()
    created = await host.call(host.service.create, "s1", "tui", "key-detach-reserved", recovery=RECOVERY)
    recording_id = created["id"]

    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(2)
        return "done"

    inflight = asyncio.create_task(host.call(blocking))
    try:
        await wait_until(started.is_set)
        with pytest.raises(RecorderUnavailable):
            await host.call(lambda: None)  # normal admission is full
        views.detach("s1")
        views.detach("s1")
        views.detach("s1")
    finally:
        release.set()
    assert await inflight == "done"
    await wait_until(lambda: len(calls) >= 1)
    await host.call(lambda: None)  # drain the reserved detach job

    state = await host.call(host.service.get, recording_id)
    assert state["stream_detached"] is True
    assert calls == [("s1", calls[0][1])]
    assert calls[0][1] != threading.get_ident()


async def test_detach_reserve_is_bounded_and_coalesces_overflow(make_host, caplog):
    host = make_host(max_pending_detach=1)
    await host.start()
    await host.call(lambda: None)

    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(2)
        return "done"

    inflight = asyncio.create_task(host.call(blocking))
    delivered: list[str] = []

    def listener(stream_id: str) -> None:
        delivered.append(stream_id)

    try:
        await wait_until(started.is_set)
        assert host.submit_detach(listener, "s1") is True
        assert host.submit_detach(listener, "s2") is True  # accepted into the owner scan
        assert "coalescing into the active-owner scan" in caplog.text
    finally:
        release.set()
    assert await inflight == "done"
    await host.call(lambda: None)
    assert delivered == ["s1"]


async def test_owner_detach_survives_a_full_detach_reserve(make_host):
    views = FakeViews()
    host = make_host(views=views, max_pending_detach=1)
    await host.start()
    created = await host.call(host.service.create, "s1", "tui", "key-owner-overflow", recovery=RECOVERY)
    recording_id = created["id"]

    started = threading.Event()
    release = threading.Event()

    def blocking() -> str:
        started.set()
        release.wait(2)
        return "done"

    inflight = asyncio.create_task(host.call(blocking))
    try:
        await wait_until(started.is_set)
        views.detach("s2")  # fills the bounded per-stream reserve
        views.detach("s1")  # the active owner's detach overflows it
    finally:
        release.set()
    assert await inflight == "done"
    await host.call(lambda: None)  # drain the queued batch and owner scan

    state = await host.call(host.service.get, recording_id)
    assert state["stream_detached"] is True
    assert state["active"] is True


async def test_startup_failure_fails_closed(make_host):
    host = make_host()
    gate = threading.Event()
    started = threading.Event()
    ran: list[str] = []

    def failing_startup() -> list[dict[str, Any]]:
        started.set()
        gate.wait(2)
        raise RuntimeError("reconciliation exploded")

    host.service.startup = failing_startup
    await host.start()
    try:
        await wait_until(started.is_set)
        queued = asyncio.create_task(host.call(lambda: ran.append("queued")))
        await asyncio.sleep(0)
    finally:
        gate.set()

    with pytest.raises(RecorderUnavailable) as failed:
        await queued
    assert failed.value.code == "recorder_startup_failed"
    assert ran == []
    with pytest.raises(RecorderUnavailable):
        await host.call(lambda: ran.append("later"))
    assert host.submit_nowait(lambda: ran.append("background")) is False
    assert ran == []


async def test_overlapping_sweeps_are_not_queued(make_host):
    host = make_host(sweep_interval=0.01)
    started = threading.Event()
    release = threading.Event()
    submissions: list[float] = []
    original_control = host._call_control
    original_sweep = host.service.sweep

    def slow_sweep() -> list[dict[str, Any]]:
        started.set()
        release.wait(2)
        return original_sweep()

    async def counting_control(fn: Any, *args: Any, **kwargs: Any) -> Any:
        if fn is slow_sweep:
            submissions.append(time.monotonic())
        return await original_control(fn, *args, **kwargs)

    host.service.sweep = slow_sweep
    host._call_control = counting_control  # type: ignore[method-assign]
    await host.start()
    try:
        await wait_until(started.is_set)
        await asyncio.sleep(0.1)
        assert len(submissions) == 1, "the sweeper queued another sweep while one was in flight"
    finally:
        release.set()
    await wait_until(lambda: len(submissions) >= 2)


async def test_start_does_not_wait_for_reconciliation(make_host):
    host = make_host()
    gate = threading.Event()
    started = threading.Event()
    original = host.service.startup

    def slow_startup() -> list[dict[str, Any]]:
        started.set()
        gate.wait(2)
        return original()

    host.service.startup = slow_startup
    before = time.perf_counter()
    await host.start()
    elapsed = time.perf_counter() - before
    try:
        assert elapsed < 0.2
        await wait_until(started.is_set)
        assert host.closed is False
    finally:
        gate.set()
    await host.call(lambda: None)


async def test_thread_safe_bus_uses_publish_on_loop_and_threadsafe_off_loop(make_host):
    bus = FakeBus()
    host = make_host(bus=bus)
    await host.start()

    first = host.bus.publish("loop.event", {"n": 1})
    second = await host.call(host.bus.publish, "worker.event", {"n": 2})

    assert first["type"] == "loop.event" and second["type"] == "worker.event"
    assert bus.publish_threads == [threading.get_ident()]
    assert len(bus.threadsafe_threads) == 1
    assert bus.threadsafe_threads[0] != threading.get_ident()
    assert bus.threadsafe_loops == [host.loop]


async def test_off_loop_publish_without_publish_threadsafe_is_refused(make_host):
    class OldBus:
        def publish(self, type_: str, data: Any = None, **_: Any) -> dict[str, Any]:
            raise AssertionError("publish must not be called from the recorder worker")

    host = make_host(bus=OldBus())
    await host.start()
    with pytest.raises(RuntimeError, match="publish_threadsafe"):
        await host.call(host.bus.publish, "worker.event", {})


async def test_worker_and_loop_publishes_deliver_in_commit_order(make_host):
    host = make_host()
    await host.start()
    subscription = host.bus.bus.open()

    try:
        await host.call(host.bus.publish, "worker.one", {"n": 1})
        host.bus.publish("loop.two", {"n": 2})  # drains the worker's pending event first
        await host.call(host.bus.publish, "worker.three", {"n": 3})

        delivered = [await asyncio.wait_for(subscription.get(), 2) for _ in range(3)]
    finally:
        subscription.close()
    assert [event["type"] for event in delivered] == ["worker.one", "loop.two", "worker.three"]
    ids = [event["id"] for event in delivered]
    assert ids == sorted(ids)
    assert all(event["session_id"] is None for event in delivered)


async def test_close_before_start_is_idempotent(make_host):
    host = make_host()
    await host.close()
    await host.close()
    with pytest.raises(RecorderUnavailable):
        await host.call(lambda: None)
    with pytest.raises(RecorderUnavailable):
        await host.start()


async def test_scheduler_attach_close_and_hooks_use_the_right_threads(make_host):
    scheduler = FakeScheduler()
    host = make_host(scheduler=scheduler)
    loop_thread = threading.get_ident()
    await host.start()
    assert scheduler.loop is host.loop and scheduler.host is host
    assert scheduler.attach_thread == loop_thread
    await wait_until(lambda: any(name == "recovered" for name, _args, _thread in scheduler.calls))

    created = await host.call(host.service.create, "s1", "tui", "key-1", recovery=RECOVERY)
    recording_id, lease = created["id"], created["lease_token"]
    chunk = b"\x01\x02" * 160
    digest = __import__("hashlib").sha256(chunk).hexdigest()
    await host.call(host.service.put_chunk, recording_id, 0, 0, chunk, digest, lease)
    await host.call(host.service.stop, recording_id, lease)

    hook_threads = {thread for _name, _args, thread in scheduler.calls}
    assert len(hook_threads) == 1 and loop_thread not in hook_threads
    assert any(name == "audio_committed" for name, _args, _thread in scheduler.calls)
    assert any(name == "capture_ended" for name, _args, _thread in scheduler.calls)

    await host.close()
    assert scheduler.closed == 1 and scheduler.close_thread == loop_thread


async def test_background_lane_is_separate_from_normal_call_admission(make_host):
    host = make_host(max_pending_calls=1, max_pending_background=1)
    await host.start()
    await wait_until(lambda: host._startup_future is not None and host._startup_future.done())

    entered = threading.Event()
    release = threading.Event()

    def block() -> str:
        entered.set()
        assert release.wait(2.0)
        return "released"

    normal = asyncio.create_task(host.call(block))
    await wait_until(entered.is_set)
    with pytest.raises(RecorderUnavailable) as backpressure:
        await host.call(lambda: "nope")
    assert backpressure.value.code == "recorder_backpressure"

    background = asyncio.create_task(host.call_background(lambda: "bg"))
    await wait_until(lambda: host._pending_background == 1)
    with pytest.raises(RecorderUnavailable) as background_full:
        await host.call_background(lambda: "nope")
    assert background_full.value.code == "recorder_background_full"
    release.set()
    assert await asyncio.wait_for(normal, 2.0) == "released"
    assert await asyncio.wait_for(background, 2.0) == "bg"

    entered.clear()
    release.clear()
    background_block = asyncio.create_task(host.call_background(block))
    await wait_until(lambda: host._pending_background == 1 and entered.is_set())
    with pytest.raises(RecorderUnavailable) as background_full:
        await host.call_background(lambda: "nope")
    assert background_full.value.code == "recorder_background_full"
    normal_during_background = asyncio.create_task(host.call(lambda: "normal"))
    release.set()
    assert await asyncio.wait_for(normal_during_background, 2.0) == "normal"
    assert await asyncio.wait_for(background_block, 2.0) == "released"


async def test_scheduler_close_can_use_background_lane_while_external_calls_are_closed(make_host):
    scheduler = ClosingScheduler()
    host = make_host(scheduler=scheduler)
    loop_thread = threading.get_ident()
    await host.start()

    first_close = asyncio.create_task(host.close())
    second_close = asyncio.create_task(host.close())
    await wait_until(lambda: scheduler.close_started is not None and scheduler.close_started.is_set())

    with pytest.raises(RecorderUnavailable) as closed_call:
        await host.call(lambda: "no")
    assert closed_call.value.code == "recorder_unavailable"
    with pytest.raises(RecorderUnavailable):
        await host.call_background(lambda: "no")
    assert host.submit_nowait(lambda: None) is False

    scheduler.release.set()
    await asyncio.wait_for(asyncio.gather(first_close, second_close), 3.0)
    assert scheduler.closed == 1
    assert scheduler.checkpoint_result == "checkpointed"
    assert scheduler.executor_present is True
    assert scheduler.order == ["checkpoint-start", "checkpoint-end", "close-end"]
    assert scheduler.checkpoint_thread not in (None, loop_thread)

    with pytest.raises(RecorderUnavailable):
        await host.call_background(lambda: "after")


async def test_abandoned_inner_shielded_future_exception_is_retrieved():
    future = asyncio.get_running_loop().create_future()
    future.set_exception(RuntimeError("abandoned"))
    assert future._log_traceback is True  # type: ignore[attr-defined]
    RecorderHost._retrieve_asyncio_exception(future)
    assert future._log_traceback is False  # type: ignore[attr-defined]


async def test_recording_enumeration_runs_on_the_recorder_background_worker(make_host):
    host = make_host()
    loop_thread = threading.get_ident()
    await host.start()
    created = await host.call(host.service.create, "s1", "tui", "key-1", recovery=RECOVERY)
    await host.call(host.service.stop, created["id"], created["lease_token"])

    threads: list[int] = []
    original = host.service.enumerate_recordings

    def wrapped(after: str | None = None, limit: int = 50) -> dict[str, Any]:
        threads.append(threading.get_ident())
        return original(after, limit)

    host.service.enumerate_recordings = wrapped
    page = await host.enumerate_recordings(None, 10)
    assert [item["id"] for item in page["recordings"]] == [created["id"]]
    assert page["recordings"][0]["capture_state"] == "stopped"
    assert threads and loop_thread not in threads
