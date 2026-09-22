"""Append-only, versioned JSONL session log (plan section 5.1).

One JSON object per line, terminated by ``\\n``, flushed and ``fsync``'d on every
append. History is otherwise only ever added.

Two record kinds share the envelope, distinguished by a top-level ``type`` tag:

* ``{"type": "event",   "v": 1, "seq": n, "event": {...}}``
* ``{"type": "message", "v": 1, "seq": n, "message": {...}}``
* ``{"type": "summary", "v": 1, "seq": n, "text": "...", ...}``

The ``type`` field is the *record* discriminator and never collides with the
public event catalogue, which lives inside ``event.type``. Message payloads are
encoded with msgspec, so ``Image.data`` / ``Document.data`` bytes round-trip
losslessly through base64. Event ``data`` is expected to be JSON-native (the
plan defines events as JSON-serializable), so it carries no bytes by contract.

A **summary record** is a durable, append-only compaction artifact. It is *not*
a transcript message and never masquerades as an assistant/user turn: it records
the summary text, the strategy that produced it, the source record range it
covers, and the token accounting around the compaction. Full original history
remains authoritative; a summary is an additional, reproducible artifact.

Crash-tail policy
-----------------

The writer emits exactly ``record + b"\\n"`` in a single ``write`` followed by
``fsync``. Two failure shapes are therefore possible at the end of the file, and
``_repair_tail`` fixes both **before the next append** (under the caller's
exclusive session lock) so append-after-crash always yields a readable log:

* **Invalid tail** — a final unterminated segment that is not valid JSON. It is
  truncated back to the last known-good byte boundary and ``fsync``'d.
* **Valid record, missing newline** — a complete final record whose terminating
  newline was lost. A newline is appended and ``fsync``'d so the next record
  cannot concatenate onto the same line.

Only these two shapes are touched. Valid records are never rewritten or
truncated, and no record is ever replaced. A malformed record that *is*
newline-terminated is genuine corruption, not a crash tail: it stays fail-closed
on both read and append because silently dropping it would be data loss.
"""
from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import msgspec

from ..errors import SessionError
from ..events import Event
from ..model.message import Message
from .ids import validate_session_id

#: Envelope version. Bumping this is how a future runtime refuses old logs
#: explicitly instead of misreading them.
SESSION_LOG_VERSION = 1


class EventRecord(msgspec.Struct, tag="event", frozen=True):
    seq: int
    event: Event
    ts: float = 0.0
    v: int = SESSION_LOG_VERSION


class MessageRecord(msgspec.Struct, tag="message", frozen=True):
    seq: int
    message: Message
    ts: float = 0.0
    v: int = SESSION_LOG_VERSION


class SummaryRecord(msgspec.Struct, tag="summary", frozen=True):
    """An append-only, versioned compaction/summary artifact.

    This is deliberately **not** an assistant/user transcript message: it is a
    separate record kind so a summary can never be mistaken for model output or
    replay as a conversational turn. The full original history stays
    authoritative; this record makes the compaction reproducible.
    """

    seq: int
    text: str = ""
    summary_id: str = ""
    strategy: str = ""
    #: Deterministic digest of the summarizer's semantic inputs; identical
    #: ``(strategy, inputs)`` reuse the existing artifact instead of re-running.
    input_digest: str = ""
    #: Inclusive record-sequence range the summary covers (``0`` = unknown).
    source_from_seq: int = 0
    source_to_seq: int = 0
    source_messages: int = 0
    tokens_before: int | None = None
    tokens_after: int | None = None
    provider: str | None = None
    model: str | None = None
    ts: float = 0.0
    v: int = SESSION_LOG_VERSION


SessionRecord = EventRecord | MessageRecord | SummaryRecord


@dataclass(frozen=True)
class ReadResult:
    """A parsed log plus crash-tail bookkeeping."""

    records: tuple[SessionRecord, ...] = ()
    truncated_tail: bool = False
    #: Byte length of the valid prefix; ``< len(file)`` when a tail was discarded.
    valid_bytes: int = 0
    #: Whether the file's final byte is a newline. ``False`` with records present
    #: means a complete record is missing its terminator and must be fixed before
    #: the next append.
    ends_with_newline: bool = True

    @property
    def next_seq(self) -> int:
        return self.records[-1].seq if self.records else 0

    def messages(self) -> list[Message]:
        return [r.message for r in self.records if isinstance(r, MessageRecord)]

    def events(self) -> list[Event]:
        return [r.event for r in self.records if isinstance(r, EventRecord)]

    def summaries(self) -> list[SummaryRecord]:
        return [r for r in self.records if isinstance(r, SummaryRecord)]


