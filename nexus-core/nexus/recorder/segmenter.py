"""Sample-offset speech segmentation for the standalone recorder (plan 54, C2).

This module is deliberately pure: it sees bounded PCM16 bytes and absolute sample offsets, never wall-clock
time, a store, the voice engine or the event bus. A caller can feed chunks as they become durable and consume
closed :class:`Segment` boundaries; or call :func:`segment_pcm` over one bounded window. Segment positions are
integer sample offsets, so they remain exact regardless of upload latency.

The energy VAD uses a bounded rolling noise floor. It is an utterance *planner*, not proof that audio contains
or does not contain speech, and these defaults are not a transcription-quality claim: very quiet, unusual or
stationary sustained speech can be missed; steady background noise can look like speech while the floor adapts.
Audio outside a segment is still saved; the recorder never deletes or fabricates samples based on this decision.
Tune the thresholds against real devices and speech before relying on them.

Forced boundaries cut content every ``window_s`` seconds. The following inference window starts ``overlap_s``
earlier, so consecutive model requests share explicit context without ever re-inferring the whole recording.
The overlap is context only: it is not a word timestamp and does not let this module claim word alignment.
"""

from __future__ import annotations

import math
import sys
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Any

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
MIN_INFERENCE_SECONDS = 0.2

DEFAULT_FRAME_SECONDS = 0.02
DEFAULT_SILENCE_SECONDS = 0.65
DEFAULT_WINDOW_SECONDS = 10.0
DEFAULT_OVERLAP_SECONDS = 0.75
DEFAULT_PREVIEW_CADENCE_SECONDS = 0.75
DEFAULT_NOISE_WINDOW_SECONDS = 1.5
DEFAULT_CALIBRATION_SECONDS = 0.2
DEFAULT_MIN_RMS = 0.003
DEFAULT_NOISE_RATIO = 1.8
MAX_FEED_BYTES = 64_000  # 2 s of 16 kHz mono PCM16; mirrors one durable recorder chunk


class SegmenterError(Exception):
    """A caller error with a stable code; no audio is discarded to recover from it."""

    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.details = details


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def samples_for(seconds: float) -> int:
    """Convert seconds to an integer number of 16 kHz samples (at least one)."""
    return max(1, round(_finite(seconds, "seconds") * SAMPLE_RATE))


@dataclass(frozen=True)
class SegmenterConfig:
    """Starting parameters from plan 54 §5. Values are seconds, not sample counts."""

    frame_s: float = DEFAULT_FRAME_SECONDS
    silence_s: float = DEFAULT_SILENCE_SECONDS
    window_s: float = DEFAULT_WINDOW_SECONDS
    overlap_s: float = DEFAULT_OVERLAP_SECONDS
    preview_cadence_s: float = DEFAULT_PREVIEW_CADENCE_SECONDS
    noise_window_s: float = DEFAULT_NOISE_WINDOW_SECONDS
    calibration_s: float = DEFAULT_CALIBRATION_SECONDS
    min_rms: float = DEFAULT_MIN_RMS
    noise_ratio: float = DEFAULT_NOISE_RATIO

    def __post_init__(self) -> None:
        for name in (
            "frame_s",
            "silence_s",
            "window_s",
            "overlap_s",
            "preview_cadence_s",
            "noise_window_s",
            "calibration_s",
            "min_rms",
            "noise_ratio",
        ):
            _finite(getattr(self, name), name)
        if not 0.005 <= self.frame_s <= 0.1:
            raise ValueError("frame_s must be between 0.005 and 0.1 seconds")
        if not 0.1 <= self.silence_s <= 5:
            raise ValueError("silence_s must be between 0.1 and 5 seconds")
        if not MIN_INFERENCE_SECONDS <= self.window_s <= 110:
            raise ValueError("window_s must be between 0.2 and 110 seconds")
        if not 0 <= self.overlap_s < self.window_s:
            raise ValueError("overlap_s must be at least 0 and less than window_s")
        if not 0.05 <= self.preview_cadence_s <= self.window_s:
            raise ValueError("preview_cadence_s must be between 0.05 and window_s seconds")
        if not 0.2 <= self.noise_window_s <= 10:
            raise ValueError("noise_window_s must be between 0.2 and 10 seconds")
        if not 0 <= self.calibration_s <= self.noise_window_s:
            raise ValueError("calibration_s must be between 0 and noise_window_s seconds")
        if not 0 <= self.min_rms < 1:
            raise ValueError("min_rms must be between 0 and 1")
        if not 0 < self.noise_ratio <= 10:
            raise ValueError("noise_ratio must be between 0 and 10")

    @property
    def frame_samples(self) -> int:
        return samples_for(self.frame_s)

    @property
    def silence_samples(self) -> int:
        return samples_for(self.silence_s)

    @property
    def window_samples(self) -> int:
        return samples_for(self.window_s)

    @property
    def overlap_samples(self) -> int:
        return round(self.overlap_s * SAMPLE_RATE)

    @property
    def preview_cadence_samples(self) -> int:
        return samples_for(self.preview_cadence_s)

    @property
    def noise_window_frames(self) -> int:
        return max(1, round(self.noise_window_s / self.frame_s))

    @property
    def calibration_frames(self) -> int:
        return max(0, round(self.calibration_s / self.frame_s))

    @property
    def min_inference_samples(self) -> int:
        return samples_for(MIN_INFERENCE_SECONDS)


