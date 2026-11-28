"""Deterministic scheduler integration tests (plan 54, C3c).

Every test uses synthetic PCM, fake STT inference and a scratch recorder store. No microphone, model
download or network is involved. The real RecorderHost/RecorderService/RecorderStore/TranscriptLog and the
real VoiceManager scheduling primitives are exercised together.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest

from nexus.recorder import RecorderInvalid, RecorderStore
from nexus.recorder import scheduler as scheduler_module
from nexus.recorder.host import RecorderHost
from nexus.recorder.scheduler import (
    MAX_OUTSTANDING_FINALS,
    MAX_RETRY_HEAP,
    RecorderScheduler,
    RetryRefused,
    SchedulerLimits,
)
from nexus.recorder.segmenter import SegmenterConfig
from nexus.recorder.transcript_log import TranscriptLog
from nexus.voice.audio import silence as wav_silence
from nexus.voice.manager import VoiceManager, WindowResult
from nexus.voice.model import VoiceError
from nexus.voice.store import MODEL_ID, ModelPaths

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2


# -- synthetic audio -------------------------------------------------------------------------------------------


def speech(samples: int, *, seed: int = 7) -> bytes:
    """Deterministic varying-energy PCM16 that the energy VAD reliably opens."""
    rng = random.Random(seed)
    out = bytearray()
    for index in range(samples):
        amplitude = 9_000 if (index // 320) % 2 == 0 else 4_000
        value = int(amplitude * math.sin(index * 0.17 + rng.random() * 0.1))
        out += int(value).to_bytes(2, "little", signed=True)
    return bytes(out)


def silence(samples: int) -> bytes:
    return b"\x00\x00" * samples


def utterance(samples: int = 9_600, *, seed: int = 7) -> bytes:
    return speech(samples, seed=seed)


# -- fakes -----------------------------------------------------------------------------------------------------


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.closed = False

    def publish(self, type_, data=None, *, session_id=None, turn_id=None, durable=True):
        with self.lock:
            event = {"type": type_, "data": data, "session_id": session_id,
                     "turn_id": turn_id, "durable": durable}
            self.events.append(event)
            return event

    def publish_threadsafe(self, loop, type_, data=None, *, session_id=None, turn_id=None,
                           durable=True):
        return self.publish(type_, data, session_id=session_id, turn_id=turn_id, durable=durable)

    def of(self, kind: str) -> list[dict]:
        with self.lock:
            return [event for event in self.events if event["type"] == kind]

    def after(self, index: int) -> list[dict]:
        with self.lock:
            return self.events[index:]


class FakeStreams:
    def __init__(self) -> None:
        self.connected = {"s1"}
        self.listeners: list[Callable[[str], None]] = []

    def has_stream(self, stream_id: str) -> bool:
        return stream_id in self.connected

    def view_for(self, stream_id: str) -> dict | None:
        return {"stream_id": stream_id, "client_id": "tui"} if stream_id in self.connected else None

    def on_detach(self, listener: Callable[[str], None]) -> None:
        self.listeners.append(listener)


class FakeModelStore:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.calls = 0
        self.sizes = {MODEL_ID: 0}

    def verified_paths(self) -> ModelPaths:
        self.calls += 1
        if self.missing:
            raise FileNotFoundError("model not cached")
        return ModelPaths(Path("/fake/photon"))

    def cache_sizes(self) -> dict[str, int]:
        return dict(self.sizes)


class FakeEngine:
    """Deterministic engine with optional gates/failures. All inference is bounded and observable."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.loads = 0
        self.active = 0
        self.max_active = 0
        self.mode = "ok"  # ok | fail | dead | hang
        self.fail_remaining = 0
        self.release: asyncio.Event | None = None
        self.started: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def load(self) -> None:
        self.loads += 1
        self.closed = False

    async def transcribe(self, audio: bytes) -> str:
        duration = round((len(audio) - 44) / BYTES_PER_SAMPLE / SAMPLE_RATE, 3)
        self.calls.append(duration)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await self.started.put(f"{duration:g}")
        try:
            if self.release is not None:
                await self.release.wait()
            if self.mode == "dead":
                raise RuntimeError("kestrel worker died")
            if self.mode == "hang":
                await asyncio.Event().wait()
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                raise RuntimeError("injected engine failure")
            return f"text {duration:g}"
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True


