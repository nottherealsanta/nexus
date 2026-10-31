"""Offline lifecycle and serialization coverage using a fake engine.

Adapted from old Nexus tests/test_voice_manager.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nexus.voice.audio import silence
from nexus.voice.manager import VoiceManager
from nexus.voice.model import VoiceError
from nexus.voice.store import BARK_MODEL_ID, EXPECTED_SIZES, MODEL_ID, ModelPaths


class FakeStore:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.sizes = {MODEL_ID: 0}

    def verified_paths(self) -> ModelPaths:
        if self.missing:
            raise FileNotFoundError("not cached")
        return ModelPaths(Path("/fake/photon"))

    def cache_sizes(self) -> dict[str, int]:
        return dict(self.sizes)


class FakeEngine:
    def __init__(self) -> None:
        self.loads = 0
        self.started = 0
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.gate: asyncio.Event | None = None
        self.started_event = asyncio.Event()

    async def load(self) -> None:
        self.loads += 1

    async def transcribe(self, audio: bytes) -> str:
        self.started += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started_event.set()
        try:
            if self.gate is not None and len(audio) != len(silence(0.5)):
                await self.gate.wait()
            return "hello"
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True


def make_manager(*, engine: FakeEngine | None = None, store: FakeStore | None = None, **kwargs):
    engine = engine or FakeEngine()
    kwargs.setdefault("idle_unload_seconds", 0)
    manager = VoiceManager(
        engine_factory=lambda _paths: engine,
        store=store or FakeStore(),
        **kwargs,
    )
    return manager, engine


@pytest.mark.asyncio
async def test_model_is_lazy_then_transcribes_with_fake_engine() -> None:
    manager, engine = make_manager()
    assert not manager.loaded and engine.loads == 0
    result = await manager.transcribe(silence(0.5))
    assert result.text == "hello"
    assert result.duration_s == 0.5 and result.elapsed_s >= 0
    assert engine.loads == 1 and manager.loaded
    await manager.close()
    assert engine.closed


@pytest.mark.asyncio
async def test_missing_model_never_downloads_implicitly() -> None:
    manager, engine = make_manager(store=FakeStore(missing=True))
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.5))
    assert error.value.code == "voice_model_not_cached"
    assert engine.loads == 0
    await manager.close()


@pytest.mark.asyncio
async def test_partial_preview_returns_busy_instead_of_waiting() -> None:
    engine = FakeEngine()
    engine.gate = asyncio.Event()
    manager, _ = make_manager(engine=engine)
    first = asyncio.create_task(manager.transcribe(silence(1.0)))
    await engine.started_event.wait()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.3), partial=True)
    assert error.value.code == "voice_busy"
    engine.gate.set()
    assert (await first).text == "hello"
    assert engine.max_active == 1
    await manager.close()


@pytest.mark.asyncio
async def test_normal_requests_are_serialized_and_bounded() -> None:
    engine = FakeEngine()
    engine.gate = asyncio.Event()
    manager, _ = make_manager(engine=engine)
    tasks = [asyncio.create_task(manager.transcribe(silence(1.0))) for _ in range(3)]
    await engine.started_event.wait()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(1.0))
    assert error.value.code == "voice_busy"
    engine.gate.set()
    assert [r.text for r in await asyncio.gather(*tasks)] == ["hello"] * 3
    assert engine.max_active == 1
    await manager.close()


@pytest.mark.asyncio
async def test_audio_is_validated_before_model_load() -> None:
    manager, engine = make_manager()
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(b"not a wav")
    assert error.value.code == "voice_bad_audio"
    assert engine.loads == 0
    await manager.close()


@pytest.mark.asyncio
async def test_runtime_missing_is_reported_without_constructing_engine() -> None:
    def unavailable(_paths):
        raise AssertionError("engine must not be constructed")

    unavailable.available = staticmethod(lambda: False)
    manager = VoiceManager(engine_factory=unavailable, store=FakeStore())
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.5))
    assert error.value.code == "voice_dependency_missing"
    await manager.close()


@pytest.mark.asyncio
async def test_idle_engine_is_unloaded() -> None:
    manager, engine = make_manager(idle_unload_seconds=0.01)
    await manager.transcribe(silence(0.5))
    await asyncio.sleep(0.03)
    assert not manager.loaded and engine.closed
    await manager.close()


@pytest.mark.asyncio
async def test_idle_unload_reloads_a_fresh_worker() -> None:
    engines: list[FakeEngine] = []

    def factory(_paths):
        engines.append(FakeEngine())
        return engines[-1]

    manager = VoiceManager(engine_factory=factory, store=FakeStore(), idle_unload_seconds=0.01)
    await manager.transcribe(silence(0.5))
    await asyncio.sleep(0.03)
    assert len(engines) == 1 and engines[0].closed
    await manager.transcribe(silence(0.5))
    assert len(engines) == 2 and not engines[1].closed
    await manager.close()


@pytest.mark.asyncio
async def test_corrupt_snapshot_is_reported_without_loading() -> None:
    class CorruptStore(FakeStore):
        def verified_paths(self) -> ModelPaths:
            raise ValueError("voice snapshot hash mismatch for model.safetensors")

    manager, engine = make_manager(store=CorruptStore())
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.5))
    assert error.value.code == "voice_model_invalid"
    assert engine.loads == 0
    assert manager.error is not None and "hash mismatch" in manager.error
    await manager.close()


@pytest.mark.asyncio
async def test_consent_download_publishes_progress_and_does_not_load_engine() -> None:
    store = FakeStore()
    engine = FakeEngine()

    def fake_download(target):
        target.sizes = dict(EXPECTED_SIZES)
        return ModelPaths(Path("/fake/photon"))

    manager, _ = make_manager(store=store, engine=engine, download_runner=fake_download)
    events: list[dict] = []
    initial = await manager.start_download(events.append)
    assert initial["state"] == "downloading"
    assert manager.download_status["bytes_total"] > 0
    assert manager._download_task is not None
    await manager._download_task
    assert manager.download_status["state"] == "ready"
    assert events[0]["state"] == "downloading" and events[-1]["state"] == "ready"
    assert not manager.loaded and engine.loads == 0
    await manager.close()


@pytest.mark.asyncio
async def test_custom_model_is_not_loaded_or_downloaded() -> None:
    manager, engine = make_manager(repo_getter="org/custom")
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.5))
    assert error.value.code == "voice_model_unsupported"
    with pytest.raises(VoiceError) as download_error:
        await manager.start_download(lambda _event: None)
    assert download_error.value.code == "voice_model_unsupported"
    assert engine.loads == 0
    await manager.close()


@pytest.mark.asyncio
async def test_cancelled_transcription_clears_the_engine_and_reloads() -> None:
    class BlockingEngine(FakeEngine):
        async def transcribe(self, audio: bytes) -> str:
            self.started_event.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    engines: list[FakeEngine] = []

    def factory(_paths):
        engine = BlockingEngine() if not engines else FakeEngine()
        engines.append(engine)
        return engine

    manager = VoiceManager(engine_factory=factory, store=FakeStore(), idle_unload_seconds=0)
    task = asyncio.create_task(manager.transcribe(silence(0.5)))
    for _ in range(100):
        if engines:
            break
        await asyncio.sleep(0.01)
    assert engines
    await engines[0].started_event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._engine is None and engines[0].closed
    result = await manager.transcribe(silence(0.5))
    assert result.text == "hello" and len(engines) == 2
    await manager.close()


@pytest.mark.asyncio
async def test_bark_config_gets_an_explicit_migration_message() -> None:
    manager, engine = make_manager(repo_getter=BARK_MODEL_ID)
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.5))
    assert error.value.code == "voice_model_unsupported"
    assert "no longer supported" in str(error.value)
    assert "moondream/parakeet-redux" in str(error.value)
    assert "config.toml" in str(error.value)
    assert "left in place" in str(error.value)
    with pytest.raises(VoiceError) as download_error:
        await manager.start_download(lambda _event: None)
    assert download_error.value.code == "voice_model_unsupported"
    assert engine.loads == 0
    await manager.close()


@pytest.mark.asyncio
async def test_engine_failure_is_reported_and_released() -> None:
    class FailingEngine(FakeEngine):
        async def transcribe(self, audio: bytes) -> str:
            raise RuntimeError("kestrel failed")

    engine = FailingEngine()
    manager, _ = make_manager(engine=engine)
    with pytest.raises(VoiceError) as error:
        await manager.transcribe(silence(0.5))
    assert error.value.code == "voice_failed"
    assert manager.error is not None and "kestrel failed" in manager.error
    assert not manager.loaded and engine.closed
    await manager.close()