@dataclass(frozen=True)
class Segment:
    """One closed content range plus the explicit inference window used for it.

    ``start``/``end`` are the speech range the transcript displays. ``window_start``/``window_end`` are the
    samples an inference request may read; for a forced boundary ``window_start`` precedes ``start`` by up to
    ``overlap_s`` of context. Saved audio is never trimmed to these bounds.
    """

    segment_id: int
    generation: int
    start: int
    end: int
    window_start: int
    window_end: int
    close_reason: str

    @property
    def samples(self) -> int:
        return max(0, self.end - self.start)

    @property
    def too_short(self) -> bool:
        return self.samples < samples_for(MIN_INFERENCE_SECONDS)

    def inference_bounds(self, *, minimum_samples: int | None = None) -> tuple[int, int]:
        """The window to feed the engine, padding only this copy for a tiny tail.

        The padding is returned as a wider end offset; a caller building the request appends zero samples there.
        The saved recording and ``start``/``end`` are unchanged.
        """
        minimum = samples_for(MIN_INFERENCE_SECONDS) if minimum_samples is None else max(0, minimum_samples)
        end = max(self.window_end, self.window_start + minimum)
        return self.window_start, end

    def document(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "generation": self.generation,
            "start": self.start,
            "end": self.end,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "close_reason": self.close_reason,
            "too_short": self.too_short,
        }


@dataclass(frozen=True)
class PreviewWindow:
    """A bounded provisional window for the still-open segment."""

    segment_id: int
    generation: int
    start: int
    end: int
    window_start: int
    window_end: int

    @property
    def samples(self) -> int:
        return max(0, self.end - self.start)

    def document(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "generation": self.generation,
            "start": self.start,
            "end": self.end,
            "window_start": self.window_start,
            "window_end": self.window_end,
        }


def _samples(frame: bytes) -> array[int]:
    values = array("h")
    values.frombytes(frame)
    if sys.byteorder == "big":
        values.byteswap()
    return values


def frame_rms(frame: bytes) -> float:
    """Root-mean-square of one PCM16 frame, normalised to 0..1."""
    if len(frame) % BYTES_PER_SAMPLE:
        raise ValueError("PCM16 frame must contain whole samples")
    values = _samples(frame)
    if not values:
        return 0.0
    mean_square = sum(value * value for value in values) / len(values)
    return math.sqrt(mean_square) / 32768.0


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))
    return ordered[index]