class FlakyVoice:
    """Delegating voice wrapper that fails only recorder finals for deterministic attempt tests."""

    def __init__(self, voice: VoiceManager, *, fail_finals: int = 0) -> None:
        self.voice = voice
        self.fail_finals = fail_finals

    def __getattr__(self, name: str):
        return getattr(self.voice, name)

    async def transcribe_window(self, pcm, *, kind, generation):
        if kind in {"final", "recorder_final"} and self.fail_finals > 0:
            self.fail_finals -= 1
            raise VoiceError("voice_failed", "injected final failure")
        return await self.voice.transcribe_window(pcm, kind=kind, generation=generation)


class StaleGenerationVoice:
    """Delegates but tags the first final result with the wrong generation."""

    def __init__(self, voice: VoiceManager) -> None:
        self.voice = voice
        self.poisoned = False

    def __getattr__(self, name: str):
        return getattr(self.voice, name)

    async def transcribe_window(self, pcm, *, kind, generation):
        result = await self.voice.transcribe_window(pcm, kind=kind, generation=generation)
        if kind in {"final", "recorder_final"} and not self.poisoned:
            self.poisoned = True
            return WindowResult(text="stale text", duration_s=result.duration_s,
                                elapsed_s=result.elapsed_s, generation=generation + 1,
                                kind=result.kind)
        return result


class ManualClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def small_config(window_s: float = 0.5) -> SegmenterConfig:
    return SegmenterConfig(
        frame_s=0.02,
        silence_s=0.1,
        window_s=window_s,
        overlap_s=0.1,
        preview_cadence_s=0.1,
        noise_window_s=0.2,
        calibration_s=0.1,
        min_rms=0.003,
        noise_ratio=1.8,
    )


class Harness:
    def __init__(self, root: Path, *, engine: FakeEngine | None = None,
                 model_missing: bool = False, max_recorder_finals: int = 8,
                 timeout: float | None = 2.0, limits: SchedulerLimits | None = None,
                 clock: Callable[[], float] | None = None, config: SegmenterConfig | None = None,
                 bus: FakeBus | None = None) -> None:
        self.root = root
        self.bus = bus or FakeBus()
        self.streams = FakeStreams()
        self.engine = engine or FakeEngine()
        self.model = FakeModelStore(missing=model_missing)
        self.voice = VoiceManager(
            engine_factory=lambda _paths: self.engine,
            store=self.model,
            idle_unload_seconds=0,
            max_recorder_finals=max_recorder_finals,
            recorder_execute_timeout_s=timeout,
        )
        self.clock = clock or time.monotonic
        self.store = RecorderStore(root / "recordings")
        self.scheduler = RecorderScheduler(
            self.voice,
            config=config or small_config(),
            clock=self.clock,
            limits=limits or SchedulerLimits(),
        )
        self.host = RecorderHost(self.bus, self.streams, store=self.store,
                                 scheduler=self.scheduler, sweep_interval=60.0)
        self.created: dict | None = None
        self.recovery = secrets.token_urlsafe(32)

    async def start(self) -> dict:
        await self.host.start()
        self.created = await self.host.call(self.host.service.create, "s1", "tui", "key-1",
                                           recovery=self.recovery)
        return self.created

    async def upload(self, data: bytes | list[bytes], *, start: int = 0,
                     sequence: int = 0) -> None:
        assert self.created is not None
        chunks = data if isinstance(data, list) else [data]
        bounded: list[bytes] = []
        for chunk in chunks:
            bounded.extend(chunk[at:at + 64_000] for at in range(0, len(chunk), 64_000))
        offset = start
        for index, chunk in enumerate(bounded):
            digest = hashlib.sha256(chunk).hexdigest()
            await self.host.call(
                self.host.service.put_chunk,
                self.created["id"], sequence + index, offset, chunk, digest,
                self.created["lease_token"],
            )
            offset += len(chunk) // BYTES_PER_SAMPLE

    async def stop(self, final: int | None = None) -> None:
        assert self.created is not None
        await self.host.call(self.host.service.stop, self.created["id"],
                             self.created["lease_token"], final)

    async def close(self) -> None:
        await self.host.close()
        await self.voice.close()

    async def wait(self, predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"condition did not settle; snapshot={self.scheduler.snapshot(self.created['id'])!r}")

    def snapshot(self) -> dict | None:
        assert self.created is not None
        return self.scheduler.snapshot(self.created["id"])

    def transcript_log(self) -> TranscriptLog:
        assert self.created is not None
        return TranscriptLog(lambda: self.store.transcript_dir(self.created["id"]))


