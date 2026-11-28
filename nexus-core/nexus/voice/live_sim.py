"""Deterministic simulated-live windowed STT workload (plan 54, milestone A1).

The simulator models the recorder's proposed ``§5`` cadence without a microphone
or a real-time clock: audio arrives on a simulated 1x timeline, energy VAD closes
utterances, forced boundaries split long speech with a short overlap, and preview
and final windows are scheduled through one serialized engine. Inference windows
are always bounded WAVs built from integer sample offsets.

A fake engine can declare ``latency_seconds``; the simulator then uses that value
for scheduling instead of wall time, which makes cadence, overload and backlog
tests deterministic. Real engines have no such attribute, so their measured
per-call duration is used. Wall-clock waiting is intentionally not simulated:
the audio timeline advances event by event, while inference calls happen
back-to-back. The resulting latency/backlog numbers answer the feasibility gate
(inference time versus audio time) without a microphone.
"""

from __future__ import annotations

import math
import struct
import sys
import time
from array import array
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, Protocol

from .audio import SAMPLE_RATE, parse_wav

MIN_INFERENCE_SECONDS = 0.2
DEFAULT_WINDOW_SECONDS = 10.0
DEFAULT_CADENCE_SECONDS = 1.0
DEFAULT_OVERLAP_SECONDS = 0.75
DEFAULT_SILENCE_SECONDS = 0.8
DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
_EPSILON = 1e-9


class WindowEngine(Protocol):
    """The part of a voice engine the simulator needs."""

    async def transcribe(self, wav: bytes) -> str: ...


@dataclass(frozen=True)
class LiveSimConfig:
    """Starting parameters from plan 54 §5; tune only through measurement."""

    window_s: float = DEFAULT_WINDOW_SECONDS
    cadence_s: float = DEFAULT_CADENCE_SECONDS
    overlap_s: float = DEFAULT_OVERLAP_SECONDS
    silence_s: float = DEFAULT_SILENCE_SECONDS
    vad_frame_s: float = 0.02
    vad_min_rms: float = 0.003
    vad_noise_ratio: float = 1.8
    sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if not MIN_INFERENCE_SECONDS <= self.window_s <= 110:
            raise ValueError("window_s must be between 0.2 and 110 seconds")
        if not 0.05 <= self.cadence_s <= self.window_s:
            raise ValueError("cadence_s must be between 0.05 and window_s")
        if not 0 <= self.overlap_s < self.window_s:
            raise ValueError("overlap_s must be at least 0 and less than window_s")
        if not 0.1 <= self.silence_s <= 5:
            raise ValueError("silence_s must be between 0.1 and 5 seconds")
        if not 0.005 <= self.vad_frame_s <= 0.1:
            raise ValueError("vad_frame_s must be between 0.005 and 0.1 seconds")
        if not 0 <= self.vad_min_rms < 1:
            raise ValueError("vad_min_rms must be between 0 and 1")
        if not 0 < self.vad_noise_ratio <= 10:
            raise ValueError("vad_noise_ratio must be between 0 and 10")
        if not 0 < self.sample_interval_s <= 60:
            raise ValueError("sample_interval_s must be between 0 and 60 seconds")


@dataclass(frozen=True)
class JobRecord:
    """One submitted preview or final window."""

    kind: str
    segment_id: int
    submit_s: float
    audio_start_s: float
    audio_end_s: float
    completed_s: float | None = None
    latency_s: float | None = None
    audio_end_latency_s: float | None = None
    replaced: bool = False


@dataclass(frozen=True)
class BacklogSample:
    """Queue state at one point on the simulated audio timeline.

    ``queued_audio_s`` sums the queued windows, so forced-boundary overlap is
    counted in both neighbours; it measures outstanding inference, not unique
    audio. ``finalization_lag_s`` is the latest submitted final window end minus
    the latest completed final window end.
    """

    at_s: float
    pending_final_jobs: int
    pending_preview: bool
    queued_audio_s: float
    finalization_lag_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "at_s": round(self.at_s, 3),
            "pending_final_jobs": self.pending_final_jobs,
            "pending_preview": self.pending_preview,
            "queued_audio_s": round(self.queued_audio_s, 3),
            "finalization_lag_s": round(self.finalization_lag_s, 3),
        }


