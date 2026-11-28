"""Durable chunk storage for standalone recordings (plan 54, B1).

Layout under `$NEXUS10_HOME/recordings/<opaque-id>/` (root and directories 0700, files 0600):

    meta.json          state and a checkpoint hint; written tmp + fsync + rename + directory fsync
    chunks.log         append-only JSONL `{seq,start,samples,sha256}` index, fsynced after each line
    chunks/NNNNNNNN.pcm  raw mono 16 kHz PCM16, one file per chunk
    chunks/quarantine/  orphan or unusable chunk files kept for inspection, never silently deleted
    exports/audio.wav  materialised streamed WAV (source chunks stay authoritative)
    transcript/        durable transcript log directory (C3b), created only on request

Bounded scheduler reads (`read_pcm`, `read_window_wav`) touch only the contiguous durable prefix, hash every
chunk they return and never create or extend a recording. `transcript_dir` creates only the child of an
existing recording and refuses a missing or unsafe parent. `recording_ids_page` enumerates every recording
directory, including stopped ones, in cursor order while retaining only one bounded page of candidates.

Commit order for a chunk: write a private `.tmp-*` file, fsync it, rename it into `chunks/`, fsync that
directory, append the index line, fsync `chunks.log`, then advance the in-memory checkpoint and acknowledge.
`meta.json` is checkpointed periodically and on seal; it is a hint. Recovery derives the durable range from the
index and chunk files, so a crash after the index fsync can only leave an unreported unacknowledged chunk, never
lose an acknowledged one. A chunk file that was renamed before its index line was durable is adopted during
recovery when it is the next contiguous chunk; other orphans are quarantined.

The index is the authority for ordering and content hashes. A partial final index line is truncated on recovery
and a chunk belonging to it is adopted as an orphan. A hash, size or offset mismatch stops the contiguous prefix
at that chunk and is reported as a hole; corrupt data is never replaced with silence or dropped silently.

`meta.json` also carries optional creation capability hashes (create idempotency and recovery) in the same atomic
write as the recording identity. They are hashes only, are not part of `RecorderState.document()`, and let the
service recover a create that crashed before writing its own sidecar.

Directory creation is made durable by fsyncing the parent that gains each new entry: missing ancestors of the
recordings root, the root after a recording directory, and the recording directory after its children/meta. A
failed parent fsync leaves the directory in the process ledger as not yet durable, so a retry fsyncs its parent
again. Orphan adoption likewise re-fsyncs `chunks/` before appending its index line.

A verified in-process checkpoint is reused only while file identities (device, inode, size, mtime, ctime) still
match. Any detectable same-size rewrite forces a full hash pass before a later cumulative acknowledgement. This
does not detect a writer that restores every identity field; seal, export and reconcile always start with a full
re-read/hash, so no stronger guarantee is claimed for ordinary chunk commits.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import heapq
import json
import os
import re
import secrets
import shutil
import stat
import struct
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..paths import home

SAMPLE_RATE = 16_000
CHANNELS = 1
BITS_PER_SAMPLE = 16
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE  # 32,000 bytes/s
DEFAULT_CHUNK_SAMPLES = SAMPLE_RATE  # about one second
MAX_CHUNK_BYTES = 64_000  # two seconds; mirrors the HTTP chunk body limit
MAX_CHUNK_SAMPLES = MAX_CHUNK_BYTES // BYTES_PER_SAMPLE
MAX_WINDOW_SECONDS = 120
MAX_WINDOW_SAMPLES = MAX_WINDOW_SECONDS * SAMPLE_RATE
MAX_ENUM_PAGE = 200
MAX_SEQUENCE = 99_999_999  # keeps the 8-digit chunk filename bounded
FORMAT = {"rate": SAMPLE_RATE, "channels": CHANNELS, "bits": BITS_PER_SAMPLE}
META_VERSION = 1
META_CHECKPOINT_CHUNKS = 10  # periodic meta.json hint; the index is authoritative
MAX_META_BYTES = 2 * 1024 * 1024
MAX_INDEX_BYTES = 32 * 1024 * 1024
READ_BLOCK = 64 * 1024

_RECORDING_ID = re.compile(r"[0-9a-f]{32}")
_CHUNK_NAME = re.compile(r"(\d{8})\.pcm")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PRIVATE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
_DURABLE_DIRS: dict[Path, tuple[int, int]] = {}  # path -> (dev, inode) whose parent entry was fsynced in this process


def recordings_root() -> Path:
    """The Core state home's recorder directory; follows `paths.home()` (and its migration guard)."""
    return home() / "recordings"


class RecorderError(Exception):
    """A recorder storage failure with a stable code for the service/HTTP layer."""

    code = "recorder_error"

    def __init__(self, code: str | None = None, message: str = "", **details: Any) -> None:
        super().__init__(message or code or self.code)
        if code is not None:
            self.code = code
        self.details = details


class RecorderNotFound(RecorderError):
    code = "recording_not_found"


class RecorderConflict(RecorderError):
    code = "recording_conflict"


class RecorderInvalid(RecorderError):
    code = "recording_invalid"


class RecorderCorrupt(RecorderError):
    code = "recording_corrupt"


class RecorderSecurityError(RecorderError):
    code = "recording_insecure"


_FileIdentity = tuple[int, int, int, int, int]  # dev, inode, size, mtime_ns, ctime_ns


@dataclass(frozen=True)
class _Chunk:
    seq: int
    start: int
    samples: int
    sha256: str


@dataclass(frozen=True)
class _Checkpoint:
    durable_offset: int
    durable_bytes: int
    chunks: int
    next_sequence: int
    holes: tuple[tuple[int, int], ...]
    problems: tuple[dict[str, Any], ...]
    records: tuple[_Chunk, ...]
    valid: tuple[_Chunk, ...]
    prefix: tuple[_Chunk, ...]
    identities: tuple[tuple[int, _FileIdentity], ...] = field(default=())
    verified: bool = field(default=False)
    quarantined: tuple[int, ...] = field(default=())


@dataclass(frozen=True)
class ChunkReceipt:
    """The durable checkpoint returned after a chunk commit (or an identical retry)."""

    id: str
    sequence: int
    start: int
    samples: int
    sha256: str
    durable_offset: int
    durable_bytes: int
    chunks: int
    next_sequence: int
    duplicate: bool = False

    def document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "start": self.start,
            "samples": self.samples,
            "sha256": self.sha256,
            "durable_offset": self.durable_offset,
            "durable_bytes": self.durable_bytes,
            "chunks": self.chunks,
            "next_sequence": self.next_sequence,
            "duplicate": self.duplicate,
        }