async def wait_state(harness: Harness, state: str, *, timeout: float = 5.0) -> dict:
    await harness.wait(lambda: (harness.snapshot() or {}).get("state") == state, timeout=timeout)
    return harness.snapshot() or {}


def failing_log_class(failures: dict[str, int], *, error: str = "invalid",
                      commit_before_error: bool = False):
    """TranscriptLog subclass injecting bounded durable append failures by record kind.

    ``error="os"`` raises a raw ``OSError`` (the fsync/write path), which must never escape the scheduler.
    ``commit_before_error`` writes the batch first, modelling a successful write whose fsync then fails.
    """

    class FailingLog(TranscriptLog):
        remaining: ClassVar[dict[str, int]] = dict(failures)

        def append(self, records):
            batch = [records] if isinstance(records, dict) else list(records)
            kinds = {str(record.get("k")) for record in batch if isinstance(record, dict)}
            for kind in kinds:
                if self.remaining.get(kind, 0) > 0:
                    self.remaining[kind] -= 1
                    if commit_before_error:
                        super().append(records)
                    if error == "os":
                        raise OSError(28, "injected fsync failure")
                    raise RecorderInvalid("injected_append", "injected transcript append failure")
            return super().append(records)

    return FailingLog


def read_all(harness: Harness) -> list[dict]:
    log = harness.transcript_log()
    items: list[dict] = []
    after = -1
    while True:
        page = log.read_page(after, 200)
        if not page.segments:
            break
        items.extend(page.segments)
        if page.next_after is None or page.next_after <= after:
            break
        after = page.next_after
    return items


