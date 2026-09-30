"""Strict bounded PCM WAV handling for voice input (VOICE_PLAN.md §7).

This module is deliberately pure and dependency-free so callers can validate
transient audio before passing it to an inference engine.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from math import isfinite

SAMPLE_RATE = 16_000
CHANNELS = 1
BITS_PER_SAMPLE = 16
MIN_DURATION_S = 0.2
DEFAULT_MAX_SECONDS = 120.0
_BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * (BITS_PER_SAMPLE // 8)


class VoiceAudioError(ValueError):
    """Invalid voice audio, with a stable code suitable for host translation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PcmInfo:
    """Validated mono 16 kHz PCM samples and their measured duration."""

    sample_rate: int
    channels: int
    bits_per_sample: int
    frames: int
    duration_s: float
    pcm: bytes


def parse_wav(data: bytes, *, max_seconds: float = DEFAULT_MAX_SECONDS) -> PcmInfo:
    """Parse a bounded RIFF/WAVE PCM16 file, rejecting malformed input."""
    if not isinstance(data, bytes):
        raise VoiceAudioError("voice_bad_audio", "audio must be bytes")
    if not isfinite(max_seconds) or not 0 < max_seconds <= DEFAULT_MAX_SECONDS:
        raise ValueError("max_seconds must be between 0 and 120")
    max_bytes = 44 + int(_BYTES_PER_SECOND * max_seconds)
    if len(data) > max_bytes:
        raise VoiceAudioError("voice_too_long", "audio exceeds the size limit")
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise VoiceAudioError("voice_bad_audio", "not a RIFF/WAVE file")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size != len(data) - 8:
        raise VoiceAudioError("voice_bad_audio", "inconsistent or truncated RIFF size")

    fmt: tuple[int, int, int, int, int, int] | None = None
    pcm: bytes | None = None
    offset = 12
    while offset < len(data):
        if len(data) - offset < 8:
            raise VoiceAudioError("voice_bad_audio", "truncated WAV chunk header")
        chunk_id = data[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        start = offset + 8
        end = start + chunk_size
        padded_end = end + (chunk_size & 1)
        if end > len(data) or padded_end > len(data):
            raise VoiceAudioError("voice_bad_audio", "truncated WAV chunk")
        chunk = data[start:end]
        if chunk_id == b"fmt ":
            if fmt is not None or chunk_size < 16:
                raise VoiceAudioError("voice_bad_audio", "invalid WAV format chunk")
            fmt = struct.unpack_from("<HHIIHH", chunk)
        elif chunk_id == b"data":
            if pcm is not None:
                raise VoiceAudioError("voice_bad_audio", "multiple WAV data chunks")
            pcm = chunk
        offset = padded_end

    if fmt is None or pcm is None:
        raise VoiceAudioError("voice_bad_audio", "WAV is missing format or audio data")
    audio_format, channels, rate, byte_rate, block_align, bits = fmt
    if (audio_format, channels, rate, byte_rate, block_align, bits) != (
        1,
        CHANNELS,
        SAMPLE_RATE,
        _BYTES_PER_SECOND,
        2,
        BITS_PER_SAMPLE,
    ):
        raise VoiceAudioError("voice_bad_audio", "WAV must be mono 16 kHz PCM16")
    if len(pcm) % 2:
        raise VoiceAudioError("voice_bad_audio", "PCM16 data has an incomplete sample")
    frames = len(pcm) // 2
    duration = frames / SAMPLE_RATE
    if duration < MIN_DURATION_S:
        raise VoiceAudioError("voice_bad_audio", "audio is shorter than 0.2 seconds")
    if duration > max_seconds:
        raise VoiceAudioError("voice_too_long", "audio exceeds the duration limit")
    return PcmInfo(SAMPLE_RATE, CHANNELS, BITS_PER_SAMPLE, frames, duration, pcm)


def silence(seconds: float) -> bytes:
    """Build a valid PCM16 WAV containing bounded digital silence."""
    if not 0 <= seconds <= DEFAULT_MAX_SECONDS:
        raise ValueError("seconds must be between 0 and 120")
    frames = int(SAMPLE_RATE * seconds)
    pcm = bytes(frames * 2)
    fmt = struct.pack("<HHIIHH", 1, CHANNELS, SAMPLE_RATE, _BYTES_PER_SECOND, 2, BITS_PER_SAMPLE)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body
