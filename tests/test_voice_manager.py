"""Offline coverage for the voice manager's lifecycle and bounded worker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.voice.audio import silence
from nexus.voice.manager import VoiceManager
from nexus.voice.model import VoiceError


class FakeStore:
    def __init__(self, *, missing=False):
        self.missing = missing
        self.calls = []
        self.removed = False

    def cached(self) -> bool:
        return not self.missing and not self.removed

    async def ensure(self, progress_cb, *, allow_download=True):
        self.calls.append(allow_download)
        if self.missing:
            raise FileNotFoundError("not cached")
        progress_cb(5, 10)
        return Path("/fake/model")

    async def remove(self):
        self.removed = True
        return None


class FakeEngine:
    def __init__(self):
        self.started = 0
        self.gate = None
        self.closed = False
        self.started_event = asyncio.Event()

    async def load(self):
        return None

    async def transcribe(self, audio):
        self.started += 1
        self.started_event.set()
        if self.gate and len(audio) != len(silence(0.5)):
            await self.gate.wait()
        return "hello"

    async def close(self):
        self.closed = True


def make_manager(*, engine=None, store=None, **overrides):
    config = SimpleNamespace(
        enabled=True,
        autoload=False,
        max_seconds=120,
        revision="rev1",
        device="cpu",
        unload_after_minutes=0,
        **overrides,
    )
    engine = engine or FakeEngine()
    manager = VoiceManager(
        config, engine_factory=lambda _path: engine, store=store or FakeStore()
    )
    return manager, engine


@pytest.mark.asyncio
async def test_prepare_lifecycle_and_retry_after_failure(monkeypatch):
    manager, engine = make_manager()
    assert manager.status().state == "absent"
    assert manager.schedule_prepare().state == "absent"
    await manager._prepare_task
    assert manager.status().state == "ready"
    assert manager.status().revision == "rev1"
    await manager.shutdown()
    assert engine.closed


@pytest.mark.asyncio
async def test_cached_status_survives_idle_unload_and_loads_without_download():
    store = FakeStore()
    manager, _ = make_manager(store=store)
    assert manager.status().cached
    await manager.prepare(allow_download=False)
    await manager._unload_after(0)
    assert manager.status().state == "absent"
    assert manager.status().cached
    await manager.prepare(allow_download=False)
    assert store.calls == [False, False]
    assert manager.status().state == "ready"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_missing_model_reports_not_cached():
    manager, _ = make_manager(store=FakeStore(missing=True))
    assert not manager.status().cached
    await manager.shutdown()


@pytest.mark.asyncio
async def test_transcribe_requires_ready_and_returns_timing():
    manager, _ = make_manager()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.2), "a")
    assert error.value.code == "voice_not_ready"
    await manager.prepare()
    result = await manager.transcribe(silence(0.2), "b")
    assert result.text == "hello"
    assert result.duration_s == pytest.approx(0.2)
    assert result.elapsed_s >= 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_queue_is_one_active_plus_two_waiting():
    engine = FakeEngine()
    engine.gate = asyncio.Event()
    manager, _ = make_manager(engine=engine)
    await manager.prepare()
    engine.started = 0
    engine.started_event.clear()
    requests = [
        asyncio.create_task(manager.transcribe(silence(0.2), str(i))) for i in range(3)
    ]
    await engine.started_event.wait()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.2), "overflow")
    assert error.value.code == "voice_busy"
    assert engine.started == 1
    engine.gate.set()
    await asyncio.gather(*requests)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cancel_does_not_free_worker_until_engine_stops():
    engine = FakeEngine()
    engine.gate = asyncio.Event()
    manager, _ = make_manager(engine=engine)
    await manager.prepare()
    engine.started = 0
    engine.started_event.clear()
    first = asyncio.create_task(manager.transcribe(silence(0.2), "first"))
    await engine.started_event.wait()
    assert await manager.cancel("first")
    await asyncio.sleep(0)
    # The first coroutine is still awaiting the shielded engine operation.
    assert manager.active
    second = asyncio.create_task(manager.transcribe(silence(0.2), "second"))
    third = asyncio.create_task(manager.transcribe(silence(0.2), "third"))
    await asyncio.sleep(0)
    assert manager._pending == 3
    engine.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.gather(second, third)
    await manager.shutdown()


@pytest.mark.asyncio
async def test_environment_can_disable_voice(monkeypatch):
    monkeypatch.setenv("NEXUS_VOICE", "off")
    manager, _ = make_manager()
    assert manager.status().state == "disabled"
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.2))
    assert error.value.code == "voice_unavailable"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_timeout_returns_while_worker_keeps_its_slot():
    engine = FakeEngine()
    engine.gate = asyncio.Event()
    manager, _ = make_manager(engine=engine)
    manager.request_timeout = 0.01
    await manager.prepare()
    engine.started = 0
    engine.started_event.clear()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.2), "slow")
    assert error.value.code == "voice_timeout"
    await engine.started_event.wait()
    assert manager._pending == 1
    manager.request_timeout = 10
    queued = [
        asyncio.create_task(manager.transcribe(silence(0.2), str(i))) for i in range(2)
    ]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    with pytest.raises(VoiceError) as busy:
        await manager.transcribe(silence(0.2), "too-many")
    assert busy.value.code == "voice_busy"
    assert manager._pending == 3
    engine.gate.set()
    await asyncio.gather(*queued)
    for _ in range(20):
        if manager._pending == 0:
            break
        await asyncio.sleep(0)
    assert manager._pending == 0
    await manager.shutdown()


@pytest.mark.asyncio
async def test_wav_is_validated_and_error_code_is_preserved():
    manager, _ = make_manager()
    await manager.prepare()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(b"bad wav", "bad")
    assert error.value.code == "voice_bad_audio"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_transcribe_never_downloads_as_an_implicit_prepare():
    store = FakeStore(missing=True)
    manager, _ = make_manager(store=store)

    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.2))
    assert error.value.code == "voice_not_ready"
    await manager._prepare_task

    assert store.calls == [False]
    assert manager.status().state == "absent"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_disabling_during_prepare_cannot_transition_to_ready():
    started = asyncio.Event()
    never_finish = asyncio.Event()

    class BlockingStore(FakeStore):
        async def ensure(self, progress_cb, *, allow_download=True):
            self.calls.append(allow_download)
            started.set()
            await never_finish.wait()
            return Path("/fake/model")

    engine = FakeEngine()
    store = BlockingStore()
    manager, _ = make_manager(engine=engine, store=store)
    manager.schedule_prepare(allow_download=False)
    await started.wait()
    manager.configure(SimpleNamespace(enabled=False, revision="rev2", device="cpu"))
    await asyncio.gather(manager._prepare_task, return_exceptions=True)

    assert manager.status().state == "disabled"
    assert not engine.started
    assert store.calls == [False]
    manager.configure(SimpleNamespace(enabled=True, revision="rev2", device="cpu"))
    assert manager.status().state == "absent"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_explicit_prepare_checks_engine_availability_before_download():
    store = FakeStore()

    def unavailable_engine(_path):
        raise AssertionError("unavailable engine must not be constructed")

    unavailable_engine.available = staticmethod(lambda: False)
    manager = VoiceManager(
        SimpleNamespace(
            enabled=True, revision="rev1", device="cpu", unload_after_minutes=0
        ),
        engine_factory=unavailable_engine,
        store=store,
    )

    state = await manager.prepare()

    assert state.state == "unsupported"
    assert store.calls == []
    await manager.shutdown()


@pytest.mark.asyncio
async def test_remove_cancels_prepare_before_removing_cached_model():
    started = asyncio.Event()

    class BlockingStore(FakeStore):
        async def ensure(self, progress_cb, *, allow_download=True):
            self.calls.append(allow_download)
            started.set()
            await asyncio.Event().wait()
            return Path("/fake/model")

    store = BlockingStore()
    manager, engine = make_manager(store=store)
    manager.schedule_prepare(allow_download=False)
    await started.wait()

    state = await manager.remove()

    assert state.state == "absent"
    assert store.removed
    assert not engine.started
    await manager.shutdown()


@pytest.mark.asyncio
async def test_duplicate_request_id_is_rejected_while_request_is_active():
    engine = FakeEngine()
    engine.gate = asyncio.Event()
    manager, _ = make_manager(engine=engine)
    await manager.prepare()
    engine.started_event.clear()
    first = asyncio.create_task(manager.transcribe(silence(0.2), "same-id"))
    await engine.started_event.wait()

    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.2), "same-id")
    assert error.value.code == "voice_busy"

    engine.gate.set()
    await first
    await manager.shutdown()


@pytest.mark.asyncio
async def test_partial_preview_never_queues_behind_running_inference():
    manager, engine = make_manager()
    await manager.prepare()
    preview = await manager.transcribe(silence(0.3), "p0", partial=True)
    assert preview.text == "hello"
    engine.gate = asyncio.Event()
    engine.started_event.clear()
    final = asyncio.create_task(manager.transcribe(silence(1.0), "final"))
    await engine.started_event.wait()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.3), "p1", partial=True)
    assert error.value.code == "voice_busy"
    engine.gate.set()
    assert (await final).text == "hello"
    await manager.shutdown()
