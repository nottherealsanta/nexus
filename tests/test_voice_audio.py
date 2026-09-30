"""Coverage for strict bounded PCM WAV handling."""

from __future__ import annotations

import struct

import pytest

from nexus.voice.audio import VoiceAudioError, parse_wav, silence


def wav(
    *, rate: int = 16_000, channels: int = 1, bits: int = 16, seconds: float = 0.25,
    audio_format: int = 1,
) -> bytes:
    align = channels * bits // 8
    frames = int(rate * seconds)
    pcm = bytes(frames * align)
    fmt = struct.pack("<HHIIHH", audio_format, channels, rate, rate * align, align, bits)
    body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_parse_valid_pcm_wav() -> None:
    raw = wav(seconds=0.5)
    info = parse_wav(raw)
    assert info.sample_rate == 16_000
    assert info.channels == 1
    assert info.bits_per_sample == 16
    assert info.frames == 8_000
    assert info.duration_s == 0.5
    assert len(info.pcm) == 16_000


@pytest.mark.parametrize(
    "raw",
    [wav(rate=44_100), wav(channels=2), wav(bits=8), wav(audio_format=3)],
)
def test_reject_unsupported_wav_format(raw: bytes) -> None:
    with pytest.raises(VoiceAudioError) as error:
        parse_wav(raw)
    assert error.value.code == "voice_bad_audio"


@pytest.mark.parametrize("raw", [b"", b"RIFF", wav()[:-1], b"NOPE" + wav()[4:]])
def test_reject_truncated_or_malformed_files(raw: bytes) -> None:
    with pytest.raises(VoiceAudioError) as error:
        parse_wav(raw)
    assert error.value.code == "voice_bad_audio"


def test_reject_file_over_size_limit() -> None:
    with pytest.raises(VoiceAudioError) as error:
        parse_wav(wav(seconds=1), max_seconds=0.25)
    assert error.value.code == "voice_too_long"


def test_reject_too_short_audio() -> None:
    with pytest.raises(VoiceAudioError) as error:
        parse_wav(wav(seconds=0.19))
    assert error.value.code == "voice_bad_audio"


def test_silence_builds_pcm_wav() -> None:
    info = parse_wav(silence(0.25))
    assert info.frames == 4_000
    assert info.duration_s == 0.25
    assert not any(info.pcm)
