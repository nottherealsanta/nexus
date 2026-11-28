"""Durable recorder storage (plan 54, B1): commit boundaries, recovery, WAV export and security."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import tracemalloc
import wave
from pathlib import Path

import pytest

from nexus.recorder import (
    DEFAULT_CHUNK_SAMPLES,
    RecorderConflict,
    RecorderCorrupt,
    RecorderInvalid,
    RecorderNotFound,
    RecorderSecurityError,
    RecorderStore,
)
from nexus.recorder import store as store_module

SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2


def body(index: int, samples: int = 160) -> bytes:
    return bytes([index % 251]) * (samples * BYTES_PER_SAMPLE)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(store: RecorderStore, recording_id: str, sequence: int, start: int, data: bytes, *, recovery=False):
    return store.put_chunk(recording_id, sequence=sequence, start=start, data=data, sha256=digest(data), recovery=recovery)


def write_private(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


@pytest.fixture
def store(tmp_path: Path) -> RecorderStore:
    return RecorderStore(tmp_path / "recordings")


def test_create_layout_permissions_and_path_guards(store: RecorderStore, tmp_path: Path):
    result = store.create(owner_stream_id="stream-1", owner_client_id="client-1", title="memo", now=1_700_000_000)
    rid = result["id"]
    rec_dir = store.root / rid
    assert len(rid) == 32 and rid == rid.lower()
    assert result["free_seconds"] > 0
    assert result["capture_state"] == "recording" and result["durable_offset"] == 0
    assert result["format"] == {"rate": SAMPLE_RATE, "channels": 1, "bits": 16}
    assert result["created_at"] == "2023-11-14T22:13:20.000Z"
    assert result["owner"] == {"stream_id": "stream-1", "client_id": "client-1"}

    for path in (store.root, rec_dir, rec_dir / "chunks", rec_dir / "exports"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((rec_dir / "meta.json").stat().st_mode) == 0o600

    put(store, rid, 0, 0, body(0))
    assert stat.S_IMODE((rec_dir / "chunks.log").stat().st_mode) == 0o600
    assert stat.S_IMODE((rec_dir / "chunks" / "00000000.pcm").stat().st_mode) == 0o600

    missing = "a" * 32
    with pytest.raises(RecorderNotFound):
        store.get(missing)
    for bad in ("../../etc", "ABC", "", "a" * 31, "a" * 33, "g" * 32):
        with pytest.raises(RecorderInvalid):
            store.get(bad)
    assert not (tmp_path / "etc").exists()


def test_create_fsyncs_new_ancestors_root_and_recording_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[Path] = []
    real_fsync_dir = store_module._fsync_dir

    def record_fsync_dir(path):
        calls.append(Path(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", record_fsync_dir)
    root = tmp_path / "state" / "second" / "recordings"
    store = RecorderStore(root)
    rid = store.create()["id"]

    for path in (tmp_path, root.parent.parent, root.parent, root, root / rid):
        assert Path(path) in calls
    assert calls.index(Path(tmp_path)) < calls.index(root.parent.parent)
    assert calls.index(root.parent.parent) < calls.index(root.parent)
    assert calls.index(root.parent) < calls.index(root)
    assert calls.index(root) < calls.index(root / rid)

    calls.clear()
    store.create()
    assert root in calls
    assert root.parent not in calls  # the root already exists; no parent barrier is owed again


def test_durable_mkdir_loses_a_creation_race_without_changing_owner_or_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    shared = tmp_path / "shared"
    root = shared / "recordings"
    real_mkdir = Path.mkdir
    raced = {"done": False}

    def racing_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if self == shared and not raced["done"]:
            raced["done"] = True
            real_mkdir(self, mode=0o750)  # another component wins the race
            os.chmod(self, 0o750)
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    real_fsync_dir = store_module._fsync_dir
    calls: list[Path] = []

    def record_fsync(path):
        calls.append(Path(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", record_fsync)
    rid = RecorderStore(root).create()["id"]

    assert raced["done"]
    assert shared.is_dir() and stat.S_IMODE(shared.stat().st_mode) == 0o750  # the winner's mode is preserved
    assert shared.stat().st_uid == os.getuid()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert root.stat().st_uid == os.getuid()
    assert (root / rid / "meta.json").is_file()
    assert tmp_path in calls and shared in calls  # both raced and child directory entries are barriered


def test_durable_mkdir_retry_refsyncs_an_ancestor_left_by_a_failed_barrier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "a" / "b" / "recordings"
    store = RecorderStore(root)
    real_fsync_dir = store_module._fsync_dir
    calls: list[Path] = []
    failed = {"done": False}

    def flaky_fsync(path):
        path = Path(path)
        calls.append(path)
        if path == tmp_path and (tmp_path / "a").exists() and not (tmp_path / "a" / "b").exists() and not failed["done"]:
            failed["done"] = True
            raise OSError("injected ancestor fsync failure")
        return real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", flaky_fsync)
    with pytest.raises(OSError):
        store.create()
    assert (tmp_path / "a").exists() and not (tmp_path / "a" / "b").exists()

    calls.clear()
    assert store.create()["id"]
    assert tmp_path in calls  # the leftover `a` is not assumed durable; its parent is fsynced again


def test_durable_mkdir_retry_refsyncs_a_leftover_directory_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    root = state / "recordings"
    store = RecorderStore(root)
    real_fsync_dir = store_module._fsync_dir
    calls: list[Path] = []
    failed = {"done": False}

    def flaky_fsync(path):
        path = Path(path)
        calls.append(path)
        if path == state and not failed["done"]:
            failed["done"] = True
            raise OSError("injected recording-root entry fsync failure")
        return real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", flaky_fsync)
    with pytest.raises(OSError):
        store.create()
    assert root.is_dir() and list(root.iterdir()) == []  # missing metadata is not acknowledged

    calls.clear()
    assert store.create()["id"]
    assert state in calls  # the leftover root's entry is fsynced again on retry


def test_fresh_process_refsyncs_an_existing_root_parent_on_first_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "state" / "recordings"
    rid = RecorderStore(root).create()["id"]
    assert root.is_dir()

    # Simulate a new process: the directory exists, but this process has made no fsync ledger entry for it.
    monkeypatch.setattr(store_module, "_DURABLE_DIRS", {})
    real_fsync_dir = store_module._fsync_dir
    calls: list[Path] = []

    def record_fsync(path):
        calls.append(Path(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", record_fsync)
    fresh = RecorderStore(root)
    assert [item["id"] for item in fresh.list()] == [rid]
    assert calls == [root.parent]  # the existing root's entry is not assumed durable in a new process

    calls.clear()
    fresh.list()
    assert calls == []  # this process remembers the successful barrier


def test_create_does_not_ack_when_the_recording_dir_barrier_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "recordings"
    store = RecorderStore(root)
    real_fsync_dir = store_module._fsync_dir

    def fail_root_fsync(path):
        if Path(path) == root:
            raise OSError("injected root directory fsync failure")
        return real_fsync_dir(path)

    monkeypatch.setattr(store_module, "_fsync_dir", fail_root_fsync)
    with pytest.raises(OSError):
        store.create()
    assert root.is_dir() and list(root.iterdir()) == []


def test_retries_are_idempotent_and_conflicts_are_rejected(store: RecorderStore):
    rid = store.create()["id"]
    first = body(0)
    receipt = put(store, rid, 0, 0, first)
    assert (receipt.durable_offset, receipt.chunks, receipt.next_sequence, receipt.duplicate) == (160, 1, 1, False)

    retry = put(store, rid, 0, 0, first)
    assert retry.duplicate and retry.durable_offset == 160 and retry.chunks == 1

    with pytest.raises(RecorderConflict) as same_seq:
        put(store, rid, 0, 0, body(1))
    assert same_seq.value.code == "chunk_conflict"

    with pytest.raises(RecorderConflict) as moved:
        put(store, rid, 0, 10, first)
    assert moved.value.code == "chunk_conflict"

    with pytest.raises(RecorderConflict) as gap:
        put(store, rid, 2, 160, body(2))
    assert gap.value.code == "noncontiguous" and gap.value.details["next_sequence"] == 1

    with pytest.raises(RecorderConflict) as offset_gap:
        put(store, rid, 1, 200, body(1))
    assert offset_gap.value.code == "noncontiguous" and offset_gap.value.details["durable_offset"] == 160

    with pytest.raises(RecorderInvalid) as bad_hash:
        store.put_chunk(rid, sequence=1, start=160, data=body(1), sha256="0" * 64)
    assert bad_hash.value.code == "bad_chunk_hash"

    for data in (b"", b"\x00", b"\x00" * (store_module.MAX_CHUNK_BYTES + 2)):
        with pytest.raises(RecorderInvalid) as bad_size:
            put(store, rid, 1, 160, data)
        assert bad_size.value.code == "bad_chunk_size"

    with pytest.raises(RecorderInvalid):
        store.put_chunk(rid, sequence=True, start=160, data=body(1), sha256=digest(body(1)))
    with pytest.raises(RecorderInvalid):
        store.put_chunk(rid, sequence=1, start=-1, data=body(1), sha256=digest(body(1)))
    with pytest.raises(RecorderInvalid):
        store.put_chunk(rid, sequence=store_module.MAX_SEQUENCE + 1, start=160, data=body(1), sha256=digest(body(1)))

    assert store.get(rid).document()["durable_offset"] == 160
    assert len((store.root / rid / "chunks.log").read_text().splitlines()) == 1


def test_same_size_corruption_after_a_verified_commit_blocks_a_later_cumulative_ack(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    put(store, rid, 1, 160, body(1))
    damaged = store.root / rid / "chunks" / "00000000.pcm"
    original = damaged.read_bytes()
    write_private(damaged, bytes([original[0] ^ 0xFF]) + original[1:])

    with pytest.raises(RecorderConflict) as conflict:
        put(store, rid, 2, 320, body(2))
    assert conflict.value.code == "noncontiguous" and conflict.value.details["durable_offset"] == 0
    assert len((store.root / rid / "chunks.log").read_text().splitlines()) == 2
    assert store.get(rid).durable_offset == 0  # status rechecks the cached identities and rehashes the change
    assert RecorderStore(store.root).reconcile(rid).durable_offset == 0
    sealed = store.seal(rid, 320)
    assert sealed.durable_offset == 0 and sealed.final_sample_count == 320
    assert sealed.holes == [{"start": 0, "end": 160}]


def test_stream_and_export_after_a_verified_commit_do_not_emit_bad_audio(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    put(store, rid, 1, 160, body(1))
    damaged = store.root / rid / "chunks" / "00000000.pcm"
    original = damaged.read_bytes()
    write_private(damaged, bytes([original[0] ^ 0xFF]) + original[1:])

    # The header is published only after a fresh hash pass, so no corrupt byte is yielded first.
    assert b"".join(store.stream_wav(rid)) == store_module._wav_header(0)
    assert store.export_wav(rid).read_bytes() == store_module._wav_header(0)
    assert RecorderStore(store.root).reconcile(rid).durable_offset == 0


def test_an_unverified_status_read_is_not_reused_for_a_commit(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    reader = RecorderStore(store.root)
    assert reader.get(rid).durable_offset == 160  # size-checked status read caches an unverified checkpoint
    damaged = store.root / rid / "chunks" / "00000000.pcm"
    original = damaged.read_bytes()
    write_private(damaged, bytes([original[0] ^ 0xFF]) + original[1:])
    with pytest.raises(RecorderConflict) as conflict:
        put(reader, rid, 1, 160, body(1))
    assert conflict.value.code == "noncontiguous"
    assert len((store.root / rid / "chunks.log").read_text().splitlines()) == 1


def test_stale_meta_checkpoint_is_a_hint_only(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    put(store, rid, 1, 160, body(1))
    meta = json.loads((store.root / rid / "meta.json").read_text())
    assert meta["durable_offset"] == 0  # periodic checkpoint, not the acknowledgement boundary
    recovered = RecorderStore(store.root).get(rid)
    assert recovered.durable_offset == 320 and recovered.chunks == 2
    assert RecorderStore(store.root).reconcile(rid).durable_offset == 320


@pytest.mark.parametrize("fault", ["write", "data_fsync", "rename", "dir_fsync", "index_fsync"])
def test_crash_at_each_commit_boundary_never_loses_acknowledged_audio(
    fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = RecorderStore(tmp_path / "recordings")
    rid = store.create()["id"]
    first = body(0)
    put(store, rid, 0, 0, first)
    second = body(1)
    before = 160
    after = 320

    if fault == "write":
        real_write = store_module._write_bytes

        def crash_write(path, data):
            if path.name.startswith(".tmp-"):
                raise OSError("injected crash while writing the chunk")
            return real_write(path, data)

        monkeypatch.setattr(store_module, "_write_bytes", crash_write)
    elif fault == "rename":
        real_replace = os.replace

        def crash_rename(src, dst):
            if str(dst).endswith(f"{(store.get(rid).next_sequence):08d}.pcm"):
                raise OSError("injected crash before rename")
            return real_replace(src, dst)

        monkeypatch.setattr(store_module.os, "replace", crash_rename)
    elif fault == "dir_fsync":
        real_fsync_dir = store_module._fsync_dir

        def crash_dir_fsync(path):
            if Path(path).name == "chunks":
                raise OSError("injected crash at chunks directory fsync")
            return real_fsync_dir(path)

        monkeypatch.setattr(store_module, "_fsync_dir", crash_dir_fsync)
    else:
        real_fsync = store_module._fsync_fd
        calls = {"n": 0}
        fail_at = 1 if fault == "data_fsync" else 3

        def crash_fsync(fd, *, full=True):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise OSError(f"injected crash at fsync {fail_at}")
            return real_fsync(fd, full=full)

        monkeypatch.setattr(store_module, "_fsync_fd", crash_fsync)

    with pytest.raises(OSError):
        put(store, rid, 1, before, second)
    monkeypatch.undo()

    # A fresh store is a new process: it must recover from chunks + index, not from the meta hint.
    recovered = RecorderStore(store.root).reconcile(rid)
    assert recovered.durable_offset in (before, after)
    assert recovered.chunks in (1, 2)
    assert (store.root / rid / "chunks" / "00000000.pcm").read_bytes() == first

    retry = put(RecorderStore(store.root), rid, 1, before, second)
    assert retry.durable_offset == after and retry.chunks == 2
    final = RecorderStore(store.root).reconcile(rid)
    assert final.durable_offset == after
    assert sum(1 for line in (store.root / rid / "chunks.log").read_text().splitlines() if line) == 2


def test_partial_index_tail_is_truncated_and_an_unindexed_chunk_is_adopted(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    put(store, rid, 1, 160, body(1))
    index = store.root / rid / "chunks.log"
    index.write_bytes(index.read_bytes() + b'{"seq":2,"start":')
    recovered = RecorderStore(store.root).reconcile(rid)
    assert recovered.durable_offset == 320 and recovered.chunks == 2
    assert index.read_bytes().endswith(b"\n")

    # A complete line without its newline is not durable either; the renamed chunk is adopted instead.
    third = body(2)
    write_private(store.root / rid / "chunks" / "00000002.pcm", third)
    line = json.dumps({"seq": 2, "start": 320, "samples": 160, "sha256": digest(third)}, sort_keys=True,
                      separators=(",", ":")).encode()
    index.write_bytes(index.read_bytes() + line)
    adopted = RecorderStore(store.root).reconcile(rid)
    assert adopted.durable_offset == 480 and adopted.chunks == 3
    assert index.read_bytes().endswith(b"\n")
    assert RecorderStore(store.root).get(rid).durable_offset == 480


def test_orphan_adoption_refsyncs_chunks_dir_before_indexing(store: RecorderStore, monkeypatch: pytest.MonkeyPatch):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    rec_dir = store.root / rid
    chunks_dir = rec_dir / "chunks"
    write_private(chunks_dir / "00000001.pcm", body(1))
    index = rec_dir / "chunks.log"

    real_fsync_dir = store_module._fsync_dir
    real_append_index = RecorderStore._append_index
    events: list[tuple[str, object]] = []
    failed = {"done": False}

    def flaky_fsync(path):
        path = Path(path)
        events.append(("fsync", path))
        if path == chunks_dir and not failed["done"]:
            failed["done"] = True
            raise OSError("injected chunks directory fsync failure")
        return real_fsync_dir(path)

    def tracked_append(self, rec_dir, chunk):
        events.append(("index", chunk.seq))
        return real_append_index(self, rec_dir, chunk)

    monkeypatch.setattr(store_module, "_fsync_dir", flaky_fsync)
    monkeypatch.setattr(RecorderStore, "_append_index", tracked_append)
    with pytest.raises(OSError):
        RecorderStore(store.root).reconcile(rid)
    assert ("index", 1) not in events
    assert len(index.read_text().splitlines()) == 1

    events.clear()
    recovered = RecorderStore(store.root).reconcile(rid)
    assert recovered.durable_offset == 320 and recovered.chunks == 2
    assert events.index(("fsync", chunks_dir)) < events.index(("index", 1))
    assert len(index.read_text().splitlines()) == 2


def test_contiguous_orphan_is_adopted_other_orphans_are_quarantined(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    rec_dir = store.root / rid
    write_private(rec_dir / "chunks" / "00000001.pcm", body(1))
    write_private(rec_dir / "chunks" / ".tmp-00000002-1-deadbeef", b"partial")
    write_private(rec_dir / "chunks" / "00000005.pcm", body(5))
    write_private(rec_dir / "chunks" / "00000006.pcm", b"odd")

    first = RecorderStore(store.root).reconcile(rid)
    assert first.durable_offset == 320 and first.chunks == 2
    assert not (rec_dir / "chunks" / ".tmp-00000002-1-deadbeef").exists()
    quarantined = sorted(path.name for path in (rec_dir / "chunks" / "quarantine").iterdir())
    assert len(quarantined) == 2 and all(name.endswith(".pcm") for name in quarantined)
    assert not (rec_dir / "chunks" / "00000005.pcm").exists()

    # The quarantined tail does not stop later contiguous commits, and the index stays ordered.
    store = RecorderStore(store.root)
    put(store, rid, 2, 320, body(2))
    assert RecorderStore(store.root).reconcile(rid).durable_offset == 480


def test_missing_and_corrupted_chunks_stop_the_prefix_and_report_holes(store: RecorderStore):
    rid = store.create()["id"]
    for sequence in range(3):
        put(store, rid, sequence, sequence * 160, body(sequence))
    store.seal(rid, 5 * 160)
    rec_dir = store.root / rid
    (rec_dir / "chunks" / "00000001.pcm").unlink()

    recovered = RecorderStore(store.root).reconcile(rid)
    assert recovered.durable_offset == 160 and recovered.chunks == 1
    assert recovered.holes == [{"start": 160, "end": 320}, {"start": 480, "end": 800}]
    assert recovered.capture_state == "stopped"
    wav = b"".join(RecorderStore(store.root).stream_wav(rid))
    assert len(wav) == 44 + 160 * BYTES_PER_SAMPLE
    with wave.open(io.BytesIO(wav)) as parsed:
        assert parsed.getnframes() == 160

    other = store.create()["id"]
    put(store, other, 0, 0, body(0))
    put(store, other, 1, 160, body(1))
    damaged = store.root / other / "chunks" / "00000000.pcm"
    original = damaged.read_bytes()
    write_private(damaged, bytes([original[0] ^ 0xFF]) + original[1:])
    corrupted = RecorderStore(store.root).reconcile(other)
    assert corrupted.durable_offset == 0 and corrupted.chunks == 0
    assert corrupted.holes == [{"start": 0, "end": 160}]
    assert any("sha256 mismatch" in problem["reason"] for problem in corrupted.problems)
    assert damaged.read_bytes()[0] == (original[0] ^ 0xFF)  # corrupt data is reported, not deleted or hidden
    assert len(b"".join(RecorderStore(store.root).stream_wav(other))) == 44


def test_seal_is_idempotent_and_recovery_upload_is_bounded(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    put(store, rid, 1, 160, body(1))
    sealed = store.seal(rid, 3 * 160)
    assert sealed.capture_state == "stopped" and sealed.sealed and sealed.final_sample_count == 480
    assert sealed.holes == [{"start": 320, "end": 480}]

    with pytest.raises(RecorderConflict) as closed:
        put(store, rid, 2, 320, body(2))
    assert closed.value.code == "recording_sealed"

    recovered = put(store, rid, 2, 320, body(2), recovery=True)
    assert recovered.durable_offset == 480 and recovered.chunks == 3
    state = store.get(rid)
    assert state.holes == [] and state.capture_state == "stopped"
    assert len(b"".join(store.stream_wav(rid))) == 44 + 480 * BYTES_PER_SAMPLE

    with pytest.raises(RecorderConflict) as beyond:
        put(store, rid, 3, 480, body(3), recovery=True)
    assert beyond.value.code == "beyond_sealed"

    assert store.seal(rid, 480).revision == sealed.revision
    with pytest.raises(RecorderConflict) as different:
        store.seal(rid, 500)
    assert different.value.code == "final_count_conflict"

    other = store.create()["id"]
    put(store, other, 0, 0, body(0))
    with pytest.raises(RecorderConflict) as before_durable:
        store.seal(other, 10)
    assert before_durable.value.code == "final_count_before_durable"


@pytest.mark.parametrize("samples", [0, 100, DEFAULT_CHUNK_SAMPLES])
def test_wav_is_valid_for_empty_tiny_and_one_second_recordings(store: RecorderStore, samples: int):
    rid = store.create()["id"]
    payload = b""
    if samples:
        payload = bytes(range(256)) * ((samples * 2) // 256) + bytes((samples * 2) % 256)
        put(store, rid, 0, 0, payload)
    store.seal(rid)
    wav = b"".join(store.stream_wav(rid))
    assert len(wav) == 44 + samples * 2
    with wave.open(io.BytesIO(wav)) as parsed:
        assert parsed.getnchannels() == 1 and parsed.getsampwidth() == 2 and parsed.getframerate() == SAMPLE_RATE
        assert parsed.getnframes() == samples
        assert parsed.readframes(samples) == payload
        if samples == 0:
            assert parsed.getnframes() == 0


def test_one_hour_of_generated_pcm_streams_with_bounded_memory(store: RecorderStore, monkeypatch: pytest.MonkeyPatch):
    # Durability of an individual commit is covered above; skip device flushes to keep the synthetic hour fast.
    monkeypatch.setattr(store_module, "_fsync_fd", lambda fd, *, full=True: None)
    rid = store.create()["id"]
    chunk_samples = SAMPLE_RATE
    chunks = 3600
    for sequence in range(chunks):
        data = bytes([sequence % 251]) * (chunk_samples * 2)
        receipt = put(store, rid, sequence, sequence * chunk_samples, data)
    assert receipt.durable_offset == chunks * chunk_samples
    store.seal(rid)
    assert store.get(rid).final_sample_count == 57_600_000

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        total = 0
        first = b""
        for block in store.stream_wav(rid):
            if not first:
                first = block
            total += len(block)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert total == 44 + 115_200_000
    assert len(first) == 44
    assert struct.unpack_from("<I", first, 40)[0] == 115_200_000
    with wave.open(io.BytesIO(first)) as parsed:
        assert parsed.getnframes() == 57_600_000 and parsed.getframerate() == SAMPLE_RATE
    assert peak < 4 * 1024 * 1024


def test_export_is_atomic_and_keeps_source_chunks(store: RecorderStore):
    rid = store.create()["id"]
    for sequence in range(2):
        put(store, rid, sequence, sequence * 160, body(sequence))
    store.seal(rid)
    source = (store.root / rid / "chunks" / "00000000.pcm").read_bytes()
    exported = store.export_wav(rid)
    assert stat.S_IMODE(exported.stat().st_mode) == 0o600
    assert exported.read_bytes() == b"".join(store.stream_wav(rid))
    assert (store.root / rid / "chunks" / "00000000.pcm").read_bytes() == source
    with wave.open(str(exported)) as parsed:
        assert parsed.getnframes() == 320

    before = exported.read_bytes()

    def failed_write(fd, data):
        raise OSError("injected export failure")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "_write_all", failed_write)
        with pytest.raises(OSError):
            RecorderStore(store.root).export_wav(rid)
    assert exported.read_bytes() == before  # a failed export never replaces a verified one
    assert not list((store.root / rid / "exports").glob(".audio.wav.*.tmp"))

    # A damaged tail is excluded from the declared WAV size (holes are reported by get/reconcile), not fabricated.
    write_private(store.root / rid / "chunks" / "00000001.pcm", b"\x00" * 320)
    RecorderStore(store.root).export_wav(rid)
    assert len(exported.read_bytes()) == 44 + 160 * BYTES_PER_SAMPLE


def test_list_delete_and_metadata_validation(store: RecorderStore):
    older = store.create(title="older", now=1_700_000_000)["id"]
    newer = store.create(title="newer", now=1_700_000_100)["id"]
    assert [item["id"] for item in store.list()] == [newer, older]
    assert [item["id"] for item in store.list(limit=1)] == [newer]
    with pytest.raises(RecorderInvalid):
        store.list(limit=0)
    with pytest.raises(RecorderInvalid):
        store.create(title="x" * 501)

    assert store.delete(older) is True
    assert store.delete(older) is False
    assert [item["id"] for item in store.list()] == [newer]
    assert not (store.root / older).exists()

    meta_path = store.root / newer / "meta.json"
    meta_path.write_bytes(b"{")
    with pytest.raises(RecorderCorrupt):
        RecorderStore(store.root).get(newer)


def test_reconcile_all_marks_unsealed_recordings_and_skips_broken_or_stopped_ones(store: RecorderStore):
    active = store.create()["id"]
    put(store, active, 0, 0, body(0))
    finished = store.create()["id"]
    store.seal(finished)
    incomplete = store.root / ("b" * 32)
    incomplete.mkdir(mode=0o700)

    results = {item["id"]: item for item in store.reconcile_all()}
    assert set(results) == {active, incomplete.name}
    assert results[active]["capture_state"] == "interrupted" and results[active]["durable_offset"] == 160
    assert results[incomplete.name]["error"] == "missing_meta"
    assert store.get(finished).capture_state == "stopped"


def test_malformed_index_and_symlinks_fail_safely(store: RecorderStore, tmp_path: Path):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    index = store.root / rid / "chunks.log"
    index.write_bytes(index.read_bytes() + b"not json\n")
    with pytest.raises(RecorderCorrupt):
        RecorderStore(store.root).reconcile(rid)

    duplicate = store.create()["id"]
    put(store, duplicate, 0, 0, body(0))
    line = (store.root / duplicate / "chunks.log").read_text()
    (store.root / duplicate / "chunks.log").write_text(line + line)
    with pytest.raises(RecorderCorrupt):
        RecorderStore(store.root).reconcile(duplicate)

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = store.root / ("f" * 32)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RecorderSecurityError):
        store.get("f" * 32)
    assert not any(outside.iterdir())


def test_read_pcm_returns_hash_verified_contiguous_ranges(store: RecorderStore, monkeypatch):
    rid = store.create()["id"]
    first = body(0, 160)
    second = body(1, 160)
    put(store, rid, 0, 0, first)
    put(store, rid, 1, 160, second)

    assert store.read_pcm(rid, 0, 160) == first
    assert store.read_pcm(rid, 160, 320) == second
    assert store.read_pcm(rid, 80, 240) == first[160:] + second[:160]

    with pytest.raises(RecorderConflict) as beyond:
        store.read_pcm(rid, 0, 321)
    assert beyond.value.code == "range_unavailable"
    with pytest.raises(RecorderInvalid) as too_long:
        store.read_pcm(rid, 0, store_module.MAX_CHUNK_SAMPLES + 1)
    assert too_long.value.code == "range_too_long"
    with pytest.raises(RecorderInvalid):
        store.read_pcm(rid, -1, 10)

    real_read = store_module._read_private

    def altered(path, limit):
        data = real_read(path, limit)
        if path.name.endswith(".pcm"):
            return data[:-1] + bytes([data[-1] ^ 0xFF])
        return data

    monkeypatch.setattr(store_module, "_read_private", altered)
    with pytest.raises(RecorderCorrupt) as corrupt:
        store.read_pcm(rid, 0, 160)
    assert corrupt.value.code in ("chunk_hash", "range_gap")


def test_read_window_wav_is_bounded_and_pads_only_the_copy(store: RecorderStore):
    rid = store.create()["id"]
    first = body(0, 160)
    second = body(1, 160)
    put(store, rid, 0, 0, first)
    put(store, rid, 1, 160, second)

    audio = store.read_window_wav(rid, 0, 320, pad_to_samples=400)
    assert len(audio) == 44 + 400 * BYTES_PER_SAMPLE
    with wave.open(io.BytesIO(audio), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == SAMPLE_RATE
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 400
        frames = handle.readframes(400)
    assert frames[:640] == first + second
    assert frames[640:] == b"\0" * (80 * BYTES_PER_SAMPLE)
    # The source chunks were not padded or rewritten.
    assert (store.root / rid / "chunks" / "00000001.pcm").stat().st_size == len(second)

    with pytest.raises(RecorderInvalid) as too_long:
        store.read_window_wav(rid, 0, store_module.MAX_WINDOW_SAMPLES + 1)
    assert too_long.value.code == "range_too_long"
    with pytest.raises(RecorderInvalid) as bad_pad:
        store.read_window_wav(rid, 0, 320, pad_to_samples=store_module.MAX_WINDOW_SAMPLES + 1)
    assert bad_pad.value.code == "bad_padding"


def test_transcript_dir_is_private_and_never_creates_the_parent(store: RecorderStore):
    rid = store.create()["id"]
    path = store.transcript_dir(rid)
    assert path == store.root / rid / "transcript"
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert path.is_dir()

    missing = "a" * 32
    with pytest.raises(RecorderNotFound):
        store.transcript_dir(missing)
    assert not (store.root / missing).exists()

    os.chmod(path, 0o755)
    with pytest.raises(RecorderSecurityError) as unsafe:
        store.transcript_dir(rid)
    assert unsafe.value.code == "directory_mode"


def test_range_reads_and_transcript_dir_do_not_resurrect_a_deleted_recording(store: RecorderStore):
    rid = store.create()["id"]
    put(store, rid, 0, 0, body(0))
    assert store.delete(rid) is True
    for action in (
        lambda: store.read_pcm(rid, 0, 10),
        lambda: store.read_window_wav(rid, 0, 10),
        lambda: store.transcript_dir(rid),
    ):
        with pytest.raises(RecorderNotFound):
            action()
    assert not (store.root / rid).exists()


def test_recording_ids_page_is_ordered_cursor_paged_and_includes_all(store: RecorderStore):
    ids = [f"{value:032x}" for value in (1, 2, 3, 4, 5)]
    for recording_id in ids:
        store.create(recording_id=recording_id)

    first, cursor = store.recording_ids_page(None, 2)
    assert first == ids[:2] and cursor == ids[1]
    second, cursor = store.recording_ids_page(cursor, 2)
    assert second == ids[2:4] and cursor == ids[3]
    third, cursor = store.recording_ids_page(cursor, 2)
    assert third == ids[4:] and cursor is None

    with pytest.raises(RecorderInvalid):
        store.recording_ids_page("not-an-id", 2)
    with pytest.raises(RecorderInvalid):
        store.recording_ids_page(None, 0)


def test_recording_ids_page_survives_concurrent_add_delete_without_duplicates(store: RecorderStore):
    ids = [f"{value:032x}" for value in (1, 2, 3, 4, 5)]
    for recording_id in ids:
        store.create(recording_id=recording_id)
    first, cursor = store.recording_ids_page(None, 2)
    assert first == ids[:2]

    assert store.delete(ids[2]) is True
    store.create(recording_id=f"{6:032x}")
    store.create(recording_id=f"{0:032x}")  # below the cursor: intentionally left to a fresh scan

    remainder, next_cursor = store.recording_ids_page(cursor, 10)
    assert remainder == [ids[3], ids[4], f"{6:032x}"]
    assert next_cursor is None
    assert len(set(first + remainder)) == len(first + remainder)