class SessionStore:
    """Reads and appends session logs inside a single directory."""

    def __init__(
        self,
        directory: str | Path,
        *,
        fsync: Callable[[int], None] | None = None,
    ):
        self.directory = Path(directory)
        # Injectable so append durability is testable without touching the disk
        # from the test's perspective.
        self._fsync = fsync if fsync is not None else os.fsync
        self._tails: dict[Path, tuple[int, int]] = {}
        #: Last known fully-valid byte length per path, so a crash tail is only
        #: re-scanned once and repaired exactly once.
        self._valid_end: dict[Path, int] = {}

    # -- paths -------------------------------------------------------------

    def log_path(self, session: str) -> Path:
        return self.directory / f"{validate_session_id(session)}.jsonl"

    def lock_path(self, session: str) -> Path:
        return self.directory / f"{validate_session_id(session)}.lock"

    def exists(self, session: str) -> bool:
        return self.log_path(session).exists()

    def create(self, session: str) -> Path:
        """Create an empty log if absent; never truncates an existing one."""
        path = self.log_path(session)
        self.directory.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with open(path, "xb") as handle:
                handle.flush()
                self._fsync(handle.fileno())
            created = True
        except FileExistsError:
            pass
        if created:
            # Durably record the new directory entry where the platform allows.
            self._fsync_dir()
        return path

    @property
    def fsync(self) -> Callable[[int], None]:
        """The injectable fsync used for log and directory durability.

        Exposed so sibling helpers (snapshots, fork publish) reuse the same
        injection point, keeping tests able to observe every durability barrier.
        """
        return self._fsync

    def create_from_records(
        self, session: str, records: Sequence[SessionRecord]
    ) -> Path:
        """Atomically publish a new log from ``records``; never overwrites.

        Used by :meth:`SessionManager.fork` to materialize an exact prefix in a
        new session file. The payload is written to a same-directory temp file
        and ``fsync``'d, then published with an **atomic hard link** into the
        destination name. ``link`` fails with ``FileExistsError`` if the name is
        already taken, so a concurrent creator can never be overwritten — the
        check-and-create is a single filesystem operation, not a TOCTOU window.
        Record ``seq``/``ts``/payloads are preserved verbatim.
        """
        path = self.log_path(session)
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = b"".join(msgspec.json.encode(record) + b"\n" for record in records)
        fd, temp = tempfile.mkstemp(dir=self.directory, prefix=f".{session}-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                self._fsync(handle.fileno())
            try:
                os.link(temp, path)
            except FileExistsError as exc:
                raise SessionError(
                    f"Session log already exists: {path.name}"
                ) from exc
            self._fsync_dir()
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        # The published file supersedes any cached view of this path.
        self._tails.pop(path, None)
        self._valid_end.pop(path, None)
        return path

    # -- reading -----------------------------------------------------------

    def read(self, session: str) -> ReadResult:
        return self._read_path(self.log_path(session))

    def _read_path(self, path: Path) -> ReadResult:
        if not path.exists():
            return ReadResult()
        data = path.read_bytes()
        if not data:
            return ReadResult()
        segments = data.split(b"\n")
        ends_with_newline = data.endswith(b"\n")
        last_index = len(segments) - 1
        records: list[SessionRecord] = []
        truncated = False
        offset = 0
        for index, segment in enumerate(segments):
            start = offset
            if segment == b"":
                # Only the empty segment produced by a trailing newline is legal.
                if index == last_index and ends_with_newline:
                    offset = start + 1
                    continue
                raise SessionError(f"Malformed empty record in session log: {path}")
            is_tail = index == last_index and not ends_with_newline
            try:
                record = self._decode_record(segment)
            except SessionError:
                raise
            except msgspec.DecodeError as exc:
                if is_tail:
                    truncated = True
                    break
                raise SessionError(f"Malformed record in session log: {path}") from exc
            records.append(record)
            offset = start + len(segment) + 1  # include the terminating newline
        valid_bytes = min(offset, len(data))
        return ReadResult(
            tuple(records), truncated, valid_bytes, ends_with_newline
        )

    @staticmethod
    def _decode_record(raw: bytes) -> SessionRecord:
        record = msgspec.json.decode(raw, type=SessionRecord)
        if record.v != SESSION_LOG_VERSION:
            raise SessionError(
                f"Unsupported session log version {record.v}; expected {SESSION_LOG_VERSION}"
            )
        return record

    # -- crash-tail repair -------------------------------------------------

    def _repair_tail(self, path: Path) -> None:
        """Repair a crash tail so the next append stays readable.

        Two repairable shapes exist: an invalid unterminated tail is truncated to
        the last valid boundary, and a valid final record missing its newline has
        one appended. Valid records are never rewritten. Raises
        :class:`~nexus.errors.SessionError` for interior corruption (fail-closed).
        Callers are expected to hold the session's exclusive lock.
        """
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self._tails.pop(path, None)
            self._valid_end.pop(path, None)
            return
        if self._valid_end.get(path) == size:
            return  # known fully valid at this exact size
        result = self._read_path(path)  # raises on interior corruption
        repaired = size
        if result.truncated_tail and result.valid_bytes < size:
            self._truncate(path, result.valid_bytes)
            repaired = result.valid_bytes
        elif result.records and not result.ends_with_newline:
            self._append_newline(path)
            repaired = size + 1
        self._valid_end[path] = repaired
        self._tails[path] = (repaired, result.next_seq)

    def _truncate(self, path: Path, length: int) -> None:
        with open(path, "r+b") as handle:
            handle.truncate(length)
            handle.flush()
            self._fsync(handle.fileno())

    def _append_newline(self, path: Path) -> None:
        with open(path, "ab") as handle:
            handle.write(b"\n")
            handle.flush()
            self._fsync(handle.fileno())

    def _fsync_dir(self) -> None:
        try:
            fd = os.open(self.directory, os.O_RDONLY)
        except OSError:  # pragma: no cover - platform without dir fsync
            return
        try:
            self._fsync(fd)
        except OSError:  # pragma: no cover
            pass
        finally:
            os.close(fd)

    # -- appending ---------------------------------------------------------

    def next_seq(self, session: str) -> int:
        path = self.log_path(session)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            self._tails.pop(path, None)
            self._valid_end.pop(path, None)
            return 1
        cached = self._tails.get(path)
        if cached is not None and cached[0] == size:
            return cached[1] + 1
        result = self._read_path(path)
        # A final record missing its newline is not "fully valid": leave the
        # cache empty so the next append repairs (terminates) it first.
        if result.truncated_tail or not result.ends_with_newline:
            self._valid_end.pop(path, None)
        else:
            self._valid_end[path] = result.valid_bytes
        self._tails[path] = (size, result.next_seq)
        return result.next_seq + 1

    def append_event(self, session: str, event: Event, *, seq: int | None = None) -> EventRecord:
        if not isinstance(event, Event):
            raise TypeError("append_event requires an Event")
        path = self.log_path(session)
        self._repair_tail(path)
        requested = seq if seq is not None else (event.seq if event.seq > 0 else None)
        assigned = self._assign_seq(session, requested)
        if assigned != event.seq or event.session is None:
            event = msgspec.structs.replace(
                event,
                seq=assigned,
                session=event.session if event.session is not None else session,
            )
        record = EventRecord(seq=assigned, event=event, ts=event.ts)
        self._append(path, record)
        return record

    def append_message(
        self, session: str, message: Message, *, seq: int | None = None
    ) -> MessageRecord:
        if not isinstance(message, Message):
            raise TypeError("append_message requires a Message")
        path = self.log_path(session)
        self._repair_tail(path)
        assigned = self._assign_seq(session, seq)
        ts = message.meta.ts if message.meta.ts is not None else time.time()
        record = MessageRecord(seq=assigned, message=message, ts=ts)
        self._append(path, record)
        return record

    def append_summary(
        self,
        session: str,
        *,
        text: str = "",
        summary_id: str = "",
        strategy: str = "",
        input_digest: str = "",
        source_from_seq: int = 0,
        source_to_seq: int = 0,
        source_messages: int = 0,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        ts: float | None = None,
        seq: int | None = None,
    ) -> SummaryRecord:
        """Append one durable summary/compaction artifact.

        Sequence assignment matches messages/events so a summary sits in the
        same monotonic record ordering. No field is interpreted here.
        """
        path = self.log_path(session)
        self._repair_tail(path)
        assigned = self._assign_seq(session, seq)
        record = SummaryRecord(
            seq=assigned,
            text=text,
            summary_id=summary_id,
            strategy=strategy,
            input_digest=input_digest,
            source_from_seq=source_from_seq,
            source_to_seq=source_to_seq,
            source_messages=source_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            provider=provider,
            model=model,
            ts=time.time() if ts is None else ts,
        )
        self._append(path, record)
        return record

    def _assign_seq(self, session: str, requested: int | None) -> int:
        if requested is not None:
            if type(requested) is not int or requested < 1:
                raise ValueError("seq must be a positive integer")
            return requested
        return self.next_seq(session)

    def _append(self, path: Path, record: SessionRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        line = msgspec.json.encode(record) + b"\n"
        with open(path, "ab") as handle:
            handle.write(line)
            handle.flush()
            self._fsync(handle.fileno())
        try:
            size = path.stat().st_size
        except FileNotFoundError:  # pragma: no cover - raced deletion
            self._tails.pop(path, None)
            self._valid_end.pop(path, None)
            return
        previous = self._tails.get(path, (0, 0))[1]
        self._valid_end[path] = size
        self._tails[path] = (size, max(previous, record.seq))

    # -- iteration ---------------------------------------------------------

    def records(self, session: str) -> Iterator[SessionRecord]:
        yield from self.read(session).records


__all__ = [
    "SESSION_LOG_VERSION",
    "EventRecord",
    "MessageRecord",
    "ReadResult",
    "SessionRecord",
    "SessionStore",
    "SummaryRecord",
]
