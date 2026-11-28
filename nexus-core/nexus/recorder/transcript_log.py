"""Durable, bounded transcript log for one recording (plan 54, C3b).

The recording directory is the only place transcript text may live. This module owns
``recordings/<id>/transcript/log.jsonl`` and ``recordings/<id>/transcript/checkpoint.json``:

* ``log.jsonl`` is append-only JSON Lines. Every line is ``{"v": 1, "k": ...}`` where ``k`` is ``seg``
  (a closed range and finalization job), ``rev`` (an accepted durable revision) or ``end``/``resume``
  (segmentation state). Each append batch is written with one ``write`` and fsynced before it returns.
* ``checkpoint.json`` is an atomic hint (temp file + fsync + rename + directory fsync), sealed with a body
  hash and a hash of the log prefix it describes. The log is always authoritative; a missing, malformed,
  stale, altered or inconsistent checkpoint falls back to a full replay. When ``log_offset > 0`` its
  ``counts``/``transcript_revision`` cover only the durable prefix before that offset, because replay
  re-reads the tail from ``log_offset``; writing totals there would count the tail twice. A checkpoint
  with ``log_offset == 0`` or without seals is ignored in favour of a full replay. The seals are integrity
  checks for accidental/structural corruption, not authentication: an actor able to rewrite the log and
  recompute both seals is outside this module's trust boundary.

``append`` also checks the durable sequence before writing: segment ids are dense, a revision needs an earlier
segment, revisions never move backwards or conflict, a final is immutable without an explicit retranscription
token, and an exact duplicate batch is still accepted for at-least-once delivery.

The log is deliberately independent of the scheduler and store: the constructor takes a provider for the
transcript directory (for example ``store.transcript_dir``). It never creates the recording directory, opens
files with ``O_NOFOLLOW`` and private modes, and therefore cannot recreate a deleted recording. A newly
created log file also fsyncs its transcript directory entry. One writer per directory is assumed, as in
plan 54; a per-instance lock only protects callers of this object.

Replay, paging and exports are streaming. ``replay`` pages terminal segments out of the in-memory
``Transcript`` while it scans; per-segment replay/cursor state lives in unlinked sparse temporary disk indexes,
so live memory is independent of recording length even with a large pending or retranscription backlog.
``read_page`` keeps only the requested window plus one predecessor for join flags and validates stale or
conflicting revisions with exact log lookups, and exports consume those pages. A malformed record is always
corruption: only an unterminated final line (crash tail) is ignored/truncated and exact duplicate records are
tolerated.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import struct
import sys
import tempfile
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import (
    RecorderCorrupt,
    RecorderError,
    RecorderInvalid,
    RecorderNotFound,
    RecorderSecurityError,
)
from .transcript import (
    DEFAULT_MAX_TEXT_CHARS,
    MAX_ATTEMPTS,
    JoinPolicy,
    Transcript,
    TranscriptCheckpoint,
    TranscriptError,
    TranscriptSegment,
    join_boundary,
)

LOG_VERSION = 1
LOG_NAME = "log.jsonl"
CHECKPOINT_NAME = "checkpoint.json"

MAX_RECORD_BYTES = 256 * 1024
MAX_CHECKPOINT_BYTES = 1024 * 1024
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50

# Replay keeps the live set well below Transcript's 512 default. The threshold leaves headroom for an
# in-flight final, and 8 terminal segments remain as join context after a page.
REPLAY_MAX_ACTIVE = 512
REPLAY_PAGE_THRESHOLD = 256
REPLAY_KEEP = 8
# Late retranscription revisions for already-paged segments are validated at the tail. Their segment
# records are before the replay boundary, so only a small bounded window of revision numbers is kept.
MAX_PAGED_REVISIONS = 256
RECENT_SEGMENTS = 64
MAX_REVISION = (1 << 64) - 1

_STATUS_PENDING = 1
_STATUS_FINAL = 2
_STATUS_ERROR = 3
_STATUS_SKIPPED = 4
_STATUS_CODES = {
    "pending": _STATUS_PENDING,
    "final": _STATUS_FINAL,
    "error": _STATUS_ERROR,
    "skipped": _STATUS_SKIPPED,
}
_STATUS_NAMES = {value: key for key, value in _STATUS_CODES.items()}
_TERMINAL_CODES = frozenset({_STATUS_FINAL, _STATUS_ERROR, _STATUS_SKIPPED})

_PRIVATE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
_READ_BLOCK = 64 * 1024
_END_REASONS = frozenset({"stopped", "interrupted"})
_DURABLE_STATUSES = frozenset({"final", "error", "skipped"})
_STORE_CHECKPOINT = object()


def _is_int(value: Any, minimum: int = 0) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


def _corrupt(message: str, **details: Any) -> RecorderCorrupt:
    return RecorderCorrupt("transcript_corrupt", message, **details)


def _invalid(message: str, **details: Any) -> RecorderInvalid:
    return RecorderInvalid("invalid_transcript", message, **details)


# -- secure private filesystem primitives ---------------------------------------------------------------------


def _private_dir_info(path: Path, *, missing_ok: bool) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RecorderNotFound("recording_not_found", f"recording directory is gone: {path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RecorderSecurityError("directory_unsafe", f"transcript directory is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise RecorderSecurityError("directory_owner", f"transcript directory is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE:
        raise RecorderSecurityError("directory_mode", f"transcript directory must be mode 0700: {path}")
    return info


def _ensure_private_dir(path: Path) -> Path:
    """Create the transcript directory inside an existing, private recording directory.

    Missing parents are never created: if the recording directory is gone, this raises ``RecorderNotFound``.
    That property is what makes a late append unable to resurrect a deleted recording.
    """
    if _private_dir_info(path, missing_ok=True) is not None:
        return path
    parent = path.parent
    _private_dir_info(parent, missing_ok=False)
    for _attempt in range(4):
        try:
            path.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=False)
        except FileExistsError:
            _private_dir_info(path, missing_ok=False)
            return path
        os.chmod(path, _PRIVATE_DIR_MODE)
        _fsync_dir(parent)
        return path
    raise RecorderSecurityError("directory_race", f"transcript directory kept changing: {path}")


def _open_private(path: Path, flags: int, *, create: bool = False) -> int:
    try:
        existed = path.lstat()
    except FileNotFoundError:
        existed = None
    try:
        # O_NONBLOCK keeps a planted FIFO from blocking the recorder thread; the fstat check below then
        # rejects anything that is not a private regular file.
        fd = os.open(path, flags | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, _PRIVATE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise RecorderSecurityError("file_symlink", f"refusing to follow a transcript symlink: {path}") from exc
        if exc.errno in (errno.EISDIR, errno.ENXIO):
            raise RecorderSecurityError("file_type", f"transcript file is not a regular file: {path}") from exc
        raise
    try:
        if create and existed is None:
            os.fchmod(fd, _PRIVATE_MODE)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RecorderSecurityError("file_type", f"transcript file is not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise RecorderSecurityError("file_owner", f"transcript file is not owned by this user: {path}")
        if stat.S_IMODE(info.st_mode) != _PRIVATE_MODE:
            raise RecorderSecurityError("file_mode", f"transcript file must be mode 0600: {path}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)
    if sys.platform == "darwin":
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.ENOTTY):
                raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _fsync_fd(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _read_all(fd: int, limit: int) -> bytes:
    blocks: list[bytes] = []
    total = 0
    while True:
        block = os.read(fd, min(_READ_BLOCK, limit + 1 - total))
        if not block:
            return b"".join(blocks)
        blocks.append(block)
        total += len(block)
        if total > limit:
            raise _corrupt("transcript file is too large")


def _truncate_partial_tail(fd: int) -> None:
    """Drop an unterminated last line. Runs before replay and before append."""
    size = os.fstat(fd).st_size
    if size == 0:
        return
    if os.pread(fd, 1, size - 1) == b"\n":
        return
    position = size
    while position > 0:
        start = max(0, position - _READ_BLOCK)
        block = os.pread(fd, position - start, start)
        index = block.rfind(b"\n")
        if index >= 0:
            os.ftruncate(fd, start + index + 1)
            _fsync_fd(fd)
            return
        position = start
    os.ftruncate(fd, 0)
    _fsync_fd(fd)


# -- record validation and framing ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LogRecord:
    """One framed log record and its byte range. ``end_offset`` is the byte after its newline."""

    offset: int
    end_offset: int
    document: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return str(self.document["k"])


def _require_int(document: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = document.get(key)
    if not _is_int(value, minimum):
        raise _corrupt("transcript record has an invalid integer field", field=key, value=value)
    return value


def _require_generation(document: Mapping[str, Any]) -> int:
    return _require_int(document, "generation")


def _validate_segment_document(document: Mapping[str, Any]) -> TranscriptSegment:
    try:
        segment = TranscriptSegment.from_document(dict(document))
    except TranscriptError as exc:
        raise _corrupt("transcript revision is invalid", detail=exc.code) from exc
    if segment.status not in _DURABLE_STATUSES:
        raise _corrupt("only final, error and skipped revisions are durable", status=segment.status)
    if not _is_int(segment.segment_id) or not _is_int(segment.revision, minimum=1):
        raise _corrupt("transcript revision ids are invalid")
    if segment.revision > MAX_REVISION:
        raise _corrupt("transcript revision exceeds the supported range", revision=segment.revision)
    if not _is_int(segment.start) or not _is_int(segment.end) or segment.end < segment.start:
        raise _corrupt("transcript revision range is invalid")
    if not _is_int(segment.attempt, minimum=1) or segment.attempt > MAX_ATTEMPTS:
        raise _corrupt("transcript revision attempt is invalid", attempt=segment.attempt)
    if segment.window_start is not None and (
        not _is_int(segment.window_start) or segment.window_start > segment.start
    ):
        raise _corrupt("transcript revision window_start is invalid")
    if not isinstance(segment.text, str) or not isinstance(segment.raw_text, str):
        raise _corrupt("transcript revision text is invalid")
    if len(segment.text) > DEFAULT_MAX_TEXT_CHARS or len(segment.raw_text) > DEFAULT_MAX_TEXT_CHARS:
        raise _corrupt("transcript revision text is too long")
    if segment.retranscription_token is not None and not isinstance(segment.retranscription_token, str):
        raise _corrupt("transcript retranscription token is invalid")
    return segment


def _validate_record(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise _corrupt("transcript log line is not an object")
    if document.get("v") != LOG_VERSION:
        raise _corrupt("unsupported transcript log version", version=document.get("v"))
    kind = document.get("k")
    if kind == "seg":
        _require_generation(document)
        start = _require_int(document, "start")
        end = _require_int(document, "end")
        window_start = _require_int(document, "window_start")
        window_end = _require_int(document, "window_end")
        _require_int(document, "segment_id")
        if _require_int(document, "final_revision", minimum=1) > MAX_REVISION:
            raise _corrupt("transcript final_revision exceeds the supported range")
        if end < start or window_start > start or window_end < end:
            raise _corrupt("transcript segment range is invalid")
        if not isinstance(document.get("close_reason"), str):
            raise _corrupt("transcript segment close_reason is invalid")
    elif kind == "rev":
        _validate_segment_document(document)
    elif kind == "end":
        _require_generation(document)
        _require_int(document, "end_sample")
        if document.get("reason") not in _END_REASONS:
            raise _corrupt("transcript end reason is invalid", reason=document.get("reason"))
    elif kind == "resume":
        if "generation" in document:
            _require_generation(document)
        _require_int(document, "from_sample")
    else:
        raise _corrupt("unknown transcript log record kind", kind=kind)


def _encode_record(document: Mapping[str, Any]) -> bytes:
    framed = dict(document)
    framed.setdefault("v", LOG_VERSION)
    _validate_record(framed)
    try:
        data = json.dumps(framed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise _invalid("transcript record is not JSON serializable") from exc
    encoded = data.encode("utf-8")
    if len(encoded) + 1 > MAX_RECORD_BYTES:
        raise _invalid("transcript record is too large")
    return encoded + b"\n"


def _segments_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("generation", "segment_id", "start", "end", "window_start", "window_end", "close_reason", "final_revision"))


def _segment_fields(segment: TranscriptSegment) -> tuple[Any, ...]:
    return (
        segment.segment_id,
        segment.revision,
        segment.start,
        segment.end,
        segment.status,
        segment.raw_text,
        segment.attempt,
        segment.generation,
        segment.window_start,
        segment.retranscription_token,
    )


def _raw_digest(text: str) -> int:
    digest = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _validate_compact_transition(
    previous_start: int,
    previous_end: int,
    previous_status: int,
    previous_raw_digest: int,
    current: TranscriptSegment,
) -> str | None:
    """Transition check for read_page using compact disk-indexed previous fields."""
    if current.retranscription_token is None:
        if current.start != previous_start or current.end < previous_end:
            return "boundary_regression"
        if (
            _STATUS_NAMES.get(previous_status, "pending") == "final"
            and not (
                current.status == "final"
                and current.end == previous_end
                and _raw_digest(current.raw_text) == previous_raw_digest
            )
        ):
            return "final_immutable"
    return None


def _validate_transition(previous: tuple[Any, ...], current: tuple[Any, ...]) -> str | None:
    """Mirror Transcript's durable transition rules for append-time sequence validation."""
    previous_start, previous_end, previous_status, previous_raw = previous[2], previous[3], previous[4], previous[5]
    current_start, current_end, current_status, current_raw = current[2], current[3], current[4], current[5]
    current_token = current[9]
    if current_token is None and (current_start != previous_start or current_end < previous_end):
        return "boundary_regression"
    if (
        previous_status == "final"
        and current_token is None
        and not (current_status == "final" and current_end == previous_end and current_raw == previous_raw)
    ):
        return "final_immutable"
    return None