@dataclass(frozen=True)
class RecorderState:
    """A recording's state with the durable checkpoint derived from the chunk index."""

    id: str
    created_at: str
    owner: dict[str, Any]
    generation: int
    format: dict[str, int]
    capture_state: str
    final_sample_count: int | None
    sealed: bool
    title: str | None
    revision: int
    durable_offset: int
    durable_bytes: int
    chunks: int
    next_sequence: int
    holes: list[dict[str, int]]
    problems: list[dict[str, Any]]

    def document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "owner": dict(self.owner),
            "generation": self.generation,
            "format": dict(self.format),
            "capture_state": self.capture_state,
            "final_sample_count": self.final_sample_count,
            "sealed": self.sealed,
            "title": self.title,
            "revision": self.revision,
            "durable_offset": self.durable_offset,
            "durable_bytes": self.durable_bytes,
            "chunks": self.chunks,
            "next_sequence": self.next_sequence,
            "holes": [dict(hole) for hole in self.holes],
            "problems": [dict(problem) for problem in self.problems],
        }


# -- secure filesystem primitives -----------------------------------------------------------------------------


def _private_directory(path: Path, *, parents: bool = False) -> Path:
    """Create or validate a private directory (0700, owned, not a symlink)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=_PRIVATE_DIR_MODE, parents=parents)
        os.chmod(path, _PRIVATE_DIR_MODE)
        info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RecorderSecurityError("directory_unsafe", f"recorder directory is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise RecorderSecurityError("directory_owner", f"recorder directory is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE:
        raise RecorderSecurityError("directory_mode", f"recorder directory must be mode 0700: {path}")
    return path


def _open_private(path: Path, flags: int, *, create: bool = False) -> int:
    """Open a regular file without following symlinks and require a private (0600) file."""
    existed = path.exists()
    try:
        fd = os.open(path, flags | os.O_CLOEXEC | os.O_NOFOLLOW, _PRIVATE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RecorderSecurityError("file_symlink", f"refusing to follow a recorder symlink: {path}") from exc
        raise
    try:
        if create and not existed:
            os.fchmod(fd, _PRIVATE_MODE)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RecorderSecurityError("file_type", f"recorder file is not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise RecorderSecurityError("file_owner", f"recorder file is not owned by this user: {path}")
        if stat.S_IMODE(info.st_mode) != _PRIVATE_MODE:
            raise RecorderSecurityError("file_mode", f"recorder file must be mode 0600: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _fsync_fd(fd: int, *, full: bool = True) -> None:
    """fsync; on macOS also ask for a full device flush (the plan's B1 durability barrier)."""
    os.fsync(fd)
    if full and sys.platform == "darwin":
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except OSError as exc:
            # Some filesystems (and directory descriptors) do not support F_FULLFSYNC.
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOTTY):
                raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _fsync_fd(fd, full=True)  # also attempt F_FULLFSYNC on darwin; unsupported filesystems fall back
    finally:
        os.close(fd)


def _dir_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RecorderSecurityError("directory_unsafe", f"recorder directory is not a real directory: {path}")
    return (info.st_dev, info.st_ino)


def _ensure_dir_entry_durable(path: Path) -> None:
    """Fsync a directory's parent once per process/inode, including after a failed earlier attempt.

    A failed parent fsync can leave the directory on disk but not know whether its entry survived. Keeping the
    directory in this ledger only after a successful parent fsync makes a retry re-barrier that entry. The inode
    is part of the key so a removed/recreated directory at the same path is not mistaken for the old one.
    """
    identity = _dir_identity(path)
    if _DURABLE_DIRS.get(path) == identity:
        return
    parent = path.parent
    if parent != path:
        _fsync_dir(parent)
    _DURABLE_DIRS[path] = identity


def _durable_mkdir(path: Path) -> bool:
    """Create a directory and missing ancestors at 0700, fsyncing each parent that gains an entry.

    This is the ancestor barrier for the first `recordings/` creation: a child directory is not durable merely
    because it was created. Existing directories are checked against the in-process durability ledger, so a retry
    after a failed parent fsync re-establishes the barrier instead of assuming the leftover directory is durable.
    If another component wins the mkdir race, its directory is validated and barriered without changing its mode.
    """
    path = Path(path)
    # Another component (for example home() creation) may create the same directory between lstat and mkdir. Treat
    # a lost race as an existing directory: validate it, fsync its parent and do not chmod it.
    for _attempt in range(4):
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RecorderSecurityError("directory_unsafe", f"recorder directory is not a real directory: {path}")
            _ensure_dir_entry_durable(path)
            return False
        parent = path.parent
        if parent != path:
            _durable_mkdir(parent)
        try:
            path.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=False)
        except FileExistsError:
            continue  # re-inspect the winner: preserve its owner/mode and fsync its parent
        os.chmod(path, _PRIVATE_DIR_MODE)
        _fsync_dir(parent)
        _DURABLE_DIRS[path] = _dir_identity(path)
        return True
    raise RecorderError("directory_race", f"directory kept changing while being created: {path}")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _write_bytes(path: Path, data: bytes) -> None:
    """Write one new private file completely."""
    fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True)
    try:
        _write_all(fd, data)
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _read_private(path: Path, limit: int) -> bytes:
    fd = _open_private(path, os.O_RDONLY)
    try:
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(READ_BLOCK, limit + 1 - total))
            if not block:
                break
            blocks.append(block)
            total += len(block)
            if total > limit:
                raise RecorderCorrupt("file_too_large", f"recorder file is too large: {path}")
        return b"".join(blocks)
    finally:
        os.close(fd)


def _file_sha256(path: Path) -> str:
    fd = _open_private(path, os.O_RDONLY)
    digest = hashlib.sha256()
    try:
        while True:
            block = os.read(fd, READ_BLOCK)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _chunk_path(rec_dir: Path, sequence: int) -> Path:
    return rec_dir / "chunks" / f"{sequence:08d}.pcm"


def _validate_recording_id(recording_id: str) -> str:
    if not isinstance(recording_id, str) or not _RECORDING_ID.fullmatch(recording_id):
        raise RecorderInvalid("bad_recording_id", "recording id must be 32 lowercase hexadecimal characters")
    return recording_id


def _validate_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RecorderInvalid("bad_chunk", f"{name} must be an integer >= {minimum}")
    return value