# -- tests -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_segment_is_durable_then_final_and_stop_returns_first(tmp_path):
    engine = FakeEngine()
    engine.release = asyncio.Event()
    harness = Harness(tmp_path, engine=engine)
    try:
        await harness.start()
        audio = utterance() + silence(3_200) + utterance(seed=8) + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)

        # Stop has already returned (upload/stop calls above). A closed segment is pending on disk.
        await harness.wait(lambda: (harness.snapshot() or {}).get("pending_finals", 0) > 0)
        pending = [item for item in read_all(harness) if item.get("status") == "pending"]
        assert pending, read_all(harness)

        engine.release.set()
        await wait_state(harness, "complete", timeout=8.0)
        items = read_all(harness)
        assert items and all(item["status"] in {"final", "skipped"} for item in items)
        revisions: dict[int, list[int]] = {}
        for item in items:
            revisions.setdefault(item["segment_id"], []).append(item["revision"])
        for values in revisions.values():
            assert values == sorted(values)

        # Provisional text never reached disk.
        raw = (harness.store.transcript_dir(harness.created["id"]) / "log.jsonl").read_text()
        assert '"provisional"' not in raw
        assert '"status":"final"' in raw

        # Durable transcript text is only live on the bus, never in a durable event.
        for event in harness.bus.events:
            if event["type"] == "recorder.segment":
                assert event["durable"] is False
            if event["durable"]:
                assert "text 0" not in json.dumps(event["data"])
        assert harness.bus.of("recorder.segment")

        # A valid stop retry re-signals capture_ended; it must not duplicate durable records.
        before = read_all(harness)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await asyncio.sleep(0.1)
        assert harness.snapshot()["state"] == "complete"
        assert read_all(harness) == before
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_feed_oserror_retries_without_new_closes(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "TranscriptLog",
                        failing_log_class({"seg": 1}, error="os"))
    harness = Harness(tmp_path)
    try:
        await harness.start()
        await harness.upload(utterance(9_600))
        # The first closed range hits a raw OSError. No new range or stop follows; the deferred record
        # must still be flushed by the retry path alone, without a leaked exception.
        await harness.wait(lambda: (harness.snapshot() or {}).get("pending_finals", 0) >= 1)
        assert read_all(harness)
        await harness.stop(9_600)
        await wait_state(harness, "complete", timeout=8.0)
        assert all(item["status"] in {"final", "skipped"} for item in read_all(harness))
        raw = (harness.store.transcript_dir(harness.created["id"]) / "log.jsonl").read_text()
        assert raw.index('"k":"seg"') < raw.index('"k":"end"')
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_committed_seg_batch_then_oserror_is_deduplicated(tmp_path, monkeypatch):
    # The batch lands durably, then the simulated fsync/OSError fires. Replay/reconcile must see the
    # committed segment records, drop them from the deferred retry, and neither duplicate nor lose one.
    monkeypatch.setattr(
        scheduler_module, "TranscriptLog",
        failing_log_class({"seg": 1}, error="os", commit_before_error=True),
    )
    harness = Harness(tmp_path)
    try:
        await harness.start()
        await harness.upload(utterance(9_600))
        await harness.stop(9_600)
        await wait_state(harness, "complete", timeout=8.0)

        items = read_all(harness)
        ids = [item["segment_id"] for item in items]
        assert ids == list(range(len(ids)))
        assert all(item["status"] in {"final", "skipped"} for item in items)
        raw = (harness.store.transcript_dir(harness.created["id"]) / "log.jsonl").read_text()
        segment_records = [
            json.loads(line) for line in raw.splitlines()
            if line.startswith("{") and '"k":"seg"' in line
        ]
        segment_ids = [int(record["segment_id"]) for record in segment_records]
        assert segment_ids == list(range(len(segment_ids)))
        assert len(segment_ids) == len(ids)
        assert harness.snapshot()["counts"]["pending"] == 0
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stop_finish_retries_after_seg_and_end_oserrors(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "TranscriptLog",
                        failing_log_class({"seg": 1, "end": 1}, error="os"))
    harness = Harness(tmp_path, config=small_config(window_s=2.0))
    try:
        await harness.start()
        # No trailing silence and a window longer than the utterance: the residual segment closes only
        # in finish(), exactly where a failed append used to strand the final range forever.
        await harness.upload(utterance(9_600))
        await harness.stop(9_600)
        await wait_state(harness, "complete", timeout=8.0)
        items = read_all(harness)
        assert items and all(item["status"] in {"final", "skipped"} for item in items)
        raw = (harness.store.transcript_dir(harness.created["id"]) / "log.jsonl").read_text()
        assert '"k":"end"' in raw
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_missed_service_notifications_reconcile_and_stop_retry_is_idempotent(tmp_path):
    limits = SchedulerLimits(reconcile_interval=0.05)
    harness = Harness(tmp_path, limits=limits)
    try:
        await harness.start()
        harness.scheduler.audio_committed = lambda *args: None  # drop hooks like a service failure
        harness.scheduler.capture_ended = lambda *args: None
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        # Explicit wake so the periodic reconciliation does not depend on wall time.
        harness.scheduler.wake()
        await wait_state(harness, "complete", timeout=8.0)

        # A valid stop retry re-signals capture_ended; the scheduler must stay idempotent.
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await asyncio.sleep(0.1)
        assert harness.snapshot()["state"] == "complete"
        assert read_all(harness)  # transcript still readable
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stale_generation_is_ignored(tmp_path):
    harness = Harness(tmp_path)
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        rid = harness.created["id"]
        await harness.wait(lambda: harness.snapshot() is not None)
        before = harness.snapshot()["durable_offset"]
        harness.scheduler.audio_committed(rid, 999, before + 99_999)
        harness.scheduler.capture_ended(rid, 999, before + 99_999, "stopped")
        await asyncio.sleep(0.05)
        assert harness.snapshot()["durable_offset"] == before
        assert harness.snapshot()["ended"] is False
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_stale_result_generation_is_dropped_and_retried(tmp_path):
    harness = Harness(tmp_path)
    harness.scheduler.voice = StaleGenerationVoice(harness.voice)
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await wait_state(harness, "complete", timeout=8.0)
        items = read_all(harness)
        assert items
        assert all("stale text" not in item.get("text", "") for item in items)
        assert all(item["status"] in {"final", "skipped"} for item in items)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_slow_engine_keeps_one_preview_and_capture_does_not_wait(tmp_path):
    engine = FakeEngine()
    engine.release = asyncio.Event()
    harness = Harness(tmp_path, engine=engine, config=small_config(window_s=2.0))
    try:
        await harness.start()
        await harness.upload(utterance(9_600))
        await harness.wait(lambda: (harness.snapshot() or {}).get("preview_in_flight") is True)
        await harness.wait(lambda: len(engine.calls) >= 1)
        calls = len(engine.calls)
        # More audio arrives while the preview is in flight; a second preview must not queue.
        await harness.upload(utterance(9_600, seed=3), start=9_600, sequence=1)
        await asyncio.sleep(0.1)
        assert harness.snapshot()["preview_in_flight"] is True
        assert len(engine.calls) == calls
        assert engine.max_active == 1
        # Capture progress is independent of the slow preview.
        assert harness.snapshot()["durable_offset"] >= 19_200

        await harness.stop(19_200)
        engine.release.set()
        await wait_state(harness, "complete", timeout=8.0)
        assert engine.max_active == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_unavailable_model_never_downloads_and_drains_after_recovery(tmp_path):
    download_calls: list[int] = []

    async def fail_download(_store):
        download_calls.append(1)
        raise AssertionError("the scheduler must never download")

    engine = FakeEngine()
    harness = Harness(tmp_path, engine=engine, model_missing=True,
                      limits=SchedulerLimits(probe_interval=0.05))
    harness.voice.download_runner = fail_download
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await harness.wait(lambda: (harness.snapshot() or {}).get("state") == "unavailable")
        assert engine.calls == []
        assert download_calls == []
        assert harness.snapshot()["reason"] == "model_not_cached"

        harness.model.missing = False
        harness.scheduler.wake()
        await wait_state(harness, "complete", timeout=8.0)
        assert engine.calls
        assert download_calls == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_busy_download_parks_without_inference_then_recovers(tmp_path):
    engine = FakeEngine()
    harness = Harness(tmp_path, engine=engine)
    release_download = threading.Event()

    def fake_downloader(_store):
        release_download.wait(3)
        return ModelPaths(Path("/fake/photon"))

    harness.voice.download_runner = fake_downloader
    try:
        await harness.start()
        await harness.voice.start_download(lambda _status: None)
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await harness.wait(lambda: (harness.snapshot() or {}).get("state") == "preparing")
        assert engine.calls == []
        release_download.set()
        await wait_state(harness, "complete", timeout=8.0)
    finally:
        release_download.set()
        await harness.close()