def _document_fields(document: Mapping[str, Any]) -> tuple[Any, ...] | None:
    try:
        segment = TranscriptSegment.from_document(dict(document))
    except TranscriptError:
        return None
    return _segment_fields(segment)


# -- public result types --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalCursor:
    """The next segment record that still needs a durable final revision."""

    segment_id: int
    log_offset: int

    def as_document(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "log_offset": self.log_offset}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> FinalCursor:
        if not isinstance(document, Mapping):
            raise _corrupt("checkpoint final_cursor is not an object")
        return cls(
            segment_id=_require_int(document, "segment_id"),
            log_offset=_require_int(document, "log_offset"),
        )


@dataclass(frozen=True)
class Segmentation:
    """Segmentation resume point, mirroring the ``end``/``resume`` records."""

    next_segment_id: int
    resume_sample: int
    ended_at: int | None

    def as_document(self) -> dict[str, Any]:
        return {
            "next_segment_id": self.next_segment_id,
            "resume_sample": self.resume_sample,
            "ended_at": self.ended_at,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Segmentation:
        if not isinstance(document, Mapping):
            raise _corrupt("checkpoint segmentation is not an object")
        ended_at = document.get("ended_at")
        if ended_at is not None and not _is_int(ended_at):
            raise _corrupt("checkpoint ended_at is invalid")
        return cls(
            next_segment_id=_require_int(document, "next_segment_id"),
            resume_sample=_require_int(document, "resume_sample"),
            ended_at=ended_at,
        )


@dataclass(frozen=True)
class Counts:
    """Latest-status counts. ``pending`` includes segments with a non-terminal error revision."""

    final: int = 0
    error: int = 0
    skipped: int = 0
    pending: int = 0

    def as_document(self) -> dict[str, int]:
        return {"final": self.final, "error": self.error, "skipped": self.skipped, "pending": self.pending}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Counts:
        if not isinstance(document, Mapping):
            raise _corrupt("checkpoint counts is not an object")
        return cls(
            final=_require_int(document, "final"),
            error=_require_int(document, "error"),
            skipped=_require_int(document, "skipped"),
            pending=_require_int(document, "pending"),
        )


def _checkpoint_body_sha256(document: Mapping[str, Any]) -> str:
    body = {
        key: value
        for key, value in document.items()
        if key not in {"body_sha256", "prefix_sha256"}
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    """The atomic checkpoint hint.

    ``transcript`` restores the paged boundary. ``final_cursor`` and ``segmentation`` describe the latest
    observed state. ``counts`` and ``transcript_revision`` describe only records before ``log_offset`` when
    that offset is non-zero; replay applies the records from ``log_offset`` again and adds them back.
    The seals are accidental/structural-corruption checks, not authentication or signatures.
    """

    generation: int
    transcript: TranscriptCheckpoint
    log_offset: int
    final_cursor: FinalCursor | None
    segmentation: Segmentation
    transcript_revision: int
    counts: Counts
    state: str
    reason: str | None
    # Integrity seals added by ``write_checkpoint``. ``body_sha256`` covers this object without the seals;
    # ``prefix_sha256`` covers log bytes ``[0, log_offset)``. A checkpoint without both is never trusted
    # for a tail replay (the log is replayed in full instead).
    body_sha256: str | None = None
    prefix_sha256: str | None = None

    def as_document(self) -> dict[str, Any]:
        document = {
            "v": LOG_VERSION,
            "generation": self.generation,
            "transcript": self.transcript.as_document(),
            "log_offset": self.log_offset,
            "final_cursor": None if self.final_cursor is None else self.final_cursor.as_document(),
            "segmentation": self.segmentation.as_document(),
            "transcript_revision": self.transcript_revision,
            "counts": self.counts.as_document(),
            "state": self.state,
            "reason": self.reason,
        }
        if self.body_sha256 is not None:
            document["body_sha256"] = self.body_sha256
        if self.prefix_sha256 is not None:
            document["prefix_sha256"] = self.prefix_sha256
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> Checkpoint:
        if not isinstance(document, Mapping) or document.get("v") != LOG_VERSION:
            raise _corrupt("unsupported transcript checkpoint version")
        generation = _require_int(document, "generation")
        transcript_document = document.get("transcript")
        if not isinstance(transcript_document, Mapping):
            raise _corrupt("checkpoint transcript state is invalid")
        try:
            transcript = TranscriptCheckpoint.from_document(dict(transcript_document))
        except (KeyError, TypeError, ValueError, TranscriptError) as exc:
            raise _corrupt("checkpoint transcript state is invalid") from exc
        if transcript.generation != generation:
            raise _corrupt("checkpoint generation does not match its transcript")
        log_offset = _require_int(document, "log_offset")
        final_document = document.get("final_cursor")
        final_cursor = None if final_document is None else FinalCursor.from_document(final_document)
        try:
            segmentation = Segmentation.from_document(document["segmentation"])
        except (KeyError, TypeError) as exc:
            raise _corrupt("checkpoint segmentation is missing") from exc
        if segmentation.next_segment_id <= transcript.paged_through:
            raise _corrupt("checkpoint next_segment_id is at or before its paged boundary")
        if final_cursor is not None and (
            final_cursor.segment_id <= transcript.paged_through
            or final_cursor.segment_id >= segmentation.next_segment_id
        ):
            raise _corrupt("checkpoint final_cursor is outside the unpaged range")
        if final_cursor is not None and final_cursor.log_offset < log_offset and log_offset > 0:
            # A tail checkpoint must not point behind its own boundary.
            raise _corrupt("checkpoint final_cursor precedes the log boundary")
        transcript_revision = _require_int(document, "transcript_revision")
        try:
            counts = Counts.from_document(document["counts"])
        except (KeyError, TypeError) as exc:
            raise _corrupt("checkpoint counts are missing") from exc
        state = document.get("state")
        if not isinstance(state, str) or not state:
            raise _corrupt("checkpoint transcription state is invalid")
        reason = document.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise _corrupt("checkpoint reason is invalid")
        body_sha256 = document.get("body_sha256")
        prefix_sha256 = document.get("prefix_sha256")
        for name, value in (("body_sha256", body_sha256), ("prefix_sha256", prefix_sha256)):
            if value is not None and (
                not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise _corrupt("checkpoint integrity seal is invalid", field=name)
        if body_sha256 is not None and _checkpoint_body_sha256(document) != body_sha256:
            raise _corrupt("checkpoint body seal does not match its fields")
        return cls(
            generation=generation,
            transcript=transcript,
            log_offset=log_offset,
            final_cursor=final_cursor,
            segmentation=segmentation,
            transcript_revision=transcript_revision,
            counts=counts,
            state=state,
            reason=reason,
            body_sha256=body_sha256,
            prefix_sha256=prefix_sha256,
        )


@dataclass(frozen=True)
class ReplayCursors:
    """State a scheduler needs after replay, without holding the transcript in memory twice."""

    generation: int
    transcript: TranscriptCheckpoint
    log_offset: int
    end_offset: int
    final_cursor: FinalCursor | None
    segmentation: Segmentation
    transcript_revision: int
    counts: Counts
    # Aggregates for records before ``log_offset`` only. A checkpoint carries these, because replay applies
    # the records from ``log_offset`` again; storing totals would count the tail twice.
    prefix_transcript_revision: int
    prefix_counts: Counts
    state: str | None
    reason: str | None

    def as_checkpoint(self, state: str, *, reason: str | None = None) -> Checkpoint:
        if self.log_offset == 0:
            revision = self.transcript_revision
            counts = self.counts
        else:
            revision = self.prefix_transcript_revision
            counts = self.prefix_counts
        return Checkpoint(
            generation=self.generation,
            transcript=self.transcript,
            log_offset=self.log_offset,
            final_cursor=self.final_cursor,
            segmentation=self.segmentation,
            transcript_revision=revision,
            counts=counts,
            state=state,
            reason=reason,
        )


@dataclass(frozen=True)
class Page:
    """One bounded page of latest revisions (or pending segment summaries)."""

    segments: tuple[dict[str, Any], ...]
    next_after: int | None


@dataclass
class _AppendState:
    """Bounded append-time sequence state, rebuilt from the log once per instance."""

    generation: int | None
    next_segment_id: int
    recent_segments: OrderedDict[int, Mapping[str, Any]]
    latest_revisions: OrderedDict[int, tuple[int, tuple[Any, ...]]]


@dataclass(frozen=True)
class _Summary:
    """Authoritative cursors and aggregates derived by a bounded disk-indexed log scan."""

    counts: Counts
    prefix_counts: Counts
    transcript_revision: int
    prefix_transcript_revision: int
    next_segment_id: int
    resume_sample: int
    ended_at: int | None
    pending_id: int | None
    tail_offset: int
    pending_offset: int | None


# status, final_revision, highest_revision, latest start/end, latest status, latest raw-text digest
_STATE_RECORD = struct.Struct(">B7xQQQQB7xQ")


class _DiskIndex:
    """Sparse temporary per-segment index with O(1) live memory.

    The file lives under the transcript directory (or the system temporary directory when the transcript
    directory does not exist) and is unlinked immediately. It holds no transcript text; it is only replay
    bookkeeping. Keeping this state off the heap is what makes replay independent of recording length.
    """

    def __init__(self, directory: Path | None) -> None:
        self._file = tempfile.TemporaryFile(dir=directory)  # noqa: SIM115 - closed by close()/replay finally
        self._fd = self._file.fileno()

    def close(self) -> None:
        self._file.close()

    def _read(self, segment_id: int) -> tuple[int, int, int, int, int, int, int]:
        if segment_id < 0:
            raise _corrupt("negative segment id in replay state")
        data = os.pread(self._fd, _STATE_RECORD.size, segment_id * _STATE_RECORD.size)
        if len(data) < _STATE_RECORD.size:
            return (0, 0, 0, 0, 0, 0, 0)
        return _STATE_RECORD.unpack(data)

    def get(self, segment_id: int) -> tuple[int, int, int, int, int, int, int]:
        return self._read(segment_id)

    def set(
        self,
        segment_id: int,
        *,
        status: int | None = None,
        final_revision: int | None = None,
        highest_revision: int | None = None,
        latest_start: int | None = None,
        latest_end: int | None = None,
        latest_status: int | None = None,
        latest_raw_digest: int | None = None,
    ) -> None:
        current = self._read(segment_id)
        values = [
            current[0] if status is None else status,
            current[1] if final_revision is None else final_revision,
            current[2] if highest_revision is None else highest_revision,
            current[3] if latest_start is None else latest_start,
            current[4] if latest_end is None else latest_end,
            current[5] if latest_status is None else latest_status,
            current[6] if latest_raw_digest is None else latest_raw_digest,
        ]
        if values[1] > MAX_REVISION or values[2] > MAX_REVISION:
            raise _corrupt("replay revision exceeds the supported range")
        data = _STATE_RECORD.pack(
            values[0] & 0xFF,
            values[1],
            values[2],
            values[3],
            values[4],
            values[5] & 0xFF,
            values[6],
        )
        view = memoryview(data)
        position = segment_id * _STATE_RECORD.size
        while view:
            written = os.pwrite(self._fd, view, position)
            if written <= 0:
                raise OSError(errno.EIO, "short pwrite to replay index")
            view = view[written:]
            position += written


# -- the log --------------------------------------------------------------------------------------------------


class TranscriptLog:
    """Durable transcript log for one recording directory.

    ``rec_dir_provider`` is a path or a zero-argument provider returning the transcript directory, typically
    ``store.transcript_dir(recording_id)``. The provider is called on every operation so a deleted recording
    is observed and never recreated; a plain path is accepted for focused tests and simple embedding.
    """

    def __init__(
        self,
        rec_dir_provider: str | os.PathLike[str] | Callable[[], str | os.PathLike[str]],
        *,
        policy: JoinPolicy | None = None,
    ) -> None:
        self._source = rec_dir_provider
        self._policy = policy or JoinPolicy()
        self._lock = threading.RLock()
        self._corrupt: str | None = None
        self._append_state_cache: _AppendState | None = None
        self._log_entry_durable = False

    # -- paths ---------------------------------------------------------------------------------------------

    def _directory(self) -> Path:
        value = self._source() if callable(self._source) else self._source
        return Path(value)

    def _existing_directory(self) -> Path | None:
        path = self._directory()
        if _private_dir_info(path, missing_ok=True) is not None:
            return path
        parent_info = _private_dir_info(path.parent, missing_ok=True)
        if parent_info is None:
            raise RecorderNotFound("recording_not_found", f"recording directory is gone: {path.parent}")
        return None

    # -- framing -------------------------------------------------------------------------------------------

    def _iter_records(self, start: int = 0, *, truncate_tail: bool = False) -> Iterator[LogRecord]:
        try:
            yield from self._scan_records(start, truncate_tail=truncate_tail)
        except RecorderCorrupt:
            self._corrupt = "transcript_corrupt"
            raise

    def _scan_records(self, start: int = 0, *, truncate_tail: bool = False) -> Iterator[LogRecord]:
        if not _is_int(start):
            raise _invalid("log offset must be a non-negative integer")
        directory = self._existing_directory()
        if directory is None:
            if start:
                raise _invalid("log offset points past a missing log", offset=start)
            return
        path = directory / LOG_NAME
        try:
            fd = _open_private(path, os.O_RDWR if truncate_tail else os.O_RDONLY)
        except FileNotFoundError:
            if start:
                raise _invalid("log offset points past a missing log", offset=start) from None
            return
        try:
            size = os.fstat(fd).st_size
            if start > size:
                raise _invalid("log offset is past the end of the log", offset=start, size=size)
            if start and os.pread(fd, 1, start - 1) != b"\n":
                raise _invalid("log offset is not at a record boundary", offset=start)
            if truncate_tail:
                _truncate_partial_tail(fd)
                size = os.fstat(fd).st_size
            if start > size:
                raise _invalid("log offset is past the end of the log", offset=start, size=size)
            os.lseek(fd, start, os.SEEK_SET)
            buffer = bytearray()
            line_start = start
            while True:
                block = os.read(fd, _READ_BLOCK)
                if not block:
                    break
                buffer.extend(block)
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break
                    raw = bytes(buffer[:index])
                    del buffer[: index + 1]
                    if not raw:
                        raise _corrupt("empty line in transcript log", offset=line_start)
                    if len(raw) > MAX_RECORD_BYTES:
                        raise _corrupt("transcript log line is too large", offset=line_start)
                    try:
                        document = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise _corrupt("invalid JSON in transcript log", offset=line_start) from exc
                    if not isinstance(document, Mapping):
                        raise _corrupt("transcript log line is not an object", offset=line_start)
                    _validate_record(document)
                    end_offset = line_start + len(raw) + 1
                    yield LogRecord(line_start, end_offset, document)
                    line_start = end_offset
                if len(buffer) > MAX_RECORD_BYTES:
                    raise _corrupt("transcript log line is too large", offset=line_start)
            # An unterminated final line is a crash tail: it was already truncated when requested, and is
            # otherwise ignored (never parsed as a complete record).
        finally:
            os.close(fd)

    # -- append --------------------------------------------------------------------------------------------

    def append(self, records: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> int:
        """Append one record or a batch atomically and durably; return the new byte offset.

        Every record is validated, framed and checked against the durable sequence before the file is opened,
        so an orphan revision, a segment gap or a conflicting sequence writes nothing. Exact duplicate
        records (a retried append after a crash) remain accepted, preserving at-least-once delivery; replay
        tolerates those duplicate lines. A partial tail is truncated first, a batch is written with one
        ``write`` and fsynced once. If the transcript or recording directory has been removed, this raises and
        never recreates it.
        """
        batch = [records] if isinstance(records, Mapping) else list(records)
        if not batch:
            return self._log_size()
        try:
            framed = [dict(record) if isinstance(record, Mapping) else record for record in batch]
            for record in framed:
                if isinstance(record, Mapping):
                    record.setdefault("v", LOG_VERSION)
            encoded = [_encode_record(record) for record in framed]
        except RecorderCorrupt as exc:
            raise _invalid("invalid transcript record", detail=str(exc), **exc.details) from exc
        with self._lock:
            if self._corrupt is not None:
                raise _corrupt("transcript log is corrupt; no new records are appended", reason=self._corrupt)
            before = self._log_size()
            state = self._load_append_state()
            next_state = self._validate_append_sequence(state, framed, before)
            transcript_dir = _ensure_private_dir(self._directory())
            path = transcript_dir / LOG_NAME
            try:
                path.lstat()
                existed = True
            except FileNotFoundError:
                existed = False
            fd = _open_private(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, create=True)
            try:
                start = os.fstat(fd).st_size
                _truncate_partial_tail(fd)
                start = os.fstat(fd).st_size
                _write_all(fd, b"".join(encoded))
                _fsync_fd(fd)
                # A newly created log (or one whose directory entry is not yet known durable) needs the
                # transcript directory fsynced as well; otherwise the file contents can survive while its
                # name is lost in a crash.
                if not existed or not self._log_entry_durable:
                    _fsync_dir(transcript_dir)
                    self._log_entry_durable = True
                self._append_state_cache = next_state
                return start + sum(len(item) for item in encoded)
            except BaseException:
                self._append_state_cache = None
                self._log_entry_durable = False
                raise
            finally:
                os.close(fd)

    def _load_append_state(self) -> _AppendState:
        if self._append_state_cache is not None:
            return self._append_state_cache
        generation: int | None = None
        expected_segment = 0
        recent: OrderedDict[int, Mapping[str, Any]] = OrderedDict()
        latest: OrderedDict[int, tuple[int, tuple[Any, ...]]] = OrderedDict()
        for record in self._iter_records(0, truncate_tail=True):
            document = record.document
            record_generation = document.get("generation", generation)
            if generation is None:
                generation = int(record_generation) if record_generation is not None else 0
            elif int(record_generation) != generation:
                raise _corrupt("transcript record generation changed", expected=generation, actual=record_generation)
            if record.kind == "seg":
                segment_id = int(document["segment_id"])
                if segment_id < expected_segment:
                    previous = recent.get(segment_id)
                    if previous is None or not _segments_equal(previous, document):
                        raise _corrupt("conflicting transcript segment records", segment_id=segment_id)
                    continue
                if segment_id > expected_segment:
                    raise _corrupt("transcript segment ids are not dense", expected=expected_segment, actual=segment_id)
                expected_segment = segment_id + 1
                recent[segment_id] = document
                while len(recent) > RECENT_SEGMENTS:
                    recent.popitem(last=False)
            elif record.kind == "rev":
                segment_id = int(document["segment_id"])
                if segment_id >= expected_segment:
                    raise _corrupt("durable revision has no segment record", segment_id=segment_id)
                segment = _validate_segment_document(document)
                fields = _segment_fields(segment)
                previous = latest.get(segment_id)
                if previous is None:
                    previous = self._latest_revision_before(segment_id, record.offset)
                if previous is not None:
                    previous_revision, previous_fields = previous
                    if segment.revision < previous_revision:
                        # A repeated older revision is a duplicate only when the exact earlier record is
                        # identical. Skipping it without that check would let a forged stale line pass.
                        earlier = self._revision_before(segment_id, segment.revision, record.offset)
                        if earlier is None or earlier != fields:
                            raise _corrupt(
                                "stale transcript revision conflicts with the durable log",
                                segment_id=segment_id,
                                revision=segment.revision,
                            )
                        continue
                    if segment.revision == previous_revision:
                        if fields != previous_fields:
                            raise _corrupt("conflicting transcript revisions", segment_id=segment_id)
                        continue
                    transition = _validate_transition(previous_fields, fields)
                    if transition is not None:
                        raise _corrupt(
                            "transcript revision is not a legal durable transition",
                            segment_id=segment_id,
                            revision=segment.revision,
                            reason=transition,
                        )
                latest[segment_id] = (segment.revision, fields)
                while len(latest) > MAX_PAGED_REVISIONS:
                    latest.popitem(last=False)
        state = _AppendState(generation, expected_segment, recent, latest)
        self._append_state_cache = state
        return state

    def _revision_before(
        self,
        segment_id: int,
        revision: int,
        before_offset: int,
    ) -> tuple[Any, ...] | None:
        found: tuple[Any, ...] | None = None
        for record in self._iter_records(0):
            if record.offset >= before_offset:
                break
            if record.kind != "rev" or int(record.document.get("segment_id", -1)) != segment_id:
                continue
            segment = _validate_segment_document(record.document)
            if segment.revision != revision:
                continue
            fields = _segment_fields(segment)
            if found is not None and fields != found:
                raise _corrupt("conflicting transcript revisions", segment_id=segment_id, revision=revision)
            found = fields
        return found

    def _segment_before(self, segment_id: int, before_offset: int) -> Mapping[str, Any] | None:
        found: Mapping[str, Any] | None = None
        for record in self._iter_records(0):
            if record.offset >= before_offset:
                break
            if record.kind != "seg" or int(record.document["segment_id"]) != segment_id:
                continue
            if found is not None and not _segments_equal(found, record.document):
                raise _corrupt("conflicting transcript segment records", segment_id=segment_id)
            found = record.document
        return found

    def _validate_append_sequence(
        self,
        state: _AppendState,
        batch: list[Mapping[str, Any]],
        before_offset: int,
    ) -> _AppendState:
        generation = state.generation
        next_segment = state.next_segment_id
        recent = OrderedDict(state.recent_segments)
        latest = OrderedDict(state.latest_revisions)
        batch_history: dict[int, dict[int, tuple[Any, ...]]] = {}
        for document in batch:
            record_generation = document.get("generation", generation)
            if generation is None:
                generation = int(record_generation) if record_generation is not None else 0
            elif int(record_generation) != generation:
                raise _invalid(
                    "transcript record generation does not match the log",
                    expected=generation,
                    actual=record_generation,
                )
            kind = document.get("k")
            if kind == "seg":
                segment_id = int(document["segment_id"])
                if segment_id == next_segment:
                    next_segment += 1
                    recent[segment_id] = document
                    while len(recent) > RECENT_SEGMENTS:
                        recent.popitem(last=False)
                elif segment_id < next_segment:
                    previous = recent.get(segment_id)
                    if previous is None:
                        previous = self._segment_before(segment_id, before_offset)
                    if previous is None or not _segments_equal(previous, document):
                        raise _invalid(
                            "transcript segment conflicts with the durable sequence",
                            segment_id=segment_id,
                        )
                else:
                    raise _invalid(
                        "transcript segment ids must be dense",
                        expected=next_segment,
                        actual=segment_id,
                    )
            elif kind == "rev":
                segment_id = int(document["segment_id"])
                if segment_id >= next_segment:
                    raise _invalid("transcript revision has no durable segment record", segment_id=segment_id)
                segment = _validate_segment_document(document)
                fields = _segment_fields(segment)
                previous = latest.get(segment_id)
                if previous is None:
                    previous = self._latest_revision_before(segment_id, before_offset)
                history = batch_history.setdefault(segment_id, {})
                if previous is not None:
                    previous_revision, previous_fields = previous
                    if segment.revision < previous_revision:
                        # At-least-once delivery may repeat an older accepted revision. Accept it only when
                        # the exact durable revision already exists; a conflicting or invented one is a
                        # sequence error, not a duplicate.
                        known = history.get(segment.revision)
                        if known is None:
                            known = self._revision_before(segment_id, segment.revision, before_offset)
                        if known is None or known != fields:
                            raise _invalid(
                                "transcript revision is older than the durable revision",
                                segment_id=segment_id,
                                revision=segment.revision,
                            )
                        continue
                    if segment.revision == previous_revision:
                        if fields != previous_fields:
                            raise _invalid(
                                "transcript revision conflicts with the durable revision",
                                segment_id=segment_id,
                                revision=segment.revision,
                            )
                        continue
                    transition = _validate_transition(previous_fields, fields)
                    if transition is not None:
                        raise _invalid(
                            "transcript revision is not a legal durable transition",
                            segment_id=segment_id,
                            revision=segment.revision,
                            reason=transition,
                        )
                latest[segment_id] = (segment.revision, fields)
                history[segment.revision] = fields
                while len(latest) > MAX_PAGED_REVISIONS:
                    latest.popitem(last=False)
        return _AppendState(generation, next_segment, recent, latest)

    def _log_size(self) -> int:
        directory = self._existing_directory()
        if directory is None:
            return 0
        path = directory / LOG_NAME
        try:
            fd = _open_private(path, os.O_RDONLY)
        except FileNotFoundError:
            return 0
        try:
            return os.fstat(fd).st_size
        finally:
            os.close(fd)

    # -- checkpoint ----------------------------------------------------------------------------------------

    def read_checkpoint(self) -> Checkpoint | None:
        """Read and validate ``checkpoint.json``; missing returns None, malformed raises corruption."""
        directory = self._existing_directory()
        if directory is None:
            return None
        path = directory / CHECKPOINT_NAME
        try:
            fd = _open_private(path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            data = _read_all(fd, MAX_CHECKPOINT_BYTES)
        finally:
            os.close(fd)
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _corrupt("invalid JSON in transcript checkpoint") from exc
        return Checkpoint.from_document(document)

    def _prefix_sha256(self, offset: int) -> str:
        digest = hashlib.sha256()
        if offset == 0:
            return digest.hexdigest()
        directory = self._existing_directory()
        if directory is None:
            raise _invalid("checkpoint prefix is missing")
        path = directory / LOG_NAME
        try:
            fd = _open_private(path, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise _invalid("checkpoint prefix is missing") from exc
        try:
            if os.fstat(fd).st_size < offset:
                raise _invalid("checkpoint prefix is past the end of the log")
            if os.pread(fd, 1, offset - 1) != b"\n":
                raise _invalid("checkpoint prefix is not at a record boundary")
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = offset
            while remaining:
                block = os.read(fd, min(_READ_BLOCK, remaining))
                if not block:
                    raise _invalid("checkpoint prefix is shorter than expected")
                digest.update(block)
                remaining -= len(block)
        finally:
            os.close(fd)
        return digest.hexdigest()

    def write_checkpoint(self, checkpoint: Checkpoint | Mapping[str, Any]) -> None:
        """Atomically write a checkpoint (temp + fsync + rename + directory fsync).

        The written object is sealed with a body hash and a hash of the log prefix it describes. A later
        reader can therefore detect an altered checkpoint; replay also re-hashes the prefix against the
        authoritative log and falls back to a full replay if either seal is missing or does not match.
        """
        value = checkpoint if isinstance(checkpoint, Checkpoint) else Checkpoint.from_document(checkpoint)
        with self._lock:
            document = value.as_document()
            document.pop("body_sha256", None)
            document.pop("prefix_sha256", None)
            document["prefix_sha256"] = self._prefix_sha256(value.log_offset)
            document["body_sha256"] = _checkpoint_body_sha256(document)
            data = (json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
            if len(data) > MAX_CHECKPOINT_BYTES:
                raise _invalid("transcript checkpoint is too large")
            transcript_dir = _ensure_private_dir(self._directory())
            temporary = transcript_dir / f".{CHECKPOINT_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            try:
                fd = _open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True)
                try:
                    _write_all(fd, data)
                    _fsync_fd(fd)
                finally:
                    os.close(fd)
                os.replace(temporary, transcript_dir / CHECKPOINT_NAME)
                _fsync_dir(transcript_dir)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    # -- replay --------------------------------------------------------------------------------------------

    def _checkpoint_hint(self, checkpoint: object) -> Checkpoint | None:
        if checkpoint is _STORE_CHECKPOINT:
            try:
                value = self.read_checkpoint()
            except RecorderCorrupt:
                return None
        elif checkpoint is None:
            return None
        elif isinstance(checkpoint, Checkpoint):
            value = checkpoint
        else:
            try:
                value = Checkpoint.from_document(checkpoint)
            except (RecorderCorrupt, TypeError, KeyError, ValueError):
                return None
        if (
            value is None
            or value.log_offset <= 0
            or value.transcript.paged_through < 0
            or value.prefix_sha256 is None
            or value.body_sha256 is None
        ):
            return None
        try:
            actual_prefix = self._prefix_sha256(value.log_offset)
        except (RecorderError, OSError):
            return None
        if actual_prefix != value.prefix_sha256:
            return None
        return value

    def replay(self, checkpoint: object = _STORE_CHECKPOINT) -> tuple[Transcript, ReplayCursors]:
        """Replay the log into a bounded transcript and return the scheduler cursors.

        The default uses the stored checkpoint when it is usable. ``None`` forces a full replay. A supplied
        checkpoint is only a hint: a malformed, stale, altered or inconsistent one falls back to a full replay.
        Its seals are verified against its own body and the authoritative log prefix before it is trusted.

        Live memory is independent of recording length. Per-segment replay state (status, final-revision
        floor) lives in an unlinked temporary disk index, and cursors/aggregates come from a second bounded
        disk-indexed scan at the end.
        """
        with self._lock:
            hint = self._checkpoint_hint(checkpoint)
            start = hint.log_offset if hint is not None else 0
            records = self._iter_records(start, truncate_tail=True)
            first = next(records, None)
            if hint is not None:
                usable = first is None or (
                    first.document.get("generation", hint.generation) == hint.generation
                    and (
                        first.kind in {"end", "resume"}
                        or (first.kind == "seg" and first.document["segment_id"] == hint.transcript.paged_through + 1)
                    )
                )
                if not usable:
                    records.close()
                    hint = None
                    start = 0
                    records = self._iter_records(0, truncate_tail=True)
                    first = next(records, None)
            generation = hint.generation if hint is not None else None
            if generation is None:
                generation = int(first.document.get("generation", 0)) if first is not None else 0
            paged = hint.transcript.paged_through if hint is not None else -1
            directory = self._existing_directory()
            index = _DiskIndex(directory)
            highest_index: _DiskIndex | None = None
            try:
                transcript = Transcript(
                    generation=generation,
                    max_active_segments=REPLAY_MAX_ACTIVE,
                    persist_revisions=lambda _records: None,
                )
                if hint is not None:
                    try:
                        transcript.restore_checkpoint(hint.transcript)
                    except TranscriptError:
                        # The hint was structurally valid but inconsistent with its own transcript state.
                        records.close()
                        hint = None
                        start = 0
                        records = self._iter_records(0, truncate_tail=True)
                        first = next(records, None)
                        generation = int(first.document.get("generation", 0)) if first is not None else 0
                        paged = -1
                        transcript = Transcript(
                            generation=generation,
                            max_active_segments=REPLAY_MAX_ACTIVE,
                            persist_revisions=lambda _records: None,
                        )

                expected_segment = paged + 1
                recent_segments: OrderedDict[int, Mapping[str, Any]] = OrderedDict()
                active_status: dict[int, int] = {}
                highest_index = _DiskIndex(directory)
                end_offset = start
                if start:
                    # Seed revision watermarks from the sealed prefix so a paged late retry needs one
                    # bounded disk lookup, not a log scan or an in-memory per-segment map.
                    seeded_expected = 0
                    for seeded in self._iter_records(0):
                        if seeded.offset >= start:
                            break
                        if seeded.kind == "seg":
                            seeded_id = int(seeded.document["segment_id"])
                            if seeded_id < seeded_expected:
                                continue
                            if seeded_id > seeded_expected:
                                raise _corrupt(
                                    "transcript segment ids are not dense",
                                    expected=seeded_expected,
                                    actual=seeded_id,
                                )
                            seeded_expected = seeded_id + 1
                        elif seeded.kind == "rev":
                            seeded_segment = _validate_segment_document(seeded.document)
                            seeded_highest = int(highest_index.get(seeded_segment.segment_id)[2])
                            if seeded_segment.revision > seeded_highest:
                                highest_index.set(seeded_segment.segment_id, highest_revision=seeded_segment.revision)

                def remember_segment(segment_id: int, document: Mapping[str, Any]) -> None:
                    recent_segments[segment_id] = document
                    recent_segments.move_to_end(segment_id)
                    while len(recent_segments) > RECENT_SEGMENTS:
                        recent_segments.popitem(last=False)

                def reserve_or_fail(segment_id: int, revision: int) -> None:
                    try:
                        transcript.reserve_through(segment_id, revision)
                    except TranscriptError as exc:
                        if exc.code == "segment_paged_out":
                            return
                        if exc.code == "active_segments_full":
                            maybe_page()
                            try:
                                transcript.reserve_through(segment_id, revision)
                            except TranscriptError as retry:
                                raise _corrupt(
                                    "replay could not restore a revision floor",
                                    segment_id=segment_id,
                                    reason=retry.code,
                                ) from retry
                            return
                        raise _corrupt(
                            "replay could not restore a revision floor",
                            segment_id=segment_id,
                            reason=exc.code,
                        ) from exc

                def maybe_page() -> None:
                    nonlocal paged
                    if len(transcript) < REPLAY_PAGE_THRESHOLD:
                        return
                    ids = transcript.active_segment_ids
                    if not ids:
                        return
                    terminal: list[int] = []
                    expected = ids[0]
                    for segment_id in ids:
                        if segment_id != expected:
                            break
                        if active_status.get(segment_id, _STATUS_PENDING) not in _TERMINAL_CODES:
                            break
                        terminal.append(segment_id)
                        expected += 1
                    if len(terminal) <= REPLAY_KEEP:
                        return
                    target = terminal[-REPLAY_KEEP - 1]
                    try:
                        transcript.page_out_through(target)
                    except TranscriptError:
                        return
                    paged = transcript.paged_through
                    for segment_id in [segment_id for segment_id in active_status if segment_id <= paged]:
                        del active_status[segment_id]

                iterator = iter(())
                if first is not None:
                    iterator = _chain_first(first, records)
                for record in iterator:
                    document = record.document
                    record_generation = document.get("generation", generation)
                    if int(record_generation) != generation:
                        raise _corrupt("transcript record generation changed", expected=generation, actual=record_generation)
                    end_offset = record.end_offset
                    kind = record.kind
                    if kind == "seg":
                        segment_id = int(document["segment_id"])
                        if segment_id < expected_segment:
                            previous = recent_segments.get(segment_id)
                            if previous is None:
                                previous = self._segment_before(segment_id, record.offset)
                            if previous is None or not _segments_equal(previous, document):
                                raise _corrupt("duplicate or out-of-order transcript segment", segment_id=segment_id)
                            continue
                        if segment_id > expected_segment:
                            raise _corrupt("transcript segment ids are not dense", expected=expected_segment, actual=segment_id)
                        expected_segment = segment_id + 1
                        index.set(segment_id, final_revision=int(document["final_revision"]))
                        remember_segment(segment_id, document)
                    elif kind == "rev":
                        segment_id = int(document["segment_id"])
                        segment = _validate_segment_document(document)
                        fields = _segment_fields(segment)
                        highest = int(highest_index.get(segment_id)[2])
                        if segment.revision > highest:
                            highest_index.set(segment_id, highest_revision=segment.revision)
                        if segment_id <= paged:
                            # A late retranscription of an already-paged segment. The watermark comes from
                            # the sealed prefix + this scan; a stale revision is tolerated only when its
                            # exact earlier record is identical.
                            if highest == 0:
                                raise _corrupt(
                                    "paged revision has no earlier durable revision",
                                    segment_id=segment_id,
                                )
                            if segment.revision < highest:
                                earlier = self._revision_before(segment_id, segment.revision, record.offset)
                                if earlier is None or earlier != fields:
                                    raise _corrupt(
                                        "stale paged revision conflicts with the durable log",
                                        segment_id=segment_id,
                                        revision=segment.revision,
                                    )
                                continue
                            if segment.revision == highest:
                                earlier = self._revision_before(segment_id, segment.revision, record.offset)
                                if earlier is None or earlier != fields:
                                    raise _corrupt(
                                        "conflicting paged retranscription revisions",
                                        segment_id=segment_id,
                                        revision=segment.revision,
                                    )
                                continue
                            if fields[9] is None:
                                # A non-retranscription paged revision must still be a legal transition;
                                # retranscription revisions carry an explicit token and may rewrite a final.
                                previous = self._latest_revision_before(segment_id, record.offset)
                                if previous is None:
                                    raise _corrupt(
                                        "paged revision has no earlier durable revision",
                                        segment_id=segment_id,
                                    )
                                transition = _validate_transition(previous[1], fields)
                                if transition is not None:
                                    raise _corrupt(
                                        "paged retranscription is not a legal transition",
                                        segment_id=segment_id,
                                        revision=segment.revision,
                                        reason=transition,
                                    )
                            continue
                        if segment_id >= expected_segment:
                            raise _corrupt("durable revision has no segment record", segment_id=segment_id)
                        floor = int(index.get(segment_id)[1])
                        if floor:
                            reserve_or_fail(segment_id, floor)
                        if segment.retranscription_token is not None and not self._duplicate_in_history(transcript, segment):
                            self._arm_retranscription(transcript, segment)
                        outcome = transcript.apply(segment)
                        if not outcome.accepted and outcome.reason == "active_segments_full":
                            maybe_page()
                            outcome = transcript.apply(segment)
                        if outcome.accepted:
                            active_status[segment_id] = _STATUS_CODES[self._terminal_status(segment)]
                            if outcome.reason == "duplicate":
                                continue
                            maybe_page()
                        elif outcome.reason == "stale_revision":
                            earlier = self._revision_before(segment_id, segment.revision, record.offset)
                            if earlier is None or earlier != fields:
                                raise _corrupt(
                                    "stale revision conflicts with the durable log",
                                    segment_id=segment_id,
                                    revision=segment.revision,
                                )
                        elif outcome.reason == "duplicate":
                            continue
                        else:
                            raise _corrupt(
                                "durable revision was rejected during replay",
                                segment_id=segment_id,
                                revision=segment.revision,
                                reason=outcome.reason,
                            )
                    # end/resume state comes from the authoritative summary scan below.

                paged = transcript.paged_through
                summary = self._summary(paged, end_offset)
                final_cursor = None
                if summary.pending_id is not None:
                    floor = int(index.get(summary.pending_id)[1])
                    if floor:
                        try:
                            reserve_or_fail(summary.pending_id, floor)
                        except RecorderCorrupt:
                            # A pending cursor can be blocked by a full active set of later segments. The
                            # floor remains in its durable seg record and is re-reserved on dequeue.
                            pass
                    final_cursor = FinalCursor(
                        summary.pending_id,
                        end_offset if summary.pending_offset is None else summary.pending_offset,
                    )
                cursors = ReplayCursors(
                    generation=generation,
                    transcript=transcript.checkpoint(),
                    log_offset=summary.tail_offset,
                    end_offset=end_offset,
                    final_cursor=final_cursor,
                    segmentation=Segmentation(
                        next_segment_id=summary.next_segment_id,
                        resume_sample=summary.resume_sample,
                        ended_at=summary.ended_at,
                    ),
                    transcript_revision=summary.transcript_revision,
                    counts=summary.counts,
                    prefix_transcript_revision=summary.prefix_transcript_revision,
                    prefix_counts=summary.prefix_counts,
                    state=hint.state if hint is not None else None,
                    reason=hint.reason if hint is not None else None,
                )
                return transcript, cursors
            finally:
                index.close()
                if highest_index is not None:
                    highest_index.close()

    def _summary(self, paged: int, end_offset: int) -> _Summary:
        """Derive authoritative cursors with O(1) live memory and disk-backed per-segment indexes.

        Prefix revision counts records physically before the first unpaged segment. Prefix counts describe
        the latest statuses of paged segments (including late retries anywhere in the log), which is what a
        tail replay must seed so a retried paged status is not lost.
        """
        directory = self._existing_directory()
        status_index = _DiskIndex(directory)
        highest_index: _DiskIndex | None = None
        try:
            highest_index = _DiskIndex(directory)
            generation: int | None = None
            expected_segment = 0
            max_segment = -1
            ended_at: int | None = None
            resume_sample = 0
            transcript_revision = 0
            prefix_revision = 0
            tail_seen = False
            tail_offset = end_offset
            for record in self._iter_records(0):
                document = record.document
                record_generation = document.get("generation", generation)
                if generation is None:
                    if record_generation is None:
                        raise _corrupt("transcript log has no generation")
                    generation = int(record_generation)
                elif int(record_generation) != generation:
                    raise _corrupt("transcript record generation changed", expected=generation, actual=record_generation)
                if record.kind == "seg":
                    segment_id = int(document["segment_id"])
                    if segment_id < expected_segment:
                        continue
                    if segment_id > expected_segment:
                        raise _corrupt("transcript segment ids are not dense", expected=expected_segment, actual=segment_id)
                    expected_segment = segment_id + 1
                    max_segment = max(max_segment, segment_id)
                    status_index.set(segment_id, status=_STATUS_PENDING)
                    if not tail_seen and segment_id == paged + 1:
                        tail_seen = True
                        tail_offset = record.offset
                        prefix_revision = transcript_revision
                elif record.kind == "rev":
                    segment_id = int(document["segment_id"])
                    if segment_id >= expected_segment:
                        raise _corrupt("durable revision has no segment record", segment_id=segment_id)
                    segment = _validate_segment_document(document)
                    highest = int(highest_index.get(segment_id)[2])
                    if segment.revision > highest:
                        highest_index.set(segment_id, highest_revision=segment.revision)
                        status_index.set(
                            segment_id,
                            status=_STATUS_CODES[self._terminal_status(segment)],
                        )
                        transcript_revision += 1
                        if not tail_seen:
                            prefix_revision += 1
                    # Equal/stale revisions are duplicates and never change the latest status.
                elif record.kind == "end":
                    ended_at = int(document["end_sample"])
                    resume_sample = ended_at
                elif record.kind == "resume":
                    resume_sample = int(document["from_sample"])
                    ended_at = None
            if not tail_seen:
                tail_offset = end_offset
                prefix_revision = transcript_revision

            total_counts = {name: 0 for name in ("final", "error", "skipped", "pending")}
            prefix_counts = {name: 0 for name in ("final", "error", "skipped", "pending")}
            for segment_id in range(max_segment + 1):
                status = int(status_index.get(segment_id)[0])
                if status in _TERMINAL_CODES or status == _STATUS_PENDING:
                    name = _STATUS_NAMES[status]
                    total_counts[name] += 1
                    if segment_id <= paged:
                        prefix_counts[name] += 1

            pending_id = None
            for segment_id in range(paged + 1, max_segment + 1):
                if int(status_index.get(segment_id)[0]) == _STATUS_PENDING:
                    pending_id = segment_id
                    break
            pending_offset = None
            if pending_id is not None:
                for record in self._iter_records(0):
                    if record.kind == "seg" and int(record.document["segment_id"]) == pending_id:
                        pending_offset = record.offset
                        break
            return _Summary(
                counts=Counts(**total_counts),
                prefix_counts=Counts(**prefix_counts),
                transcript_revision=transcript_revision,
                prefix_transcript_revision=prefix_revision,
                next_segment_id=max_segment + 1,
                resume_sample=resume_sample,
                ended_at=ended_at,
                pending_id=pending_id,
                tail_offset=tail_offset,
                pending_offset=pending_offset,
            )
        finally:
            status_index.close()
            if highest_index is not None:
                highest_index.close()

    def _latest_revision_before(
        self,
        segment_id: int,
        before_offset: int,
    ) -> tuple[int, tuple[Any, ...]] | None:
        """Find the latest accepted revision for one segment before ``before_offset`` with O(1) memory."""
        latest_revision = 0
        latest_fields: tuple[Any, ...] | None = None
        for record in self._iter_records(0):
            if record.offset >= before_offset:
                break
            if record.kind != "rev":
                continue
            document = record.document
            if int(document.get("segment_id", -1)) != segment_id:
                continue
            segment = _validate_segment_document(document)
            fields = _segment_fields(segment)
            if segment.revision > latest_revision:
                latest_revision = segment.revision
                latest_fields = fields
            elif segment.revision == latest_revision and latest_fields is not None and fields != latest_fields:
                raise _corrupt(
                    "conflicting transcript revisions",
                    segment_id=segment_id,
                    revision=segment.revision,
                )
        if latest_fields is None:
            return None
        return latest_revision, latest_fields

    @staticmethod
    def _terminal_status(segment: TranscriptSegment) -> str:
        if segment.status == "error" and segment.attempt < MAX_ATTEMPTS:
            return "pending"
        return segment.status

    @staticmethod
    def _arm_retranscription(transcript: Transcript, segment: TranscriptSegment) -> None:
        if segment.retranscription_token is None:
            return
        tokens = getattr(transcript, "_retranscription_tokens", None)
        if not isinstance(tokens, dict):
            raise _corrupt("cannot restore an accepted retranscription token")
        tokens[segment.segment_id] = segment.retranscription_token

    @staticmethod
    def _duplicate_in_history(transcript: Transcript, segment: TranscriptSegment) -> bool:
        target = _segment_fields(segment)
        for document in transcript.history_documents():
            if document.get("segment_id") == segment.segment_id and document.get("revision") == segment.revision:
                return _document_fields(document) == target
        return False

    # -- cursor iteration and paging -----------------------------------------------------------------------

    def iter_seg(self, from_offset: int = 0) -> Iterator[LogRecord]:
        """Stream ``seg`` records (with byte offsets) from ``from_offset`` without materialising the log."""
        return (record for record in self._iter_records(from_offset) if record.kind == "seg")

    def read_page(self, after: int = -1, limit: int = DEFAULT_PAGE_LIMIT) -> Page:
        """Return at most ``limit`` latest revisions after ``after`` (dense ids, window ``after+1..after+limit``).

        The log is scanned once. Only the window and the segment at ``after`` (for join flags) are retained,
        so memory is independent of recording length. A segment with a ``seg`` record but no revision is
        returned as ``{"status": "pending", "start", "end"}``.
        """
        if not _is_int(after, minimum=-1):
            raise _invalid("after must be an integer of at least -1")
        if not _is_int(limit, minimum=1) or limit > MAX_PAGE_LIMIT:
            raise _invalid(f"limit must be between 1 and {MAX_PAGE_LIMIT}")
        low = after + 1
        high = after + limit
        latest: dict[int, Mapping[str, Any]] = {}
        segments: dict[int, Mapping[str, Any]] = {}
        expected_segment = 0
        generation: int | None = None
        highest_index = _DiskIndex(self._existing_directory())
        try:
            with self._lock:
                for record in self._iter_records(0):
                    document = record.document
                    kind = record.kind
                    record_generation = document.get("generation", generation)
                    if generation is None:
                        if record_generation is None:
                            raise _corrupt("transcript log has no generation")
                        generation = int(record_generation)
                    elif int(record_generation) != generation:
                        raise _corrupt("transcript record generation changed", expected=generation, actual=record_generation)
                    if kind == "seg":
                        segment_id = int(document["segment_id"])
                        if segment_id < expected_segment:
                            existing = segments.get(segment_id)
                            if existing is None:
                                existing = self._segment_before(segment_id, record.offset)
                            if existing is None or not _segments_equal(existing, document):
                                raise _corrupt("conflicting transcript segment records", segment_id=segment_id)
                            continue
                        if segment_id > expected_segment:
                            raise _corrupt("transcript segment ids are not dense", expected=expected_segment, actual=segment_id)
                        expected_segment = segment_id + 1
                        if low <= segment_id <= high or segment_id == after:
                            segments[segment_id] = document
                    elif kind == "rev":
                        segment_id = int(document["segment_id"])
                        if segment_id >= expected_segment:
                            raise _corrupt("durable revision has no segment record", segment_id=segment_id)
                        incoming = _validate_segment_document(document)
                        fields = _segment_fields(incoming)
                        indexed = highest_index.get(segment_id)
                        highest = int(indexed[2])
                        in_window = low <= segment_id <= high or segment_id == after
                        if incoming.revision > highest:
                            if highest:
                                transition = _validate_compact_transition(
                                    int(indexed[3]),
                                    int(indexed[4]),
                                    int(indexed[5]),
                                    int(indexed[6]),
                                    incoming,
                                )
                                if transition is not None:
                                    raise _corrupt(
                                        "transcript revision is not a legal durable transition",
                                        segment_id=segment_id,
                                        revision=incoming.revision,
                                        reason=transition,
                                    )
                            highest_index.set(
                                segment_id,
                                highest_revision=incoming.revision,
                                latest_start=incoming.start,
                                latest_end=incoming.end,
                                latest_status=_STATUS_CODES[incoming.status],
                                latest_raw_digest=_raw_digest(incoming.raw_text),
                            )
                            if in_window:
                                latest[segment_id] = document
                            continue
                        # A duplicate/stale revision is tolerated only when the exact earlier record is
                        # identical; off-page conflicts are caught here too.
                        earlier = self._revision_before(segment_id, incoming.revision, record.offset)
                        if earlier is None or earlier != fields:
                            raise _corrupt(
                                "conflicting transcript revision",
                                segment_id=segment_id,
                                revision=incoming.revision,
                            )
                        if incoming.revision == highest and in_window and segment_id not in latest:
                            latest[segment_id] = document
        finally:
            highest_index.close()
        output: list[dict[str, Any]] = []
        previous_document = latest.get(after) if after >= 0 else None
        previous: dict[str, Any] | None = None
        if previous_document is not None:
            previous = dict(previous_document)
        for segment_id in range(low, high + 1):
            document = latest.get(segment_id)
            if document is not None:
                result = dict(document)
                text = str(result.get("text") or "")
                if previous is not None and str(previous.get("text") or "") and text:
                    overlap = int(result.get("window_start", result["start"])) < int(previous["end"])
                    joined = join_boundary(str(previous["text"]), text, overlap=overlap, policy=self._policy)
                    result["uncertain_join"] = joined.uncertain
                    result["join"] = joined.reason
                output.append(result)
                previous = result
            elif segment_id in segments:
                segment = segments[segment_id]
                pending = {
                    "segment_id": segment_id,
                    "status": "pending",
                    "start": int(segment["start"]),
                    "end": int(segment["end"]),
                }
                output.append(pending)
                previous = None
            else:
                previous = previous if segment_id == after else None
        if not output:
            return Page(segments=(), next_after=None)
        return Page(segments=tuple(output), next_after=int(output[-1]["segment_id"]))

    # -- exports -------------------------------------------------------------------------------------------

    def _ended_at(self) -> int | None:
        ended_at: int | None = None
        for record in self._iter_records(0):
            if record.kind == "end":
                ended_at = int(record.document["end_sample"])
            elif record.kind == "resume":
                ended_at = None
        return ended_at

    def _pages(self) -> Iterator[list[dict[str, Any]]]:
        after = -1
        while True:
            page = self.read_page(after, MAX_PAGE_LIMIT)
            if not page.segments:
                return
            yield list(page.segments)
            if page.next_after is None or page.next_after <= after:
                return
            after = page.next_after

    def export_txt(self, separator: str = " ") -> Iterator[str]:
        """Yield canonical text in segment order as fragments with bounded memory.

        The log is read in ``read_page`` windows, so no whole-recording dict or output string is materialised.
        A later retranscription of an early segment is resolved by each page scan and appears in id order.
        """
        first = True
        for documents in self._pages():
            for document in documents:
                if document.get("status") == "skipped":
                    continue
                text = str(document.get("text") or "")
                if not text:
                    continue
                if not first:
                    yield separator
                first = False
                yield text

    def export_json(self, *, complete: bool | None = None, indent: int | None = None) -> Iterator[str]:
        """Yield a JSON object with the complete ordered transcript using bounded page memory.

        ``complete`` is emitted at the end of the object so the value can be derived while streaming when it
        is not supplied. Paged exports use ``read_page`` windows; the export string itself is never built.
        """
        yield '{"segments":['
        first = True
        pending = False
        for documents in self._pages():
            for document in documents:
                if document.get("status") == "pending":
                    pending = True
                if not first:
                    yield ","
                first = False
                yield json.dumps(document, ensure_ascii=False, indent=indent, sort_keys=True)
        if complete is None:
            complete = self._ended_at() is not None and not pending
        yield '],"complete":'
        yield "true" if complete else "false"
        yield "}"


def _chain_first(first: LogRecord, rest: Iterable[LogRecord]) -> Iterator[LogRecord]:
    yield first
    yield from rest
