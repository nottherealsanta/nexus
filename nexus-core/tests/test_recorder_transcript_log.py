"""Durable bounded transcript log (plan 54, C3b)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tracemalloc
from dataclasses import replace
from pathlib import Path

import pytest

import nexus.recorder.transcript_log as transcript_log_module
from nexus.recorder.store import RecorderCorrupt, RecorderInvalid, RecorderNotFound, RecorderSecurityError
from nexus.recorder.transcript import TranscriptSegment
from nexus.recorder.transcript_log import (
    REPLAY_PAGE_THRESHOLD,
    TranscriptLog,
)

RATE = 16_000


def private_dir(tmp_path: Path, name: str = "rec") -> Path:
    recording = tmp_path / name
    recording.mkdir(mode=0o700)
    return recording


def make_log(tmp_path: Path) -> tuple[TranscriptLog, Path]:
    recording = private_dir(tmp_path)
    return TranscriptLog(recording / "transcript"), recording


def seg_record(
    segment_id: int,
    start: int,
    end: int,
    *,
    generation: int = 0,
    window_start: int | None = None,
    window_end: int | None = None,
    close_reason: str = "silence",
    final_revision: int = 1,
) -> dict:
    return {
        "v": 1,
        "k": "seg",
        "generation": generation,
        "segment_id": segment_id,
        "start": start,
        "end": end,
        "window_start": start if window_start is None else window_start,
        "window_end": end if window_end is None else window_end,
        "close_reason": close_reason,
        "final_revision": final_revision,
    }


def rev_record(
    segment_id: int,
    revision: int,
    *,
    start: int,
    end: int,
    text: str,
    status: str = "final",
    attempt: int = 1,
    generation: int = 0,
    window_start: int | None = None,
    token: str | None = None,
) -> dict:
    document = TranscriptSegment(
        segment_id=segment_id,
        revision=revision,
        start=start,
        end=end,
        status=status,
        raw_text=text,
        attempt=attempt,
        generation=generation,
        window_start=window_start,
        retranscription_token=token,
    ).as_document()
    document.update({"v": 1, "k": "rev"})
    return document


def end_record(end_sample: int, reason: str = "stopped", *, generation: int = 0) -> dict:
    return {"v": 1, "k": "end", "generation": generation, "end_sample": end_sample, "reason": reason}


def resume_record(from_sample: int, *, generation: int = 0) -> dict:
    return {"v": 1, "k": "resume", "generation": generation, "from_sample": from_sample}


def encode(document: dict) -> bytes:
    return (json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def raw_log(path: Path, data: bytes) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    target = path / "log.jsonl"
    target.write_bytes(data)
    os.chmod(target, 0o600)


def raw_append(path: Path, records: list[dict]) -> None:
    target = path / "log.jsonl"
    target.write_bytes(target.read_bytes() + b"".join(encode(record) for record in records))
    os.chmod(target, 0o600)


def pairs(count: int, *, start: int = 0) -> list[dict]:
    records: list[dict] = []
    for segment_id in range(start, start + count):
        begin = segment_id * 1000
        records.append(seg_record(segment_id, begin, begin + 1000))
        records.append(rev_record(segment_id, 1, start=begin, end=begin + 1000, text=f"word{segment_id}"))
    return records


def test_append_replay_page_and_exports(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    assert isinstance(log, TranscriptLog)
    log.append([seg_record(0, 0, 1000), rev_record(0, 1, start=0, end=1000, text="hello")])
    log.append(seg_record(1, 1000, 2000, window_start=900))
    log.append(rev_record(1, 1, start=1000, end=2000, text="world", window_start=900))
    end = log.append(end_record(2000))
    size = (recording / "transcript" / "log.jsonl").stat().st_size
    assert end == size

    transcript, cursors = log.replay(None)
    assert transcript.text() == "hello world"
    assert [segment.segment_id for segment in transcript.current_segments] == [0, 1]
    assert cursors.generation == 0
    assert cursors.log_offset == 0
    assert cursors.end_offset == size
    assert cursors.counts.final == 2
    assert cursors.counts.pending == 0
    assert cursors.transcript_revision == 2
    assert cursors.final_cursor is None
    assert cursors.segmentation.next_segment_id == 2
    assert cursors.segmentation.ended_at == 2000
    assert cursors.segmentation.resume_sample == 2000
    assert transcript.checkpoint().paged_through == -1

    page = log.read_page(-1, 10)
    assert [segment["segment_id"] for segment in page.segments] == [0, 1]
    assert [segment["status"] for segment in page.segments] == ["final", "final"]
    assert page.next_after == 1
    assert log.read_page(1, 10).segments == ()
    assert log.read_page(1, 10).next_after is None

    assert "".join(log.export_txt()) == "hello world"
    exported = json.loads("".join(log.export_json()))
    assert exported["complete"] is True
    assert [segment["segment_id"] for segment in exported["segments"]] == [0, 1]

    segments = list(log.iter_seg())
    assert [record.document["segment_id"] for record in segments] == [0, 1]
    assert segments[0].offset == 0
    assert segments[1].offset > segments[0].end_offset


def test_pending_segment_and_final_cursor(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(seg_record(0, 0, 1000))
    log.append(rev_record(0, 1, start=0, end=1000, text="done"))
    log.append(seg_record(1, 1000, 2000))

    transcript, cursors = log.replay(None)
    assert transcript.text() == "done"
    assert cursors.counts.final == 1
    assert cursors.counts.pending == 1
    assert cursors.final_cursor is not None
    assert cursors.final_cursor.segment_id == 1
    second = next(record for record in log.iter_seg() if record.document["segment_id"] == 1)
    assert cursors.final_cursor.log_offset == second.offset

    page = log.read_page(-1, 10)
    assert page.segments[1] == {"segment_id": 1, "status": "pending", "start": 1000, "end": 2000}
    assert "".join(log.export_txt()) == "done"
    exported = json.loads("".join(log.export_json()))
    assert exported["complete"] is False
    assert exported["segments"][1]["status"] == "pending"


def test_crash_truncation_at_every_byte_of_last_line(tmp_path: Path) -> None:
    records = [
        seg_record(0, 0, 1000),
        rev_record(0, 1, start=0, end=1000, text="hello"),
        seg_record(1, 1000, 2000),
    ]
    last = rev_record(1, 1, start=1000, end=2000, text="hello world")
    prefix = b"".join(encode(record) for record in records)
    line = encode(last)

    for cut in range(len(line) + 1):
        recording = private_dir(tmp_path, f"cut{cut}")
        path = recording / "transcript"
        raw_log(path, prefix + line[:cut])
        transcript, _cursors = TranscriptLog(path).replay(None)
        assert transcript.text() in {"hello", "hello hello world"}
        if cut < len(line):
            assert transcript.text() == "hello"
            # Replay truncates the crash tail so a later append starts at a record boundary.
            assert (path / "log.jsonl").stat().st_size == len(prefix)
        else:
            assert transcript.text() == "hello hello world"


def test_checkpoint_plus_tail_replay_equals_full_replay(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(REPLAY_PAGE_THRESHOLD + 44))
    _transcript, cursors = log.replay(None)
    assert cursors.log_offset > 0
    checkpoint = cursors.as_checkpoint("live")
    log.write_checkpoint(checkpoint)
    stored = log.read_checkpoint()
    assert stored is not None
    assert replace(stored, body_sha256=None, prefix_sha256=None) == checkpoint
    assert stored.body_sha256 is not None and stored.prefix_sha256 is not None

    log.append(pairs(20, start=REPLAY_PAGE_THRESHOLD + 44))
    log.append(end_record((REPLAY_PAGE_THRESHOLD + 64) * 1000))

    tail_transcript, tail_cursors = log.replay()
    full_transcript, full_cursors = log.replay(None)

    assert tail_transcript.text() == full_transcript.text()
    assert tail_transcript.paged_through == full_transcript.paged_through
    assert len(tail_transcript) <= REPLAY_PAGE_THRESHOLD
    assert tail_cursors.counts == full_cursors.counts
    assert tail_cursors.transcript_revision == full_cursors.transcript_revision
    assert tail_cursors.segmentation == full_cursors.segmentation
    assert tail_cursors.final_cursor == full_cursors.final_cursor
    assert tail_cursors.log_offset == full_cursors.log_offset
    assert tail_cursors.end_offset == full_cursors.end_offset


def test_duplicates_are_idempotent_and_conflicts_are_corruption(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    first = seg_record(0, 0, 1000)
    old = rev_record(0, 1, start=0, end=1000, text="first")
    new = rev_record(0, 2, start=0, end=1000, text="second", token="retry")
    log.append([first, old, new, old, new, first])

    transcript, cursors = log.replay(None)
    assert transcript.text() == "second"
    assert cursors.transcript_revision == 2
    assert cursors.counts.final == 1

    # The at-least-once duplicate text is replayed through the live revision ring.
    log.append([old, new])
    transcript, cursors = log.replay(None)
    assert transcript.text() == "second"
    assert cursors.transcript_revision == 2

    conflicting = rev_record(0, 2, start=0, end=1000, text="different")
    (tmp_path / "conflict").mkdir()
    log2_log, conflict_recording = make_log(tmp_path / "conflict")
    with pytest.raises(RecorderInvalid):
        log2_log.append([first, new, conflicting])
    assert not (conflict_recording / "transcript").exists()

    # A conflicting log written by an older/rogue writer is still corruption at replay time.
    conflict_path = conflict_recording / "transcript"
    raw_log(conflict_path, b"".join(encode(record) for record in [first, new, conflicting]))
    with pytest.raises(RecorderCorrupt) as caught:
        TranscriptLog(conflict_path).replay(None)
    assert caught.value.code == "transcript_corrupt"


@pytest.mark.parametrize(
    "records",
    [
        [rev_record(0, 1, start=0, end=1000, text="orphan")],
        [seg_record(0, 0, 1000), seg_record(2, 2000, 3000)],
        [seg_record(0, 0, 1000, generation=0), rev_record(0, 1, start=0, end=1000, text="x", generation=1)],
    ],
)
def test_strict_corruption(tmp_path: Path, records: list[dict]) -> None:
    recording = private_dir(tmp_path)
    path = recording / "transcript"
    raw_log(path, b"".join(encode(record) for record in records))
    with pytest.raises(RecorderCorrupt) as caught:
        TranscriptLog(path).replay(None)
    assert caught.value.code == "transcript_corrupt"


def test_read_views_reject_mixed_generations(tmp_path: Path) -> None:
    recording = private_dir(tmp_path)
    path = recording / "transcript"
    raw_log(
        path,
        b"".join(
            encode(record)
            for record in [
                seg_record(0, 0, 1000),
                rev_record(0, 1, start=0, end=1000, text="x"),
                end_record(1000, generation=1),
            ]
        ),
    )
    log = TranscriptLog(path)
    with pytest.raises(RecorderCorrupt):
        log.read_page(-1, 10)
    with pytest.raises(RecorderCorrupt):
        list(log.export_txt())
    with pytest.raises(RecorderCorrupt):
        list(log.export_json())


def test_malformed_middle_line_fails_and_blocks_appends(tmp_path: Path) -> None:
    recording = private_dir(tmp_path)
    path = recording / "transcript"
    data = encode(seg_record(0, 0, 1000)) + b'{"v":1,"k":\n' + encode(seg_record(1, 1000, 2000))
    raw_log(path, data)
    log = TranscriptLog(path)

    with pytest.raises(RecorderCorrupt) as caught:
        log.replay(None)
    assert caught.value.code == "transcript_corrupt"
    with pytest.raises(RecorderCorrupt):
        log.read_page(-1, 10)
    with pytest.raises(RecorderCorrupt):
        list(log.iter_seg())
    with pytest.raises(RecorderCorrupt):
        log.append(rev_record(0, 1, start=0, end=1000, text="x"))

    # A fresh instance sees the same corruption rather than silently repairing it.
    with pytest.raises(RecorderCorrupt):
        TranscriptLog(path).replay(None)
    assert (path / "log.jsonl").read_bytes() == data


def test_replay_truncates_partial_tail_but_reads_ignore_it(tmp_path: Path) -> None:
    recording = private_dir(tmp_path)
    path = recording / "transcript"
    complete = encode(seg_record(0, 0, 1000))
    raw_log(path, complete + encode(seg_record(1, 1000, 2000))[:20])
    log = TranscriptLog(path)

    page = log.read_page(-1, 10)
    assert [segment["status"] for segment in page.segments] == ["pending"]
    assert "".join(log.export_txt()) == ""
    assert (path / "log.jsonl").stat().st_size == len(complete) + 20

    transcript, cursors = log.replay(None)
    assert len(transcript) == 0
    assert cursors.counts.pending == 1
    assert (path / "log.jsonl").stat().st_size == len(complete)
    log.append(rev_record(0, 1, start=0, end=1000, text="after crash"))
    transcript, _cursors = log.replay(None)
    assert transcript.text() == "after crash"


def test_append_after_recording_deletion_is_refused_and_does_not_recreate(tmp_path: Path) -> None:
    recording = private_dir(tmp_path)
    log = TranscriptLog(lambda: recording / "transcript")
    log.append(seg_record(0, 0, 1000))
    log.write_checkpoint(
        log.replay(None)[1].as_checkpoint("live"),
    )
    shutil.rmtree(recording)

    with pytest.raises(RecorderNotFound):
        log.append(seg_record(1, 1000, 2000))
    assert not recording.exists()
    assert not (recording / "transcript").exists()

    with pytest.raises(RecorderNotFound):
        log.replay(None)
    with pytest.raises(RecorderNotFound):
        log.read_page(-1, 10)
    with pytest.raises(RecorderNotFound):
        list(log.export_txt())


def test_private_modes_and_symlink_safety(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    log.append(seg_record(0, 0, 1000))
    log.append(rev_record(0, 1, start=0, end=1000, text="private"))
    log.write_checkpoint(log.replay(None)[1].as_checkpoint("complete"))

    transcript_dir = recording / "transcript"
    assert stat.S_IMODE(transcript_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((transcript_dir / "log.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((transcript_dir / "checkpoint.json").stat().st_mode) == 0o600

    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"untouched\n")
    os.chmod(outside, 0o600)
    symlink_recording = private_dir(tmp_path, "linked")
    (symlink_recording / "transcript").symlink_to(transcript_dir)
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(symlink_recording / "transcript").append(seg_record(1, 1000, 2000))
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(symlink_recording / "transcript").replay(None)
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(symlink_recording / "transcript").read_page(-1, 10)

    file_link_recording = private_dir(tmp_path, "filelink")
    file_link_dir = file_link_recording / "transcript"
    file_link_dir.mkdir(mode=0o700)
    (file_link_dir / "log.jsonl").symlink_to(outside)
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(file_link_dir).append(seg_record(0, 0, 1000))
    assert outside.read_bytes() == b"untouched\n"

    wrong_mode_recording = private_dir(tmp_path, "wrongmode")
    wrong_mode_dir = wrong_mode_recording / "transcript"
    wrong_mode_dir.mkdir(mode=0o700)
    wrong_mode_log = wrong_mode_dir / "log.jsonl"
    wrong_mode_log.write_bytes(encode(seg_record(0, 0, 1000)))
    os.chmod(wrong_mode_log, 0o644)
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(wrong_mode_dir).replay(None)

    fifo_recording = private_dir(tmp_path, "fifo")
    fifo_dir = fifo_recording / "transcript"
    fifo_dir.mkdir(mode=0o700)
    os.mkfifo(fifo_dir / "log.jsonl")
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(fifo_dir).replay(None)


def test_invalid_batch_writes_nothing(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    path = recording / "transcript"
    with pytest.raises(RecorderInvalid):
        log.append([seg_record(0, 0, 1000), {"v": 1, "k": "unknown"}])
    assert not path.exists()
    with pytest.raises(RecorderInvalid):
        log.append(seg_record(0, 0, 1000) | {"start": 100, "end": 50})
    with pytest.raises(RecorderInvalid):
        log.append(end_record(1000, reason="expired"))
    assert not path.exists()


def test_invalid_checkpoint_falls_back_to_full_replay(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    log.append(pairs(REPLAY_PAGE_THRESHOLD + 10))
    _transcript, cursors = log.replay(None)
    checkpoint = cursors.as_checkpoint("live")
    log.write_checkpoint(checkpoint)

    checkpoint_path = recording / "transcript" / "checkpoint.json"
    checkpoint_path.write_bytes(b"not json\n")
    os.chmod(checkpoint_path, 0o600)
    with pytest.raises(RecorderCorrupt):
        log.read_checkpoint()
    transcript, fresh = log.replay()
    assert "".join(log.export_txt()).startswith("word0")
    assert len(transcript) <= REPLAY_PAGE_THRESHOLD
    assert fresh.log_offset > 0
    assert fresh.counts == cursors.counts

    # A structurally valid checkpoint with altered fields no longer matches its body seal.
    log.write_checkpoint(checkpoint)
    stored = log.read_checkpoint()
    assert stored is not None
    altered = stored.as_document()
    altered["counts"]["final"] += 1
    checkpoint_path.write_bytes((json.dumps(altered, sort_keys=True) + "\n").encode("utf-8"))
    os.chmod(checkpoint_path, 0o600)
    with pytest.raises(RecorderCorrupt):
        log.read_checkpoint()
    transcript, cursors = log.replay()
    assert len(transcript) <= REPLAY_PAGE_THRESHOLD
    assert cursors.counts.final == REPLAY_PAGE_THRESHOLD + 10

    # A checkpoint without seals is structurally readable but is never trusted for a tail replay.
    unsealed = checkpoint.as_document()
    checkpoint_path.write_bytes((json.dumps(unsealed, sort_keys=True) + "\n").encode("utf-8"))
    os.chmod(checkpoint_path, 0o600)
    assert log.read_checkpoint() is not None
    _transcript, fresh = log.replay()
    assert fresh.counts.final == REPLAY_PAGE_THRESHOLD + 10

    # An altered log prefix invalidates the checkpoint's prefix seal, so replay uses the log.
    log.write_checkpoint(checkpoint)
    log_path = recording / "transcript" / "log.jsonl"
    data = log_path.read_bytes()
    assert b"word0" in data
    log_path.write_bytes(data.replace(b"word0", b"changed0"))
    os.chmod(log_path, 0o600)
    _transcript, fresh = log.replay()
    assert "".join(log.export_txt()).startswith("changed0")
    assert fresh.counts.final == REPLAY_PAGE_THRESHOLD + 10


def test_end_and_resume_segmentation_state(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append([seg_record(0, 0, 1000), rev_record(0, 1, start=0, end=1000, text="one")])
    log.append(end_record(1000, "interrupted"))
    log.append({"v": 1, "k": "resume", "from_sample": 4000})
    log.append([seg_record(1, 4000, 5000), rev_record(1, 1, start=4000, end=5000, text="two")])
    log.append(end_record(5000, "stopped"))

    transcript, cursors = log.replay(None)
    assert transcript.text() == "one two"
    assert cursors.segmentation == cursors.segmentation.__class__(
        next_segment_id=2, resume_sample=5000, ended_at=5000
    )


def test_retranscription_revision_replays_with_its_token(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    token = "retry-token"
    log.append(seg_record(0, 0, 1000))
    log.append(rev_record(0, 1, start=0, end=1000, text="original"))
    log.append(rev_record(0, 2, start=0, end=1000, text="corrected", token=token))

    transcript, cursors = log.replay(None)
    assert transcript.text() == "corrected"
    assert cursors.transcript_revision == 2
    assert log.read_page(-1, 10).segments[0]["revision"] == 2


def test_retranscription_of_a_paged_segment_is_visible_and_replayable(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(REPLAY_PAGE_THRESHOLD + 10))
    _transcript, cursors = log.replay(None)
    assert cursors.log_offset > 0
    log.write_checkpoint(cursors.as_checkpoint("live"))

    late = rev_record(0, 2, start=0, end=1000, text="late correction", token="retry")
    log.append(late)
    log.append(late)
    transcript, tail_cursors = log.replay()
    assert tail_cursors.transcript_revision == cursors.transcript_revision + 1
    assert len(transcript) <= REPLAY_PAGE_THRESHOLD
    page = log.read_page(-1, 1)
    assert page.segments[0]["revision"] == 2
    assert page.segments[0]["text"] == "late correction"
    assert "".join(log.export_txt()).startswith("late correction")

    log.write_checkpoint(tail_cursors.as_checkpoint("live"))
    _transcript, again = log.replay()
    assert again.transcript_revision == tail_cursors.transcript_revision


def test_read_page_recomputes_join_flags_and_uses_latest_revision(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(seg_record(0, 0, 1000))
    log.append(rev_record(0, 1, start=0, end=1000, text="alpha beta gamma"))
    log.append(seg_record(1, 1000, 2000, window_start=900))
    log.append(rev_record(1, 1, start=1000, end=2000, text="older", window_start=900))
    log.append(rev_record(1, 2, start=1000, end=2000, text="beta gamma delta", window_start=900, token="retry"))

    page = log.read_page(0, 1)
    assert page.next_after == 1
    assert len(page.segments) == 1
    segment = page.segments[0]
    assert segment["segment_id"] == 1
    assert segment["revision"] == 2
    assert segment["text"] == "beta gamma delta"
    assert segment["uncertain_join"] is True
    assert segment["join"] == "ambiguous_overlap"


def test_read_page_memory_is_bounded_on_7200_segments(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(7200))
    for segment_id in range(3601, 3651):
        begin = segment_id * 1000
        log.append(rev_record(segment_id, 2, start=begin, end=begin + 1000, text=f"latest{segment_id}", token="retry"))

    tracemalloc.start()
    page = log.read_page(3600, 50)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert [segment["segment_id"] for segment in page.segments] == list(range(3601, 3651))
    assert page.next_after == 3650
    assert all(segment["revision"] == 2 for segment in page.segments)
    assert peak < 2_000_000, f"read_page peak {peak} bytes"


def test_replay_memory_and_active_set_are_bounded(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(4000))

    tracemalloc.start()
    transcript, cursors = log.replay(None)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert [segment.segment_id for segment in transcript.current_segments][-1] == 3999
    assert "".join(log.export_txt()).startswith("word0 ")
    assert "".join(log.export_txt()).endswith("word3999")
    assert len(transcript) <= REPLAY_PAGE_THRESHOLD
    assert cursors.counts.final == 4000
    assert cursors.final_cursor is None
    assert peak < 8_000_000, f"replay peak {peak} bytes"


def test_iter_seg_from_offset_and_page_limit_validation(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(3))
    records = list(log.iter_seg())
    assert [record.document["segment_id"] for record in records] == [0, 1, 2]
    streamed = list(log.iter_seg(records[1].offset))
    assert [record.document["segment_id"] for record in streamed] == [1, 2]
    with pytest.raises(RecorderInvalid):
        list(log.iter_seg(records[1].offset + 1))

    assert log.read_page(2, 10).segments == ()
    with pytest.raises(RecorderInvalid):
        log.read_page(-1, 0)
    with pytest.raises(RecorderInvalid):
        log.read_page(-1, 201)
    with pytest.raises(RecorderInvalid):
        log.read_page(-2, 10)


def test_checkpoint_replacement_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log, recording = make_log(tmp_path)
    log.append(pairs(2))
    first = log.replay(None)[1].as_checkpoint("live")
    log.write_checkpoint(first)
    before = (recording / "transcript" / "checkpoint.json").read_bytes()

    def fail_replace(*_args, **_kwargs) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr("nexus.recorder.transcript_log.os.replace", fail_replace)
    with pytest.raises(OSError):
        log.write_checkpoint(first.as_document() | {"state": "catching_up"})
    monkeypatch.undo()

    assert (recording / "transcript" / "checkpoint.json").read_bytes() == before
    leftovers = [path for path in (recording / "transcript").iterdir() if path.name.startswith(".checkpoint.json.")]
    assert leftovers == []
    stored = log.read_checkpoint()
    assert stored is not None
    assert replace(stored, body_sha256=None, prefix_sha256=None) == first


def test_unsafe_directory_modes_are_rejected(tmp_path: Path) -> None:
    recording = tmp_path / "rec"
    recording.mkdir(mode=0o700)
    transcript_dir = recording / "transcript"
    transcript_dir.mkdir(mode=0o700)
    os.chmod(transcript_dir, 0o755)
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(transcript_dir).append(seg_record(0, 0, 1000))

    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(recording)
    with pytest.raises(RecorderSecurityError):
        TranscriptLog(linked_parent / "transcript").append(seg_record(0, 0, 1000))


def test_replay_restores_segment_final_revision_floors(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(
        [
            seg_record(0, 0, 1000, final_revision=5),
            rev_record(0, 1, start=0, end=1000, text="one"),
            seg_record(1, 1000, 2000, final_revision=9),
        ]
    )
    transcript, cursors = log.replay(None)
    assert cursors.final_cursor is not None and cursors.final_cursor.segment_id == 1
    assert transcript.next_revision(0) == 6
    assert transcript.next_revision(1) == 10


def test_replay_pending_backlog_memory_is_compact(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    records = [seg_record(segment_id, segment_id * 100, (segment_id + 1) * 100) for segment_id in range(20_000)]
    log.append(records)

    tracemalloc.start()
    transcript, cursors = log.replay(None)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(transcript) == 0
    assert cursors.counts.pending == 20_000
    assert cursors.final_cursor is not None
    assert cursors.final_cursor.segment_id == 0
    assert cursors.final_cursor.log_offset == 0
    assert peak < 4_000_000, f"pending replay peak {peak} bytes"


def test_append_rejects_orphans_gaps_and_out_of_order_revisions(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    with pytest.raises(RecorderInvalid):
        log.append(rev_record(0, 1, start=0, end=1000, text="orphan"))
    assert not (recording / "transcript").exists()

    with pytest.raises(RecorderInvalid):
        log.append([rev_record(0, 1, start=0, end=1000, text="orphan"), seg_record(0, 0, 1000)])
    with pytest.raises(RecorderInvalid):
        log.append([seg_record(0, 0, 1000), seg_record(2, 2000, 3000)])
    assert not (recording / "transcript").exists()

    log.append([seg_record(0, 0, 1000), rev_record(0, 1, start=0, end=1000, text="first")])
    with pytest.raises(RecorderInvalid):
        log.append(rev_record(0, 2, start=0, end=1000, text="silent rewrite"))
    log.append(rev_record(0, 2, start=0, end=1000, text="explicit retry", token="retry"))
    with pytest.raises(RecorderInvalid):
        log.append(seg_record(2, 2000, 3000))
    with pytest.raises(RecorderInvalid):
        log.append(rev_record(5, 1, start=0, end=1000, text="orphan"))
    with pytest.raises(RecorderInvalid):
        log.append(rev_record(0, 1, start=0, end=1000, text="conflict"))
    with pytest.raises(RecorderInvalid):
        log.append(rev_record(0, 0, start=0, end=1000, text="invented older"))
    log.append(seg_record(1, 1000, 2000))
    transcript, _cursors = log.replay(None)
    assert transcript.text() == "explicit retry"


def test_append_retry_batch_is_idempotent(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    batch = [seg_record(0, 0, 1000), rev_record(0, 1, start=0, end=1000, text="once")]
    log.append(batch)
    log.append(batch)
    transcript, cursors = log.replay(None)
    assert transcript.text() == "once"
    assert cursors.transcript_revision == 1
    assert cursors.counts.final == 1


def test_paged_late_retry_dedup_survives_cache_eviction(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(600))
    paged_through = log.replay(None)[0].paged_through
    assert paged_through > 256
    log.write_checkpoint(log.replay(None)[1].as_checkpoint("live"))

    late = rev_record(0, 2, start=0, end=1000, text="late", token="retry")
    log.append(late)
    for segment_id in range(1, paged_through + 1):
        document = rev_record(
            segment_id,
            2,
            start=segment_id * 1000,
            end=(segment_id + 1) * 1000,
            text=f"late{segment_id}",
            token="retry",
        )
        log.append(document)
    log.append(late)

    _transcript, tail = log.replay()
    assert tail.transcript_revision == 600 + paged_through + 1
    assert log.read_page(-1, 1).segments[0]["revision"] == 2
    assert log.read_page(-1, 1).segments[0]["text"] == "late"
    log.write_checkpoint(tail.as_checkpoint("live"))
    _transcript, again = log.replay()
    assert again.transcript_revision == tail.transcript_revision


def test_exports_stream_with_bounded_memory(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(7200))

    tracemalloc.start()
    total = 0
    first = None
    last = None
    for fragment in log.export_txt():
        total += len(fragment)
        if first is None:
            first = fragment
        last = fragment
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert first == "word0"
    assert last == "word7199"
    assert total > 7200
    assert peak < 4_000_000, f"export peak {peak} bytes"

    small_root = tmp_path / "small"
    small_root.mkdir()
    small, _recording = make_log(small_root)
    small.append(pairs(3))
    exported = json.loads("".join(small.export_json()))
    assert exported["segments"][0]["text"] == "word0"
    assert exported["complete"] is False


def test_new_log_creation_fsyncs_the_transcript_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log, recording = make_log(tmp_path)
    transcript_dir = recording / "transcript"
    calls: list[Path] = []
    original = transcript_log_module._fsync_dir

    def record(path: Path) -> None:
        calls.append(Path(path))
        original(path)

    monkeypatch.setattr(transcript_log_module, "_fsync_dir", record)
    log.append(seg_record(0, 0, 1000))
    assert transcript_dir in calls
    calls.clear()
    log.append(rev_record(0, 1, start=0, end=1000, text="durable"))
    assert transcript_dir not in calls


def test_replay_rejects_forged_stale_paged_revision(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    log.append(pairs(REPLAY_PAGE_THRESHOLD + 20))
    _transcript, cursors = log.replay(None)
    log.write_checkpoint(cursors.as_checkpoint("live"))
    transcript_dir = recording / "transcript"

    raw_append(
        transcript_dir,
        [
            rev_record(0, 2, start=0, end=1000, text="late", token="retry"),
            rev_record(0, 1, start=0, end=1000, text="forged stale"),
        ],
    )
    with pytest.raises(RecorderCorrupt) as caught:
        log.replay()
    assert caught.value.code == "transcript_corrupt"


def test_read_page_rejects_off_page_stale_revision_and_duplicate_segment(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    log.append(pairs(10))
    transcript_dir = recording / "transcript"
    raw_append(transcript_dir, [rev_record(0, 1, start=0, end=1000, text="forged stale")])
    with pytest.raises(RecorderCorrupt) as caught:
        log.read_page(5, 2)
    assert caught.value.code == "transcript_corrupt"

    second_root = tmp_path / "second"
    second_root.mkdir()
    second_log, second_recording = make_log(second_root)
    second_log.append(pairs(10))
    raw_append(
        second_recording / "transcript",
        [seg_record(0, 500, 900)],
    )
    with pytest.raises(RecorderCorrupt):
        second_log.read_page(5, 2)


def test_final_revision_above_u32_is_supported_and_above_u64_is_controlled(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    floor = (1 << 32) + 5
    log.append([seg_record(0, 0, 1000, final_revision=floor), rev_record(0, 1, start=0, end=1000, text="wide")])
    transcript, _cursors = log.replay(None)
    assert transcript.next_revision(0) == floor + 1

    with pytest.raises(RecorderInvalid):
        log.append(seg_record(1, 1000, 2000, final_revision=1 << 64))
    with pytest.raises(RecorderInvalid):
        log.append(rev_record(0, 1 << 64, start=0, end=1000, text="too large"))


def test_large_late_retry_workload_stays_bounded(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    base = 1500
    log.append(pairs(base))
    paged = log.replay(None)[0].paged_through
    assert paged > 256
    log.write_checkpoint(log.replay(None)[1].as_checkpoint("live"))

    transcript_dir = recording / "transcript"
    raw_append(
        transcript_dir,
        [
            rev_record(
                segment_id,
                2,
                start=segment_id * 1000,
                end=(segment_id + 1) * 1000,
                text=f"late{segment_id}",
                token="retry",
            )
            for segment_id in range(paged + 1)
        ],
    )

    tracemalloc.start()
    transcript, cursors = log.replay()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(transcript) <= REPLAY_PAGE_THRESHOLD
    assert cursors.transcript_revision == base + paged + 1
    assert log.read_page(-1, 1).segments[0]["revision"] == 2
    assert peak < 4_000_000, f"late replay peak {peak} bytes"


def test_summary_does_not_let_a_stale_revision_override_latest_status(tmp_path: Path) -> None:
    log, _recording = make_log(tmp_path)
    first_error = rev_record(0, 1, start=0, end=1000, text="first error", status="error")
    final = rev_record(0, 2, start=0, end=1000, text="final")
    log.append([seg_record(0, 0, 1000), first_error, final, first_error])
    _transcript, cursors = log.replay(None)
    assert cursors.counts == cursors.counts.__class__(final=1, error=0, skipped=0, pending=0)
    assert cursors.transcript_revision == 2


def test_append_state_rejects_forged_stale_revision_without_exact_record(tmp_path: Path) -> None:
    recording = private_dir(tmp_path)
    path = recording / "transcript"
    raw_log(
        path,
        b"".join(
            encode(record)
            for record in [
                seg_record(0, 0, 1000),
                rev_record(0, 2, start=0, end=1000, text="new"),
                rev_record(0, 1, start=0, end=1000, text="forged stale"),
            ]
        ),
    )
    log = TranscriptLog(path)
    with pytest.raises(RecorderCorrupt) as caught:
        log.append(seg_record(1, 1000, 2000))
    assert caught.value.code == "transcript_corrupt"


def test_read_page_rejects_untokened_final_rewrite_and_accepts_tokened_retry(tmp_path: Path) -> None:
    log, recording = make_log(tmp_path)
    log.append([seg_record(0, 0, 1000), rev_record(0, 1, start=0, end=1000, text="original")])
    raw_append(recording / "transcript", [rev_record(0, 2, start=0, end=1000, text="silent rewrite")])
    with pytest.raises(RecorderCorrupt) as caught:
        log.read_page(-1, 1)
    assert caught.value.code == "transcript_corrupt"

    retry_root = tmp_path / "retry"
    retry_root.mkdir()
    retry_log, retry_recording = make_log(retry_root)
    retry_log.append([seg_record(0, 0, 1000), rev_record(0, 1, start=0, end=1000, text="original")])
    raw_append(
        retry_recording / "transcript",
        [rev_record(0, 2, start=0, end=1000, text="explicit retry", token="retry")],
    )
    page = retry_log.read_page(-1, 1)
    assert page.segments[0]["revision"] == 2
    assert page.segments[0]["text"] == "explicit retry"


def test_disk_index_handles_short_pwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_pwrite = transcript_log_module.os.pwrite
    chunks: list[int] = []

    def short_pwrite(fd: int, data: bytes, offset: int) -> int:
        chunk = bytes(data)[:3]
        chunks.append(len(chunk))
        return real_pwrite(fd, chunk, offset)

    monkeypatch.setattr(transcript_log_module.os, "pwrite", short_pwrite)
    index = transcript_log_module._DiskIndex(tmp_path)
    try:
        index.set(3, final_revision=123, highest_revision=9)
        _status, final_revision, highest_revision, *_rest = index.get(3)
        assert (final_revision, highest_revision) == (123, 9)
        assert len(chunks) > 1
    finally:
        index.close()


def test_replay_index_allocation_failure_does_not_mask_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log, _recording = make_log(tmp_path)
    log.append(pairs(3))
    real_index = transcript_log_module._DiskIndex
    calls = 0

    def failing_index(directory: Path | None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated index allocation failure")
        return real_index(directory)

    monkeypatch.setattr(transcript_log_module, "_DiskIndex", failing_index)
    with pytest.raises(OSError, match="simulated index allocation failure"):
        log.replay(None)
