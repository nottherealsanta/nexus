"""Revisioned transcript primitives with conservative overlap joins (plan 54, C2).

The voice adapter returns text only. This module therefore keeps positions as integer sample ranges and never
invents word timestamps or claims reliable word alignment. Provisional and final results are revisions of a
segment; a stale generation or revision is ignored, and a conflicting duplicate revision never overwrites text
already accepted.

Adjacent forced-boundary windows can overlap in *audio* and therefore repeat words in *text*. Text alone cannot
prove that a repeated suffix/prefix is overlap rather than genuine repetition (for example ``I said go home`` ->
``I said go home again``). The join therefore never removes words on text evidence: every exact boundary match is
preserved in full and marked ``uncertain_join``. Non-exact similarity is likewise preserved. Only a future source
with real word timing could license removal. Joins are recomputed when an earlier segment arrives out of order.

Live state is bounded in two dimensions. At most ``live_revisions_per_segment`` raw revisions are kept per
segment; at most ``max_active_segments`` canonical segments are kept in memory; each text is capped by
``max_text_chars``. Pruning a revision or paging a segment requires a ``persist_revisions`` sink. The sink is
called *before* anything is removed; if it is missing or raises, the operation fails closed with no state change.
This makes persistence-before-pruning synchronous rather than a caller promise. A crash after the sink returns
but before the in-memory delete is an at-least-once duplicate, never a loss.

``TranscriptPage`` and ``TranscriptCheckpoint`` are explicit: the page carries the records to persist, and the
checkpoint carries the paged boundary, constant-size join context and reserved revision floors for restoring a
tail after restart. ``reserve_through`` restores a floor from durable job metadata; ``apply_tolerant`` skips
at-least-once duplicates during replay while live :meth:`Transcript.apply` remains strict about stale results.

Revision numbers are reserved atomically in this object via :meth:`Transcript.next_revision`. Retranscription
that may change a final segment or its boundaries requires a token from :meth:`Transcript.begin_retranscription`;
a callback carrying any other token, or a token from a superseded request, is rejected. Tokens are random and
persistable, but fully coordinating pending retranscription jobs across process restart still belongs to storage
and scheduler integration.
"""

from __future__ import annotations

import re
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from itertools import chain
from typing import Any

MAX_ATTEMPTS = 3
STATUSES = ("provisional", "final", "error", "skipped")

DEFAULT_MAX_OVERLAP_TOKENS = 32
DEFAULT_MAX_ACTIVE_SEGMENTS = 512
DEFAULT_MAX_TEXT_CHARS = 8_192

PersistenceSink = Callable[[tuple["TranscriptSegment", ...]], None]

_TOKEN = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)