def _validate_range(start: Any, end: Any, max_samples: int) -> tuple[int, int]:
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise RecorderInvalid("bad_range", "sample range must be two integers with 0 <= start < end")
    if end - start > max_samples:
        raise RecorderInvalid(
            "range_too_long", f"sample range must be at most {max_samples} samples")
    return start, end


def _json_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _read_json(path: Path, limit: int) -> dict[str, Any]:
    raw = _read_private(path, limit)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise RecorderCorrupt("bad_json", f"invalid JSON in {path.name}") from exc
    if not isinstance(value, dict):
        raise RecorderCorrupt("bad_json", f"{path.name} must contain a JSON object")
    return value


def _wav_header(samples: int) -> bytes:
    data_bytes = samples * BYTES_PER_SAMPLE
    if data_bytes > 0xFFFFFFFF - 36:
        raise RecorderInvalid("recording_too_large", "recording is too large for a WAV file")
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        SAMPLE_RATE,
        BYTES_PER_SECOND,
        BYTES_PER_SAMPLE,
        BITS_PER_SAMPLE,
        b"data",
        data_bytes,
    )


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _holes(valid: tuple[_Chunk, ...], final: int | None) -> tuple[tuple[int, int], ...]:
    intervals = [(chunk.start, chunk.start + chunk.samples) for chunk in valid]
    if final is not None:
        intervals = [(max(0, start), min(end, final)) for start, end in intervals if start < final]
        bound = final
    else:
        bound = max((end for _, end in intervals), default=0)
    holes: list[tuple[int, int]] = []
    cursor = 0
    for start, end in _merge_intervals(intervals):
        if start > cursor:
            holes.append((cursor, start))
        cursor = max(cursor, end)
    if final is not None and cursor < bound:
        holes.append((cursor, bound))
    return tuple(holes)


def _build_checkpoint(
    records: tuple[_Chunk, ...],
    valid: tuple[_Chunk, ...],
    problems: tuple[dict[str, Any], ...],
    final: int | None,
    quarantined: tuple[int, ...] = (),
    *,
    identities: dict[int, _FileIdentity] | None = None,
    verified: bool = False,
) -> _Checkpoint:
    valid_by_seq = {chunk.seq: chunk for chunk in valid}
    prefix: list[_Chunk] = []
    expected = 0
    sequence = 0
    while (chunk := valid_by_seq.get(sequence)) is not None and chunk.start == expected:
        prefix.append(chunk)
        expected += chunk.samples
        sequence += 1
    return _Checkpoint(
        durable_offset=expected,
        durable_bytes=sum(chunk.samples * BYTES_PER_SAMPLE for chunk in prefix),
        chunks=len(prefix),
        next_sequence=len(prefix),
        holes=_holes(valid, final),
        problems=problems,
        records=records,
        valid=valid,
        prefix=tuple(prefix),
        identities=tuple(sorted((identities or {}).items())),
        verified=verified,
        quarantined=quarantined,
    )


def _identity(info: os.stat_result) -> _FileIdentity:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _file_identity(path: Path) -> _FileIdentity:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RecorderSecurityError("file_type", f"recorder file is not a regular file: {path}")
    return _identity(info)


def _chunk_probe(rec_dir: Path, chunk: _Chunk, *, verify_hash: bool) -> tuple[str | None, _FileIdentity | None]:
    """Return (problem, identity). An identity is returned only for an intact, correctly sized regular file."""
    path = _chunk_path(rec_dir, chunk.seq)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing chunk file", None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "not a regular file", None
    if info.st_size != chunk.samples * BYTES_PER_SAMPLE:
        return f"size {info.st_size} does not match {chunk.samples} samples", None
    identity = _identity(info)
    if not verify_hash:
        return None, identity
    try:
        actual = _file_sha256(path)
    except RecorderError as exc:
        return str(exc), None
    if actual != chunk.sha256:
        return "sha256 mismatch", None
    return None, identity


def _chunk_record(value: Any) -> _Chunk:
    if not isinstance(value, dict):
        raise RecorderCorrupt("bad_index", "index line is not an object")
    sequence = value.get("seq")
    start = value.get("start")
    samples = value.get("samples")
    sha = value.get("sha256")
    if type(sequence) is not int or sequence < 0 or sequence > MAX_SEQUENCE:
        raise RecorderCorrupt("bad_index", "index sequence is invalid")
    if type(start) is not int or start < 0:
        raise RecorderCorrupt("bad_index", "index start is invalid")
    if type(samples) is not int or samples <= 0 or samples > MAX_CHUNK_SAMPLES:
        raise RecorderCorrupt("bad_index", "index sample count is invalid")
    if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise RecorderCorrupt("bad_index", "index sha256 is invalid")
    return _Chunk(seq=sequence, start=start, samples=samples, sha256=sha)


def _read_index(rec_dir: Path, *, repair: bool) -> list[_Chunk]:
    path = rec_dir / "chunks.log"
    if not path.exists():
        return []
    flags = os.O_RDWR if repair else os.O_RDONLY
    fd = _open_private(path, flags)
    try:
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, READ_BLOCK)
            if not block:
                break
            blocks.append(block)
            total += len(block)
            if total > MAX_INDEX_BYTES:
                raise RecorderCorrupt("index_too_large", "recorder chunk index is too large")
        raw = b"".join(blocks)
        if raw and not raw.endswith(b"\n"):
            cut = raw.rfind(b"\n") + 1
            if repair:
                os.ftruncate(fd, cut)
                _fsync_fd(fd)
            raw = raw[:cut]
    finally:
        os.close(fd)
    records: list[_Chunk] = []
    for line in raw.splitlines():
        if not line.strip():
            raise RecorderCorrupt("bad_index", "empty line in recorder chunk index")
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise RecorderCorrupt("bad_index", "invalid JSON in recorder chunk index") from exc
        records.append(_chunk_record(value))
    return records


def _cleanup_temps(directory: Path) -> None:
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return
    for entry in entries:
        stale = entry.name.startswith(".tmp-") or (entry.name.startswith(".meta.json.") and entry.name.endswith(".tmp"))
        if not stale:
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            pass


