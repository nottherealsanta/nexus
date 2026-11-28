"""Deterministic coverage for the simulated-live windowed STT workload (A1).

No microphone, recording, model download or ML runtime is involved: a fake
engine records the bounded WAV windows it receives and declares its latency.
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
from array import array

import pytest

from nexus.voice.audio import parse_wav
from nexus.voice.live_sim import (
    LiveSimConfig,
    _window_wav,
    simulate,
    simulate_wav,
)

RATE = 16_000
MIN_SAMPLES = round(0.2 * RATE)


class FakeEngine:
    """Real inference calls are replaced by declared, deterministic latency."""

    def __init__(self, latency_seconds: float = 0.05) -> None:
        self.latency_seconds = latency_seconds
        self.windows: list[tuple[int, int]] = []
        self.texts: list[str] = []

    async def transcribe(self, wav: bytes) -> str:
        info = parse_wav(wav)
        self.windows.append((info.frames, len(wav)))
        text = f"window {len(self.windows)}"
        self.texts.append(text)
        return text


def tone(seconds: float, *, amplitude: int = 12_000, frequency: float = 220.0) -> bytes:
    frames = round(seconds * RATE)
    samples = array(
        "h",
        (int(amplitude * math.sin(2 * math.pi * frequency * index / RATE)) for index in range(frames)),
    )
    return samples.tobytes()


def quiet(seconds: float) -> bytes:
    return bytes(round(seconds * RATE) * 2)


def speech_like(seconds: float, *, amplitude: int = 12_000) -> bytes:
    """Syllabic 100 ms tone bursts with 50 ms gaps; gaps are below VAD close."""

    period = tone(0.10, amplitude=amplitude) + quiet(0.05)
    frames = round(seconds * RATE)
    return (period * (frames // (len(period) // 2) + 1))[: frames * 2]


def wav(pcm: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", 1, 1, RATE, RATE * 2, 2, 16)
    payload = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    payload += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


async def test_cadence_previews_grow_the_active_segment() -> None:
    pcm = quiet(0.24) + tone(4.0) + quiet(1.0)
    engine = FakeEngine(latency_seconds=0.05)
    config = LiveSimConfig(window_s=10, cadence_s=1.0, overlap_s=0.75, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    previews = [job for job in result.preview_jobs if not job.replaced]
    assert [round(job.submit_s, 2) for job in previews] == [1.24, 2.24, 3.24]
    assert [round(job.audio_end_s, 2) for job in previews] == [1.24, 2.24, 3.24]
    assert all(round(job.audio_start_s, 2) == 0.24 for job in previews)
    assert [job.kind for job in result.final_jobs] == ["final"]
    assert round(result.final_jobs[0].audio_start_s, 2) == 0.24
    assert round(result.final_jobs[0].audio_end_s, 2) == 4.24
    assert round(result.final_jobs[0].submit_s, 2) == 4.74
    assert engine.windows and all(frames <= round(config.window_s * RATE) for frames, _ in engine.windows)
    payload = result.to_dict()
    assert payload["windows"]["previews_submitted"] == 3
    assert payload["windows"]["previews_replaced"] == 0
    assert payload["preview_latency_seconds"]["enqueue_to_text"]["p95"] == 0.05
    assert payload["final_latency_seconds"]["audio_end_to_text"]["p95"] == 0.55
    assert payload["inference_to_audio_ratio"] < 0.2
    assert payload["audio_per_inference_ratio"] > 5


async def test_energy_vad_closes_after_silence_and_keeps_silence_out_of_windows() -> None:
    pcm = tone(1.0) + quiet(0.7) + tone(1.0) + quiet(1.0)
    engine = FakeEngine()
    config = LiveSimConfig(window_s=10, cadence_s=0.5, overlap_s=0.5, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    # 0.7 s of silence is long enough to close the first utterance at 1.0 s.
    assert [(round(job.audio_start_s, 2), round(job.audio_end_s, 2)) for job in result.final_jobs] == [
        (0.0, 1.0),
        (1.7, 2.7),
    ]
    assert round(result.speech_seconds, 2) == 2.0
    assert result.final_jobs[0].submit_s == pytest.approx(1.5)
    assert result.final_jobs[1].submit_s == pytest.approx(3.2)


async def test_forced_boundaries_use_bounded_windows_with_overlap() -> None:
    pcm = speech_like(22.0)
    engine = FakeEngine(latency_seconds=0.1)
    config = LiveSimConfig(window_s=6, cadence_s=1.5, overlap_s=1.0, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    finals = result.final_jobs
    assert [round(job.audio_start_s, 2) for job in finals] == [0.0, 5.0, 10.0, 15.0, 20.0]
    assert [round(job.audio_end_s, 2) for job in finals] == [6.0, 11.0, 16.0, 21.0, 22.0]
    assert max(job.audio_end_s - job.audio_start_s for job in finals) <= config.window_s
    assert all(frames <= round(config.window_s * RATE) for frames, _ in engine.windows)
    assert every_window_is_valid(engine.windows)


def every_window_is_valid(windows: list[tuple[int, int]]) -> bool:
    return all(frames >= MIN_SAMPLES and size == 44 + frames * 2 for frames, size in windows)


async def test_slow_engine_grows_backlog_and_shows_overload() -> None:
    pcm = speech_like(20.0)
    engine = FakeEngine(latency_seconds=4.0)
    config = LiveSimConfig(
        window_s=2.0,
        cadence_s=0.5,
        overlap_s=0.25,
        silence_s=0.5,
        sample_interval_s=1.0,
    )

    result = await simulate(pcm, engine, config)
    samples = {round(sample.at_s): sample for sample in result.backlog}

    assert result.inference_to_audio_ratio > 1
    assert result.audio_per_inference_ratio is not None and result.audio_per_inference_ratio < 1
    assert result.previews_replaced > 0
    assert result.max_pending_final_jobs >= 3
    assert samples[15].pending_final_jobs > samples[5].pending_final_jobs
    assert result.max_finalization_lag_seconds > 1
    assert result.max_queued_audio_seconds >= config.window_s


async def test_fast_engine_keeps_up_without_growing_backlog() -> None:
    pcm = speech_like(20.0)
    engine = FakeEngine(latency_seconds=0.2)
    config = LiveSimConfig(window_s=10, cadence_s=1.0, overlap_s=0.75, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    assert result.inference_to_audio_ratio < 0.6
    assert result.max_pending_final_jobs <= 1
    assert result.max_queued_audio_seconds <= config.window_s
    assert result.previews_replaced == 0
    assert result.to_dict()["preview_latency_seconds"]["enqueue_to_text"]["p95"] == 0.2


@pytest.mark.parametrize("amplitude", [300, 12_000])
async def test_stationary_signal_is_not_speech(amplitude: int) -> None:
    # Exactly four 200 Hz cycles per 20 ms frame: every frame has the same RMS,
    # so the low and high percentile are equal (the case that used to fail).
    # A flat contour is treated as non-speech at any level; a perfectly steady
    # sustained vowel is therefore missed by this harness VAD.
    pcm = tone(2.0, amplitude=amplitude, frequency=200.0)
    engine = FakeEngine()
    config = LiveSimConfig(window_s=10, cadence_s=0.5, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    assert result.speech_seconds == 0
    assert result.final_jobs == ()
    assert result.preview_jobs == ()
    assert engine.windows == []


async def test_modulated_speech_is_detected() -> None:
    pcm = speech_like(2.0)
    engine = FakeEngine()
    config = LiveSimConfig(window_s=10, cadence_s=0.5, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    assert result.speech_seconds == pytest.approx(2.0)
    assert len(result.final_jobs) == 1
    assert len(result.preview_jobs) == 3
    assert engine.windows


async def test_steady_noise_does_not_hide_speech() -> None:
    noise = tone(3.0, amplitude=300, frequency=200.0)
    speech = speech_like(1.0)
    pcm = noise[: round(1.0 * RATE) * 2] + speech + noise[: round(1.0 * RATE) * 2]
    engine = FakeEngine()
    config = LiveSimConfig(window_s=10, cadence_s=0.5, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    assert result.speech_seconds == pytest.approx(1.0)
    assert len(result.final_jobs) == 1
    assert engine.windows


async def test_short_tail_is_skipped_without_shrinking_saved_audio() -> None:
    pcm = tone(0.15) + quiet(0.6)
    engine = FakeEngine()
    config = LiveSimConfig(window_s=10, cadence_s=0.5, silence_s=0.5)

    result = await simulate(pcm, engine, config)

    assert result.final_jobs == ()
    assert result.skipped_final_jobs == 1
    assert result.audio_seconds == pytest.approx(0.75)
    assert engine.windows == []


async def test_bounded_wav_helper_pads_only_short_inference_copies() -> None:
    pcm = tone(0.05)
    padded = _window_wav(pcm, 0, round(0.05 * RATE), min_samples=MIN_SAMPLES)
    info = parse_wav(padded)
    assert info.frames == MIN_SAMPLES


async def test_simulate_wav_validates_the_supplied_file() -> None:
    engine = FakeEngine()
    result = await simulate_wav(wav(speech_like(0.3)), engine, LiveSimConfig(cadence_s=0.2, silence_s=0.5))
    assert result.audio_seconds == pytest.approx(0.3)
    with pytest.raises(ValueError):
        await simulate_wav(b"not a wav", engine)


def test_config_rejects_unbounded_or_inverted_values() -> None:
    with pytest.raises(ValueError):
        LiveSimConfig(window_s=0)
    with pytest.raises(ValueError):
        LiveSimConfig(window_s=5, cadence_s=6)
    with pytest.raises(ValueError):
        LiveSimConfig(window_s=5, overlap_s=5)


async def test_sampling_callback_and_simulated_time_are_deterministic() -> None:
    pcm = speech_like(4.0)
    config = LiveSimConfig(window_s=10, cadence_s=1.0, silence_s=0.5, sample_interval_s=0.5)
    seen: list[float] = []

    async def run_once() -> tuple[float, int]:
        result = await simulate(pcm, FakeEngine(latency_seconds=0.1), config, on_sample=lambda s: seen.append(s.at_s))
        return result.inference_seconds, len(result.backlog)

    first = await run_once()
    second = await run_once()

    assert first == second
    assert seen[:4] == [0.5, 1.0, 1.5, 2.0]
    assert all(round(value, 3) == value for value in seen)


def test_declared_latency_rejects_invalid_values() -> None:
    async def check(value: object) -> None:
        engine = FakeEngine()
        engine.latency_seconds = value  # type: ignore[assignment]
        with pytest.raises(ValueError):
            await simulate(speech_like(1.0), engine, LiveSimConfig(cadence_s=0.5))

    asyncio.run(check(float("nan")))
    asyncio.run(check(-1))


def test_benchmark_cli_keeps_the_one_shot_form_and_accepts_both_live_forms(tmp_path) -> None:
    from nexus.voice import benchmark

    with pytest.raises(SystemExit) as missing_positional:
        benchmark.main([])
    assert missing_positional.value.code == 2
    with pytest.raises(SystemExit) as missing_live_value:
        benchmark.main(["--live-sim"])
    assert missing_live_value.value.code == 2

    missing = tmp_path / "missing.wav"
    assert benchmark.main([str(missing)]) == 2
    assert benchmark.main(["--live-sim", str(missing)]) == 2
    assert benchmark.main([str(missing), "--live-sim"]) == 2


def test_benchmark_live_sim_cli_uses_bounded_windows_without_a_model(monkeypatch, tmp_path, capsys) -> None:
    from nexus.voice import benchmark
    from nexus.voice.store import ModelPaths

    wav_path = tmp_path / "speech.wav"
    wav_path.write_bytes(wav(speech_like(2.0)))

    class FakeWorker:
        device = "cpu"
        latency_seconds = 0.05

        def __init__(self) -> None:
            self.windows: list[tuple[int, int]] = []

        async def load(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def transcribe(self, data: bytes) -> str:
            info = parse_wav(data)
            self.windows.append((info.frames, len(data)))
            return "text"

    engines: list[FakeWorker] = []

    def factory(paths, *, device="auto"):
        del paths, device
        engine = FakeWorker()
        engines.append(engine)
        return engine

    monkeypatch.setattr(benchmark, "PhotonWorkerEngine", factory)
    monkeypatch.setattr(benchmark.ModelStore, "verified_paths", lambda self: ModelPaths(tmp_path))

    code = benchmark.main(
        ["--live-sim", str(wav_path), "--window", "1", "--cadence", "0.5", "--silence", "0.5"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "live-sim"
    assert payload["config"]["window_s"] == 1.0
    assert payload["windows"]["previews_submitted"] > 0
    assert payload["windows"]["finals_completed"] > 0
    assert payload["inference_to_audio_ratio"] < 0.6
    assert payload["preview_latency_seconds"]["enqueue_to_text"]["p95"] == 0.05
    assert payload["cold_load_seconds"] >= 0
    assert len(payload["rss_samples"]) == len(payload["backlog"])
    assert all(
        set(item) == {"at_s", "parent_rss_bytes", "worker_rss_bytes"}
        for item in payload["rss_samples"]
    )
    assert payload["worker_rss"]["available"] is False
    assert payload["worker_rss"]["unavailable_reason"]
    assert all(item["worker_rss_bytes"] is None for item in payload["rss_samples"])
    assert all(isinstance(item["parent_rss_bytes"], int) for item in payload["rss_samples"])
    assert len(engines) == 2
    assert all(frames <= RATE for frames, _ in engines[1].windows)


def test_benchmark_live_rss_samples_worker_when_available(monkeypatch, tmp_path) -> None:
    from nexus.voice import benchmark
    from nexus.voice.store import ModelPaths

    class FakeWorker:
        device = "cpu"
        latency_seconds = 0.05

        async def load(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def transcribe(self, data: bytes) -> str:
            parse_wav(data)
            return "text"

    monkeypatch.setattr(benchmark, "PhotonWorkerEngine", lambda paths, *, device: FakeWorker())
    monkeypatch.setattr(benchmark, "_worker_rss_bytes", lambda engine: 64 * 1024 * 1024)

    payload = asyncio.run(
        benchmark._run_live(
            ModelPaths(tmp_path),
            wav(speech_like(1.0)),
            LiveSimConfig(cadence_s=0.5),
            "cpu",
        )
    )

    assert payload["worker_rss"]["available"] is True
    assert payload["worker_rss"]["unavailable_reason"] is None
    assert payload["worker_rss"]["source"] == "linux /proc/<worker pid>/status"
    assert payload["rss_samples"]
    assert all(item["worker_rss_bytes"] == 64 * 1024 * 1024 for item in payload["rss_samples"])
    assert payload["rss_notes"]["worker_max_rss_bytes"].startswith("RUSAGE_CHILDREN high-water")
