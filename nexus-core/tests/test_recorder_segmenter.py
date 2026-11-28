"""Pure sample-offset segmentation for the recorder (plan 54, C2)."""

from __future__ import annotations

import math
import random
from array import array

import pytest

from nexus.recorder.segmenter import (
    MAX_FEED_BYTES,
    MIN_INFERENCE_SECONDS,
    PcmSegmenter,
    SegmenterConfig,
    SegmenterError,
    segment_pcm,
)

RATE = 16_000


def tone(seconds: float, *, amplitude: int = 12_000, frequency: float = 220.0) -> bytes:
    frames = round(seconds * RATE)
    samples = array(
        "h",
        (int(amplitude * math.sin(2 * math.pi * frequency * index / RATE)) for index in range(frames)),
    )
    return samples.tobytes()


def quiet(seconds: float) -> bytes:
    return bytes(round(seconds * RATE) * 2)


def noise(seconds: float, *, amplitude: int = 300, seed: int = 7) -> bytes:
    generator = random.Random(seed)
    frames = round(seconds * RATE)
    samples = array("h", (generator.randint(-amplitude, amplitude) for _ in range(frames)))
    return samples.tobytes()


def speech_like(seconds: float, *, amplitude: int = 12_000) -> bytes:
    """Syllabic 100 ms bursts with 50 ms gaps, so the energy contour is not flat."""
    period = tone(0.10, amplitude=amplitude) + quiet(0.05)
    frames = round(seconds * RATE)
    return (period * (frames // (len(period) // 2) + 1))[: frames * 2]


def mix(*parts: bytes) -> bytes:
    return b"".join(parts)


def feed_all(segmenter: PcmSegmenter, pcm: bytes, *, start_sample: int = 0) -> list:
    closed = []
    for offset in range(0, len(pcm), MAX_FEED_BYTES):
        piece = pcm[offset : offset + MAX_FEED_BYTES]
        closed.extend(segmenter.feed(piece, start_sample=start_sample + offset // 2))
    return closed


def test_silence_only_has_no_segments() -> None:
    segmenter = PcmSegmenter()
    assert segmenter.feed(quiet(2.0)) == []
    assert segmenter.finish() == []
    assert segmenter.next_segment_id == 0


def test_constant_background_noise_is_not_repeatedly_opened() -> None:
    segments = segment_pcm(noise(3.0, amplitude=500))
    assert segments == ()


def test_quiet_speech_over_noise_closes_on_silence() -> None:
    pcm = mix(quiet(0.3), noise(1.0, amplitude=120), speech_like(1.0, amplitude=1_200), quiet(1.0))
    segments = segment_pcm(pcm, config=SegmenterConfig(silence_s=0.6))

    assert len(segments) == 1
    segment = segments[0]
    assert segment.close_reason == "silence"
    assert 1.3 * RATE <= segment.start <= 1.4 * RATE
    assert 2.2 * RATE <= segment.end <= 2.4 * RATE
    assert not segment.too_short
    assert segment.window_start == segment.start


def test_two_utterances_are_separate_and_gaps_are_kept_in_saved_audio() -> None:
    pcm = mix(quiet(0.2), speech_like(0.8), quiet(0.9), speech_like(0.7), quiet(1.0))
    segments = segment_pcm(pcm, config=SegmenterConfig(silence_s=0.6))

    assert [segment.close_reason for segment in segments] == ["silence", "silence"]
    assert 0.15 * RATE <= segments[0].start <= 0.25 * RATE
    assert 1.0 * RATE <= segments[0].end <= 1.1 * RATE
    assert 1.8 * RATE <= segments[1].start <= 1.9 * RATE
    assert 2.5 * RATE <= segments[1].end <= 2.6 * RATE


def test_forced_boundaries_are_bounded_and_overlap_only_as_context() -> None:
    pcm = speech_like(22.0)
    config = SegmenterConfig(window_s=6.0, overlap_s=0.75, silence_s=0.6)
    segments = segment_pcm(pcm, config=config)

    assert [segment.start for segment in segments] == [0, 6 * RATE, 12 * RATE, 18 * RATE]
    assert [segment.end for segment in segments] == [6 * RATE, 12 * RATE, 18 * RATE, 22 * RATE]
    assert [segment.close_reason for segment in segments] == ["forced", "forced", "forced", "stop"]
    assert segments[1].window_start == 6 * RATE - config.overlap_samples
    assert segments[2].window_start == 12 * RATE - config.overlap_samples
    assert segments[-1].window_end == segments[-1].end
    for segment in segments:
        assert segment.window_start <= segment.start
        assert segment.window_end - segment.window_start <= config.window_samples + config.overlap_samples
        assert segment.samples <= config.window_samples


def test_preview_follows_durable_offsets_and_is_bounded() -> None:
    config = SegmenterConfig(preview_cadence_s=0.5, overlap_s=0.5, window_s=4.0)
    segmenter = PcmSegmenter(config)
    assert feed_all(segmenter, speech_like(3.0)) == []

    first = segmenter.preview(available_end=RATE // 2)
    assert first is not None
    assert (first.start, first.end, first.window_start, first.window_end) == (0, RATE // 2, 0, RATE // 2)

    assert segmenter.preview(available_end=RATE // 2) is None
    assert segmenter.preview(available_end=round(0.8 * RATE)) is None
    second = segmenter.preview(available_end=RATE)
    assert second is not None
    assert second.start == 0 and second.end == RATE

    # A preview never reads beyond the durable limit.
    assert segmenter.preview(available_end=RATE + 100) is None
    third = segmenter.preview(available_end=round(1.5 * RATE))
    assert third is not None and third.end <= round(1.5 * RATE)


def test_preview_after_forced_boundary_carries_overlap_context() -> None:
    config = SegmenterConfig(window_s=1.0, overlap_s=0.25, preview_cadence_s=0.25)
    segmenter = PcmSegmenter(config)
    closed = feed_all(segmenter, speech_like(2.5))
    assert closed and closed[0].close_reason == "forced"

    preview = segmenter.preview(available_end=segmenter.fed_samples)
    assert preview is not None
    assert preview.segment_id == closed[-1].segment_id + 1
    assert preview.window_start == closed[-1].end - config.overlap_samples
    assert preview.start == closed[-1].end
    assert preview.window_end - preview.window_start <= config.window_samples + config.overlap_samples


def test_tiny_tail_is_reported_and_padded_only_for_inference() -> None:
    pcm = mix(tone(0.04), quiet(0.04), tone(0.04))
    segmenter = PcmSegmenter()
    closed = segmenter.feed(pcm)
    closed.extend(segmenter.finish())

    assert len(closed) == 1
    segment = closed[0]
    assert segment.start == 0
    assert segment.end <= round(0.12 * RATE)
    assert segment.too_short
    start, end = segment.inference_bounds()
    assert start == segment.window_start
    assert end - start >= round(MIN_INFERENCE_SECONDS * RATE)
    assert segment.end < end  # only the inference copy is padded


def test_chunking_does_not_change_boundaries() -> None:
    pcm = mix(quiet(0.2), speech_like(3.0), quiet(0.9), speech_like(0.5), quiet(0.8))
    config = SegmenterConfig(window_s=2.0, overlap_s=0.5, silence_s=0.5)
    whole = segment_pcm(pcm, config=config)

    segmenter = PcmSegmenter(config)
    pieces = []
    for offset in range(0, len(pcm), 317 * 2):
        pieces.extend(segmenter.feed(pcm[offset : offset + 317 * 2], start_sample=offset // 2))
    pieces.extend(segmenter.finish())
    assert tuple(pieces) == whole


def test_noncontiguous_audio_is_rejected_not_silenced() -> None:
    segmenter = PcmSegmenter()
    segmenter.feed(b"\x00\x00" * 100, start_sample=0)
    with pytest.raises(SegmenterError) as caught:
        segmenter.feed(b"\x00\x00" * 100, start_sample=101)
    assert caught.value.code == "noncontiguous_audio"


def test_partial_sample_and_feed_after_finish_fail() -> None:
    segmenter = PcmSegmenter()
    segmenter.feed(b"\x00\x00" * 10 + b"\x00")
    with pytest.raises(SegmenterError) as caught:
        segmenter.finish()
    assert caught.value.code == "partial_sample"

    segmenter = PcmSegmenter()
    segmenter.feed(b"\x00\x00" * 10)
    segmenter.finish()
    with pytest.raises(SegmenterError) as caught:
        segmenter.feed(b"\x00\x00")
    assert caught.value.code == "segmenter_finished"


def test_finish_offset_must_match_fed_samples() -> None:
    segmenter = PcmSegmenter()
    segmenter.feed(b"\x00\x00" * 10)
    with pytest.raises(SegmenterError) as caught:
        segmenter.finish(end_sample=5)
    assert caught.value.code == "finish_offset_mismatch"


def test_stationary_sustained_tone_is_a_documented_energy_vad_miss() -> None:
    # A flat energy contour has no evidence of syllables. This is a limitation, not a speech detector claim.
    assert segment_pcm(tone(2.0, amplitude=12_000, frequency=200.0)) == ()


def test_very_quiet_speech_below_the_floor_is_not_claimed() -> None:
    assert segment_pcm(speech_like(2.0, amplitude=40)) == ()


def test_oversized_feed_is_rejected_before_copying_or_changing_state() -> None:
    segmenter = PcmSegmenter()
    with pytest.raises(SegmenterError) as caught:
        segmenter.feed(b"\x00\x00" * (MAX_FEED_BYTES // 2 + 1))
    assert caught.value.code == "feed_too_large"
    assert segmenter.fed_samples == 0
    assert segmenter.active_start is None


def test_resume_continues_segment_ids_from_the_resume_sample() -> None:
    config = SegmenterConfig(silence_s=0.5)
    first = PcmSegmenter(config)
    first_closed = feed_all(first, mix(speech_like(0.8), quiet(0.8)))
    first_closed.extend(first.finish())
    assert [segment.segment_id for segment in first_closed] == [0]
    resume_sample = first_closed[-1].end
    assert first.next_segment_id == 1

    resumed = PcmSegmenter(config, first_segment_id=first.next_segment_id)
    resumed_closed = feed_all(resumed, mix(speech_like(0.8), quiet(0.8)), start_sample=resume_sample)
    resumed_closed.extend(resumed.finish())
    assert [segment.segment_id for segment in resumed_closed] == [1]
    assert resumed_closed[0].start >= resume_sample
    assert resumed.next_segment_id == 2


def test_first_segment_id_is_validated_and_batch_wrapper_accepts_it() -> None:
    with pytest.raises(ValueError):
        PcmSegmenter(first_segment_id=-1)
    with pytest.raises(ValueError):
        PcmSegmenter(first_segment_id=True)
    segments = segment_pcm(mix(speech_like(0.8), quiet(0.8)), first_segment_id=40)
    assert [segment.segment_id for segment in segments] == [40]


def test_invalid_config_and_offsets_are_rejected() -> None:
    with pytest.raises(ValueError):
        SegmenterConfig(window_s=0.1)
    with pytest.raises(ValueError):
        SegmenterConfig(overlap_s=11.0)
    with pytest.raises(ValueError):
        SegmenterConfig(noise_ratio=0)
    segmenter = PcmSegmenter()
    with pytest.raises(SegmenterError) as caught:
        segmenter.feed(b"", start_sample=-1)
    assert caught.value.code == "invalid_offset"
