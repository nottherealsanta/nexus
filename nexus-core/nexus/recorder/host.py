"""Event-loop integration for the synchronous recorder service (plan 54, B3).

`RecorderService` is synchronous, serializes every operation on one re-entrant lock and may fsync while holding
it. This module puts all of it on one dedicated worker thread and gives the loop an awaitable facade:

* `RecorderHost.call(fn, *args)` submits to a single-worker executor and awaits the result shielded, so a client
  disconnect cannot abandon a committed chunk or publish.
* `StreamsAdapter` re-dispatches `ClientViews` detach listeners to that executor instead of running them on the
  loop while a commit holds the service lock.
* `ThreadSafeBus` keeps `RecorderService` code unchanged: on the loop it calls `Bus.publish`, and from the worker it
  calls `Bus.publish_threadsafe(loop, ...)`, which commits on the calling thread and delivers through the loop in
  event-id order.
* `RecorderHost.call_background(fn, *args)` is a separately bounded lane for the scheduler's feed/
  transcription jobs. It has its own admission counter (`MAX_PENDING_BACKGROUND`), so normal calls and
  background work cannot starve each other's admission; overflow is `recorder_background_full`.
* `RecorderHost.enumerate_recordings(after, limit)` runs the service's cursor-paged recording discovery
  on that background/worker lane, including stopped recordings.
* A periodic sweeper runs `RecorderService.sweep` without letting a slow disk queue overlapping sweeps.

The scheduler lifecycle is explicit: `start()` calls the injected scheduler's synchronous, non-blocking
`attach(loop, host)` before submitting service startup, and shutdown calls `await scheduler.close()` first
(even though `RecorderHost.close()` drains the service worker afterwards). During that close only the
scheduler's close context may submit through `call_background` (to drain/checkpoint); every external call,
including external `call_background`, is already refused. The executor stays alive until scheduler.close
returns. Service→scheduler hooks are notifications, not durable delivery; the scheduler must periodically
reconcile persisted capture/transcript state as its safety net.

Normal calls have bounded admission: when `max_pending_calls` work already waits on the worker, `call` raises
`RecorderUnavailable` with `code="recorder_backpressure"` and `retry_after`. Detach notifications bypass that admission
through a coalesced, bounded reserve, so they cannot be lost because request callers filled the queue. If that reserve
fills, the host remembers the stream bound by a successful `create`/`rebind` through `call` and coalesces overflow into
a disconnected-owner scan on the recorder worker; only one recording is active, so the owner's detach is not lost.
If `service.startup()` raises, the host fails closed (`code="recorder_startup_failed"`). The service no longer
swallows reconciliation failures, so every later call also fails before touching the store.

Threading contract: the recorder executor is the only thread that touches the service. `ClientViews.has_stream`,
`view_for` and the detach callback are the only registry calls made from the recorder thread; the first two are
single dict/set reads and the last is re-dispatched. The loop only awaits futures.

Lifecycle: `await host.start()` records the running loop, creates the executor and sweeper and submits
`service.startup()` in the background (so a large recordings directory does not delay readiness). `await
host.close()` cancels the sweeper, refuses new work and then drains the executor before the caller shuts the store
or the runtime down. Calls after close raise `RecorderUnavailable`.

Durability is the service's, not the host's: if the service's sidecar outbox write and its bus publish both fail,
that individual state event can be lost even though the committed store state remains authoritative. The host
adds ordering and thread safety, not a stronger announcement guarantee.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import inspect
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .service import RecorderService
from .store import RecorderStore

log = logging.getLogger(__name__)

SWEEP_INTERVAL = 0.5  # matches STATE_MIN_INTERVAL_SECONDS, so coalesced state reaches a subscriber within ~1 s
MAX_PENDING_CALLS = 32  # queued/running normal service calls; beyond this callers get a retryable 503
MAX_PENDING_BACKGROUND = 8  # scheduler pump lane; independent of normal calls and control jobs
MAX_PENDING_CONTROL = 16  # generic fire-and-forget jobs; detach jobs use their own coalesced reserve
MAX_PENDING_DETACH = 256  # distinct stream ids waiting for the detach drain
BACKPRESSURE_RETRY_AFTER = 1

# Set only while awaiting scheduler.close() inside the host shutdown task. It lets that close path
# checkpoint through call_background even though external admission is already closed.
_SCHEDULER_CLOSING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "recorder_scheduler_closing", default=False)


class RecorderUnavailable(RuntimeError):
    """The recorder host cannot run this operation now; callers should answer 503 (retry when advertised)."""

    code = "recorder_unavailable"
    status = 503

    def __init__(self, message: str = "", *, code: str | None = None,
                 retry_after: int | None = None) -> None:
        super().__init__(message or code or self.code)
        if code is not None:
            self.code = code
        self.retry_after = retry_after


class ThreadSafeBus:
    """The `publish` seam `RecorderService` expects, safe from its worker thread.

    On the loop, `Bus.publish` is correct as-is. Off the loop it must not deliver to asyncio subscriber queues,
    so this adapter requires `Bus.publish_threadsafe(loop, ...)` (plan 54, W1), which commits, appends to the bus's
    ordered `_pending` deque and wakes the loop without touching a subscriber.
    """

    def __init__(self, bus: Any, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.bus = bus
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def set_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self._loop = loop

    def publish(
        self,
        type_: str,
        data: Any = None,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        durable: bool = True,
    ) -> dict[str, Any]:
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and (loop is None or running is loop):
            return self.bus.publish(type_, data, session_id=session_id, turn_id=turn_id, durable=durable)
        target = loop if loop is not None else running
        if target is None:
            raise RuntimeError("the recorder event bus has no loop to deliver on")
        publish_threadsafe = getattr(self.bus, "publish_threadsafe", None)
        if publish_threadsafe is None:
            # Never fall back to `Bus.publish` here: it would touch asyncio queues from the worker thread.
            raise RuntimeError("the event bus does not implement publish_threadsafe")
        return publish_threadsafe(
            target, type_, data, session_id=session_id, turn_id=turn_id, durable=durable)


class StreamsAdapter:
    """The `streams` seam `RecorderService` expects, over the real `ClientViews`.

    `has_stream`/`view_for` are GIL-atomic reads of the registry's current entries. Detach listeners are never
    invoked on the loop: they are queued to the recorder executor so `on_stream_detached` takes the service lock
    only after any in-flight commit.
    """

    def __init__(self, views: Any, host: RecorderHost) -> None:
        self._views = views
        self._host = host

    def has_stream(self, stream_id: str) -> bool:
        return bool(self._views.has_stream(stream_id))

    def view_for(self, stream_id: str) -> dict[str, Any] | None:
        view = self._views.view_for(stream_id)
        return view if isinstance(view, dict) else None

    def on_detach(self, listener: Callable[[str], None]) -> None:
        self._views.on_detach(lambda stream_id: self._host.submit_detach(listener, stream_id))


class RecorderHost:
    """One recorder worker, its bus/stream adapters, the service and the sweeper."""

    def __init__(
        self,
        bus: Any,
        client_views: Any,
        *,
        store: RecorderStore | None = None,
        scheduler: Any = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        executor: ThreadPoolExecutor | None = None,
        sweep_interval: float = SWEEP_INTERVAL,
        max_pending_calls: int = MAX_PENDING_CALLS,
        max_pending_background: int = MAX_PENDING_BACKGROUND,
        max_pending_control: int = MAX_PENDING_CONTROL,
        max_pending_detach: int = MAX_PENDING_DETACH,
    ) -> None:
        if sweep_interval <= 0:
            raise ValueError("sweep_interval must be positive")
        if (max_pending_calls <= 0 or max_pending_background <= 0
                or max_pending_control <= 0 or max_pending_detach <= 0):
            raise ValueError("admission limits must be positive")
        self._bus = ThreadSafeBus(bus)
        self._views = StreamsAdapter(client_views, self)
        self._scheduler = scheduler
        self._executor = executor
        self._sweep_interval = sweep_interval
        self._max_pending_calls = max_pending_calls
        self._max_pending_background = max_pending_background
        self._max_pending_control = max_pending_control
        self._max_pending_detach = max_pending_detach
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closed = False
        self._close_event: asyncio.Event | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._sweep_task: asyncio.Task[None] | None = None
        self._startup_future: concurrent.futures.Future[Any] | None = None
        self._startup_error: BaseException | None = None
        self._scheduler_attached = False
        self._scheduler_closing = False
        self._pending_calls = 0
        self._pending_background = 0
        self._pending_control = 0
        self._detach_pending: dict[Callable[[str], None], set[str]] = {}
        self._detach_pending_count = 0
        self._detach_job_queued = False
        self._detach_scan = False
        self._owner_stream_id: str | None = None
        options: dict[str, Any] = {}
        if clock is not None:
            options["clock"] = clock
        if monotonic is not None:
            options["monotonic"] = monotonic
        self._service = RecorderService(
            bus=self._bus, streams=self._views, store=store, scheduler=scheduler, **options)

    @property
    def service(self) -> RecorderService:
        """The service, exposed for tests and for the read-only routes' status calls."""
        return self._service

    @property
    def bus(self) -> ThreadSafeBus:
        """The adapter the service publishes through."""
        return self._bus

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    @property
    def closed(self) -> bool:
        return self._closed

    # -- lifecycle ---------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Create the worker and sweeper and start reconciliation without waiting for it."""
        with self._lock:
            if self._closed:
                raise RecorderUnavailable("the recorder host is closed")
            if self._started:
                return
            self._started = True
            self._loop = asyncio.get_running_loop()
            self._bus.set_loop(self._loop)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recorder")
            executor = self._executor
            loop = self._loop
            scheduler = self._scheduler
        if scheduler is not None and hasattr(scheduler, "attach"):
            try:
                # Contract: attach is synchronous, non-blocking and records the loop/host only.
                scheduler.attach(loop, self)
            except BaseException:
                with self._lock:
                    self._started = False
                raise
            with self._lock:
                self._scheduler_attached = True
        # FIFO: requests that arrive before startup finishes queue behind it and see reconciled state.
        try:
            future = executor.submit(self._service.startup)
        except RuntimeError as exc:
            with self._lock:
                self._started = False
            raise RecorderUnavailable("the recorder executor is shut down") from exc
        with self._lock:
            self._startup_future = future
        future.add_done_callback(self._startup_done)
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="recorder-sweeper")

    async def close(self) -> None:
        """Drain the worker and stop the sweeper; cancellation of a caller does not cancel the drain.

        The shutdown is one internal task. Every `close()` caller awaits it through `asyncio.shield`, so cancelling
        one caller leaves the drain running and other callers waiting for real completion. `_close_event` is set by
        that task only after the executor has drained.
        """
        with self._lock:
            if self._shutdown_task is None:
                self._closed = True
                self._started = False
                self._close_event = asyncio.Event()
                self._shutdown_task = asyncio.create_task(self._shutdown(), name="recorder-shutdown")
            task = self._shutdown_task
        await asyncio.shield(task)

    async def _shutdown(self) -> None:
        try:
            scheduler = self._scheduler
            if scheduler is not None and self._scheduler_attached and hasattr(scheduler, "close"):
                with self._lock:
                    self._scheduler_closing = True
                token = _SCHEDULER_CLOSING.set(True)
                try:
                    closed = scheduler.close()
                    if inspect.isawaitable(closed):
                        await closed
                except Exception:
                    log.warning("recorder scheduler close failed", exc_info=True)
                finally:
                    _SCHEDULER_CLOSING.reset(token)
                    with self._lock:
                        self._scheduler_closing = False
            with self._lock:
                self._scheduler_attached = False
            sweeper, self._sweeper = self._sweeper, None
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            executor, self._executor = self._executor, None
            if executor is not None:
                # `shutdown(wait=True)` returns after the queued commit/publish has finished; the loop stays free.
                await asyncio.to_thread(executor.shutdown, wait=True)
            sweep_task, self._sweep_task = self._sweep_task, None
            if sweep_task is not None:
                await asyncio.gather(sweep_task, return_exceptions=True)
            with self._lock:
                pending_detach = self._detach_pending_count
                pending_scan = self._detach_scan
            if pending_detach or pending_scan:
                log.warning("recorder shutdown left %d detach event(s) and scan=%s unqueued",
                            pending_detach, pending_scan)
        finally:
            with self._lock:
                self._detach_pending.clear()
                self._detach_pending_count = 0
                self._detach_job_queued = False
                self._detach_scan = False
            self._bus.set_loop(None)
            if self._close_event is not None:
                self._close_event.set()

    # -- worker calls ------------------------------------------------------------------------------------------

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one service operation on the recorder worker and await its result.

        Bounded admission: a full worker queue raises `RecorderUnavailable` with
        `code="recorder_backpressure"` and `retry_after` instead of growing unboundedly. Shielded: cancelling the
        waiter does not cancel a queued commit. A client retry gets the same answer from the service's own
        idempotency.
        """
        with self._lock:
            if self._closed:
                raise RecorderUnavailable("the recorder host is closed")
            if not self._started or self._executor is None:
                raise RecorderUnavailable("the recorder host is not started")
            self._raise_startup_failure_locked()
            if self._pending_calls >= self._max_pending_calls:
                raise RecorderUnavailable(
                    "the recorder is busy; retry shortly",
                    code="recorder_backpressure",
                    retry_after=BACKPRESSURE_RETRY_AFTER,
                )
            self._pending_calls += 1
            executor = self._executor
            owner_stream = self._owner_stream_from_call(fn, args, kwargs)
        try:
            future = executor.submit(self._guarded, fn, args, kwargs, owner_stream)
        except RuntimeError as exc:
            self._call_done(None)
            raise RecorderUnavailable("the recorder host is closed") from exc
        future.add_done_callback(self._call_done)
        future.add_done_callback(self._retrieve_future_exception)
        return await self._await_shielded(future)

    @staticmethod
    async def _await_shielded(future: concurrent.futures.Future[Any]) -> Any:
        """Await worker work shielded, retrieving an inner exception even if the caller is cancelled."""
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(RecorderHost._retrieve_asyncio_exception)
        return await asyncio.shield(wrapped)

    def _background_allowed_during_close_locked(self) -> bool:
        """Only the scheduler close path may use the background lane after external admission closes.

        Direct calls from the shutdown task and tasks created inside its context are allowed; other
        loop tasks (including client requests) are refused. After scheduler.close returns the flag is
        cleared, so inherited contexts cannot keep submitting work.
        """
        if not self._scheduler_closing:
            return False
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if current is self._shutdown_task:
            return True
        return _SCHEDULER_CLOSING.get()

    async def call_background(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one scheduler/transcription job on the recorder worker (separately bounded lane).

        This lane has its own admission counter, so normal HTTP calls cannot fill it and a busy background
        pump cannot consume normal-call admission. Overflow raises `RecorderUnavailable` with
        `code="recorder_background_full"`; the scheduler retries on its next wake. Work is shielded from
        caller cancellation and still runs behind the same single worker, so it stays on the recorder
        thread and serializes with chunk commits.
        """
        with self._lock:
            closing = self._closed and self._background_allowed_during_close_locked()
            if self._closed and not closing:
                raise RecorderUnavailable("the recorder host is closed")
            if self._executor is None or (not self._started and not closing):
                raise RecorderUnavailable("the recorder host is not started")
            if not self._closed:
                self._raise_startup_failure_locked()
            if self._pending_background >= self._max_pending_background:
                raise RecorderUnavailable(
                    "the recorder background lane is busy; retry shortly",
                    code="recorder_background_full",
                    retry_after=BACKPRESSURE_RETRY_AFTER,
                )
            self._pending_background += 1
            executor = self._executor
        try:
            future = executor.submit(self._guarded, fn, args, kwargs, None)
        except RuntimeError as exc:
            self._background_done(None)
            raise RecorderUnavailable("the recorder host is closed") from exc
        future.add_done_callback(self._background_done)
        future.add_done_callback(self._retrieve_future_exception)
        return await self._await_shielded(future)

    async def enumerate_recordings(self, after: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Run the service's bounded cursor enumeration on the recorder worker/background lane."""
        return await self.call_background(self._service.enumerate_recordings, after, limit)

    async def _call_control(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a control job (the sweeper) without consuming a normal-call admission slot."""
        with self._lock:
            if self._closed:
                raise RecorderUnavailable("the recorder host is closed")
            if not self._started or self._executor is None:
                raise RecorderUnavailable("the recorder host is not started")
            executor = self._executor
        try:
            future = executor.submit(self._guarded, fn, args, kwargs, None)
        except RuntimeError as exc:
            raise RecorderUnavailable("the recorder host is closed") from exc
        future.add_done_callback(self._retrieve_future_exception)
        return await self._await_shielded(future)

    def submit_nowait(self, fn: Callable[..., Any], *args: Any) -> bool:
        """Queue a bounded fire-and-forget job; never blocks and never raises."""
        with self._lock:
            if self._closed or not self._started or self._executor is None:
                return False
            if self._startup_failure_locked() is not None:
                log.warning("recorder startup failed; background job not queued")
                return False
            if self._pending_control >= self._max_pending_control:
                log.warning("recorder control queue is full; background job not queued")
                return False
            self._pending_control += 1
            executor = self._executor
        try:
            future = executor.submit(self._guarded, fn, args, {}, None)
        except RuntimeError:
            self._control_done(None)
            return False
        future.add_done_callback(self._control_done)
        self._log_failure("background", future)
        return True

    def submit_detach(self, listener: Callable[[str], None], stream_id: str) -> bool:
        """Queue a detach notification, bypassing normal-call admission.

        Detach events coalesce by `(listener, stream_id)` and drain through one reserved control job. If the
        bounded per-stream reserve fills, overflow is coalesced into a scan of the tracked active owner: the owner's
        detach cannot be lost, and unrelated overflow is logged rather than silently disappearing. Normal-call
        saturation cannot block this path.
        """
        if not isinstance(stream_id, str):
            return False
        queue_job = False
        with self._lock:
            if self._closed or not self._started or self._executor is None:
                return False
            if self._startup_failure_locked() is not None:
                log.warning("recorder startup failed; detach not queued")
                return False
            pending = self._detach_pending.get(listener)
            if pending is not None and stream_id in pending:
                return True
            if self._detach_pending_count >= self._max_pending_detach:
                # The per-stream reserve is full, but one recording is active: coalesce overflow into a scan that
                # detaches the tracked owner if its stream is gone. This keeps the active owner's detach safe.
                self._detach_scan = True
                if not self._detach_job_queued:
                    self._detach_job_queued = True
                    queue_job = True
                executor = self._executor
                log.warning("recorder detach reserve is full; coalescing into the active-owner scan")
                if not queue_job:
                    return True
                try:
                    future = executor.submit(self._drain_detach)
                except RuntimeError:
                    with self._lock:
                        self._detach_job_queued = False
                    return False
                self._log_failure("detach drain", future)
                return True
            if pending is None:
                pending = set()
                self._detach_pending[listener] = pending
            pending.add(stream_id)
            self._detach_pending_count += 1
            if not self._detach_job_queued:
                self._detach_job_queued = True
                queue_job = True
            executor = self._executor
        if not queue_job:
            return True
        try:
            future = executor.submit(self._drain_detach)
        except RuntimeError:
            with self._lock:
                self._detach_job_queued = False
            return False
        self._log_failure("detach drain", future)
        return True

    def _drain_detach(self) -> None:
        """Run on the recorder worker: deliver coalesced detach notifications, then any arrived meanwhile."""
        while True:
            with self._lock:
                if self._startup_failure_locked() is not None:
                    dropped = self._detach_pending_count
                    self._detach_pending.clear()
                    self._detach_pending_count = 0
                    self._detach_job_queued = False
                    self._detach_scan = False
                    if dropped:
                        log.warning("recorder startup failed; %d detach event(s) discarded", dropped)
                    return
                scan, self._detach_scan = self._detach_scan, False
                if not self._detach_pending and not scan:
                    self._detach_job_queued = False
                    return
                batch, self._detach_pending = self._detach_pending, {}
                self._detach_pending_count = 0
            if scan:
                self._detach_disconnected_owner()
            for listener, stream_ids in batch.items():
                for stream_id in stream_ids:
                    try:
                        listener(stream_id)
                    except Exception as exc:  # noqa: BLE001 - one bad listener must not strand the rest
                        log.warning("recorder detach listener failed (%s)", type(exc).__name__)

    def _detach_disconnected_owner(self) -> None:
        """The overflow fallback: mark the tracked active owner detached when its stream is gone.

        Detach notifications for other streams can be coalesced away by the bounded reserve, but there is only one
        active recording. This runs on the recorder worker before the batch, so the owner cannot lose its grace
        period because unrelated detaches filled the reserve. The service re-checks that the stream is still the
        active owner's, so a stale tracked id is a harmless no-op.
        """
        with self._lock:
            stream_id = self._owner_stream_id
        if stream_id is None:
            log.warning("recorder detach overflow has no tracked active-owner stream to reconcile")
            return
        try:
            connected = self._views.has_stream(stream_id)
        except Exception as exc:  # noqa: BLE001 - a registry read failure must not break the drain
            log.warning("recorder owner detach scan could not read the stream registry (%s)",
                        type(exc).__name__)
            return
        if connected:
            return
        try:
            self._service.on_stream_detached(stream_id)
        except Exception as exc:  # noqa: BLE001 - the service itself refuses or fails; log without stream id
            log.warning("recorder owner detach scan failed (%s)", type(exc).__name__)

    def _owner_stream_from_call(self, fn: Callable[..., Any], args: tuple[Any, ...],
                                kwargs: dict[str, Any]) -> str | None:
        """The stream a successful `create`/`rebind` binds, so overflow can reconcile the active owner."""
        if getattr(fn, "__self__", None) is not self._service:
            return None
        name = getattr(fn, "__name__", "")
        if name == "create":
            stream_id = args[0] if args else kwargs.get("stream_id")
        elif name == "rebind":
            stream_id = args[2] if len(args) >= 3 else kwargs.get("stream_id")
        else:
            return None
        return stream_id if isinstance(stream_id, str) else None

    def _guarded(self, fn: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any],
                 owner_stream: str | None) -> Any:
        """Run a queued job unless reconciliation failed before it could start."""
        with self._lock:
            self._raise_startup_failure_locked()
        result = fn(*args, **kwargs)
        if owner_stream is not None:
            with self._lock:
                self._owner_stream_id = owner_stream
        return result

    def _call_done(self, _: concurrent.futures.Future[Any] | None) -> None:
        with self._lock:
            self._pending_calls = max(0, self._pending_calls - 1)

    def _background_done(self, _: concurrent.futures.Future[Any] | None) -> None:
        with self._lock:
            self._pending_background = max(0, self._pending_background - 1)

    def _control_done(self, _: concurrent.futures.Future[Any] | None) -> None:
        with self._lock:
            self._pending_control = max(0, self._pending_control - 1)

    # -- sweeper -----------------------------------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            if self._closed:
                return
            previous = self._sweep_task
            if previous is not None and not previous.done():
                continue  # a slow disk must not queue sweeps
            self._sweep_task = asyncio.create_task(self._sweep_once())

    async def _sweep_once(self) -> None:
        try:
            await self._call_control(self._service.sweep)
        except RecorderUnavailable:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed sweep retries on the next tick; it must not kill the loop task
            log.warning("recorder sweep failed (%s)", type(exc).__name__)

    # -- startup -----------------------------------------------------------------------------------------------

    def _startup_done(self, future: concurrent.futures.Future[Any]) -> None:
        try:
            error: BaseException | None = future.exception()
        except BaseException as exc:  # noqa: BLE001 - a cancelled startup future is still a failed startup
            error = exc
        if error is None:
            return
        with self._lock:
            self._startup_error = error
        log.warning("recorder startup failed (%s)", type(error).__name__)

    def _startup_failure_locked(self) -> BaseException | None:
        """The startup failure, if reconciliation failed. Holds `_lock` so every later call fails closed."""
        if self._startup_error is not None:
            return self._startup_error
        future = self._startup_future
        if future is not None and future.done():
            try:
                error: BaseException | None = future.exception()
            except BaseException as exc:  # noqa: BLE001 - a cancelled startup future is still a failed startup
                error = exc
            if error is not None:
                self._startup_error = error
        return self._startup_error

    def _raise_startup_failure_locked(self) -> None:
        if self._startup_failure_locked() is not None:
            raise RecorderUnavailable(
                "recorder startup failed; the recorder is unavailable", code="recorder_startup_failed")

    # -- logging -----------------------------------------------------------------------------------------------

    @staticmethod
    def _retrieve_future_exception(future: concurrent.futures.Future[Any]) -> None:
        """Mark an abandoned shielded future's exception as retrieved.

        A caller may be cancelled while the worker future continues. Without this callback the concurrent
        future can report "exception was never retrieved" even though nobody is waiting for it.
        """
        try:
            future.exception()
        except concurrent.futures.CancelledError:
            return

    @staticmethod
    def _retrieve_asyncio_exception(future: asyncio.Future[Any]) -> None:
        """Retrieve an inner shielded future's exception even when its waiter was cancelled."""
        if not future.cancelled():
            future.exception()

    @staticmethod
    def _log_failure(kind: str, future: concurrent.futures.Future[Any]) -> None:
        """Log only the failure kind: job arguments may carry stream ids, and service errors may carry ids."""
        def done(completed: concurrent.futures.Future[Any]) -> None:
            try:
                error = completed.exception()
            except BaseException:  # noqa: BLE001 - a cancelled or failed future has no exception to report
                return
            if error is not None:
                log.warning("recorder %s job failed (%s)", kind, type(error).__name__)

        future.add_done_callback(done)
