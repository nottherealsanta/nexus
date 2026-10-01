"""Bounded 16 kHz microphone capture for TUI dictation (VOICE_PLAN.md §8.2)."""

from __future__ import annotations

import math
import struct
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

SAMPLE_RATE = 16_000


class VoiceCaptureError(RuntimeError):
    """Safe local microphone error for display in the activity bar."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Recorder:
    """Capture bounded mono PCM16 audio; import sounddevice only when started."""

    def __init__(self, max_seconds: int = 120, on_level: Callable[[float], None] | None = None):
        self.max_seconds = max(1, min(120, int(max_seconds)))
        self.on_level = on_level
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._limit = self.max_seconds * SAMPLE_RATE * 2
        self._lock = threading.Lock()
        self._stream: Any = None
        self._stopped = False

    def start(self) -> None:
        try:
            import sounddevice
        except ImportError as exc:
            raise VoiceCaptureError("voice_unavailable", "Install sounddevice to use the microphone") from exc

        def callback(indata, frames, _time_info, status):
            del frames, status
            chunk = bytes(memoryview(indata).cast("B"))
            with self._lock:
                remaining = max(0, self._limit - self._size)
                chunk = chunk[:remaining]
                if chunk:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
            if self.on_level and chunk:
                samples = memoryview(chunk).cast("h")
                rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
                self.on_level(min(1.0, rms))

        try:
            self._stream = sounddevice.RawInputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=0, callback=callback
            )
            self._stream.start()
        except Exception as exc:
            raise VoiceCaptureError("voice_no_input", "No microphone input device is available") from exc

    @property
    def duration(self) -> float:
        """Seconds of audio captured so far."""
        with self._lock:
            return self._size / (SAMPLE_RATE * 2)

    def snapshot(self) -> bytes:
        """A WAV of everything captured so far, leaving the recording running.

        Live previews re-send the growing recording (bounded by ``max_seconds``).
        """
        with self._lock:
            pcm = b"".join(self._chunks)
        return self._wav(pcm)

    @property
    def full(self) -> bool:
        with self._lock:
            return self._size >= self._limit

    def stop(self) -> bytes:
        if self._stopped:
            return self._wav(b"")
        self._stopped = True
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        with self._lock:
            pcm = b"".join(self._chunks)
            self._chunks.clear()
            self._size = 0
        return self._wav(pcm)

    @staticmethod
    def _wav(pcm: bytes) -> bytes:
        fmt = struct.pack("<HHIIHH", 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
        body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
        body += b"data" + struct.pack("<I", len(pcm)) + pcm
        return b"RIFF" + struct.pack("<I", len(body)) + body