@dataclass(frozen=True)
class LiveSimResult:
    """Measured workload, including the raw jobs for focused tests."""

    audio_seconds: float
    speech_seconds: float
    config: LiveSimConfig
    preview_jobs: tuple[JobRecord, ...]
    final_jobs: tuple[JobRecord, ...]
    skipped_final_jobs: int
    backlog: tuple[BacklogSample, ...]
    inference_seconds: float
    inference_wall_seconds: float

    @property
    def previews_replaced(self) -> int:
        return sum(job.replaced for job in self.preview_jobs)

    @property
    def inference_to_audio_ratio(self) -> float:
        """Inference seconds divided by audio seconds; lower is better."""

        return self.inference_seconds / self.audio_seconds if self.audio_seconds else 0.0

    @property
    def audio_per_inference_ratio(self) -> float | None:
        """Audio seconds divided by inference seconds; higher is faster.

        This has the same direction as the one-shot benchmark's
        ``real_time_factor`` (audio / inference), but here it covers the whole
        combined preview + final workload instead of the best single call.
        """

        return self.audio_seconds / self.inference_seconds if self.inference_seconds else None

    @property
    def max_pending_final_jobs(self) -> int:
        return max((sample.pending_final_jobs for sample in self.backlog), default=0)

    @property
    def max_queued_audio_seconds(self) -> float:
        return max((sample.queued_audio_s for sample in self.backlog), default=0.0)

    @property
    def max_finalization_lag_seconds(self) -> float:
        return max((sample.finalization_lag_s for sample in self.backlog), default=0.0)

    def to_dict(self) -> dict[str, Any]:
        previews = [job for job in self.preview_jobs if not job.replaced and job.completed_s is not None]
        finals = [job for job in self.final_jobs if job.completed_s is not None]
        return {
            "audio_seconds": round(self.audio_seconds, 3),
            "speech_seconds": round(self.speech_seconds, 3),
            "config": {
                field.name: round(getattr(self.config, field.name), 3)
                for field in fields(LiveSimConfig)
            },
            "windows": {
                "previews_submitted": len(self.preview_jobs),
                "previews_completed": len(previews),
                "previews_replaced": self.previews_replaced,
                "finals_submitted": len(self.final_jobs),
                "finals_completed": len(finals),
                "finals_skipped_short": self.skipped_final_jobs,
            },
            "preview_latency_seconds": {
                "enqueue_to_text": _summary([job.latency_s for job in previews]),
                "audio_end_to_text": _summary([job.audio_end_latency_s for job in previews]),
            },
            "final_latency_seconds": {
                "enqueue_to_text": _summary([job.latency_s for job in finals]),
                "audio_end_to_text": _summary([job.audio_end_latency_s for job in finals]),
            },
            "inference_to_audio_ratio": round(self.inference_to_audio_ratio, 3),
            "audio_per_inference_ratio": (
                round(self.audio_per_inference_ratio, 3) if self.audio_per_inference_ratio is not None else None
            ),
            "ratio_notes": {
                "inference_to_audio_ratio": "inference seconds / audio seconds; lower is better; gate target < 0.6",
                "audio_per_inference_ratio": (
                    "audio seconds / inference seconds; higher is faster; same direction as the one-shot "
                    "real_time_factor, but for the combined preview + final workload"
                ),
            },
            "inference_seconds": round(self.inference_seconds, 3),
            "inference_wall_seconds": round(self.inference_wall_seconds, 3),
            "max_pending_final_jobs": self.max_pending_final_jobs,
            "max_queued_audio_seconds": round(self.max_queued_audio_seconds, 3),
            "max_finalization_lag_seconds": round(self.max_finalization_lag_seconds, 3),
            "backlog": [sample.as_dict() for sample in self.backlog],
        }


@dataclass(frozen=True)
class _Run:
    start_frame: int
    end_frame: int
    closed_at_frame: int
    closed_by_silence: bool


@dataclass
class _PlannedJob:
    kind: str
    segment_id: int
    submit_s: float
    start_sample: int
    end_sample: int
    completed_s: float | None = None
    latency_s: float | None = None
    audio_end_latency_s: float | None = None
    replaced: bool = False


