"""Deterministic fair-scheduling coverage for recorder windows (plan 54, C1).

The fake engine gates every inference, so the tests decide exactly when the
single in-flight window finishes. No microphone, model or real time is needed.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from nexus.voice.audio import parse_wav, silence
from nexus.voice.manager import RECORDER_BACKLOG_FULL, VOICE_TIMEOUT, VoiceManager
from nexus.voice.model import VoiceError
from nexus.voice.store import MODEL_ID, ModelPaths


class FakeStore:
    def __init__(self) -> None:
        self.sizes = {MODEL_ID: 0}

    def verified_paths(self) -> ModelPaths:
        return ModelPaths(Path("/fake/photon"))

    def cache_sizes(self) -> dict[str, int]:
        return dict(self.sizes)


class ProbedStore(FakeStore):
    def __init__(self, error: BaseException | None = None) -> None:
        super().__init__()
        self.error = error
        self.calls = 0

    def verified_paths(self) -> ModelPaths:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return super().verified_paths()


class GatedEngine:
    """One inference at a time; each call waits for its own release event."""

    def __init__(self) -> None:
        self.started: asyncio.Queue[tuple[float, asyncio.Event]] = asyncio.Queue()
        self.calls: list[float] = []
        self.loads = 0
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.fail_next = False

    async def load(self) -> None:
        self.loads += 1
        self.closed = False

    async def transcribe(self, audio: bytes) -> str:
        duration = round(parse_wav(audio).duration_s, 3)
        self.calls.append(duration)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        release = asyncio.Event()
        await self.started.put((duration, release))
        try:
            await release.wait()
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("kestrel failed")
            return f"text {duration:g}"
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True


def make_manager(engine: GatedEngine, **kwargs) -> VoiceManager:
    return VoiceManager(
        engine_factory=lambda _paths: engine,
        store=FakeStore(),
        idle_unload_seconds=0,
        **kwargs,
    )


async def next_started(engine: GatedEngine) -> tuple[float, asyncio.Event]:
    return await asyncio.wait_for(engine.started.get(), 1)


async def start_window(engine: GatedEngine, expected: float) -> asyncio.Event:
    duration, release = await next_started(engine)
    assert duration == expected
    return release


async def queued(manager: VoiceManager, count: int) -> None:
    for _ in range(1000):
        if len(manager._waiters) == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"waiters did not reach {count}: {manager._waiters!r}")


async def wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not settle")


async def finish_window(engine: GatedEngine, expected: float) -> float:
    duration, release = await next_started(engine)
    assert duration == expected
    release.set()
    return duration


@pytest.mark.asyncio
async def test_interactive_waits_for_at_most_one_recorder_window() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)

    final = asyncio.create_task(manager.transcribe_window(silence(4.0), kind="final", generation=1))
    preview = asyncio.create_task(manager.transcribe_window(silence(2.0), kind="preview", generation=1))
    await queued(manager, 2)
    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    await queued(manager, 3)

    # Release the one recorder window that was already in flight; interactive is next.
    release3.set()
    await finish_window(engine, 0.5)
    assert (await interactive).text == "text 0.5"

    # Previews are reserved a turn between finals, but never ahead of interactive work.
    await finish_window(engine, 2.0)
    await finish_window(engine, 4.0)
    await asyncio.gather(running, final, preview)

    assert engine.calls == [3.0, 0.5, 2.0, 4.0]
    assert engine.max_active == 1
    await manager.close()


@pytest.mark.asyncio
async def test_recorder_preview_is_replaced_not_queued() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)

    first = asyncio.create_task(manager.transcribe_window(silence(1.0), kind="preview", generation=1))
    await queued(manager, 1)
    second = asyncio.create_task(manager.transcribe_window(silence(2.0), kind="preview", generation=2))
    await wait_until(lambda: len(manager._waiters) == 1 and manager._waiters[0].generation == 2)

    with pytest.raises(VoiceError) as superseded:
        await first
    assert superseded.value.code == "voice_superseded"

    release3.set()
    await finish_window(engine, 2.0)
    result = await second
    assert result.text == "text 2" and result.generation == 2 and result.kind == "preview"
    await running

    assert engine.calls == [3.0, 2.0]
    await manager.close()


@pytest.mark.asyncio
async def test_recorder_finals_progress_under_constant_previews() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)

    final1 = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    preview1 = asyncio.create_task(manager.transcribe_window(silence(1.0), kind="preview", generation=1))
    await queued(manager, 1)

    release3.set()
    release1 = await start_window(engine, 1.0)  # the reserved preview turn

    final2 = asyncio.create_task(manager.transcribe_window(silence(4.0), kind="final", generation=1))
    preview2 = asyncio.create_task(manager.transcribe_window(silence(2.0), kind="preview", generation=2))
    await queued(manager, 2)
    preview3 = asyncio.create_task(manager.transcribe_window(silence(2.5), kind="preview", generation=3))
    await wait_until(
        lambda: len(manager._waiters) == 2
        and any(job.generation == 3 for job in manager._waiters)
    )
    with pytest.raises(VoiceError) as superseded:
        await preview2
    assert superseded.value.code == "voice_superseded"

    # Constant preview arrivals cannot starve the newer final.
    release1.set()
    release4 = await start_window(engine, 4.0)
    release4.set()
    await finish_window(engine, 2.5)
    await asyncio.gather(final1, preview1, final2, preview3)

    assert engine.calls == [3.0, 1.0, 4.0, 2.5]
    await manager.close()


@pytest.mark.asyncio
async def test_recorder_work_does_not_consume_interactive_pending_slots() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    finals = [
        asyncio.create_task(manager.transcribe_window(silence(seconds), kind="final", generation=1))
        for seconds in (4.0, 5.0, 6.0)
    ]
    await queued(manager, 3)
    assert not manager.busy  # recorder work is not interactive pending work

    with pytest.raises(VoiceError) as busy:
        await manager.transcribe(silence(0.3), partial=True)
    assert busy.value.code == "voice_busy"

    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    await queued(manager, 4)
    assert manager.busy

    release3.set()
    await finish_window(engine, 0.5)
    assert (await interactive).text == "text 0.5"
    for seconds in (4.0, 5.0, 6.0):
        await finish_window(engine, seconds)
    await asyncio.gather(running, *finals)

    assert engine.max_active == 1
    await manager.close()


@pytest.mark.asyncio
async def test_byte_backed_final_backlog_is_bounded_and_interactive_still_wins() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, max_recorder_finals=2)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    queued_final = asyncio.create_task(
        manager.transcribe_window(silence(4.0), kind="final", generation=1)
    )
    await queued(manager, 1)
    assert manager.recorder_final_backlog == 2

    with pytest.raises(VoiceError) as full:
        await manager.transcribe_window(silence(5.0), kind="final", generation=1)
    assert full.value.code == RECORDER_BACKLOG_FULL
    assert manager.recorder_final_backlog == 2

    # The recorder bound never blocks interactive work, which still takes priority.
    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    await wait_until(lambda: len(manager._waiters) == 2)
    release3.set()
    await finish_window(engine, 0.5)
    assert (await interactive).text == "text 0.5"
    await running

    # With the queued final now in flight, completion frees a slot for another final.
    release4 = await start_window(engine, 4.0)
    assert manager.recorder_final_backlog == 1
    recovered = asyncio.create_task(
        manager.transcribe_window(silence(5.0), kind="final", generation=2)
    )
    await queued(manager, 1)
    assert manager.recorder_final_backlog == 2
    release4.set()
    assert (await queued_final).text == "text 4"
    await finish_window(engine, 5.0)
    assert (await recovered).text == "text 5"
    await manager.close()


@pytest.mark.asyncio
async def test_cancelling_a_queued_final_frees_backlog_capacity() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, max_recorder_finals=2)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    queued_final = asyncio.create_task(
        manager.transcribe_window(silence(4.0), kind="final", generation=1)
    )
    await queued(manager, 1)
    with pytest.raises(VoiceError) as full:
        await manager.transcribe_window(silence(5.0), kind="final", generation=1)
    assert full.value.code == RECORDER_BACKLOG_FULL

    queued_final.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued_final
    await queued(manager, 0)
    assert manager.recorder_final_backlog == 1

    retry = asyncio.create_task(manager.transcribe_window(silence(5.0), kind="final", generation=2))
    await queued(manager, 1)
    assert manager.recorder_final_backlog == 2
    release3.set()
    await running
    await finish_window(engine, 5.0)
    assert (await retry).text == "text 5"
    assert manager.recorder_final_backlog == 0
    await manager.close()


@pytest.mark.asyncio
async def test_lazy_final_backlog_bounds_loaders_without_loading_rejected_jobs() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, max_recorder_finals=2)
    loaded: list[float] = []

    def load(seconds: float) -> bytes:
        loaded.append(seconds)
        return silence(seconds)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    lazy = asyncio.create_task(manager.transcribe_window(lambda: load(4.0), kind="final", generation=2))
    await queued(manager, 1)
    assert loaded == [] and manager.recorder_final_backlog == 2

    with pytest.raises(VoiceError) as full:
        await manager.transcribe_window(lambda: load(5.0), kind="final", generation=3)
    assert full.value.code == RECORDER_BACKLOG_FULL
    assert loaded == []  # rejection happens before a lazy job can materialise audio

    release3.set()
    await running
    release4 = await start_window(engine, 4.0)
    assert loaded == [4.0]
    release4.set()
    assert (await lazy).duration_s == 4.0
    assert manager.recorder_final_backlog == 0

    recovered = asyncio.create_task(manager.transcribe_window(lambda: load(6.0), kind="final", generation=4))
    release6 = await start_window(engine, 6.0)
    assert loaded == [4.0, 6.0]
    release6.set()
    assert (await recovered).duration_s == 6.0
    await manager.close()


@pytest.mark.asyncio
async def test_queued_recorder_final_is_loaded_only_when_it_runs() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)
    loaded: list[str] = []

    def load_final() -> bytes:
        loaded.append("loaded")
        return silence(2.0)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    lazy = asyncio.create_task(manager.transcribe_window(load_final, kind="final", generation=2))
    await queued(manager, 1)
    assert loaded == []

    release3.set()
    await finish_window(engine, 2.0)
    result = await lazy
    assert loaded == ["loaded"] and result.duration_s == 2.0
    await running
    await manager.close()


@pytest.mark.asyncio
async def test_engine_error_does_not_wedge_queued_work() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)
    engine.fail_next = True

    failing = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    waiting = asyncio.create_task(manager.transcribe_window(silence(4.0), kind="final", generation=1))
    await queued(manager, 1)

    release3.set()
    with pytest.raises(VoiceError) as error:
        await failing
    assert error.value.code == "voice_failed"
    assert not manager.loaded and engine.closed

    await finish_window(engine, 4.0)
    assert (await waiting).text == "text 4"
    assert engine.loads == 2
    await manager.close()


@pytest.mark.asyncio
async def test_cancelling_a_queued_window_removes_it_cleanly() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    cancelled = asyncio.create_task(manager.transcribe_window(silence(4.0), kind="final", generation=1))
    await queued(manager, 1)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await queued(manager, 0)

    release3.set()
    await running
    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    await finish_window(engine, 0.5)
    assert (await interactive).text == "text 0.5"
    assert engine.calls == [3.0, 0.5]
    await manager.close()


@pytest.mark.asyncio
async def test_close_fails_queued_work_and_still_closes_the_engine() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)

    running = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    waiting = asyncio.create_task(manager.transcribe_window(silence(4.0), kind="final", generation=1))
    await queued(manager, 1)

    closing = asyncio.create_task(manager.close())
    with pytest.raises(VoiceError) as unavailable:
        await asyncio.wait_for(waiting, 1)
    assert unavailable.value.code == "voice_unavailable"
    assert not closing.done()

    release3.set()
    assert (await running).text == "text 3"
    await asyncio.wait_for(closing, 1)
    assert engine.closed
    with pytest.raises(VoiceError) as closed:
        await manager.transcribe_window(silence(1.0), kind="preview", generation=2)
    assert closed.value.code == "voice_unavailable"


@pytest.mark.asyncio
async def test_readiness_is_non_loading_offline_and_does_not_download() -> None:
    engine = GatedEngine()
    constructed: list[ModelPaths] = []
    downloads: list[str] = []
    store = ProbedStore()

    def factory(paths: ModelPaths) -> GatedEngine:
        constructed.append(paths)
        return engine

    manager = VoiceManager(
        engine_factory=factory,
        store=store,
        download_runner=lambda _store: downloads.append("download") or ModelPaths(Path("/fake")),
        idle_unload_seconds=0,
    )
    assert await manager.readiness() is None
    assert store.calls == 1
    assert constructed == [] and downloads == [] and not manager.loaded
    await manager.close()


@pytest.mark.asyncio
async def test_readiness_reports_missing_runtime_without_touching_cache() -> None:
    def unavailable(_paths: ModelPaths) -> GatedEngine:
        raise AssertionError("engine must not be constructed")

    unavailable.available = staticmethod(lambda: False)
    store = ProbedStore()
    manager = VoiceManager(engine_factory=unavailable, store=store, idle_unload_seconds=0)
    assert await manager.readiness() == "voice_dependency_missing"
    assert store.calls == 0 and not manager.loaded
    await manager.close()


@pytest.mark.asyncio
async def test_readiness_maps_cache_and_model_failures_without_loading() -> None:
    for error, expected in (
        (FileNotFoundError("not cached"), "voice_model_not_cached"),
        (ValueError("hash mismatch"), "voice_model_invalid"),
    ):
        store = ProbedStore(error)
        manager = VoiceManager(
            engine_factory=lambda _paths: pytest.fail("engine must not be constructed"),
            store=store,
            idle_unload_seconds=0,
        )
        assert await manager.readiness() == expected
        assert store.calls == 1 and not manager.loaded
        await manager.close()

    store = ProbedStore()
    manager = VoiceManager(
        repo_getter="org/custom",
        engine_factory=lambda _paths: pytest.fail("engine must not be constructed"),
        store=store,
        idle_unload_seconds=0,
    )
    assert await manager.readiness() == "voice_model_unsupported"
    assert store.calls == 0 and not manager.loaded
    await manager.close()
    assert await manager.readiness() == "voice_unavailable"


@pytest.mark.asyncio
async def test_readiness_uses_an_already_loaded_engine_without_reverifying() -> None:
    engine = GatedEngine()
    store = ProbedStore()
    manager = VoiceManager(
        engine_factory=lambda _paths: engine,
        store=store,
        idle_unload_seconds=0,
    )
    await manager.prepare()
    assert manager.loaded and store.calls == 1
    store.error = FileNotFoundError("model files vanished")
    assert await manager.readiness() is None
    assert store.calls == 1
    await manager.close()


@pytest.mark.asyncio
async def test_recorder_execution_timeout_closes_the_shared_engine_and_recovers() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, recorder_execute_timeout_s=0.02)

    timed_out = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    await start_window(engine, 3.0)
    with pytest.raises(VoiceError) as error:
        await asyncio.wait_for(timed_out, 1)
    assert error.value.code == VOICE_TIMEOUT
    assert not manager.loaded and engine.closed

    # The shared worker restarts for the next caller; it is not poisoned by the timeout.
    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    release = await start_window(engine, 0.5)
    release.set()
    assert (await interactive).text == "text 0.5"
    assert engine.loads == 2 and manager.loaded
    await manager.close()


@pytest.mark.asyncio
async def test_recorder_execution_timeout_does_not_bound_interactive_calls() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, recorder_execute_timeout_s=0.02)

    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    release = await start_window(engine, 0.5)
    await asyncio.sleep(0.05)
    assert not interactive.done()
    release.set()
    assert (await interactive).text == "text 0.5"
    await manager.close()


@pytest.mark.asyncio
async def test_recorder_timeout_can_be_disabled() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, recorder_execute_timeout_s=None)

    window = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release = await start_window(engine, 3.0)
    await asyncio.sleep(0.05)
    assert not window.done()
    release.set()
    assert (await window).text == "text 3"
    await manager.close()


@pytest.mark.asyncio
async def test_cancelling_an_active_recorder_job_preserves_the_cancellation_contract() -> None:
    engine = GatedEngine()
    manager = make_manager(engine, recorder_execute_timeout_s=0.02)

    window = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    await start_window(engine, 3.0)
    window.cancel()
    with pytest.raises(asyncio.CancelledError):
        await window
    assert not manager.loaded and engine.closed
    await manager.close()


async def wait_for_thread_event(event: threading.Event) -> None:
    for _ in range(1000):
        if event.is_set():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("thread event did not settle")


@pytest.mark.asyncio
async def test_sync_lazy_loader_runs_off_loop_and_keeps_heartbeats_responsive() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)
    loader_started = threading.Event()
    release_loader = threading.Event()
    loader_threads: list[int] = []

    def load() -> bytes:
        loader_threads.append(threading.get_ident())
        loader_started.set()
        release_loader.wait(2)
        return silence(2.0)

    window = asyncio.create_task(manager.transcribe_window(load, kind="final", generation=1))
    await wait_for_thread_event(loader_started)
    assert loader_threads and loader_threads[0] != threading.get_ident()

    heartbeat = 0

    async def ticker() -> None:
        nonlocal heartbeat
        while True:
            heartbeat += 1
            await asyncio.sleep(0.005)

    tick = asyncio.create_task(ticker())
    before = heartbeat
    await asyncio.sleep(0.03)
    assert heartbeat > before  # the loop kept running while the loader blocked its thread
    tick.cancel()
    await asyncio.gather(tick, return_exceptions=True)

    release_loader.set()
    release = await start_window(engine, 2.0)
    release.set()
    assert (await window).text == "text 2"
    assert manager.loaded
    await manager.close()


@pytest.mark.asyncio
async def test_cancelling_inside_a_sync_loader_discards_it_without_closing_the_worker() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)
    await manager.prepare()
    loader_started = threading.Event()
    release_loader = threading.Event()
    loader_completed = threading.Event()

    def load() -> bytes:
        loader_started.set()
        release_loader.wait(2)
        loader_completed.set()
        return silence(2.0)

    window = asyncio.create_task(manager.transcribe_window(load, kind="final", generation=1))
    await wait_for_thread_event(loader_started)
    window.cancel()
    with pytest.raises(asyncio.CancelledError):
        await window
    assert manager.loaded and not engine.closed  # cancellation stopped waiting, not the model

    release_loader.set()
    await wait_for_thread_event(loader_completed)

    interactive = asyncio.create_task(manager.transcribe(silence(0.5)))
    release = await start_window(engine, 0.5)
    release.set()
    assert (await interactive).text == "text 0.5"
    assert engine.calls == [0.5]  # the abandoned loader never reached inference
    await manager.close()


@pytest.mark.asyncio
async def test_async_lazy_loader_stays_on_loop_and_only_the_selected_job_runs() -> None:
    engine = GatedEngine()
    manager = make_manager(engine)
    loop_thread = threading.get_ident()
    calls: list[int] = []

    async def load() -> bytes:
        calls.append(threading.get_ident())
        return silence(2.0)

    blocker = asyncio.create_task(manager.transcribe_window(silence(3.0), kind="final", generation=1))
    release3 = await start_window(engine, 3.0)
    lazy = asyncio.create_task(manager.transcribe_window(load, kind="final", generation=2))
    await queued(manager, 1)
    assert calls == []

    release3.set()
    await blocker
    release2 = await start_window(engine, 2.0)
    assert calls == [loop_thread]
    release2.set()
    assert (await lazy).text == "text 2"
    await manager.close()