class TranscriptError(Exception):
    """Invalid revision data or transcript operation, with a stable code."""

    def __init__(self, code: str, message: str = "", **details: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class JoinPolicy:
    """Guard for boundary matching; the token cap is not a timing or word-alignment claim."""

    max_overlap_tokens: int = DEFAULT_MAX_OVERLAP_TOKENS

    def __post_init__(self) -> None:
        if isinstance(self.max_overlap_tokens, bool) or not isinstance(self.max_overlap_tokens, int) or self.max_overlap_tokens < 1:
            raise ValueError("max_overlap_tokens must be a positive integer")


@dataclass(frozen=True)
class JoinResult:
    """The canonical text for one segment after considering its predecessor.

    ``overlap_tokens`` reports an exact boundary match for diagnostics. It never authorises text removal:
    ``text`` is always the untouched input because text-only overlap is ambiguous.
    """

    text: str
    overlap_tokens: int
    uncertain: bool
    reason: str

    def document(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "overlap_tokens": self.overlap_tokens,
            "uncertain": self.uncertain,
            "reason": self.reason,
        }


def _tokens(text: str) -> list[str]:
    return [unicodedata.normalize("NFKC", match.group(0)).casefold() for match in _TOKEN.finditer(text)]


def _has_proper_border(tokens: list[str]) -> bool:
    """True when the matched phrase repeats itself, so several overlap lengths would fit."""
    length = len(tokens)
    for size in range(1, length):
        if tokens[: length - size] == tokens[size:]:
            return True
    return False


def join_boundary(
    previous_text: str,
    text: str,
    *,
    overlap: bool,
    policy: JoinPolicy | None = None,
) -> JoinResult:
    """Join one segment's raw text to the text before it without deleting any word.

    ``overlap`` must come from sample ranges (the later window starts before the previous segment ends). An exact
    normalised suffix/prefix match is reported and marked uncertain, but preserved: without word timestamps the
    same phrase could be genuine repetition. A non-overlapping boundary concatenates untouched.
    """
    policy = policy or JoinPolicy()
    if not overlap:
        return JoinResult(text=text, overlap_tokens=0, uncertain=False, reason="no_overlap")

    previous = _tokens(previous_text)
    current = _tokens(text)
    if not previous or not current:
        return JoinResult(text=text, overlap_tokens=0, uncertain=False, reason="no_match")

    limit = min(policy.max_overlap_tokens, len(previous), len(current))
    longest = 0
    for size in range(limit, 0, -1):
        if previous[-size:] == current[:size]:
            longest = size
            break
    if longest == 0:
        return JoinResult(text=text, overlap_tokens=0, uncertain=False, reason="no_match")

    matched = current[:longest]
    if longest == 1:
        reason = "ambiguous_short"
    elif _has_proper_border(matched):
        reason = "ambiguous_repetition"
    else:
        reason = "ambiguous_overlap"
    return JoinResult(text=text, overlap_tokens=longest, uncertain=True, reason=reason)


@dataclass(frozen=True)
class TranscriptSegment:
    """One accepted transcript revision (``raw_text``) and its canonical ``text``.

    ``segment_id``/``revision`` order results; ``generation`` invalidates late callbacks from another recording.
    ``start``/``end`` are the displayed content range. ``window_start`` is the inference window's first sample;
    when it is before the previous segment's ``end`` the audio (and possibly text) overlaps.
    ``retranscription_token`` must match an active explicit request to change a final segment or its boundaries.
    """

    segment_id: int
    revision: int
    start: int
    end: int
    status: str
    raw_text: str = ""
    text: str | None = None
    attempt: int = 1
    generation: int = 0
    window_start: int | None = None
    uncertain_join: bool = False
    join: str = "none"
    retranscription_token: str | None = None

    def __post_init__(self) -> None:
        if self.text is None:
            object.__setattr__(self, "text", self.raw_text)

    @property
    def document(self) -> dict[str, Any]:
        return self.as_document()

    def as_document(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "revision": self.revision,
            "start": self.start,
            "end": self.end,
            "status": self.status,
            "text": self.text or "",
            "raw_text": self.raw_text,
            "attempt": self.attempt,
            "generation": self.generation,
            "window_start": self.start if self.window_start is None else self.window_start,
            "uncertain_join": self.uncertain_join,
            "join": self.join,
            "retranscription_token": self.retranscription_token,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> TranscriptSegment:
        try:
            window_start = document.get("window_start")
            return cls(
                segment_id=document["segment_id"],
                revision=document["revision"],
                start=document["start"],
                end=document["end"],
                status=document["status"],
                raw_text=document.get("raw_text", document.get("text", "")),
                text=document.get("text"),
                attempt=document.get("attempt", 1),
                generation=document.get("generation", 0),
                window_start=window_start,
                uncertain_join=bool(document.get("uncertain_join", False)),
                join=str(document.get("join", "none")),
                retranscription_token=document.get("retranscription_token"),
            )
        except (KeyError, TypeError) as exc:
            raise TranscriptError("invalid_document", "transcript document is missing required fields") from exc


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one revision. A rejected result never changes the canonical transcript."""

    accepted: bool
    reason: str
    segment: TranscriptSegment | None = None
    changed: tuple[int, ...] = ()
    evicted: tuple[TranscriptSegment, ...] = ()

    @property
    def applied(self) -> bool:
        return self.accepted and self.reason == "applied"


@dataclass(frozen=True)
class TranscriptPage:
    """Explicitly paged-out terminal segments and all their live raw revisions.

    The caller must persist these records before discarding the page. The transcript does not itself claim they
    are durable; it only guarantees they are not silently dropped.
    """

    through_segment_id: int
    segments: tuple[TranscriptSegment, ...]
    revisions: tuple[TranscriptSegment, ...]

    def documents(self) -> tuple[dict[str, Any], ...]:
        return tuple(segment.as_document() for segment in self.segments)

    def revision_documents(self) -> tuple[dict[str, Any], ...]:
        return tuple(segment.as_document() for segment in self.revisions)


@dataclass(frozen=True)
class TranscriptCheckpoint:
    """Small restart checkpoint for an explicitly paged transcript.

    ``last_paged`` is the canonical boundary/context needed to join the first restored tail segment. It is not a
    replacement for the persisted page records; the storage layer still owns those.
    """

    generation: int
    paged_through: int
    last_paged: TranscriptSegment | None = None
    revision_floors: tuple[tuple[int, int], ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "paged_through": self.paged_through,
            "last_paged": None if self.last_paged is None else self.last_paged.as_document(),
            "revision_floors": [[segment_id, revision] for segment_id, revision in self.revision_floors],
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> TranscriptCheckpoint:
        try:
            last = document.get("last_paged")
            floors = document.get("revision_floors", [])
            return cls(
                generation=document["generation"],
                paged_through=document["paged_through"],
                last_paged=None if last is None else TranscriptSegment.from_document(last),
                revision_floors=tuple((int(pair[0]), int(pair[1])) for pair in floors),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise TranscriptError("invalid_checkpoint", "transcript checkpoint is missing or invalid fields") from exc


def _valid_int(value: Any, minimum: int = 0) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


class Transcript:
    """Bounded in-memory transcript view with explicit paging and scoped retranscription.

    Memory is bounded by ``max_active_segments`` canonical segments, ``live_revisions_per_segment`` raw revisions
    per segment and ``max_text_chars`` per text. When the active set is full, a new segment is rejected with
    ``active_segments_full``; the caller pages terminal segments (final, skipped, or an error at the retry
    limit) with :meth:`page_out_through`. Pruning or paging calls the configured synchronous ``persist_revisions`` sink
    first and fails closed without a sink or when it raises. Nothing is removed before that call returns.

    Revision numbers are reserved by :meth:`next_revision`, so concurrent jobs for one segment never receive the
    same revision. A retranscription that may move boundaries or replace a final requires a token from
    :meth:`begin_retranscription`; superseded or unknown tokens, and ordinary callbacks while a retranscription
    request is active, are rejected. A successful final revision consumes its token.
    """

    def __init__(
        self,
        *,
        generation: int = 0,
        policy: JoinPolicy | None = None,
        live_revisions_per_segment: int = 8,
        max_active_segments: int = DEFAULT_MAX_ACTIVE_SEGMENTS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        persist_revisions: PersistenceSink | None = None,
    ) -> None:
        if not _valid_int(generation):
            raise ValueError("generation must be a non-negative integer")
        if not _valid_int(live_revisions_per_segment, minimum=1):
            raise ValueError("live_revisions_per_segment must be a positive integer")
        if not _valid_int(max_active_segments, minimum=1):
            raise ValueError("max_active_segments must be a positive integer")
        if not _valid_int(max_text_chars, minimum=1):
            raise ValueError("max_text_chars must be a positive integer")
        self.generation = generation
        self.policy = policy or JoinPolicy()
        self.live_revisions_per_segment = live_revisions_per_segment
        self.max_active_segments = max_active_segments
        self.max_text_chars = max_text_chars
        self._persist_revisions = persist_revisions
        self._lock = threading.RLock()
        self._history: dict[int, list[TranscriptSegment]] = {}
        self._current: dict[int, TranscriptSegment] = {}
        self._reserved: dict[int, int] = {}
        self._retranscription_tokens: dict[int, str] = {}
        self._paged_through = -1
        self._last_paged: TranscriptSegment | None = None

    def __len__(self) -> int:
        return len(self._current)

    @property
    def current_segments(self) -> tuple[TranscriptSegment, ...]:
        with self._lock:
            return tuple(self._current[key] for key in sorted(self._current))

    @property
    def active_segment_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._current))

    @property
    def paged_through(self) -> int:
        """Highest segment id explicitly paged out, or -1 when nothing has been paged."""
        return self._paged_through

    def segment(self, segment_id: int) -> TranscriptSegment | None:
        with self._lock:
            return self._current.get(segment_id)

    def next_revision(self, segment_id: int) -> int:
        """Atomically reserve a distinct revision number for one segment.

        Reserve when a job is enqueued, not when its result arrives. The value is never handed out twice for the
        same segment, including across concurrent callers on this object.
        """
        if not _valid_int(segment_id):
            raise TranscriptError("invalid_segment_id", "segment_id must be a non-negative integer")
        with self._lock:
            if segment_id <= self._paged_through:
                raise TranscriptError("segment_paged_out", "cannot reserve a revision for a paged segment")
            if segment_id not in self._history and len(self._current) >= self.max_active_segments:
                raise TranscriptError(
                    "active_segments_full",
                    "cannot reserve a new segment until a terminal page is persisted and paged out",
                    max_active_segments=self.max_active_segments,
                )
            history = self._history.get(segment_id, [])
            latest = history[-1].revision if history else 0
            value = max(latest, self._reserved.get(segment_id, 0)) + 1
            self._reserved[segment_id] = value
            return value

    def reserve_through(self, segment_id: int, revision: int) -> int:
        """Raise one segment's revision floor to at least ``revision`` and return the effective floor.

        Used after restart replay from ``seg.final_revision`` so a new job cannot reuse a revision that was
        reserved or durable before the restart. A lower request never reduces an existing floor.
        """
        if not _valid_int(segment_id):
            raise TranscriptError("invalid_segment_id", "segment_id must be a non-negative integer")
        if not _valid_int(revision):
            raise TranscriptError("invalid_revision", "revision must be a non-negative integer")
        with self._lock:
            if segment_id <= self._paged_through:
                raise TranscriptError("segment_paged_out", "cannot reserve a revision floor for a paged segment")
            if segment_id not in self._history and len(self._current) >= self.max_active_segments:
                raise TranscriptError(
                    "active_segments_full",
                    "cannot reserve a new segment until a terminal page is persisted and paged out",
                    max_active_segments=self.max_active_segments,
                )
            history = self._history.get(segment_id, [])
            latest = history[-1].revision if history else 0
            floor = max(latest, self._reserved.get(segment_id, 0), revision)
            self._reserved[segment_id] = floor
            return floor

    def begin_retranscription(self, segment_id: int) -> str:
        """Start one explicit retranscription request and return its opaque token.

        Starting another request for the same segment supersedes the previous token. Unknown/superseded tokens
        are rejected by :meth:`apply`; a successful final revision with the current token ends the request.
        """
        with self._lock:
            if segment_id not in self._current:
                raise TranscriptError("segment_not_active", "retranscription requires an active segment")
            token = uuid.uuid4().hex
            self._retranscription_tokens[segment_id] = token
            return token

    def end_retranscription(self, token: str) -> bool:
        """Cancel a retranscription request; returns whether the token was active."""
        with self._lock:
            for segment_id, active in list(self._retranscription_tokens.items()):
                if active == token:
                    del self._retranscription_tokens[segment_id]
                    return True
            return False

    def apply(self, segment: TranscriptSegment) -> ApplyResult:
        """Validate and apply one result revision; stale, conflicting and unscoped results change nothing."""
        self._validate(segment)
        with self._lock:
            if segment.generation != self.generation:
                return ApplyResult(False, "stale_generation")

            is_new = segment.segment_id not in self._history
            if is_new:
                if segment.segment_id <= self._paged_through:
                    return ApplyResult(False, "segment_paged_out")
                if len(self._current) >= self.max_active_segments:
                    return ApplyResult(False, "active_segments_full")

            history = self._history.setdefault(segment.segment_id, [])
            if history:
                latest = history[-1]
                if segment.revision <= latest.revision:
                    if segment.revision == latest.revision and self._same_result(latest, segment):
                        return ApplyResult(True, "duplicate", self._current.get(segment.segment_id))
                    return ApplyResult(False, "stale_revision")

                active_token = self._retranscription_tokens.get(segment.segment_id)
                token = segment.retranscription_token
                if token is not None and token != active_token:
                    return ApplyResult(False, "retranscription_not_active", self._current.get(segment.segment_id))
                if active_token is not None and token is None:
                    return ApplyResult(False, "retranscription_in_progress", self._current.get(segment.segment_id))

                transition_error = self._transition_error(
                    latest,
                    segment,
                    retranscription=token is not None,
                )
                if transition_error is not None:
                    return ApplyResult(False, transition_error, self._current.get(segment.segment_id))
            elif segment.retranscription_token is not None:
                return ApplyResult(False, "retranscription_not_active")

            # History stores raw model output; joins are a derived view, so an out-of-order predecessor can be
            # incorporated without rewriting what the engine originally returned.
            raw = replace(segment, text=segment.raw_text, uncertain_join=False, join="none")
            evicted: tuple[TranscriptSegment, ...] = ()
            if len(history) >= self.live_revisions_per_segment:
                overflow = len(history) + 1 - self.live_revisions_per_segment
                evicted = tuple(history[:overflow])
                # Fails closed before any mutation if there is no sink or the sink raises.
                self._persist_before_prune(evicted)
            history.append(raw)
            if evicted:
                del history[: len(evicted)]
            changed = self._recompute_from(segment.segment_id)
            if segment.retranscription_token is not None and segment.status == "final":
                self._retranscription_tokens.pop(segment.segment_id, None)
            return ApplyResult(True, "applied", self._current.get(segment.segment_id), changed, evicted)

    def apply_tolerant(self, segment: TranscriptSegment) -> ApplyResult:
        """Apply replay/at-least-once input, skipping an already seen duplicate or older revision.

        Live callers should keep using :meth:`apply`, which reports ``stale_revision`` strictly.
        """
        outcome = self.apply(segment)
        if outcome.reason in {"duplicate", "stale_revision"}:
            return ApplyResult(
                True,
                f"skipped_{outcome.reason}",
                self._current.get(segment.segment_id),
                outcome.changed,
                outcome.evicted,
            )
        return outcome

    def page_out_through(self, segment_id: int) -> TranscriptPage:
        """Explicitly page out a contiguous run of terminal segments up to ``segment_id``.

        Persist the returned records before dropping them. Only segments for which :meth:`terminal` is true
        (final, skipped, or an error at the retry limit) are pageable; other provisional/error segments, or a
        segment with an active retranscription request, stop progress and raise instead of being discarded. The
        last paged segment's canonical text is kept as join context for the first remaining segment.
        """
        if not _valid_int(segment_id):
            raise TranscriptError("invalid_paging", "segment_id must be a non-negative integer")
        with self._lock:
            ids = sorted(self._current)
            if not ids:
                raise TranscriptError("invalid_paging", "there are no active segments to page")
            if segment_id <= self._paged_through:
                raise TranscriptError(
                    "invalid_paging",
                    "segment is already paged or precedes the paging floor",
                    paged_through=self._paged_through,
                )
            if segment_id < ids[0]:
                raise TranscriptError("invalid_paging", "through is before the oldest active segment", oldest=ids[0])

            run: list[int] = []
            expected = ids[0]
            for key in ids:
                if key != expected:
                    break
                run.append(key)
                if key >= segment_id:
                    break
                expected += 1
            if not run:
                raise TranscriptError("invalid_paging", "no contiguous segment can be paged")

            for key in run:
                segment = self._current[key]
                if not self.terminal(segment):
                    raise TranscriptError(
                        "segment_not_terminal",
                        "only terminal segments (final, skipped or error at the retry limit) can be paged",
                        segment_id=key,
                        status=segment.status,
                        attempt=segment.attempt,
                    )
                if key in self._retranscription_tokens:
                    raise TranscriptError(
                        "retranscription_in_progress",
                        "cannot page a segment with an active retranscription request",
                        segment_id=key,
                    )

            segments = [self._current[key] for key in run]
            revisions = [raw for key in run for raw in self._history.get(key, [])]
            # Fails closed before removing anything if there is no sink or the sink raises.
            self._persist_before_prune(tuple(revisions))
            for key in run:
                self._current.pop(key)
                self._history.pop(key, None)
                self._reserved.pop(key, None)
                self._retranscription_tokens.pop(key, None)
            last = segments[-1]
            self._last_paged = last
            self._paged_through = last.segment_id
            remaining = sorted(self._current)
            if remaining:
                self._recompute_from(remaining[0])
            return TranscriptPage(last.segment_id, tuple(segments), tuple(revisions))

    def checkpoint(self) -> TranscriptCheckpoint:
        """Small checkpoint of paged boundary, join context and revision floors, safe to persist."""
        with self._lock:
            return TranscriptCheckpoint(
                self.generation,
                self._paged_through,
                self._last_paged,
                tuple(sorted(self._reserved.items())),
            )

    def restore_checkpoint(self, checkpoint: TranscriptCheckpoint) -> None:
        """Restore paged boundary/context into a fresh transcript before replaying tail revisions."""
        with self._lock:
            if checkpoint.generation != self.generation:
                raise TranscriptError("checkpoint_generation", "checkpoint belongs to another generation")
            if self._current or self._history or self._paged_through != -1:
                raise TranscriptError("checkpoint_conflict", "checkpoint requires an empty transcript")
            paged = checkpoint.paged_through
            last = checkpoint.last_paged
            if not _valid_int(paged, minimum=-1):
                raise TranscriptError("invalid_checkpoint", "paged_through must be at least -1")
            if paged == -1:
                if last is not None:
                    raise TranscriptError("invalid_checkpoint", "an unpaged checkpoint cannot carry last_paged")
            elif last is None or last.segment_id != paged:
                raise TranscriptError("invalid_checkpoint", "last_paged must match paged_through")
            if last is not None:
                self._validate(last)
                if not self.terminal(last):
                    raise TranscriptError("invalid_checkpoint", "last_paged must be terminal")
            floors: dict[int, int] = {}
            for floor_id, floor_revision in checkpoint.revision_floors:
                if not _valid_int(floor_id) or not _valid_int(floor_revision):
                    raise TranscriptError("invalid_checkpoint", "revision floors must be non-negative integers")
                if floor_id <= paged:
                    raise TranscriptError(
                        "invalid_checkpoint",
                        "a revision floor cannot refer to a paged segment",
                        segment_id=floor_id,
                    )
                floors[floor_id] = max(floors.get(floor_id, 0), floor_revision)
            self._paged_through = paged
            self._last_paged = last
            self._reserved.update(floors)

    def _persist_before_prune(self, revisions: tuple[TranscriptSegment, ...]) -> None:
        if not revisions:
            return
        if self._persist_revisions is None:
            raise TranscriptError(
                "persistence_required",
                "pruning requires a persist_revisions sink; nothing was removed",
                records=len(revisions),
            )
        try:
            self._persist_revisions(revisions)
        except Exception as exc:
            raise TranscriptError(
                "persistence_failed",
                "persist_revisions failed; nothing was pruned",
                records=len(revisions),
            ) from exc

    def apply_documents(self, documents: Iterable[dict[str, Any]]) -> list[ApplyResult]:
        return [self.apply(TranscriptSegment.from_document(document)) for document in documents]

    def history_documents(self) -> tuple[dict[str, Any], ...]:
        """The bounded live revision ring, oldest first per active segment. Evicted records are in ApplyResult."""
        with self._lock:
            return tuple(
                raw.as_document()
                for segment_id in sorted(self._history)
                for raw in self._history[segment_id]
            )

    def documents(self) -> tuple[dict[str, Any], ...]:
        return tuple(segment.as_document() for segment in self.current_segments)

    def text(self, separator: str = " ") -> str:
        """Canonical visible text for *active* segments; combine paged documents for the full transcript."""
        parts = [
            (segment.text or "").strip()
            for segment in self.current_segments
            if segment.status != "skipped" and (segment.text or "").strip()
        ]
        return separator.join(parts)

    def pending_finalizations(self) -> tuple[TranscriptSegment, ...]:
        """Active segments still needing a final pass (provisional or error); ``skipped`` is terminal."""
        return tuple(segment for segment in self.current_segments if segment.status in {"provisional", "error"})

    @staticmethod
    def retryable(segment: TranscriptSegment) -> bool:
        return segment.status == "error" and segment.attempt < MAX_ATTEMPTS

    @staticmethod
    def terminal(segment: TranscriptSegment) -> bool:
        """A segment that needs no further live job: final, skipped, or an error at the retry limit."""
        if segment.status in {"final", "skipped"}:
            return True
        return segment.status == "error" and segment.attempt >= MAX_ATTEMPTS

    @classmethod
    def from_documents(
        cls,
        documents: Iterable[dict[str, Any]],
        *,
        generation: int | None = None,
        policy: JoinPolicy | None = None,
        live_revisions_per_segment: int = 8,
        max_active_segments: int = DEFAULT_MAX_ACTIVE_SEGMENTS,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        persist_revisions: PersistenceSink | None = None,
        checkpoint: TranscriptCheckpoint | None = None,
    ) -> Transcript:
        """Stream ordered raw revision documents; never materialise the whole input.

        With a bounded active set this replay refuses (``replay_incomplete``) rather than dropping a segment. Pass
        a checkpoint and only the tail documents after the paged boundary to reconstruct a paged transcript.
        """
        iterator = iter(documents)
        first_document = next(iterator, None)
        first = None if first_document is None else TranscriptSegment.from_document(first_document)
        if generation is None:
            generation = first.generation if first is not None else 0
        transcript = cls(
            generation=generation,
            policy=policy,
            live_revisions_per_segment=live_revisions_per_segment,
            max_active_segments=max_active_segments,
            max_text_chars=max_text_chars,
            persist_revisions=persist_revisions,
        )
        if checkpoint is not None:
            transcript.restore_checkpoint(checkpoint)
        rest = (TranscriptSegment.from_document(document) for document in iterator)
        for segment in chain(() if first is None else (first,), rest):
            # Accepted retranscription revisions are persisted with their request token. Restore that explicit
            # scope during replay; an unaccepted/stale token never appears in an accepted ordered log.
            if segment.retranscription_token is not None:
                transcript._retranscription_tokens[segment.segment_id] = segment.retranscription_token
            outcome = transcript.apply(segment)
            if not outcome.accepted and outcome.reason != "duplicate":
                raise TranscriptError(
                    "replay_incomplete",
                    "ordered transcript replay was rejected",
                    reason=outcome.reason,
                    segment_id=segment.segment_id,
                    revision=segment.revision,
                )
        return transcript

    def _validate(self, segment: TranscriptSegment) -> None:
        if not _valid_int(segment.segment_id):
            raise TranscriptError("invalid_segment_id", "segment_id must be a non-negative integer")
        if not _valid_int(segment.revision, minimum=1):
            raise TranscriptError("invalid_revision", "revision must be a positive integer")
        if not _valid_int(segment.start) or not _valid_int(segment.end):
            raise TranscriptError("invalid_range", "start and end must be non-negative integers")
        if segment.end < segment.start:
            raise TranscriptError("invalid_range", "end must not precede start")
        if segment.window_start is not None:
            if not _valid_int(segment.window_start):
                raise TranscriptError("invalid_range", "window_start must be a non-negative integer")
            if segment.window_start > segment.start:
                raise TranscriptError("invalid_range", "window_start must not follow content start")
        if segment.status not in STATUSES:
            raise TranscriptError("invalid_status", f"status must be one of {', '.join(STATUSES)}")
        if not _valid_int(segment.attempt, minimum=1) or segment.attempt > MAX_ATTEMPTS:
            raise TranscriptError("invalid_attempt", f"attempt must be between 1 and {MAX_ATTEMPTS}")
        if not _valid_int(segment.generation):
            raise TranscriptError("invalid_generation", "generation must be a non-negative integer")
        if not isinstance(segment.raw_text, str) or not isinstance(segment.text, str):
            raise TranscriptError("invalid_text", "transcript text must be a string")
        if len(segment.raw_text) > self.max_text_chars or len(segment.text) > self.max_text_chars:
            raise TranscriptError(
                "text_too_long",
                "transcript text exceeds max_text_chars",
                max_text_chars=self.max_text_chars,
            )
        if segment.retranscription_token is not None and not isinstance(segment.retranscription_token, str):
            raise TranscriptError("invalid_retranscription", "retranscription_token must be a string or None")

    @staticmethod
    def _same_result(left: TranscriptSegment, right: TranscriptSegment) -> bool:
        return (
            left.segment_id,
            left.revision,
            left.start,
            left.end,
            left.status,
            left.raw_text,
            left.attempt,
            left.generation,
            left.window_start,
            left.retranscription_token,
        ) == (
            right.segment_id,
            right.revision,
            right.start,
            right.end,
            right.status,
            right.raw_text,
            right.attempt,
            right.generation,
            right.window_start,
            right.retranscription_token,
        )

    @staticmethod
    def _transition_error(
        latest: TranscriptSegment,
        segment: TranscriptSegment,
        *,
        retranscription: bool,
    ) -> str | None:
        """Reject silent boundary shrinkage, final demotion and final text changes.

        Only a revision carrying the current explicit retranscription token sets ``retranscription=True``. A new
        recording generation is a separate scope, handled before this check.
        """
        if retranscription:
            return None
        if segment.start != latest.start or segment.end < latest.end:
            return "boundary_regression"
        if latest.status == "final":
            if segment.status == "final" and segment.end == latest.end and segment.raw_text == latest.raw_text:
                return None
            return "final_immutable"
        return None

    def _predecessor(self, segment_id: int) -> TranscriptSegment | None:
        candidates = [key for key in self._current if key < segment_id]
        if candidates:
            return self._current[max(candidates)]
        if self._last_paged is not None and self._last_paged.segment_id < segment_id:
            return self._last_paged
        return None

    def _recompute_from(self, segment_id: int) -> tuple[int, ...]:
        changed: list[int] = []
        for key in sorted(key for key in self._history if key >= segment_id):
            raw = self._history[key][-1]
            previous = self._predecessor(key)
            raw_text = "" if raw.status == "skipped" else raw.raw_text
            if previous is not None and (previous.text or "") and raw_text:
                overlap = raw.window_start is not None and raw.window_start < previous.end
                joined = join_boundary(previous.text or "", raw_text, overlap=overlap, policy=self.policy)
            else:
                joined = JoinResult(text=raw_text, overlap_tokens=0, uncertain=False, reason="none")
            candidate = replace(
                raw,
                text=joined.text,
                uncertain_join=joined.uncertain,
                join=joined.reason,
            )
            previous_view = self._current.get(key)
            self._current[key] = candidate
            if previous_view != candidate:
                changed.append(key)
            else:
                # This segment's visible text and join state are unchanged, so later segments cannot be affected.
                break
        return tuple(changed)