def _summary(values: list[float | None]) -> dict[str, float | int | None]:
    present = [value for value in values if value is not None]
    if not present:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    return {
        "count": len(present),
        "p50": round(_percentile(present, 0.5), 3),
        "p95": round(_percentile(present, 0.95), 3),
        "max": round(max(present), 3),
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _samples(pcm: bytes) -> array[int]:
    if len(pcm) % 2:
        raise ValueError("PCM16 data must contain whole samples")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _frame_rms(pcm: bytes, config: LiveSimConfig) -> tuple[list[float], int]:
    samples = _samples(pcm)
    frame_samples = max(1, round(SAMPLE_RATE * config.vad_frame_s))
    energies: list[float] = []
    for start in range(0, len(samples), frame_samples):
        frame = samples[start : start + frame_samples]
        if not frame:
            break
        mean_square = sum(value * value for value in frame) / len(frame)
        energies.append(math.sqrt(mean_square) / 32768.0)
    return energies, frame_samples


def _speech_flags(energies: list[float], config: LiveSimConfig) -> list[bool]:
    """Simple energy VAD with a noise floor estimated from the clip.

    The 10th percentile approximates the noise floor and the 90th keeps a very
    loud, varying signal from being classified as its own noise. A flat energy
    contour (no usable spread) has no evidence of speech activity above its own
    level, so it is treated as silence rather than as a steady utterance. That
    keeps constant background noise — including the ``low == high`` case — out
    of previews and finals.

    The cost is that a perfectly stationary sustained vowel is missed. This
    cannot separate arbitrary stationary noise from speech with energy alone;
    plan 54 defers a fully adaptive VAD to the recorder scheduler (C2). This
    harness only needs reproducible speech/silence decisions for cadence and
    load measurements.
    """

    if not energies:
        return []
    low = _percentile(energies, 0.1)
    high = _percentile(energies, 0.9)
    spread = high - low
    if spread <= max(config.vad_min_rms, low * 0.5):
        return [False] * len(energies)
    adaptive = min(low * config.vad_noise_ratio, high * 0.5)
    threshold = max(config.vad_min_rms, adaptive)
    return [energy >= threshold for energy in energies]


def _runs(flags: list[bool], frame_s: float, silence_s: float) -> list[_Run]:
    runs: list[_Run] = []
    start: int | None = None
    last_speech = 0
    silence_frames = 0
    silence_limit = max(1, math.ceil(silence_s / frame_s - _EPSILON))
    for index, speech in enumerate(flags):
        if speech:
            if start is None:
                start = index
            last_speech = index + 1
            silence_frames = 0
        elif start is not None:
            silence_frames += 1
            if silence_frames >= silence_limit:
                runs.append(_Run(start, last_speech, last_speech + silence_frames, True))
                start = None
                silence_frames = 0
    if start is not None:
        runs.append(_Run(start, last_speech, len(flags), False))
    return runs


def _plan_jobs(
    runs: list[_Run],
    config: LiveSimConfig,
    total_samples: int,
    frame_samples: int,
) -> tuple[list[_PlannedJob], list[_PlannedJob], int]:
    window_samples = max(1, round(config.window_s * SAMPLE_RATE))
    cadence_samples = max(1, round(config.cadence_s * SAMPLE_RATE))
    overlap_samples = round(config.overlap_s * SAMPLE_RATE)
    min_samples = round(MIN_INFERENCE_SECONDS * SAMPLE_RATE)
    previews: list[_PlannedJob] = []
    finals: list[_PlannedJob] = []
    skipped = 0

    for segment_id, run in enumerate(runs):
        start = min(run.start_frame * frame_samples, total_samples)
        speech_end = min(run.end_frame * frame_samples, total_samples)
        closed_at = min(run.closed_at_frame * frame_samples, total_samples)
        chunk_start = start
        chunk_index = 0
        while chunk_start < speech_end:
            chunk_end = min(chunk_start + window_samples, speech_end)
            last_chunk = chunk_end >= speech_end
            if last_chunk and chunk_end - chunk_start < min_samples:
                skipped += 1
                break

            if last_chunk:
                submit_sample = closed_at if run.closed_by_silence else total_samples
            else:
                submit_sample = chunk_end
            finals.append(
                _PlannedJob(
                    "final",
                    segment_id,
                    submit_sample / SAMPLE_RATE,
                    chunk_start,
                    chunk_end,
                )
            )

            # New audio starts at the previous boundary; overlap is context only.
            preview_base = chunk_start if chunk_index == 0 else chunk_start + overlap_samples
            preview_sample = preview_base + cadence_samples
            while preview_sample < chunk_end:
                if preview_sample - chunk_start >= min_samples:
                    previews.append(
                        _PlannedJob(
                            "preview",
                            segment_id,
                            preview_sample / SAMPLE_RATE,
                            chunk_start,
                            preview_sample,
                        )
                    )
                preview_sample += cadence_samples

            if last_chunk:
                break
            chunk_start = chunk_end - overlap_samples
            chunk_index += 1

    return previews, finals, skipped


def _window_wav(pcm: bytes, start_sample: int, end_sample: int, *, min_samples: int) -> bytes:
    start = start_sample * 2
    end = end_sample * 2
    body = pcm[start:end]
    if end_sample - start_sample < min_samples:
        body += bytes((min_samples - (end_sample - start_sample)) * 2)
    data_size = len(body)
    fmt = struct.pack("<HHIIHH", 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
    payload = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    payload += b"data" + struct.pack("<I", data_size) + body
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def _declared_latency(engine: WindowEngine) -> float | None:
    value = getattr(engine, "latency_seconds", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        raise ValueError("engine.latency_seconds must be a finite non-negative number")
    return float(value)


async def simulate(
    pcm: bytes,
    engine: WindowEngine,
    config: LiveSimConfig | None = None,
    *,
    on_sample: Callable[[BacklogSample], None] | None = None,
) -> LiveSimResult:
    """Run one simulated-live windowed workload over bounded PCM16 audio."""

    config = config or LiveSimConfig()
    if not pcm:
        raise ValueError("audio is empty")
    total_samples = len(pcm) // 2
    if len(pcm) % 2:
        raise ValueError("PCM16 data must contain whole samples")
    audio_seconds = total_samples / SAMPLE_RATE
    if audio_seconds < MIN_INFERENCE_SECONDS:
        raise ValueError("audio is shorter than 0.2 seconds")

    energies, frame_samples = _frame_rms(pcm, config)
    flags = _speech_flags(energies, config)
    runs = _runs(flags, config.vad_frame_s, config.silence_s)
    speech_seconds = sum(run.end_frame - run.start_frame for run in runs) * config.vad_frame_s
    previews, finals, skipped = _plan_jobs(runs, config, total_samples, frame_samples)
    submissions = sorted(
        previews + finals,
        key=lambda job: (job.submit_s, 0 if job.kind == "final" else 1, job.segment_id, job.start_sample),
    )

    window_samples = round(config.window_s * SAMPLE_RATE)
    min_samples = round(MIN_INFERENCE_SECONDS * SAMPLE_RATE)
    pending_preview: _PlannedJob | None = None
    pending_finals: deque[_PlannedJob] = deque()
    backlog: list[BacklogSample] = []
    inference_seconds = 0.0
    inference_wall_seconds = 0.0
    submitted_final_end = 0.0
    completed_final_end = 0.0
    next_submission = 0
    now = 0.0
    busy_until: float | None = None
    running: _PlannedJob | None = None
    next_sample = config.sample_interval_s

    def emit_sample(at: float) -> None:
        queued = sum((job.end_sample - job.start_sample) / SAMPLE_RATE for job in pending_finals)
        if pending_preview is not None:
            queued += (pending_preview.end_sample - pending_preview.start_sample) / SAMPLE_RATE
        sample = BacklogSample(
            at_s=at,
            pending_final_jobs=len(pending_finals),
            pending_preview=pending_preview is not None,
            queued_audio_s=queued,
            finalization_lag_s=max(0.0, submitted_final_end - completed_final_end),
        )
        backlog.append(sample)
        if on_sample is not None:
            on_sample(sample)

    def enqueue(job: _PlannedJob) -> None:
        nonlocal pending_preview, submitted_final_end
        if job.kind == "preview":
            if pending_preview is not None:
                pending_preview.replaced = True
            pending_preview = job
        else:
            pending_finals.append(job)
            submitted_final_end = max(submitted_final_end, job.end_sample / SAMPLE_RATE)

    def next_job() -> _PlannedJob | None:
        nonlocal pending_preview
        candidate: _PlannedJob | None = None
        if pending_preview is not None:
            candidate = pending_preview
        if pending_finals:
            final = pending_finals[0]
            if candidate is None or final.submit_s < candidate.submit_s:
                candidate = final
        if candidate is None:
            return None
        if candidate.kind == "preview":
            pending_preview = None
        else:
            pending_finals.popleft()
        return candidate

    while True:
        while next_submission < len(submissions) and submissions[next_submission].submit_s <= now + _EPSILON:
            enqueue(submissions[next_submission])
            next_submission += 1
        while next_sample <= now + _EPSILON:
            emit_sample(next_sample)
            next_sample += config.sample_interval_s

        if busy_until is not None:
            if next_submission < len(submissions) and submissions[next_submission].submit_s < busy_until - _EPSILON:
                now = submissions[next_submission].submit_s
                continue
            now = busy_until
            busy_until = None
            assert running is not None
            job = running
            job.completed_s = now
            job.latency_s = now - job.submit_s
            job.audio_end_latency_s = now - job.end_sample / SAMPLE_RATE
            if job.kind == "final":
                completed_final_end = max(completed_final_end, job.end_sample / SAMPLE_RATE)
            continue

        job = next_job()
        if job is not None:
            if job.end_sample - job.start_sample > window_samples:
                raise AssertionError("inference window exceeded the configured bound")
            wav = _window_wav(pcm, job.start_sample, job.end_sample, min_samples=min_samples)
            wall_started = time.perf_counter()
            await engine.transcribe(wav)
            wall = time.perf_counter() - wall_started
            declared = _declared_latency(engine)
            duration = declared if declared is not None else wall
            inference_seconds += duration
            inference_wall_seconds += wall
            running = job
            busy_until = now + duration
            continue

        if next_submission < len(submissions):
            now = submissions[next_submission].submit_s
            continue
        break

    # Include the end of the audio (and the drain point) in the backlog series.
    if not backlog or backlog[-1].at_s < max(now, audio_seconds) - _EPSILON:
        emit_sample(max(now, audio_seconds))

    def record(job: _PlannedJob) -> JobRecord:
        return JobRecord(
            kind=job.kind,
            segment_id=job.segment_id,
            submit_s=job.submit_s,
            audio_start_s=job.start_sample / SAMPLE_RATE,
            audio_end_s=job.end_sample / SAMPLE_RATE,
            completed_s=job.completed_s,
            latency_s=job.latency_s,
            audio_end_latency_s=job.audio_end_latency_s,
            replaced=job.replaced,
        )

    return LiveSimResult(
        audio_seconds=audio_seconds,
        speech_seconds=speech_seconds,
        config=config,
        preview_jobs=tuple(record(job) for job in previews),
        final_jobs=tuple(record(job) for job in finals),
        skipped_final_jobs=skipped,
        backlog=tuple(backlog),
        inference_seconds=inference_seconds,
        inference_wall_seconds=inference_wall_seconds,
    )


async def simulate_wav(
    wav: bytes,
    engine: WindowEngine,
    config: LiveSimConfig | None = None,
    *,
    on_sample: Callable[[BacklogSample], None] | None = None,
) -> LiveSimResult:
    """Validate a bounded 16 kHz mono PCM16 WAV and simulate it."""

    info = parse_wav(wav)
    return await simulate(info.pcm, engine, config, on_sample=on_sample)


__all__ = [
    "DEFAULT_CADENCE_SECONDS",
    "DEFAULT_OVERLAP_SECONDS",
    "DEFAULT_SILENCE_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "MIN_INFERENCE_SECONDS",
    "BacklogSample",
    "JobRecord",
    "LiveSimConfig",
    "LiveSimResult",
    "WindowEngine",
    "simulate",
    "simulate_wav",
]