class RecorderStore:
    """Chunk storage, recovery and WAV export for standalone recordings.

    One instance is the process's writer. Mutating calls are serialized by a re-entrant lock; the future recorder
    service is expected to serialize a recording's lease/chunk operations above this layer. Callers outside the
    daemon should not point two `RecorderStore`s at the same root concurrently.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else recordings_root()
        self._lock = threading.RLock()
        self._checkpoints: dict[str, _Checkpoint] = {}

    # -- paths and metadata -----------------------------------------------------------------------------------

    def _root_ready(self) -> Path:
        _durable_mkdir(self.root)
        return _private_directory(self.root)

    def _rec_dir(self, recording_id: str, *, must_exist: bool = True) -> Path:
        rid = _validate_recording_id(recording_id)
        root = self._root_ready()
        path = root / rid
        try:
            path.lstat()
        except FileNotFoundError:
            if must_exist:
                raise RecorderNotFound("recording_not_found", f"no such recording: {rid}") from None
            return path
        return _private_directory(path)

    def _read_meta(self, rec_dir: Path, recording_id: str) -> dict[str, Any]:
        try:
            meta = _read_json(rec_dir / "meta.json", MAX_META_BYTES)
        except FileNotFoundError as exc:
            raise RecorderCorrupt("missing_meta", "recording has no metadata (incomplete creation or manual loss)") from exc
        if meta.get("id") != recording_id:
            raise RecorderCorrupt("bad_meta", "recording metadata id does not match its directory")
        if meta.get("version") != META_VERSION:
            raise RecorderCorrupt("bad_meta", "unsupported recording metadata version")
        if meta.get("format") != FORMAT:
            raise RecorderCorrupt("bad_meta", "unsupported recording format")
        sealed = meta.get("sealed", False)
        final = meta.get("final_sample_count")
        if not isinstance(sealed, bool):
            raise RecorderCorrupt("bad_meta", "recording sealed flag is invalid")
        if final is not None and (type(final) is not int or final < 0):
            raise RecorderCorrupt("bad_meta", "recording final sample count is invalid")
        if sealed and final is None:
            raise RecorderCorrupt("bad_meta", "sealed recording has no final sample count")
        return meta

    def _write_meta(self, rec_dir: Path, meta: dict[str, Any]) -> None:
        payload = json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        temporary = rec_dir / f".meta.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            _write_bytes(temporary, payload.encode())
            os.replace(temporary, rec_dir / "meta.json")
            _fsync_dir(rec_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def _checkpoint_meta(self, rec_dir: Path, meta: dict[str, Any], cp: _Checkpoint, *, bump: bool = False) -> dict[str, Any]:
        meta = dict(meta)
        meta["durable_offset"] = cp.durable_offset
        meta["durable_bytes"] = cp.durable_bytes
        meta["chunks"] = cp.chunks
        meta["next_sequence"] = cp.next_sequence
        meta["holes"] = [{"start": start, "end": end} for start, end in cp.holes]
        meta["problems"] = [dict(problem) for problem in cp.problems]
        if bump:
            meta["revision"] = int(meta.get("revision", 1)) + 1
        self._write_meta(rec_dir, meta)
        return meta

    def _state(self, meta: dict[str, Any], cp: _Checkpoint) -> RecorderState:
        return RecorderState(
            id=meta["id"],
            created_at=str(meta.get("created_at", "")),
            owner=dict(meta.get("owner") or {}),
            generation=int(meta.get("generation", 1)),
            format=dict(meta.get("format") or FORMAT),
            capture_state=str(meta.get("capture_state", "interrupted")),
            final_sample_count=meta.get("final_sample_count"),
            sealed=bool(meta.get("sealed", False)),
            title=meta.get("title"),
            revision=int(meta.get("revision", 1)),
            durable_offset=cp.durable_offset,
            durable_bytes=cp.durable_bytes,
            chunks=cp.chunks,
            next_sequence=cp.next_sequence,
            holes=[{"start": start, "end": end} for start, end in cp.holes],
            problems=[dict(problem) for problem in cp.problems],
        )

    def _append_index(self, rec_dir: Path, chunk: _Chunk) -> None:
        path = rec_dir / "chunks.log"
        existed = path.exists()
        fd = _open_private(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, create=True)
        try:
            _write_all(fd, _json_line({"seq": chunk.seq, "start": chunk.start, "samples": chunk.samples,
                                       "sha256": chunk.sha256}))
            _fsync_fd(fd)
        finally:
            os.close(fd)
        if not existed:
            _fsync_dir(rec_dir)

    # -- recovery ---------------------------------------------------------------------------------------------

    def _quarantine(self, rec_dir: Path, path: Path, sequence: int) -> None:
        directory = _private_directory(rec_dir / "chunks" / "quarantine", parents=True)
        target = directory / f"{sequence:08d}-{secrets.token_hex(4)}.pcm"
        os.replace(path, target)
        _fsync_dir(directory)
        _fsync_dir(rec_dir / "chunks")

    def _scan(
        self,
        rec_dir: Path,
        meta: dict[str, Any],
        *,
        adopt_orphans: bool,
        verify_hashes: bool,
        repair_index: bool,
    ) -> _Checkpoint:
        records = tuple(_read_index(rec_dir, repair=repair_index))
        seen: set[int] = set()
        previous_seq = -1
        previous_end = -1
        for chunk in records:
            if chunk.seq in seen or chunk.seq <= previous_seq:
                raise RecorderCorrupt("bad_index", "duplicate or out-of-order sequence in recorder chunk index")
            if chunk.start < previous_end:
                raise RecorderCorrupt("bad_index", "overlapping or out-of-order chunks in recorder chunk index")
            seen.add(chunk.seq)
            previous_seq = chunk.seq
            previous_end = chunk.start + chunk.samples

        problems: list[dict[str, Any]] = []
        valid: list[_Chunk] = []
        identities: dict[int, _FileIdentity] = {}
        for chunk in records:
            problem, identity = _chunk_probe(rec_dir, chunk, verify_hash=verify_hashes)
            if problem is None:
                valid.append(chunk)
                if identity is not None:
                    identities[chunk.seq] = identity
            else:
                problems.append({"seq": chunk.seq, "start": chunk.start, "reason": problem})

        quarantined: list[int] = []
        if adopt_orphans:
            chunks_dir = _private_directory(rec_dir / "chunks")
            _cleanup_temps(chunks_dir)
            _cleanup_temps(rec_dir)
            existing = {chunk.seq for chunk in records}
            orphans: list[tuple[int, Path]] = []
            for entry in os.scandir(chunks_dir):
                match = _CHUNK_NAME.fullmatch(entry.name)
                if match is not None and int(match.group(1)) not in existing:
                    orphans.append((int(match.group(1)), Path(entry.path)))
            for sequence, path in sorted(orphans):
                # Only the next contiguous tail can be adopted by appending to the index in order.
                if sequence in existing or any(chunk.seq > sequence for chunk in records):
                    self._quarantine(rec_dir, path, sequence)
                    quarantined.append(sequence)
                    continue
                try:
                    info = path.lstat()
                    size = info.st_size
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or size <= 0 or size % 2 or size > MAX_CHUNK_BYTES:
                        raise RecorderCorrupt("bad_orphan", "orphan chunk size is invalid")
                    digest = _file_sha256(path)
                    identity = _identity(info)
                except (OSError, RecorderError):
                    self._quarantine(rec_dir, path, sequence)
                    quarantined.append(sequence)
                    continue
                current = _build_checkpoint(tuple(records), tuple(valid), tuple(problems), meta.get("final_sample_count"),
                                             tuple(quarantined), identities=identities, verified=verify_hashes)
                if sequence != current.next_sequence:
                    self._quarantine(rec_dir, path, sequence)
                    quarantined.append(sequence)
                    continue
                chunk = _Chunk(seq=sequence, start=current.durable_offset, samples=size // BYTES_PER_SAMPLE, sha256=digest)
                # The file was renamed before its index line in the crashed commit. Re-establish the chunks
                # directory barrier before making the index line (and therefore the adoption) durable.
                _fsync_dir(chunks_dir)
                self._append_index(rec_dir, chunk)
                records = records + (chunk,)
                valid.append(chunk)
                identities[sequence] = identity
                existing.add(sequence)

        cp = _build_checkpoint(records, tuple(valid), tuple(problems), meta.get("final_sample_count"), tuple(quarantined),
                              identities=identities, verified=verify_hashes)
        final = meta.get("final_sample_count")
        if final is not None and final < cp.durable_offset:
            raise RecorderCorrupt("bad_meta", "final sample count is before the durable audio")
        return cp

    @staticmethod
    def _cache_intact(rec_dir: Path, cp: _Checkpoint) -> bool:
        """Cheap identity check for every file the checkpoint accepted.

        Device/inode/size/mtime/ctime catch ordinary same-size rewrites. They cannot catch a writer that restores
        all of those fields; seal, export and reconcile therefore always re-read and hash the files.

        One directory scan plus one stat per committed chunk keeps this metadata-only; no audio is re-read. This
        grows with the duration of the recording, the deliberate price of not cumulatively acknowledging a changed
        prefix. A bounded trailing-window check would be faster but could acknowledge older corruption.
        """
        if not cp.identities:
            return True
        wanted = {f"{sequence:08d}.pcm": expected for sequence, expected in cp.identities}
        try:
            with os.scandir(rec_dir / "chunks") as entries:
                for entry in entries:
                    expected = wanted.pop(entry.name, None)
                    if expected is None:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        return False
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        return False
                    if _identity(info) != expected:
                        return False
                    if not wanted:
                        break
        except OSError:
            return False
        return not wanted

    def _checkpoint(self, rec_dir: Path, recording_id: str, meta: dict[str, Any], *,
                    adopt_orphans: bool, verify_hashes: bool, repair_index: bool) -> _Checkpoint:
        cp = self._checkpoints.get(recording_id)
        if cp is not None:
            intact = self._cache_intact(rec_dir, cp)
            if intact and (cp.verified or not verify_hashes):
                return cp
            if not intact:
                # A file changed outside this store: never build a cumulative ack/state on the old checkpoint.
                verify_hashes = True
        cp = self._scan(rec_dir, meta, adopt_orphans=adopt_orphans, verify_hashes=verify_hashes,
                        repair_index=repair_index)
        self._checkpoints[recording_id] = cp
        return cp

    def reconcile(self, recording_id: str) -> RecorderState:
        """Verify chunks and index, adopt/quarantine orphans, repair a partial index tail and mark the recording
        interrupted unless it was sealed. This is the startup recovery path, not a read of a live capture."""
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            cp = self._scan(rec_dir, meta, adopt_orphans=True, verify_hashes=True, repair_index=True)
            if not meta.get("sealed", False) and meta.get("capture_state") != "interrupted":
                meta = dict(meta)
                meta["capture_state"] = "interrupted"
                meta["revision"] = int(meta.get("revision", 1)) + 1
            meta = self._checkpoint_meta(rec_dir, meta, cp)
            self._checkpoints[recording_id] = cp
            return self._state(meta, cp)

    def reconcile_all(self) -> list[dict[str, Any]]:
        """Reconcile every non-stopped recording at Core start; one corrupt recording does not block the others."""
        results: list[dict[str, Any]] = []
        for recording_id in self._recording_ids():
            try:
                meta = self._read_meta(self.root / recording_id, recording_id)
                if meta.get("sealed") and meta.get("capture_state") == "stopped":
                    continue
                results.append(self.reconcile(recording_id).document())
            except (RecorderError, OSError) as exc:
                results.append({"id": recording_id, "error": getattr(exc, "code", "recording_unreadable"),
                                "message": str(exc)})
        return results

    # -- lifecycle --------------------------------------------------------------------------------------------

    def create(
        self,
        *,
        owner_stream_id: str | None = None,
        owner_client_id: str | None = None,
        recording_id: str | None = None,
        title: str | None = None,
        now: float | None = None,
        idempotency_sha256: str | None = None,
        recovery_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Create one recording directory and metadata. Returns state plus `free_seconds`.

        `idempotency_sha256` and `recovery_sha256` are capability hashes written in the same atomic
        `meta.json` as the recording identity. They are not exposed by `RecorderState.document()`; the
        recorder service reads them with `creation_hashes` so a crash after this call and before its own
        sidecar still leaves a replayable create and recovery capability.
        """
        for name, value in (("owner_stream_id", owner_stream_id), ("owner_client_id", owner_client_id), ("title", title)):
            if value is not None and not isinstance(value, str):
                raise RecorderInvalid("bad_metadata", f"{name} must be a string or None")
        if title is not None and len(title) > 500:
            raise RecorderInvalid("bad_metadata", "title is too long")
        for name, value in (("idempotency_sha256", idempotency_sha256), ("recovery_sha256", recovery_sha256)):
            if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
                raise RecorderInvalid("bad_metadata", f"{name} must be a lowercase sha256 hex digest or None")
        with self._lock:
            root = self._root_ready()
            rid = _validate_recording_id(recording_id) if recording_id is not None else uuid.uuid4().hex
            rec_dir = root / rid
            try:
                rec_dir.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RecorderConflict("recording_exists", f"recording already exists: {rid}")
            created = False
            try:
                _private_directory(rec_dir)
                created = True
                _fsync_dir(root)  # the recording directory entry itself must be durable before meta is acknowledged
                _private_directory(rec_dir / "chunks")
                _private_directory(rec_dir / "exports")
                timestamp = time_now(now)
                meta: dict[str, Any] = {
                    "version": META_VERSION,
                    "id": rid,
                    "created_at": timestamp,
                    "owner": {"stream_id": owner_stream_id, "client_id": owner_client_id},
                    "generation": 1,
                    "format": dict(FORMAT),
                    "capture_state": "recording",
                    "final_sample_count": None,
                    "sealed": False,
                    "durable_offset": 0,
                    "durable_bytes": 0,
                    "chunks": 0,
                    "next_sequence": 0,
                    "holes": [],
                    "problems": [],
                    "title": title,
                    "idempotency_sha256": idempotency_sha256,
                    "recovery_sha256": recovery_sha256,
                    "revision": 1,
                }
                self._write_meta(rec_dir, meta)
            except BaseException:
                if created:
                    shutil.rmtree(rec_dir, ignore_errors=True)
                    _fsync_dir(root)
                raise
            cp = _build_checkpoint((), (), (), None, verified=True)
            self._checkpoints[rid] = cp
            result = self._state(meta, cp).document()
            result["free_seconds"] = self.free_seconds()
            return result

    def creation_hashes(self, recording_id: str) -> dict[str, str | None]:
        """Creation-time capability hashes written atomically with the recording metadata.

        Only hashes are stored. Reads meta directly so it works even when the service sidecar was never
        written (a create crash) or was lost.
        """
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            return {
                "idempotency_sha256": meta.get("idempotency_sha256"),
                "recovery_sha256": meta.get("recovery_sha256"),
            }

    def get(self, recording_id: str) -> RecorderState:
        """Read metadata and the checkpoint derived from the index without mutating the recording.

        An uncached status read uses size checks; a cached checkpoint is reused only after its file identities still
        match. Use `reconcile` for a full hash-verified recovery pass. The on-disk meta checkpoint can lag the index.
        """
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            cp = self._checkpoint(rec_dir, recording_id, meta, adopt_orphans=False, verify_hashes=False, repair_index=False)
            return self._state(meta, cp)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Bounded recent-recording list, newest first, using meta.json as the cheap state hint."""
        if type(limit) is not int or limit <= 0:
            raise RecorderInvalid("bad_limit", "limit must be a positive integer")
        limit = min(limit, 50)
        with self._lock:
            items: list[dict[str, Any]] = []
            for recording_id in self._recording_ids():
                rec_dir = self.root / recording_id
                try:
                    meta = self._read_meta(rec_dir, recording_id)
                except (RecorderError, OSError):
                    continue
                cp = self._checkpoints.get(recording_id)
                items.append(self._state(meta, cp).document() if cp is not None else {
                    "id": meta["id"],
                    "created_at": str(meta.get("created_at", "")),
                    "owner": dict(meta.get("owner") or {}),
                    "generation": int(meta.get("generation", 1)),
                    "format": dict(meta.get("format") or FORMAT),
                    "capture_state": str(meta.get("capture_state", "interrupted")),
                    "final_sample_count": meta.get("final_sample_count"),
                    "sealed": bool(meta.get("sealed", False)),
                    "title": meta.get("title"),
                    "revision": int(meta.get("revision", 1)),
                    "durable_offset": int(meta.get("durable_offset", 0)),
                    "durable_bytes": int(meta.get("durable_bytes", 0)),
                    "chunks": int(meta.get("chunks", 0)),
                    "next_sequence": int(meta.get("next_sequence", 0)),
                    "holes": list(meta.get("holes") or []),
                    "problems": list(meta.get("problems") or []),
                })
            items.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
            return items[:limit]

    def _recording_ids(self) -> list[str]:
        try:
            entries = list(os.scandir(self._root_ready()))
        except FileNotFoundError:
            return []
        return sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False)
                      and _RECORDING_ID.fullmatch(entry.name) is not None)

    def recording_ids_page(self, after: str | None = None, limit: int = 50) -> tuple[list[str], str | None]:
        """One cursor page of recording ids in strict id order, including stopped recordings.

        The cursor is the last id returned. The scan keeps only `limit + 1` candidate ids in memory,
        so directory size does not become a list allocation. Existing ids are visited at most once in
        increasing order; deletions are skipped, and additions with an id above the cursor may appear
        in a later page while additions below it are left for the next full scan.
        """
        if type(limit) is not int or limit <= 0:
            raise RecorderInvalid("bad_limit", "limit must be a positive integer")
        limit = min(limit, MAX_ENUM_PAGE)
        if after is not None:
            after = _validate_recording_id(after)
        floor = int(after, 16) if after is not None else -1
        wanted = limit + 1
        smallest: list[int] = []  # max-heap of negated ids, retaining the smallest `wanted` values
        try:
            entries = os.scandir(self._root_ready())
        except FileNotFoundError:
            return [], None
        with entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False) or _RECORDING_ID.fullmatch(entry.name) is None:
                    continue
                value = int(entry.name, 16)
                if value <= floor:
                    continue
                if len(smallest) < wanted:
                    heapq.heappush(smallest, -value)
                elif value < -smallest[0]:
                    heapq.heapreplace(smallest, -value)
        ordered = sorted(-value for value in smallest)
        if len(ordered) > limit:
            ordered = ordered[:limit]
            return [f"{value:032x}" for value in ordered], f"{ordered[-1]:032x}"
        return [f"{value:032x}" for value in ordered], None

    def delete(self, recording_id: str) -> bool:
        """Remove one recording and all of its chunks/exports. The service must reject deletion of an active capture."""
        with self._lock:
            rec_dir = self._rec_dir(recording_id, must_exist=False)
            try:
                rec_dir.lstat()
            except FileNotFoundError:
                self._checkpoints.pop(recording_id, None)
                return False
            _private_directory(rec_dir)
            shutil.rmtree(rec_dir)
            _fsync_dir(self.root)
            self._checkpoints.pop(recording_id, None)
            return True

    def transcript_dir(self, recording_id: str) -> Path:
        """Return the recording's private `transcript/` directory, creating only that child.

        The recording directory must already exist; a missing or unsafe parent is refused. The child is
        created durably at 0700 and existing children are validated with the same store rules.
        """
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            path = rec_dir / "transcript"
            _durable_mkdir(path)
            return _private_directory(path)

    # -- chunks -----------------------------------------------------------------------------------------------

    def put_chunk(
        self,
        recording_id: str,
        *,
        sequence: int,
        start: int,
        data: bytes,
        sha256: str,
        recovery: bool = False,
    ) -> ChunkReceipt:
        """Commit one bounded PCM16 chunk at its exact contiguous sample offset.

        Identical retries return the current durable checkpoint; conflicting duplicates, sequence gaps and offset
        gaps raise `RecorderConflict` (codes `chunk_conflict`/`noncontiguous`/`recording_sealed`). A body must be
        non-empty, 16-bit aligned and at most two seconds of PCM. `recovery=True` allows appending pre-existing
        spooled audio up to a sealed `final_sample_count`; it is a storage hook, not capture permission.

        Before extending a cached checkpoint, every committed file is identity-checked; any detectable change
        forces a full hash pass. A writer that restores all identity fields is not detected here (see the module
        docstring); seal, export and reconcile always rehash.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise RecorderInvalid("bad_chunk", "chunk body must be bytes")
        if not len(data) or len(data) % BYTES_PER_SAMPLE or len(data) > MAX_CHUNK_BYTES:
            raise RecorderInvalid("bad_chunk_size", f"chunk must be 1..{MAX_CHUNK_BYTES} bytes of 16-bit PCM")
        body = data if isinstance(data, bytes) else bytes(data)
        sequence = _validate_int(sequence, "sequence")
        start = _validate_int(start, "start")
        if sequence > MAX_SEQUENCE:
            raise RecorderInvalid("bad_chunk", "chunk sequence is too large")
        digest = hashlib.sha256(body).hexdigest()
        if not isinstance(sha256, str) or sha256.lower() != digest:
            raise RecorderInvalid("bad_chunk_hash", "chunk sha256 does not match its body")
        samples = len(body) // BYTES_PER_SAMPLE

        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            cp = self._checkpoint(rec_dir, recording_id, meta, adopt_orphans=True, verify_hashes=True, repair_index=True)
            by_seq = {chunk.seq: chunk for chunk in cp.records}
            existing = by_seq.get(sequence)
            if existing is not None:
                if (existing.start, existing.samples, existing.sha256) != (start, samples, digest):
                    raise RecorderConflict(
                        "chunk_conflict",
                        "chunk is already committed with different content",
                        durable_offset=cp.durable_offset,
                        sequence=sequence,
                    )
                problem, _identity_value = _chunk_probe(rec_dir, existing, verify_hash=True)
                if problem is not None:
                    raise RecorderCorrupt("acked_chunk_damaged", f"committed chunk {sequence} is damaged: {problem}")
                return ChunkReceipt(
                    id=recording_id, sequence=sequence, start=start, samples=samples, sha256=digest,
                    durable_offset=cp.durable_offset, durable_bytes=cp.durable_bytes, chunks=cp.chunks,
                    next_sequence=cp.next_sequence, duplicate=True,
                )
            final = meta.get("final_sample_count")
            if meta.get("sealed", False) and not recovery:
                raise RecorderConflict(
                    "recording_sealed",
                    "recording is sealed; only recovery uploads are accepted",
                    durable_offset=cp.durable_offset,
                    final_sample_count=final,
                )
            if meta.get("sealed", False) and final is not None and start + samples > final:
                raise RecorderConflict(
                    "beyond_sealed",
                    "recovery chunk extends past the sealed final sample count",
                    durable_offset=cp.durable_offset,
                    final_sample_count=final,
                )
            if sequence != cp.next_sequence or start != cp.durable_offset:
                raise RecorderConflict(
                    "noncontiguous",
                    "chunk does not continue the durable recording",
                    durable_offset=cp.durable_offset,
                    next_sequence=cp.next_sequence,
                )

            chunks_dir = _private_directory(rec_dir / "chunks")
            final_path = _chunk_path(rec_dir, sequence)
            if final_path.exists() or final_path.is_symlink():
                raise RecorderConflict("chunk_conflict", f"an unindexed chunk file already exists for sequence {sequence}")
            temporary = chunks_dir / f".tmp-{sequence:08d}-{os.getpid()}-{secrets.token_hex(4)}"
            try:
                _write_bytes(temporary, body)
                os.replace(temporary, final_path)
                _fsync_dir(chunks_dir)
                identity = _file_identity(final_path)
                chunk = _Chunk(seq=sequence, start=start, samples=samples, sha256=digest)
                self._append_index(rec_dir, chunk)
            except BaseException:
                # The file may be renamed without an index line; a later retry/recovery must rescan and adopt it.
                self._checkpoints.pop(recording_id, None)
                raise
            finally:
                temporary.unlink(missing_ok=True)

            identities = dict(cp.identities)
            identities[sequence] = identity
            new_cp = _build_checkpoint(
                cp.records + (chunk,),
                cp.valid + (chunk,),
                cp.problems,
                meta.get("final_sample_count"),
                cp.quarantined,
                identities=identities,
                verified=True,
            )
            self._checkpoints[recording_id] = new_cp
            if new_cp.chunks % META_CHECKPOINT_CHUNKS == 0 or meta.get("sealed", False):
                self._checkpoint_meta(rec_dir, meta, new_cp)
            return ChunkReceipt(
                id=recording_id, sequence=sequence, start=start, samples=samples, sha256=digest,
                durable_offset=new_cp.durable_offset, durable_bytes=new_cp.durable_bytes, chunks=new_cp.chunks,
                next_sequence=new_cp.next_sequence, duplicate=False,
            )

    def seal(self, recording_id: str, final_sample_count: int | None = None) -> RecorderState:
        """Declare capture finished. Idempotent for the same count; the recording must not be sealed twice with
        different counts and the count cannot be below the durable offset. All files are rehashed first, because
        this is the final cumulative acknowledgement."""
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            # Sealing is the final cumulative acknowledgement: always re-read and hash, never trust a cache.
            cp = self._scan(rec_dir, meta, adopt_orphans=True, verify_hashes=True, repair_index=True)
            self._checkpoints[recording_id] = cp
            final = cp.durable_offset if final_sample_count is None else _validate_int(final_sample_count, "final_sample_count")
            if meta.get("sealed", False):
                if final != meta.get("final_sample_count"):
                    raise RecorderConflict(
                        "final_count_conflict",
                        "recording was already sealed with a different final sample count",
                        final_sample_count=meta.get("final_sample_count"),
                    )
                return self._state(meta, cp)
            if final < cp.durable_offset:
                raise RecorderConflict(
                    "final_count_before_durable",
                    "final sample count is before durable audio",
                    durable_offset=cp.durable_offset,
                    final_sample_count=final,
                )
            meta = dict(meta)
            meta["sealed"] = True
            meta["final_sample_count"] = final
            meta["capture_state"] = "stopped"
            meta["revision"] = int(meta.get("revision", 1)) + 1
            cp = _build_checkpoint(cp.records, cp.valid, cp.problems, final, cp.quarantined,
                                   identities=dict(cp.identities), verified=cp.verified)
            self._checkpoints[recording_id] = cp
            meta = self._checkpoint_meta(rec_dir, meta, cp)
            return self._state(meta, cp)

    # -- output -----------------------------------------------------------------------------------------------

    def _read_prefix_bytes(
        self,
        rec_dir: Path,
        checkpoint: _Checkpoint,
        start: int,
        end: int,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read a contiguous durable range, hashing every touched chunk before returning any of it."""
        if end > checkpoint.durable_offset:
            raise RecorderConflict(
                "range_unavailable", "sample range extends past the durable audio",
                durable_offset=checkpoint.durable_offset)
        output = bytearray()
        for chunk in checkpoint.prefix:
            chunk_end = chunk.start + chunk.samples
            if chunk_end <= start:
                continue
            if chunk.start >= end:
                break
            expected = chunk.samples * BYTES_PER_SAMPLE
            data = _read_private(_chunk_path(rec_dir, chunk.seq), expected)
            if len(data) != expected:
                raise RecorderCorrupt("chunk_short", f"chunk {chunk.seq} ended before its recorded size")
            if hashlib.sha256(data).hexdigest() != chunk.sha256:
                raise RecorderCorrupt("chunk_hash", f"chunk {chunk.seq} changed after commit")
            first = max(start, chunk.start) - chunk.start
            last = min(end, chunk_end) - chunk.start
            output.extend(data[first * BYTES_PER_SAMPLE:last * BYTES_PER_SAMPLE])
        expected_bytes = (end - start) * BYTES_PER_SAMPLE
        if len(output) != expected_bytes:
            raise RecorderCorrupt("range_gap", "contiguous durable range did not cover the requested samples")
        if len(output) > max_bytes:
            raise RecorderInvalid("range_too_long", f"read range must be at most {max_bytes} bytes")
        return bytes(output)

    def read_pcm(self, recording_id: str, start: int, end: int) -> bytes:
        """Read at most two seconds of contiguous durable PCM, hash-verified per touched chunk.

        `start`/`end` are absolute sample offsets in the durable prefix. A range past the durable
        prefix raises `range_unavailable`; a damaged touched chunk raises `RecorderCorrupt` and no
        partial audio is returned.
        """
        start, end = _validate_range(start, end, MAX_CHUNK_SAMPLES)
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            cp = self._checkpoint(rec_dir, recording_id, meta, adopt_orphans=False,
                                  verify_hashes=True, repair_index=False)
            return self._read_prefix_bytes(rec_dir, cp, start, end, max_bytes=MAX_CHUNK_BYTES)

    def read_window_wav(
        self,
        recording_id: str,
        start: int,
        end: int,
        *,
        pad_to_samples: int | None = None,
    ) -> bytes:
        """Read at most 120 s as a valid WAV, zero-padding only the returned copy.

        `pad_to_samples`, when given, is the minimum total sample count of the returned WAV (useful
        for the 0.2 s inference floor). Source chunks and the durable range are untouched.
        """
        start, end = _validate_range(start, end, MAX_WINDOW_SAMPLES)
        if (pad_to_samples is not None
                and (type(pad_to_samples) is not int or pad_to_samples < 0
                     or pad_to_samples > MAX_WINDOW_SAMPLES)):
            raise RecorderInvalid(
                "bad_padding", f"pad_to_samples must be an integer between 0 and {MAX_WINDOW_SAMPLES}")
        samples = end - start
        total = max(samples, pad_to_samples or 0)
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            cp = self._checkpoint(rec_dir, recording_id, meta, adopt_orphans=False,
                                  verify_hashes=True, repair_index=False)
            data = self._read_prefix_bytes(rec_dir, cp, start, end, max_bytes=MAX_WINDOW_SAMPLES * BYTES_PER_SAMPLE)
        return _wav_header(total) + data + b"\x00" * ((total - samples) * BYTES_PER_SAMPLE)

    def stream_wav(self, recording_id: str) -> Iterator[bytes]:
        """Yield a valid 44-byte WAV header for the contiguous durable range, then bounded chunk bytes.

        Only the contiguous prefix is exported; holes beyond it are reported in the state and are not replaced
        with silence. The prefix is hash-verified before the header is published, and each bounded chunk is read
        completely, size/hash-checked, then yielded. Nothing larger than one chunk (at most 64 KiB) is allocated.
        """
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            meta = self._read_meta(rec_dir, recording_id)
            # Verify the prefix before publishing a header, so the declared WAV size never includes a damaged chunk.
            # This is a deliberate full pass rather than a cache lookup: export must not emit bad bytes first.
            cp = self._scan(rec_dir, meta, adopt_orphans=False, verify_hashes=True, repair_index=False)
            self._checkpoints[recording_id] = cp
            header = _wav_header(cp.durable_offset)
            prefix = cp.prefix
        yield header
        for chunk in prefix:
            expected = chunk.samples * BYTES_PER_SAMPLE
            data = _read_private(_chunk_path(rec_dir, chunk.seq), expected)
            if len(data) != expected:
                raise RecorderCorrupt("chunk_short", f"chunk {chunk.seq} ended before its recorded size")
            if hashlib.sha256(data).hexdigest() != chunk.sha256:
                raise RecorderCorrupt("chunk_hash", f"chunk {chunk.seq} changed after commit")
            yield data

    def export_wav(self, recording_id: str) -> Path:
        """Materialise `exports/audio.wav` atomically from the streamed source chunks. Source chunks are untouched."""
        with self._lock:
            rec_dir = self._rec_dir(recording_id)
            exports = _private_directory(rec_dir / "exports")
            target = exports / "audio.wav"
            temporary = exports / f".audio.wav.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            fd = _open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True)
            try:
                for block in self.stream_wav(recording_id):
                    _write_all(fd, block)
                _fsync_fd(fd)
            finally:
                os.close(fd)
            os.replace(temporary, target)
            _fsync_dir(exports)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    # -- space ------------------------------------------------------------------------------------------------

    def free_bytes(self) -> int:
        root = self._root_ready()
        return shutil.disk_usage(root).free

    def free_seconds(self) -> float:
        return self.free_bytes() / BYTES_PER_SECOND


def time_now(now: float | None) -> str:
    stamp = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