class EnergyVad:
    """Frame-energy VAD with a bounded rolling floor and a short startup calibration.

    The 20th percentile of recent frame energies approximates the floor. A steady signal has no spread above
    its own floor, so constant noise/hum is not repeatedly opened as speech; the cost is that perfectly
    stationary sustained speech can be missed. Calibration frames only teach the floor and never open a segment.
    """

    def __init__(self, config: SegmenterConfig) -> None:
        self.config = config
        self._recent: deque[float] = deque(maxlen=config.noise_window_frames)
        self._seen_frames = 0
        self._floor = 0.0

    @property
    def noise_floor(self) -> float:
        return self._floor

    @property
    def calibrated(self) -> bool:
        return self._seen_frames > self.config.calibration_frames

    @property
    def seen_frames(self) -> int:
        return self._seen_frames

    def observe(self, rms: float) -> None:
        """Add one frame to the rolling floor without deciding anything."""
        self._recent.append(rms)
        self._seen_frames += 1
        self._floor = _percentile(list(self._recent), 0.2)

    def speech_at(self, rms: float, floor: float | None = None) -> bool:
        floor = self._floor if floor is None else floor
        return rms >= max(self.config.min_rms, floor * self.config.noise_ratio)


class PcmSegmenter:
    """Incremental sample-offset segmenter over contiguous PCM16 chunks.

    Feed chunks in order with their absolute start sample; :meth:`finish` closes the residual speech. Gaps,
    duplicate samples and an unfinished half sample are rejected rather than silently turned into silence.
    Each call accepts at most :data:`MAX_FEED_BYTES` (64,000 bytes, two seconds) so an accidental giant input is
    never copied wholesale; :func:`segment_pcm` splits larger bounded windows internally. Only one frame plus
    the rolling energy window and a short calibration buffer is retained, so memory does not grow with recording
    length. Segment text/inference is the caller's job; this object emits offsets only.

    For resume, construct with ``first_segment_id`` set past every durable segment and make the first
    :meth:`feed` call with the last durable segment's ``end`` as ``start_sample``. Resumed ids continue from
    there; the VAD recalibrates from the resume audio and does not retain the pre-restart noise floor.
    """

    def __init__(
        self,
        config: SegmenterConfig | None = None,
        *,
        generation: int = 0,
        first_segment_id: int = 0,
    ) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if isinstance(first_segment_id, bool) or not isinstance(first_segment_id, int) or first_segment_id < 0:
            raise ValueError("first_segment_id must be a non-negative integer")
        self.config = config or SegmenterConfig()
        self.generation = generation
        self._vad = EnergyVad(self.config)
        self._calibration: list[tuple[int, int, float]] = []
        self._calibrated = self.config.calibration_frames == 0
        self._fed_end: int | None = None
        self._pending = b""
        self._finished = False
        self._next_segment_id = first_segment_id
        self._active_start: int | None = None
        self._active_window_start = 0
        self._last_speech_end = 0
        self._silence_samples = 0
        self._last_preview_end = 0

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def fed_samples(self) -> int:
        return 0 if self._fed_end is None else self._fed_end

    @property
    def active_start(self) -> int | None:
        return self._active_start

    @property
    def active_window_start(self) -> int | None:
        return None if self._active_start is None else self._active_window_start

    @property
    def last_speech_end(self) -> int | None:
        return None if self._active_start is None else self._last_speech_end

    @property
    def next_segment_id(self) -> int:
        return self._next_segment_id

    def feed(self, pcm: bytes, *, start_sample: int | None = None) -> list[Segment]:
        """Consume a contiguous chunk and return any segments closed by it.

        ``start_sample`` defaults to the end of the previous chunk (or 0 for the first). Supplying a different
        offset is a caller bug, not a recording hole, so it raises :class:`SegmenterError`.
        """
        if self._finished:
            raise SegmenterError("segmenter_finished", "cannot feed audio after finish")
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise SegmenterError("invalid_audio", "pcm must be bytes-like")
        if len(pcm) > MAX_FEED_BYTES:
            raise SegmenterError(
                "feed_too_large",
                f"feed accepts at most {MAX_FEED_BYTES} bytes; split larger windows or use segment_pcm",
                bytes=len(pcm),
                max_bytes=MAX_FEED_BYTES,
            )
        data = bytes(pcm)
        if start_sample is None:
            start_sample = 0 if self._fed_end is None else self._fed_end
        if isinstance(start_sample, bool) or not isinstance(start_sample, int) or start_sample < 0:
            raise SegmenterError("invalid_offset", "start_sample must be a non-negative integer")
        if self._fed_end is not None and start_sample != self._fed_end:
            raise SegmenterError(
                "noncontiguous_audio",
                "audio chunks must be contiguous; never invent silence for a gap",
                expected=self._fed_end,
                received=start_sample,
            )

        pending = self._pending
        base = start_sample - len(pending) // BYTES_PER_SAMPLE
        combined = pending + data
        frame_bytes = self.config.frame_samples * BYTES_PER_SAMPLE
        closed: list[Segment] = []
        offset = 0
        while len(combined) - offset >= frame_bytes:
            frame = combined[offset : offset + frame_bytes]
            frame_start = base + offset // BYTES_PER_SAMPLE
            closed.extend(self._process_frame(frame, frame_start))
            offset += frame_bytes
        self._pending = combined[offset:]
        self._fed_end = start_sample + len(data) // BYTES_PER_SAMPLE
        return closed

    def preview(self, *, available_end: int | None = None) -> PreviewWindow | None:
        """Return a bounded preview window when the durable/available audio advanced by the cadence.

        The end never exceeds ``available_end`` (the caller's durable offset) or the last speech sample. No
        preview is returned for unchanged speech, a segment shorter than the inference minimum, or a segment
        that already emitted one within ``preview_cadence_s``. This object never reads audio itself.
        """
        if self._finished or self._active_start is None:
            return None
        if available_end is not None and (
            isinstance(available_end, bool) or not isinstance(available_end, int) or available_end < 0
        ):
            raise SegmenterError("invalid_offset", "available_end must be a non-negative integer")
        limit = self._fed_end if available_end is None else min(available_end, self._fed_end or 0)
        if limit is None:
            return None
        end = min(limit, self._last_speech_end)
        start = self._active_window_start
        if end <= start:
            return None
        if end - start < self.config.min_inference_samples:
            return None
        if end - start < self.config.preview_cadence_samples:
            return None
        if end - self._last_preview_end < self.config.preview_cadence_samples:
            return None
        self._last_preview_end = end
        return PreviewWindow(
            segment_id=self._next_segment_id,
            generation=self.generation,
            start=max(start, self._active_start),
            end=end,
            window_start=start,
            window_end=end,
        )

    def finish(self, *, end_sample: int | None = None) -> list[Segment]:
        """Close the residual segment, including a tiny tail. Idempotent once finished.

        A trailing half sample is a defect and raises; complete samples of a partial final frame are analysed.
        ``end_sample`` must equal the total fed samples: this object never truncates audio it was not given.
        """
        if self._finished:
            return []
        if end_sample is not None:
            expected = self.fed_samples
            if end_sample != expected:
                raise SegmenterError(
                    "finish_offset_mismatch",
                    "finish end_sample must equal the fed sample count",
                    expected=expected,
                    received=end_sample,
                )
        if self._pending:
            if len(self._pending) % BYTES_PER_SAMPLE:
                raise SegmenterError("partial_sample", "PCM16 audio ended with half a sample")
            base = self.fed_samples - len(self._pending) // BYTES_PER_SAMPLE
            closed = self._process_frame(self._pending, base)
            self._pending = b""
        else:
            closed = []
        closed.extend(self._release_calibration())

        if self._active_start is not None:
            segment = self._close(self._last_speech_end, "stop")
            if segment is not None:
                closed.append(segment)
        self._finished = True
        return closed

    def _process_frame(self, frame: bytes, frame_start: int) -> list[Segment]:
        end = frame_start + len(frame) // BYTES_PER_SAMPLE
        rms = frame_rms(frame)
        closed: list[Segment] = []

        # Calibration teaches the floor from the opening audio. Its frames are classified afterwards with that
        # floor, so speech that starts immediately is not lost and constant startup noise is not opened.
        if not self._calibrated and self._vad.seen_frames < self.config.calibration_frames:
            self._vad.observe(rms)
            self._calibration.append((frame_start, end, rms))
            return closed
        closed.extend(self._release_calibration())
        self._vad.observe(rms)
        closed.extend(self._process_flag(self._vad.speech_at(rms), frame_start, end))
        return closed

    def _release_calibration(self) -> list[Segment]:
        if self._calibrated:
            return []
        self._calibrated = True
        if not self._calibration:
            return []
        floor = self._vad.noise_floor
        pending_calibration = self._calibration
        self._calibration = []
        closed: list[Segment] = []
        for stored_start, stored_end, stored_rms in pending_calibration:
            closed.extend(self._process_flag(self._vad.speech_at(stored_rms, floor), stored_start, stored_end))
        return closed

    def _process_flag(self, speech: bool, frame_start: int, end: int) -> list[Segment]:
        closed: list[Segment] = []

        if speech:
            if self._active_start is None:
                self._active_start = frame_start
                self._active_window_start = frame_start
                self._last_preview_end = frame_start
            self._last_speech_end = end
            self._silence_samples = 0
            while self._active_start is not None and end - self._active_start >= self.config.window_samples:
                boundary = self._active_start + self.config.window_samples
                segment = self._close(boundary, "forced")
                if segment is not None:
                    closed.append(segment)
                self._active_start = boundary
                self._active_window_start = max(0, boundary - self.config.overlap_samples)
                self._last_preview_end = boundary
                self._last_speech_end = max(self._last_speech_end, boundary)
                self._silence_samples = 0
        elif self._active_start is not None:
            self._silence_samples += end - frame_start
            if self._silence_samples >= self.config.silence_samples:
                segment = self._close(self._last_speech_end, "silence")
                if segment is not None:
                    closed.append(segment)
        return closed

    def _close(self, content_end: int, reason: str) -> Segment | None:
        if self._active_start is None:
            return None
        start = self._active_start
        end = min(max(start, content_end), self._last_speech_end)
        segment = Segment(
            segment_id=self._next_segment_id,
            generation=self.generation,
            start=start,
            end=end,
            window_start=min(self._active_window_start, start),
            window_end=end,
            close_reason=reason,
        )
        self._next_segment_id += 1
        self._active_start = None
        self._active_window_start = 0
        self._last_preview_end = 0
        return segment


def segment_pcm(
    pcm: bytes,
    *,
    config: SegmenterConfig | None = None,
    generation: int = 0,
    start_sample: int = 0,
    first_segment_id: int = 0,
) -> tuple[Segment, ...]:
    """Convenience batch wrapper for one bounded window; offsets remain absolute.

    The input is split into :data:`MAX_FEED_BYTES` pieces so the same size discipline applies to batch callers.
    """
    segmenter = PcmSegmenter(config, generation=generation, first_segment_id=first_segment_id)
    closed: list[Segment] = []
    for offset in range(0, len(pcm), MAX_FEED_BYTES):
        piece = pcm[offset : offset + MAX_FEED_BYTES]
        closed.extend(segmenter.feed(piece, start_sample=start_sample + offset // BYTES_PER_SAMPLE))
    closed.extend(segmenter.finish())
    return tuple(closed)
