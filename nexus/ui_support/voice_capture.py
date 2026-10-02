"""Bounded 16 kHz microphone capture for TUI dictation (VOICE_PLAN.md §8.2)."""

from __future__ import annotations

import math
import os
import struct
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

SAMPLE_RATE = 16_000
_CUE_RATE = 44_100
_CUES = {"start": (660.0, 880.0), "stop": (880.0, 587.0)}  # rising on, falling off


def _cue_pcm(kind: str) -> bytes:
    """Two short soft notes as mono PCM16: rising when dictation starts, falling when it ends."""
    out = bytearray()
    for freq in _CUES[kind]:
        count = int(_CUE_RATE * 0.07)
        for i in range(count):
            envelope = math.sin(math.pi * i / count) ** 2
            out += struct.pack("<h", int(9000 * envelope * math.sin(2 * math.pi * freq * i / _CUE_RATE)))
    return bytes(out)


def play_cue(kind: str, *, wait: bool = False) -> None:
    """Best-effort start/stop sound; silent without an output device or with ``NEXUS_VOICE_SOUNDS=off``."""
    if os.environ.get("NEXUS_VOICE_SOUNDS", "").lower() in {"off", "0", "false"}:
        return

    def run() -> None:
        try:
            import sounddevice
            with sounddevice.RawOutputStream(samplerate=_CUE_RATE, channels=1, dtype="int16") as stream:
                stream.write(_cue_pcm(kind))
        except Exception:  # noqa: BLE001, S110 - a missing speaker must never break dictation
            pass

    if wait:
        run()
    else:
        threading.Thread(target=run, name="voice-cue", daemon=True).start()


class VoiceCaptureError(RuntimeError):
    """Safe local microphone error for display in the activity bar."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Recorder:
    """Capture bounded mono PCM16 audio; import sounddevice only when started."""

    def __init__(self, max_seconds: int = 120, on_level: Callable[[float], None] | None = None, *, cues: bool = True):
        self.cues = cues
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

        if self.cues:
            play_cue("start", wait=True)  # finished before the microphone opens, so it is not recorded

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
            if self.cues:
                play_cue("stop")
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