@pytest.mark.asyncio
async def test_engine_retry_backoff_and_terminal_failure_keep_paging(tmp_path):
    clock = ManualClock()
    engine = FakeEngine()
    harness = Harness(tmp_path, engine=engine, clock=clock,
                      limits=SchedulerLimits(backoff=(0.0, 0.0, 0.0)))
    harness.scheduler.voice = FlakyVoice(harness.voice, fail_finals=1)
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await harness.wait(lambda: (harness.snapshot() or {}).get("state") == "complete",
                           timeout=8.0)
        summary = harness.snapshot()
        assert summary["counts"]["final"] >= 1
        assert summary["counts"]["error"] == 0
        transcript, _ = harness.transcript_log().replay(None)
        history = transcript.history_documents()
        statuses = [item["status"] for item in history]
        attempts = [item["attempt"] for item in history]
        assert statuses[0] == "error"
        assert attempts[0] == 1
        assert statuses[-1] == "final"
        by_segment: dict[int, list[int]] = {}
        for item in history:
            by_segment.setdefault(item["segment_id"], []).append(item["revision"])
        for revisions in by_segment.values():
            assert revisions == sorted(revisions)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_dead_and_hung_engines_do_not_block_capture(tmp_path):
    for mode in ("dead", "hang"):
        engine = FakeEngine()
        engine.mode = mode
        harness = Harness(tmp_path / mode, engine=engine, timeout=0.05,
                          limits=SchedulerLimits(backoff=(0.0, 0.0, 0.0)))
        try:
            await harness.start()
            audio = utterance() + silence(3_200)
            await harness.upload(audio)
            await harness.stop(len(audio) // BYTES_PER_SAMPLE)
            await harness.wait(lambda h=harness: (h.snapshot() or {}).get("state") == "failed",
                               timeout=10.0)
            assert harness.snapshot()["counts"]["error"] >= 1
            # Audio independence: durable audio and WAV export are unaffected.
            wav = harness.store.export_wav(harness.created["id"])
            assert wav.read_bytes()[:4] == b"RIFF"
            assert harness.store.get(harness.created["id"]).durable_offset > 0
        finally:
            await harness.close()


@pytest.mark.asyncio
async def test_backpressure_does_not_consume_an_attempt(tmp_path):
    engine = FakeEngine()
    engine.release = asyncio.Event()
    harness = Harness(tmp_path, engine=engine, max_recorder_finals=1)
    try:
        await harness.start()
        # Fill the single recorder-final slot with an unrelated recorder job.
        blocker = asyncio.create_task(
            harness.voice.transcribe_window(wav_silence(0.5), kind="final", generation=0))
        await asyncio.wait_for(engine.started.get(), 2)
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await harness.wait(lambda: (harness.snapshot() or {}).get("pending_finals", 0) > 0)
        await asyncio.sleep(0.2)
        assert harness.snapshot()["counts"]["error"] == 0
        assert (harness.snapshot() or {}).get("retry_heap", 0) <= MAX_RETRY_HEAP
        engine.release.set()
        await blocker
        await wait_state(harness, "complete", timeout=10.0)
        assert harness.snapshot()["counts"]["error"] == 0
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_delete_during_final_never_recreates_or_publishes(tmp_path):
    engine = FakeEngine()
    engine.release = asyncio.Event()
    harness = Harness(tmp_path, engine=engine)
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await harness.wait(lambda: (harness.snapshot() or {}).get("outstanding_finals", 0) > 0)
        rid = harness.created["id"]
        deleted = await harness.host.call(harness.host.service.delete, rid)
        assert deleted is True
        marker = len(harness.bus.events)
        engine.release.set()
        await asyncio.sleep(0.3)
        assert not (harness.store.root / rid).exists()
        assert harness.scheduler.snapshot(rid) is None
        later = harness.bus.after(marker)
        assert not [event for event in later if event["type"] == "recorder.segment"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_restart_resumes_from_log_without_duplicates(tmp_path):
    audio = utterance(8_000) + silence(3_200) + utterance(8_000, seed=9) + silence(3_200)
    samples = len(audio) // BYTES_PER_SAMPLE

    # Reference: uninterrupted.
    reference = Harness(tmp_path / "reference")
    try:
        await reference.start()
        await reference.upload(audio)
        await reference.stop(samples)
        await wait_state(reference, "complete", timeout=8.0)
        expected = [(item["segment_id"], item["status"], item["text"])
                    for item in read_all(reference)]
    finally:
        await reference.close()

    # Crash run: interrupt before the finals settle, then restart on the same root without a checkpoint.
    engine = FakeEngine()
    engine.release = asyncio.Event()
    first = Harness(tmp_path / "restart", engine=engine)
    try:
        await first.start()
        await first.upload(audio)
        await first.stop(samples)
        await first.wait(lambda: (first.snapshot() or {}).get("pending_finals", 0) > 0)
        await first.host.close()
        # Simulate a crash without an orderly checkpoint.
        checkpoint = first.store.transcript_dir(first.created["id"]) / "checkpoint.json"
        if checkpoint.exists():
            checkpoint.unlink()
    finally:
        await first.voice.close()

    second_engine = FakeEngine()
    second = Harness(tmp_path / "restart", engine=second_engine)
    second.created = first.created
    try:
        await second.host.start()
        await wait_state(second, "complete", timeout=10.0)
        actual = [(item["segment_id"], item["status"], item["text"]) for item in read_all(second)]
        assert actual == expected
        ids = [item["segment_id"] for item in read_all(second)]
        assert ids == list(range(len(ids)))
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_recovery_upload_after_completion_reopens_segmentation(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "TranscriptLog",
                        failing_log_class({"resume": 1}, error="os"))
    harness = Harness(tmp_path)
    try:
        await harness.start()
        head = utterance(8_000)
        tail = silence(3_200)
        total = (len(head) + len(tail)) // BYTES_PER_SAMPLE
        await harness.upload(head)
        # Stop declares the full offset even though the tail has not arrived yet.
        await harness.stop(total)
        await wait_state(harness, "complete", timeout=8.0)

        digest = hashlib.sha256(tail).hexdigest()
        await harness.host.call(
            harness.host.service.recover_upload,
            harness.created["id"], 1, 8_000, tail, digest,
            harness.recovery, "tui",
        )
        log_path = harness.store.transcript_dir(harness.created["id"]) / "log.jsonl"
        await harness.wait(
            lambda: '"k":"resume"' in log_path.read_text(), timeout=8.0)
        await wait_state(harness, "complete", timeout=8.0)
        raw = log_path.read_text()
        assert '"k":"resume"' in raw
        assert all(item["status"] in {"final", "skipped"} for item in read_all(harness))
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_restart_uses_the_sealed_checkpoint(tmp_path):
    audio = utterance(8_000) + silence(3_200) + utterance(8_000, seed=5) + silence(3_200)
    samples = len(audio) // BYTES_PER_SAMPLE
    engine = FakeEngine()
    engine.release = asyncio.Event()

    first = Harness(tmp_path / "checkpoint", engine=engine)
    try:
        await first.start()
        await first.upload(audio)
        await first.stop(samples)
        await first.wait(lambda: (first.snapshot() or {}).get("pending_finals", 0) > 0)
        checkpoint = first.store.transcript_dir(first.created["id"]) / "checkpoint.json"
        assert not checkpoint.exists()
        await first.host.close()
    finally:
        await first.voice.close()
    # host.close() -> scheduler.close() must checkpoint through the close-context background lane.
    assert checkpoint.exists()

    second = Harness(tmp_path / "checkpoint", engine=FakeEngine())
    second.created = first.created
    try:
        await second.host.start()
        await wait_state(second, "complete", timeout=10.0)
        ids = [item["segment_id"] for item in read_all(second)]
        assert ids == list(range(len(ids)))
        assert all(item["status"] in {"final", "skipped"} for item in read_all(second))
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_late_results_after_close_are_ignored(tmp_path):
    engine = FakeEngine()
    engine.release = asyncio.Event()
    harness = Harness(tmp_path, engine=engine)
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await harness.wait(lambda: (harness.snapshot() or {}).get("outstanding_finals", 0) > 0)
        rid = harness.created["id"]
        log_path = harness.store.transcript_dir(rid) / "log.jsonl"
        await harness.host.close()
        before = log_path.read_bytes()
        marker = len(harness.bus.events)
        engine.release.set()
        await asyncio.sleep(0.2)
        assert not log_path.exists() or log_path.read_bytes() == before
        assert not [event for event in harness.bus.after(marker)
                    if event["type"] == "recorder.segment"]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_more_than_fifty_stopped_recordings_resume_via_enumeration(tmp_path):
    audio = utterance(4_800) + silence(3_200)
    samples = len(audio) // BYTES_PER_SAMPLE
    gated = FakeEngine()
    gated.release = asyncio.Event()  # finals stay pending; each recording has durable seg records
    first = Harness(tmp_path / "enumeration", engine=gated)
    ids: list[str] = []
    try:
        await first.start()
        for index in range(55):
            if index:
                first.created = await first.host.call(
                    first.host.service.create, "s1", "tui", f"key-{index + 1}",
                    recovery=first.recovery)
            await first.upload(audio)
            await first.stop(samples)
            ids.append(first.created["id"])
        assert len(ids) == 55
        await first.host.close()
    finally:
        await first.voice.close()

    second = Harness(tmp_path / "enumeration", engine=FakeEngine())
    try:
        await second.host.start()
        # The enumeration is the only path that discovers these stopped recordings: service.list sees at
        # most the newest 50 and startup reconcile skips sealed/stopped recordings.
        discovered: list[str] = []
        after = None
        while True:
            page = await second.host.enumerate_recordings(after=after, limit=50)
            discovered.extend(item["id"] for item in page["recordings"] if "error" not in item)
            after = page.get("next_after")
            if after is None:
                break
        assert set(ids) <= set(discovered)

        def all_done() -> bool:
            for rid in ids:
                segments = TranscriptLog(
                    lambda rid=rid: second.store.transcript_dir(rid)).read_page(-1, 200).segments
                if not segments or any(item.get("status") not in {"final", "skipped"}
                                       for item in segments):
                    return False
            return True

        await second.wait(all_done, timeout=30.0)
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_signal_hints_stay_bounded_across_history(tmp_path):
    harness = Harness(tmp_path, limits=SchedulerLimits(max_signals=64, max_queued=16))
    try:
        await harness.start()
        for index in range(500):
            harness.scheduler.audio_committed(f"{index:032x}", 1, 100)
        await harness.wait(lambda: len(harness.scheduler._signals) <= 64, timeout=5.0)
        assert len(harness.scheduler._signals) <= 64
        assert len(harness.scheduler._queue) <= harness.scheduler.limits.max_queued
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_cancelled_signal_history_stays_bounded(tmp_path):
    harness = Harness(tmp_path, limits=SchedulerLimits(
        max_signals=64, max_queued=16, max_cancelled=32))
    try:
        await harness.start()
        for index in range(500):
            rid = f"{index:032x}"
            harness.scheduler.audio_committed(rid, 1, 100)
            harness.scheduler.cancel(rid)
        await harness.wait(lambda: len(harness.scheduler._signals) <= 64, timeout=5.0)
        assert len(harness.scheduler._signals) <= 64
        assert len(harness.scheduler._tokens) <= 64
        assert len(harness.scheduler._cancelled) <= 32
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_explicit_retry_creates_a_new_revision(tmp_path):
    engine = FakeEngine()
    harness = Harness(tmp_path, engine=engine)
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await wait_state(harness, "complete", timeout=8.0)
        before = read_all(harness)
        result = await harness.scheduler.retry(harness.created["id"], "all")
        assert result["accepted"] >= 1
        await harness.wait(lambda: (harness.snapshot() or {}).get("retry_pass") is False, timeout=8.0)
        after = read_all(harness)
        old = {item["segment_id"]: item["revision"] for item in before}
        for item in after:
            if item["segment_id"] in old:
                assert item["revision"] >= old[item["segment_id"]]
        assert any(item["revision"] > old.get(item["segment_id"], 0) for item in after)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_explicit_empty_retry_scope_accepts_zero_and_does_not_retry_failed(tmp_path):
    engine = FakeEngine()
    engine.mode = "dead"
    harness = Harness(tmp_path, engine=engine,
                      limits=SchedulerLimits(backoff=(0.0, 0.0, 0.0)))
    try:
        await harness.start()
        audio = utterance() + silence(3_200)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await wait_state(harness, "failed", timeout=10.0)
        before = read_all(harness)
        failed_ids = [item["segment_id"] for item in before if item["status"] == "error"]
        assert failed_ids

        # [] is an explicit selection of nothing, not a request to retry every failed segment.
        result = await harness.scheduler.retry(harness.created["id"], [])
        assert result["accepted"] == 0
        assert harness.snapshot()["retry_pass"] is False
        await asyncio.sleep(0.1)
        assert read_all(harness) == before

        # A non-empty explicit list still works.
        selected = await harness.scheduler.retry(harness.created["id"], [failed_ids[0]])
        assert selected["accepted"] == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_retry_refused_while_capture_is_active(tmp_path):
    harness = Harness(tmp_path)
    try:
        await harness.start()
        with pytest.raises(RetryRefused) as refused:
            await harness.scheduler.retry(harness.created["id"], "all")
        assert refused.value.code == "retry_while_active"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_active_set_and_snapshot_are_bounded(tmp_path):
    engine = FakeEngine()
    harness = Harness(tmp_path, engine=engine)
    try:
        await harness.start()
        # About two minutes of alternating speech/silence, uploaded in bounded chunks.
        chunks: list[bytes] = []
        for index in range(24):
            chunks.append(utterance(4_800, seed=index) + silence(3_200))
        audio = b"".join(chunks)
        await harness.upload(audio)
        await harness.stop(len(audio) // BYTES_PER_SAMPLE)
        await wait_state(harness, "complete", timeout=20.0)
        snapshot = harness.snapshot()
        assert snapshot["active_segments"] <= 64
        assert snapshot["retry_heap"] <= MAX_RETRY_HEAP
        assert snapshot["outstanding_finals"] <= MAX_OUTSTANDING_FINALS
        assert snapshot["paged_through"] >= 0
        # The snapshot is privacy-safe: no transcript text.
        assert "text" not in json.dumps(snapshot)
    finally:
        await harness.close()
