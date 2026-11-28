"""Durable, bounded transcription scheduler for the standalone recorder (plan 54, C3c).

The scheduler is an asyncio component owned by the daemon loop. ``RecorderService`` calls its hooks from the
recorder worker thread; those hooks only merge a tiny signal under a lock and wake the loop. Every disk or
service touch happens on the recorder worker through ``RecorderHost.call_background``, so a recording
directory has one writer at a time and ``delete`` cannot race a transcript append. ``VoiceManager`` is
loop-bound, so only the loop calls it.

Durable transcript state lives in ``recordings/<id>/transcript/`` (``TranscriptLog``): a ``seg`` record is
both a closed range and a finalization job, a ``rev`` record is an accepted final/error/skipped revision, and
``end``/``resume`` describe segmentation. Provisional text never reaches disk, and every ``recorder.segment``
notification is live-only; the log is the source of truth. Pending finalization is a disk cursor plus a
bounded retry heap, not an in-memory list of audio.

The service's hooks are notifications, not durable delivery (a hook can fail or be missed). The scheduler
therefore reconciles authoritative recording states periodically and on startup. A valid stop retry re-signals
``capture_ended``; repeated signals and repeated hook delivery are idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from ..voice.manager import RECORDER_BACKLOG_FULL, VOICE_TIMEOUT
from ..voice.model import VoiceError
from .host import RecorderUnavailable
from .segmenter import (
    MIN_INFERENCE_SECONDS,
    PcmSegmenter,
    Segment,
    SegmenterConfig,
    SegmenterError,
    samples_for,
)
from .store import (
    RecorderConflict,
    RecorderCorrupt,
    RecorderError,
    RecorderInvalid,
    RecorderNotFound,
    RecorderStore,
)
from .transcript import MAX_ATTEMPTS, Transcript, TranscriptError, TranscriptSegment
from .transcript_log import (
    Checkpoint,
    Counts,
    Segmentation,
    TranscriptLog,
)

log = logging.getLogger(__name__)

# -- public limits (plan 54 C3c §2; tuning belongs to milestone E) ------------------------------------------

MAX_OUTSTANDING_FINALS = 2
MAX_RETRY_HEAP = 16
MAX_PUMPED = 2
ACTIVE_KEEP = 8
PROGRESS_INTERVAL = 0.5
PROBE_INTERVAL = 30.0
BACKOFF = (2.0, 8.0, 30.0)
LAG_CATCHUP_SECONDS = 5.0
LAG_LIVE_SECONDS = 2.0
LAG_DEGRADED_SECONDS = 20.0
FEED_SAMPLES = 32_000
CHECKPOINT_INTERVAL = 10.0
RECONCILE_INTERVAL = 5.0
RETRY_SOON = 0.25
MAX_JOBS = 32
MAX_QUEUED = 128
MAX_SIGNALS = 256
MAX_CANCELLED = 256
MAX_COMPLETE_HINTS = 1_024
ENUM_PAGE_LIMIT = 50
MAX_ENUM_PAGES_PER_CYCLE = 4
MAX_TEXT_CHARS = 8_192
MAX_RETRY_TARGETS = 100

_CATCHUP_LAG = samples_for(LAG_CATCHUP_SECONDS)
_LIVE_LAG = samples_for(LAG_LIVE_SECONDS)
_DEGRADED_LAG = samples_for(LAG_DEGRADED_SECONDS)

_BACKPRESSURE_CODES = frozenset({RECORDER_BACKLOG_FULL, "recorder_background_full",
                                 "recorder_backpressure"})
_UNAVAILABLE_CODES = {
    "voice_model_not_cached": "model_not_cached",
    "voice_dependency_missing": "dependency_missing",
    "voice_model_unsupported": "model_unsupported",
    "voice_model_invalid": "model_invalid",
}
_RETRYABLE_VOICE_CODES = frozenset({
    "voice_failed",
    "voice_load_failed",
    "voice_model_invalid",
    "voice_bad_audio",
    VOICE_TIMEOUT,
})
_DROP_CODES = frozenset({
    "voice_superseded",
    "stale_revision",
    "boundary_regression",
    "final_immutable",
    "segment_paged_out",
    "segment_not_active",
})
_TERMINAL_STATES = frozenset({"complete", "failed"})


class SchedulerError(Exception):
    """A scheduler request was refused with a stable code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class RetryRefused(SchedulerError):
    """An explicit transcript retry was refused (active capture or another pass)."""


class _StaleJob(Exception):
    """The job, generation or recording was superseded; drop the result silently."""


class _Capacity(Exception):
    """No transcript capacity was available without dropping unfinished work."""


@dataclass(frozen=True)
class SchedulerLimits:
    """Tunable bounds. Defaults are the named module constants from plan 54."""

    max_outstanding_finals: int = MAX_OUTSTANDING_FINALS
    max_retry_heap: int = MAX_RETRY_HEAP
    max_pumped: int = MAX_PUMPED
    active_keep: int = ACTIVE_KEEP
    progress_interval: float = PROGRESS_INTERVAL
    probe_interval: float = PROBE_INTERVAL
    backoff: tuple[float, ...] = BACKOFF
    checkpoint_interval: float = CHECKPOINT_INTERVAL
    reconcile_interval: float = RECONCILE_INTERVAL
    retry_soon: float = RETRY_SOON
    max_jobs: int = MAX_JOBS
    max_queued: int = MAX_QUEUED
    max_signals: int = MAX_SIGNALS
    max_cancelled: int = MAX_CANCELLED


@dataclass
class _Signal:
    """Coalesced service notification for one recording. Only merged under ``_signal_lock``."""

    generation: int = 0
    generation_known: bool = False
    durable_offset: int = 0
    ended: bool = False
    end_offset: int = 0
    end_state: str | None = None
    recovered: bool = False
    merged: int = 0
    cancelled: bool = False
    token: object | None = None


@dataclass
class _FinalWork:
    segment_id: int
    revision: int
    attempt: int
    start: int
    end: int
    window_start: int
    window_end: int
    close_reason: str = "silence"
    offset: int = 0
    retranscription_token: str | None = None
    isolated: Transcript | None = None
    previous_status: str | None = None
    due: float = 0.0


@dataclass
class _PreviewWork:
    segment_id: int
    revision: int
    start: int
    end: int
    window_start: int
    window_end: int


@dataclass
class _Result:
    kind: str
    work: _FinalWork | _PreviewWork
    value: Any = None
    error: BaseException | None = None


@dataclass
class _RecordingJob:
    """One recording's bounded scheduler state (plan 54 §2.3)."""

    rid: str
    token: object
    generation: int = 0
    transcript: Transcript | None = None
    segmenter: PcmSegmenter | None = None
    log: TranscriptLog | None = None
    ready: bool = False
    refused: bool = False
    pumping: bool = True
    active_capture: bool = True
    ended: bool = False
    end_target: int = 0
    end_state: str = "interrupted"
    finish_target: int | None = None
    finished_durable: int | None = None
    segment_finished: bool = False
    fed: int = 0
    durable: int = 0
    resume_sample: int = 0
    cursor_offset: int = 0
    cursor_exhausted: bool = True
    cursor_next_start: int | None = None
    counts: dict[str, int] = field(default_factory=lambda: {
        "final": 0, "error": 0, "skipped": 0, "pending": 0})
    transcript_revision: int = 0
    log_offset: int = 0
    # A checkpoint at ``checkpoint_log_offset > 0`` describes only the durable prefix before it; replay
    # re-reads the tail and adds its counts back. In-session paging falls back to offset 0 (full replay)
    # rather than guessing new prefix aggregates from live deltas.
    checkpoint_log_offset: int = 0
    prefix_counts: dict[str, int] = field(default_factory=lambda: {
        "final": 0, "error": 0, "skipped": 0, "pending": 0})
    prefix_transcript_revision: int = 0
    state: str = "preparing"
    reason: str | None = None
    first_success: bool = False
    probe_code: str | None = None
    last_probe_at: float = 0.0
    download_state: str | None = None
    last_progress_at: float = -1_000.0
    progress_task: asyncio.Task[None] | None = None
    last_checkpoint_at: float = -1_000.0
    checkpoint_dirty: bool = False
    checkpoint_task: asyncio.Task[None] | None = None
    log_failures: int = 0
    audio_fault: bool = False
    corrupt: bool = False
    results: deque[_Result] = field(default_factory=lambda: deque())
    outstanding: dict[int, asyncio.Task[Any]] = field(default_factory=dict)
    final_work: dict[int, _FinalWork] = field(default_factory=dict)
    preview_task: asyncio.Task[Any] | None = None
    preview_work: _PreviewWork | None = None
    last_preview_end: int = -1
    preview_through: int = 0
    final_wait_until: float = 0.0
    retry_heap: list[_FinalWork] = field(default_factory=list)
    deferred_segments: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=32))
    deferred_uncertain: bool = False
    retry_pass: bool = False
    retry_targets: deque[_FinalWork] = field(default_factory=lambda: deque())
    retry_previous_status: dict[int, str] = field(default_factory=dict)
    terminal: bool = False
    handled_merged: int = 0


class RecorderScheduler:
    """Loop-owned scheduler that turns durable recorder audio into a durable transcript.

    Constructor inputs are the loop-bound ``VoiceManager`` and optional limits/clock. ``attach`` records
    the loop and host before the host submits service startup. Hooks are synchronous and safe from any
    thread; ``close`` is an awaitable and is called by the host before the recorder worker drains.
    """

    def __init__(
        self,
        voice: Any,
        *,
        config: SegmenterConfig | None = None,
        clock: Callable[[], float] | None = None,
        limits: SchedulerLimits | None = None,
    ) -> None:
        self.voice = voice
        self.config = config or SegmenterConfig()
        self.clock = clock or time.monotonic
        self.limits = limits or SchedulerLimits()
        if self.limits.max_pumped < 1:
            raise ValueError("max_pumped must be at least 1")
        if self.limits.max_outstanding_finals < 1:
            raise ValueError("max_outstanding_finals must be at least 1")
        if not self.limits.backoff or any(delay < 0 for delay in self.limits.backoff):
            raise ValueError("backoff must be a non-empty sequence of non-negative delays")

        self._loop: asyncio.AbstractEventLoop | None = None
        self._host: Any = None
        self._store: RecorderStore | None = None
        self._closed = False
        self._attached = False
        self._driver: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._wake_queued = False
        self._signal_lock = threading.Lock()
        self._signals: dict[str, _Signal] = {}
        self._tokens: dict[object, str] = {}
        self._jobs: OrderedDict[str, _RecordingJob] = OrderedDict()
        self._queue: deque[str] = deque()
        self._cancelled: OrderedDict[str, None] = OrderedDict()
        self._complete_hints: OrderedDict[str, None] = OrderedDict()
        self._background: set[asyncio.Task[Any]] = set()
        self._last_reconcile_at = -1_000.0
        self._last_now = -1_000.0
        self._enum_after: str | None = None

    # -- lifecycle ---------------------------------------------------------------------------------------------

    def attach(self, loop: asyncio.AbstractEventLoop, host: Any) -> None:
        """Record the loop/host and start the driver. Synchronous and non-blocking (host contract)."""
        with self._signal_lock:
            if self._closed:
                raise SchedulerError("scheduler_closed", "the scheduler is closed")
            if self._attached:
                return
            self._attached = True
            self._loop = loop
            self._host = host
            service = getattr(host, "service", None)
            self._store = getattr(service, "store", None) if service is not None else None
            self._wake_event = asyncio.Event()
        self._driver = loop.create_task(self._drive(), name="recorder-scheduler")

    async def close(self) -> None:
        """Cancel and join all pre-existing work, then checkpoint in the host's close context.

        The host stops external admission before calling us and only reopens its background lane for
        this close task (and tasks it creates while closing). So every task created before close is
        cancelled and joined first; only then are final checkpoints submitted through
        ``call_background``. No notification or transcript write happens after this returns. A late
        result from a superseded job is dropped by its token.
        """
        with self._signal_lock:
            if self._closed:
                return
            self._closed = True
            signals = list(self._signals.values())
            for signal in signals:
                signal.cancelled = True
                signal.token = None
            driver = self._driver
            self._driver = None
            event = self._wake_event
        if event is not None:
            event.set()
        if driver is not None:
            driver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await driver
        tasks: list[asyncio.Task[Any]] = []
        for job in list(self._jobs.values()):
            tasks.extend(job.outstanding.values())
            job.outstanding.clear()
            job.final_work.clear()
            if job.preview_task is not None:
                tasks.append(job.preview_task)
                job.preview_task = None
            if job.progress_task is not None:
                tasks.append(job.progress_task)
                job.progress_task = None
            if job.checkpoint_task is not None:
                tasks.append(job.checkpoint_task)
                job.checkpoint_task = None
        tasks.extend(self._background)
        self._background.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._final_checkpoints()
        with self._signal_lock:
            self._signals.clear()
            self._tokens.clear()
            self._jobs.clear()
            self._queue.clear()

    # -- hooks (called from the recorder worker) ---------------------------------------------------------------

    def audio_committed(self, recording_id: str, generation: int, durable_offset: int) -> None:
        """A durable chunk advanced the recording. Idempotent: merge the maximum offset."""
        self._merge(recording_id, generation=generation, durable_offset=durable_offset)

    def capture_ended(self, recording_id: str, generation: int, end_offset: int, state: str) -> None:
        """Capture ended (stopped or interrupted). A stop retry may re-signal this idempotently."""
        self._merge(recording_id, generation=generation, ended=True, end_offset=end_offset, state=state)

    def recovered(self, recording_ids: Iterable[str]) -> None:
        """Queue recordings reported by startup reconciliation without materialising signal state.

        The queue is bounded. A recording beyond the bound is rediscovered by the periodic bounded
        enumeration (once the service exposes it) instead of growing per-recording memory here.
        """
        with self._signal_lock:
            if self._closed:
                return
            queued = set(self._queue)
            for recording_id in recording_ids:
                if not isinstance(recording_id, str):
                    continue
                if recording_id in self._cancelled or recording_id in self._jobs:
                    continue
                if recording_id in queued:
                    continue
                if len(self._queue) >= self.limits.max_queued:
                    break
                self._queue.append(recording_id)
                queued.add(recording_id)
        self._notify()

    def cancel(self, recording_id: str) -> None:
        """Mark a deleted recording stale before ``store.delete`` runs on the same worker thread.

        The driver invalidates the job and drops its results. Background functions also check this flag
        under the same lock, so a queued transcript append cannot recreate a deleted directory.
        """
        with self._signal_lock:
            self._cancelled[recording_id] = None
            self._cancelled.move_to_end(recording_id)
            while len(self._cancelled) > self.limits.max_cancelled:
                self._cancelled.popitem(last=False)
            signal = self._signals.get(recording_id)
            if signal is not None:
                signal.cancelled = True
            for token, owner in list(self._tokens.items()):
                if owner == recording_id:
                    self._tokens.pop(token, None)
            self._jobs.pop(recording_id, None)
            with contextlib.suppress(ValueError):
                self._queue.remove(recording_id)
            self._complete_hints.pop(recording_id, None)
        self._notify()

    def wake(self) -> None:
        """Wake the driver (used after an injected-clock advance or by an embedding surface)."""
        self._notify()

    # -- observation -------------------------------------------------------------------------------------------

    def snapshot(self, recording_id: str) -> dict[str, Any] | None:
        """Bounded scheduler state for tests/diagnostics. Never includes transcript text."""
        job = self._jobs.get(recording_id)
        if job is None:
            return None
        pending = job.counts.get("pending", 0)
        return {
            "id": job.rid,
            "generation": job.generation,
            "state": job.state,
            "reason": job.reason,
            "capture_active": job.active_capture,
            "ended": job.ended,
            "segment_finished": job.segment_finished,
            "durable_offset": job.durable,
            "fed_offset": job.fed,
            "processed_through": self._processed_through(job),
            "lag_samples": self._lag(job),
            "preview_through": job.preview_through,
            "pending_finals": pending,
            "outstanding_finals": len(job.outstanding),
            "retry_heap": len(job.retry_heap),
            "preview_in_flight": job.preview_task is not None,
            "transcript_revision": job.transcript_revision,
            "counts": dict(job.counts),
            "active_segments": len(job.transcript) if job.transcript is not None else 0,
            "paged_through": job.transcript.paged_through if job.transcript is not None else -1,
            "transcript_corrupt": job.corrupt,
            "log_failures": job.log_failures,
            "retry_pass": job.retry_pass,
        }

    def transcript(self, recording_id: str) -> Transcript | None:
        """The live bounded transcript view (diagnostics/tests only; the log remains authoritative)."""
        job = self._jobs.get(recording_id)
        return job.transcript if job is not None else None

    # -- explicit retry after capture (plan 54 §2.9, bounded V1) ------------------------------------------------

    async def retry(self, recording_id: str, scope: Any = "failed") -> dict[str, Any]:
        """Retry failed/all/explicit segments after capture.

        Bounded to ``MAX_RETRY_TARGETS`` and refused while capture is active or a pass runs. Paged
        segments are retried through an isolated single-segment transcript so the main bounded view does
        not grow. A new revision is always created; earlier exported text is never rewritten.
        """
        job = self._jobs.get(recording_id)
        if job is None:
            service = getattr(self._host, "service", None)
            if service is not None:
                try:
                    state = await self._call(self._guarded, None, False, service.get, recording_id)
                except (RecorderNotFound, RecorderError, RecorderUnavailable):
                    state = None
                if isinstance(state, dict) and (
                        state.get("active") or state.get("capture_state") == "recording"):
                    raise RetryRefused("retry_while_active",
                                       "stop the recording before retrying its transcript")
            raise RetryRefused("not_found", "the recording has no scheduler state")
        if job.active_capture or not job.ended:
            raise RetryRefused("retry_while_active", "stop the recording before retrying its transcript")
        if job.retry_pass:
            raise RetryRefused("retry_in_progress", "a transcript retry is already running")
        if not job.ready or job.transcript is None or job.log is None:
            raise RetryRefused("not_ready", "the transcript is not ready")
        if job.corrupt:
            raise RetryRefused("transcript_corrupt", "a corrupt transcript needs a new log generation")
        targets = await self._retry_targets(job, scope)
        if not targets:
            return {"accepted": 0, "scope": scope}
        job.retry_pass = True
        job.retry_targets.extend(targets)
        job.state = "catching_up"
        job.reason = None
        job.terminal = False
        job.pumping = True
        self._complete_hints.pop(recording_id, None)
        await self._publish_progress(job, force=True)
        self.wake()
        return {"accepted": len(targets), "scope": scope}

    # -- driver ------------------------------------------------------------------------------------------------

    async def _drive(self) -> None:
        while not self._closed:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("recorder scheduler tick failed", exc_info=True)
                await asyncio.sleep(RETRY_SOON)
            await self._wait_for_work()

    async def _tick(self) -> None:
        now = self.clock()
        self._last_now = now
        await self._admit_queue()
        await self._maybe_reconcile(now)
        self._revive_terminal_jobs()

        pumped = [job for job in self._jobs.values() if job.pumping and not job.terminal]
        for job in pumped[: self.limits.max_pumped]:
            if self._closed:
                return
            await self._step(job, now)

        self._trim_jobs()
        self._prune_signals()

    def _prune_signals(self) -> None:
        """Bound coalesced signal state; signals have no job/queue owner once reconciled or completed."""
        with self._signal_lock:
            if len(self._signals) <= self.limits.max_signals:
                return
            for rid in list(self._signals):
                if len(self._signals) <= self.limits.max_signals:
                    break
                if rid in self._jobs or rid in self._queue:
                    continue
                self._signals.pop(rid, None)
                # Drop token ownership too: any queued guarded operation now fails closed instead of
                # being allowed to write after its recording was cancelled/evicted.
                for token, owner in list(self._tokens.items()):
                    if owner == rid:
                        self._tokens.pop(token, None)

    async def _wait_for_work(self) -> None:
        event = self._wake_event
        if event is None:
            return
        if self._has_immediate_work():
            event.clear()
            await asyncio.sleep(0)
            return
        timeout = self._next_timeout()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            event.clear()

    def _has_immediate_work(self) -> bool:
        pumping = sum(1 for job in self._jobs.values() if job.pumping and not job.terminal)
        if self._queue and pumping < self.limits.max_pumped:
            return True
        for job in self._jobs.values():
            if job.terminal:
                with self._signal_lock:
                    signal = self._signals.get(job.rid)
                    merged = signal.merged if signal is not None else job.handled_merged
                if merged > job.handled_merged:
                    return True
                continue
            if not job.ready:
                if not job.corrupt and job.log_failures < 3:
                    return True
                continue
            if job.results:
                return True
            if (job.segmenter is not None and not job.segmenter.finished
                    and job.fed < job.durable):
                return True
            if job.ended and not job.segment_finished:
                return True
            if (job.ended and job.segment_finished and job.finished_durable is not None
                    and job.durable > job.finished_durable):
                return True
            if job.deferred_segments or job.deferred_uncertain:
                return True
            now = self.clock()
            if (not job.cursor_exhausted and not job.corrupt and not job.audio_fault
                    and job.state != "unavailable" and job.probe_code is None
                    and len(job.outstanding) < self.limits.max_outstanding_finals
                    and len(job.retry_heap) + len(job.outstanding) < self.limits.max_retry_heap
                    and now >= job.final_wait_until):
                return True
        return False

    def _next_timeout(self) -> float:
        now = self.clock()
        delays: list[float] = [max(0.0, self.limits.reconcile_interval - (now - self._last_reconcile_at))]
        for job in self._jobs.values():
            if job.terminal:
                continue
            delays.append(max(0.0, self.limits.probe_interval - (now - job.last_probe_at)))
            if job.retry_heap and job.state != "unavailable" and job.probe_code is None:
                delays.append(max(0.0, min(item.due for item in job.retry_heap) - now))
            if job.final_wait_until > now:
                delays.append(job.final_wait_until - now)
            if job.checkpoint_dirty:
                delays.append(max(0.0, self.limits.checkpoint_interval - (now - job.last_checkpoint_at)))
        delay = min(delays) if delays else self.limits.reconcile_interval
        return max(0.01, delay)

    async def _admit_queue(self) -> None:
        """Create jobs for queued recordings, up to the pump bound."""
        while self._queue and not self._closed:
            pumping = sum(1 for job in self._jobs.values() if job.pumping and not job.terminal)
            if pumping >= self.limits.max_pumped:
                return
            rid = self._queue.popleft()
            if rid in self._cancelled or rid in self._jobs:
                continue
            with self._signal_lock:
                signal = self._signals.get(rid)
                if signal is None:
                    signal = _Signal(recovered=True)
                    self._signals[rid] = signal
                elif signal.cancelled:
                    continue
            if not signal.generation_known and not await self._load_recording_signal(rid, signal):
                continue
            job = self._new_job(rid, signal)
            if job is None:
                continue
            self._jobs[rid] = job
            self._trim_jobs()

    async def _load_recording_signal(self, rid: str, signal: _Signal) -> bool:
        """Fetch authoritative state for a recovered recording without touching the store on the loop."""
        service = getattr(self._host, "service", None)
        if service is None:
            return False
        try:
            state = await self._call(self._guarded, None, False, service.get, rid)
        except (RecorderNotFound, _StaleJob, RecorderUnavailable):
            return False
        with self._signal_lock:
            if signal.cancelled:
                return False
            self._merge_state_locked(signal, state)
        return signal.generation_known

    def _new_job(self, rid: str, signal: _Signal) -> _RecordingJob | None:
        job = _RecordingJob(rid=rid, token=object(), generation=signal.generation)
        job.durable = signal.durable_offset
        if signal.ended:
            job.ended = True
            job.end_target = signal.end_offset
            job.end_state = signal.end_state or "interrupted"
            job.active_capture = False
        with self._signal_lock:
            current = self._signals.get(rid)
            if current is None or current.cancelled:
                return None
            current.token = job.token
            self._tokens[job.token] = rid
        return job

    # -- per-job step ------------------------------------------------------------------------------------------

    async def _step(self, job: _RecordingJob, now: float) -> None:
        if job.token is None or job.rid in self._cancelled:
            return
        await self._sync_signal(job)
        if not job.ready and not await self._prepare_job(job):
            return
        await self._process_results(job, now)
        if await self._maybe_resume(job):
            pass
        await self._probe(job, now)
        if not job.corrupt:
            await self._feed(job)
            await self._finish(job)
            await self._preview(job, now)
            await self._finals(job, now)
        self._drop_stale_preview(job)
        self._update_state(job)
        self._maybe_page(job)
        await self._publish_progress(job)
        await self._maybe_checkpoint(job, now)
        if job.terminal and job.pumping:
            job.pumping = False
            with self._signal_lock:
                signal = self._signals.get(job.rid)
                job.handled_merged = signal.merged if signal is not None else job.handled_merged
            self._complete_hints[job.rid] = None
            self._complete_hints.move_to_end(job.rid)
            while len(self._complete_hints) > MAX_COMPLETE_HINTS:
                self._complete_hints.popitem(last=False)

    def _revive_terminal_jobs(self) -> None:
        """A real hook after completion (for example recovery audio) restarts a parked job."""
        for job in self._jobs.values():
            if not job.terminal:
                continue
            with self._signal_lock:
                signal = self._signals.get(job.rid)
                if signal is None or signal.cancelled or signal.token is not job.token:
                    continue
                merged = signal.merged
            if merged <= job.handled_merged:
                continue
            job.handled_merged = merged
            job.terminal = False
            job.pumping = True
            job.state = "catching_up"
            job.reason = None
            self._complete_hints.pop(job.rid, None)

    async def _prepare_job(self, job: _RecordingJob) -> bool:
        """Replay the durable log and build the segmenter/transcript on the recorder worker."""
        store = self._store
        if store is None or self._host is None:
            job.state = "failed"
            job.reason = "storage"
            job.terminal = True
            return False
        job.log = TranscriptLog(lambda: store.transcript_dir(job.rid))
        try:
            transcript, cursors = await self._call(self._guarded, job.token, True, job.log.replay)
        except _StaleJob:
            self._cancel_job(job)
            return False
        except RecorderNotFound:
            self._cancel_job(job)
            return False
        except RecorderCorrupt:
            job.state = "failed"
            job.reason = "transcript_corrupt"
            job.corrupt = True
            job.terminal = True
            return False
        except RecorderError:
            job.log_failures += 1
            if job.log_failures >= 3:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
            return False
        except Exception:
            log.warning("recorder transcript replay failed for %s", job.rid, exc_info=True)
            job.log_failures += 1
            return False

        generation = int(cursors.generation)
        if cursors.end_offset == 0 and not cursors.final_cursor:
            # An empty log has no generation yet; adopt the recording generation before appending.
            generation = job.generation
            transcript.generation = generation
        elif generation != job.generation:
            job.state = "failed"
            job.reason = "transcript_corrupt"
            job.corrupt = True
            job.terminal = True
            return False
        job.generation = generation
        job.transcript = transcript
        job.counts = {
            "final": cursors.counts.final,
            "error": cursors.counts.error,
            "skipped": cursors.counts.skipped,
            "pending": cursors.counts.pending,
        }
        try:
            last_seg_end = await self._call(self._guarded, job.token, True, self._last_seg_end,
                                            job.log)
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return False
        except RecorderError:
            last_seg_end = 0
        job.transcript_revision = cursors.transcript_revision
        job.log_offset = cursors.log_offset
        job.checkpoint_log_offset = cursors.log_offset
        job.prefix_counts = {
            "final": cursors.prefix_counts.final,
            "error": cursors.prefix_counts.error,
            "skipped": cursors.prefix_counts.skipped,
            "pending": cursors.prefix_counts.pending,
        }
        job.prefix_transcript_revision = cursors.prefix_transcript_revision
        # ``segmentation.resume_sample`` only tracks explicit end/resume records. An unfinished capture
        # that crashed mid-segmentation must resume at the last durable segment's end (plan 54 §2.2/§5).
        job.resume_sample = max(cursors.segmentation.resume_sample, last_seg_end)
        job.segmenter = PcmSegmenter(
            self.config,
            generation=generation,
            first_segment_id=cursors.segmentation.next_segment_id,
        )
        job.fed = job.resume_sample
        if cursors.segmentation.ended_at is not None:
            job.ended = True
            job.segment_finished = True
            job.finished_durable = cursors.segmentation.ended_at
            job.end_target = self._ended_target_from_signal(job.end_target, cursors.segmentation.ended_at)
            job.active_capture = not self._signal_ended(job)
        if cursors.final_cursor is not None:
            job.cursor_offset = cursors.final_cursor.log_offset
            job.cursor_exhausted = False
        else:
            job.cursor_offset = cursors.end_offset
            job.cursor_exhausted = True
        if cursors.state in {"preparing", "live", "catching_up", "failed", "complete", "unavailable"}:
            job.state = cursors.state
        job.reason = cursors.reason
        job.ready = True
        if job.durable > job.resume_sample:
            job.cursor_exhausted = job.cursor_exhausted
        return True

    @staticmethod
    def _ended_target_from_signal(target: int, ended_at: int) -> int:
        return max(target, ended_at)

    def _signal_ended(self, job: _RecordingJob) -> bool:
        with self._signal_lock:
            signal = self._signals.get(job.rid)
            return bool(signal is not None and signal.ended and not signal.cancelled)

    async def _sync_signal(self, job: _RecordingJob) -> None:
        with self._signal_lock:
            signal = self._signals.get(job.rid)
            if signal is None or signal.cancelled or signal.token is not job.token:
                self._cancel_job(job)
                return
            generation = signal.generation if signal.generation_known else job.generation
            durable = signal.durable_offset
            ended = signal.ended
            end_offset = signal.end_offset
            end_state = signal.end_state
        if signal.generation_known and generation != job.generation:
            # A stale generation belongs to a previous recording incarnation; never mutate this job with it.
            return
        job.durable = max(job.durable, durable)
        if ended:
            job.ended = True
            job.end_target = max(job.end_target, end_offset)
            job.end_state = end_state or job.end_state
            job.active_capture = False
        elif job.ended and job.finished_durable is not None and job.durable > job.finished_durable:
            # Recovery uploads extended durable audio after an interrupted finish.
            await self._reopen(job)
        elif not ended and job.end_state == "recording":
            job.active_capture = True

    async def _maybe_resume(self, job: _RecordingJob) -> bool:
        if not job.ended or not job.segment_finished or job.finished_durable is None:
            return False
        if job.durable <= job.finished_durable:
            return False
        await self._reopen(job)
        return True

    async def _reopen(self, job: _RecordingJob) -> None:
        if job.log is None or job.transcript is None:
            return
        from_sample = job.finished_durable if job.finished_durable is not None else job.fed
        try:
            await self._call(self._guarded, job.token, True, job.log.append, {
                "k": "resume", "generation": job.generation, "from_sample": from_sample,
            })
        except asyncio.CancelledError:
            raise
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return
        except Exception as exc:  # noqa: BLE001 - raw OSError keeps the recording ended and retries later
            job.log_failures += 1
            if not isinstance(exc, RecorderError):
                log.warning("recorder transcript resume append failed for %s (%s)",
                            job.rid, type(exc).__name__)
            if job.log_failures >= 3:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
            return
        job.log_failures = 0
        next_id = job.segmenter.next_segment_id if job.segmenter is not None else 0
        job.segmenter = PcmSegmenter(self.config, generation=job.generation, first_segment_id=next_id)
        job.fed = from_sample
        job.resume_sample = from_sample
        # A sealed/interrupted recording stays ended: the recovery audio only extends the finished range.
        job.ended = self._signal_ended(job)
        job.segment_finished = False
        job.finished_durable = None
        job.active_capture = False
        job.cursor_exhausted = False
        job.checkpoint_dirty = True
        if job.state in _TERMINAL_STATES:
            job.state = "catching_up"
            job.reason = None

    async def _probe(self, job: _RecordingJob, now: float) -> None:
        voice = self.voice
        download_state = None
        with contextlib.suppress(Exception):
            status = getattr(voice, "download_status", None)
            if isinstance(status, dict):
                download_state = status.get("state")
        if job.download_state != download_state:
            job.download_state = download_state
            job.last_probe_at = -1_000.0
        if getattr(voice, "downloading", False):
            job.state = "preparing"
            job.reason = "downloading"
            job.probe_code = None
            return
        if now - job.last_probe_at < self.limits.probe_interval:
            return
        job.last_probe_at = now
        try:
            code = await voice.readiness()
        except Exception:  # noqa: BLE001 - readiness is a probe; any failure is an unavailable reason
            code = "voice_model_invalid"
        if code == "voice_unavailable":
            job.state = "failed"
            job.reason = "unavailable"
            job.terminal = True
            job.active_capture = False
            return
        if code is None:
            job.probe_code = None
            job.reason = None
            if job.state in {"unavailable", "preparing"}:
                job.state = "preparing"
            if job.first_success and job.state == "preparing":
                job.state = "catching_up"
            return
        job.probe_code = code
        job.state = "unavailable"
        job.reason = _UNAVAILABLE_CODES.get(code, "unavailable")

    async def _process_results(self, job: _RecordingJob, now: float) -> None:
        while job.results and not self._closed:
            result = job.results.popleft()
            if result.kind == "preview":
                await self._preview_result(job, result)
            else:
                await self._final_result(job, result, now)

    async def _preview_result(self, job: _RecordingJob, result: _Result) -> None:
        work = result.work
        assert isinstance(work, _PreviewWork)
        if job.preview_work is not work:
            return
        job.preview_task = None
        job.preview_work = None
        if result.error is not None:
            if self._drop_error(result.error):
                return
            if isinstance(result.error, VoiceError) and result.error.code in _UNAVAILABLE_CODES:
                job.probe_code = result.error.code
                job.state = "unavailable"
                job.reason = _UNAVAILABLE_CODES[result.error.code]
            return
        if job.transcript is None:
            return
        value = result.value
        if int(getattr(value, "generation", job.generation)) != job.generation:
            return
        text = _bounded_text(str(getattr(value, "text", "")))
        segment = TranscriptSegment(
            segment_id=work.segment_id,
            revision=work.revision,
            start=work.start,
            end=work.end,
            status="provisional",
            raw_text=text,
            window_start=work.window_start,
            attempt=1,
            generation=job.generation,
        )
        try:
            applied = job.transcript.apply(segment)
        except TranscriptError as exc:
            if exc.code in {"segment_paged_out", "active_segments_full"}:
                return
            if exc.code == "text_too_long":
                return
            raise
        if applied.accepted and applied.reason in {"applied", "duplicate"}:
            job.first_success = True
            job.preview_through = max(job.preview_through, work.end)
            if applied.reason == "applied":
                await self._publish_segment(job, segment)

    async def _final_result(self, job: _RecordingJob, result: _Result, now: float) -> None:
        work = result.work
        assert isinstance(work, _FinalWork)
        if job.outstanding.pop(work.segment_id, None) is None:
            return
        job.final_work.pop(work.segment_id, None)
        if result.error is not None:
            await self._final_error(job, work, result.error, now)
            return
        value = result.value
        if int(getattr(value, "generation", job.generation)) != job.generation:
            # A stale-generation result must not advance the cursor or consume an attempt.
            self._requeue(job, work, now, self.limits.retry_soon)
            return
        target = work.isolated or job.transcript
        if target is None:
            return
        text = _bounded_text(str(getattr(value, "text", "")))
        segment = TranscriptSegment(
            segment_id=work.segment_id,
            revision=work.revision,
            start=work.start,
            end=work.end,
            status="final",
            raw_text=text,
            window_start=work.window_start,
            attempt=work.attempt,
            generation=job.generation,
            retranscription_token=work.retranscription_token,
        )
        await self._commit_segment(job, work, segment, target)

    async def _final_error(self, job: _RecordingJob, work: _FinalWork, error: BaseException,
                           now: float) -> None:
        if self._drop_error(error):
            return
        if isinstance(error, VoiceError):
            code = error.code
            if code in _BACKPRESSURE_CODES or code == "voice_busy":
                # No attempt was consumed and no audio was read. Requeue the same disk cursor.
                self._requeue(job, work, now, self.limits.retry_soon)
                return
            if code in _UNAVAILABLE_CODES:
                job.probe_code = code
                job.state = "unavailable"
                job.reason = _UNAVAILABLE_CODES[code]
                job.last_probe_at = -1_000.0
                self._requeue(job, work, now, self.limits.retry_soon)
                return
            if code == "voice_unavailable":
                self._cancel_job(job)
                return
            if code in _DROP_CODES:
                return
            if code in _RETRYABLE_VOICE_CODES:
                await self._record_error(job, work, now, _voice_reason(code))
                return
        if isinstance(error, (RecorderCorrupt, RecorderInvalid)):
            await self._record_error(job, work, now, "audio_unavailable", terminal=True)
            return
        if isinstance(error, RecorderConflict):
            # A range became unavailable while a job was queued; retry without consuming an attempt.
            self._requeue(job, work, now, self.limits.retry_soon)
            return
        if isinstance(error, (RecorderNotFound, _StaleJob)):
            self._cancel_job(job)
            return
        log.warning("recorder transcription job failed for %s (%s)", job.rid, type(error).__name__)
        await self._record_error(job, work, now, "engine_failed")

    async def _record_error(self, job: _RecordingJob, work: _FinalWork, now: float, reason: str,
                            *, terminal: bool = False) -> None:
        attempt = min(MAX_ATTEMPTS, work.attempt)
        work.attempt = attempt
        target = work.isolated or job.transcript
        if target is None:
            return
        segment = TranscriptSegment(
            segment_id=work.segment_id,
            revision=work.revision,
            start=work.start,
            end=work.end,
            status="error",
            raw_text="",
            window_start=work.window_start,
            attempt=attempt,
            generation=job.generation,
            retranscription_token=work.retranscription_token,
        )
        await self._commit_segment(job, work, segment, target, terminal=terminal or attempt >= MAX_ATTEMPTS)
        if terminal or attempt >= MAX_ATTEMPTS:
            return
        follow = replace(work, attempt=attempt + 1)
        follow.due = now + self.limits.backoff[min(attempt - 1, len(self.limits.backoff) - 1)]
        if len(job.retry_heap) < self.limits.max_retry_heap:
            job.retry_heap.append(follow)
        # If the heap is full the cursor remains paused; this segment stays pending in the log.

    async def _commit_segment(self, job: _RecordingJob, work: _FinalWork,
                              segment: TranscriptSegment, target: Transcript, *,
                              terminal: bool = False) -> bool:
        try:
            applied = target.apply(segment)
        except TranscriptError as exc:
            if exc.code in {"segment_paged_out", "active_segments_full", "final_immutable", "stale_revision"}:
                return False
            if exc.code == "text_too_long":
                return False
            raise
        if not applied.accepted:
            if applied.reason in {"stale_revision", "duplicate", "final_immutable", "boundary_regression",
                                  "segment_paged_out", "retranscription_in_progress"}:
                return False
            return False
        if job.log is None:
            return False
        record = {"k": "rev", **segment.as_document()}
        try:
            await self._call(self._guarded, job.token, True, job.log.append, record)
        except asyncio.CancelledError:
            raise
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return False
        except RecorderCorrupt:
            job.state = "failed"
            job.reason = "transcript_corrupt"
            job.corrupt = True
            job.terminal = True
            return False
        except Exception as exc:  # noqa: BLE001 - raw OSError/fsync takes the same fail-closed path
            job.log_failures += 1
            if not isinstance(exc, RecorderError):
                log.warning("recorder transcript revision append failed for %s (%s)",
                            job.rid, type(exc).__name__)
            if job.log_failures >= 3:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
            await self._rebuild_after_append_failure(job)
            if not job.terminal:
                self._requeue(job, work, self.clock(), self.limits.retry_soon)
            return False
        job.log_failures = 0
        job.checkpoint_dirty = True
        job.transcript_revision += 1
        previous = work.previous_status
        current = _logical_status(segment)
        # Every segment record counted as pending when it became durable. A new revision replaces the
        # aggregate status, including a retryable error that remains logically pending.
        previous = previous or "pending"
        if previous != current:
            job.counts[previous] = max(0, job.counts.get(previous, 0) - 1)
            job.counts[current] = job.counts.get(current, 0) + 1
        if segment.status == "final":
            job.first_success = True
        await self._publish_segment(job, segment)
        return True

    async def _rebuild_after_append_failure(self, job: _RecordingJob) -> None:
        if job.log is None:
            return
        try:
            transcript, cursors = await self._call(self._guarded, job.token, True, job.log.replay, None)
        except asyncio.CancelledError:
            raise
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return
        except RecorderCorrupt:
            job.state = "failed"
            job.reason = "transcript_corrupt"
            job.corrupt = True
            job.terminal = True
            return
        except Exception:  # noqa: BLE001 - a rebuild failure is a storage failure, never a lost result
            job.state = "failed"
            job.reason = "storage"
            job.terminal = True
            return
        if cursors.end_offset == 0 and cursors.final_cursor is None:
            transcript.generation = job.generation
        job.transcript = transcript
        job.transcript_revision = cursors.transcript_revision
        job.counts = {
            "final": cursors.counts.final,
            "error": cursors.counts.error,
            "skipped": cursors.counts.skipped,
            "pending": cursors.counts.pending,
        }

    async def _publish_segment(self, job: _RecordingJob, segment: TranscriptSegment) -> None:
        service = getattr(self._host, "service", None)
        if service is None or self._host is None:
            return
        try:
            await self._call(self._guarded, job.token, True, service.publish_segment, job.rid,
                             segment_id=segment.segment_id, revision=segment.revision,
                             start=segment.start, end=segment.end, status=segment.status,
                             text=segment.text or "", generation=job.generation)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the durable log already owns the truth; live publish is best effort
            return

    # -- feed, finish, preview, finals -------------------------------------------------------------------------

    async def _feed(self, job: _RecordingJob) -> None:
        if job.segmenter is None:
            return
        if (job.deferred_segments and not job.corrupt and not job.segmenter.finished
                and not await self._append_closed(job, [])):
            return
        if job.fed >= job.durable or job.segmenter.finished:
            return
        if not await self._ensure_capacity(job, job.segmenter.next_segment_id):
            return
        end = min(job.fed + FEED_SAMPLES, job.durable)
        try:
            data = await self._call(self._guarded, job.token, True, self._store.read_pcm, job.rid, job.fed, end)
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return
        except (RecorderCorrupt, RecorderInvalid):
            job.state = "failed"
            job.reason = "audio_unavailable"
            job.audio_fault = True
            return
        except RecorderConflict:
            return
        except RecorderError:
            return
        try:
            closed = job.segmenter.feed(data, start_sample=job.fed)
        except SegmenterError:
            job.state = "failed"
            job.reason = "audio_unavailable"
            job.audio_fault = True
            return
        job.fed = end
        if closed:
            await self._append_closed(job, closed)

    async def _finish(self, job: _RecordingJob) -> None:
        if not job.ended or job.segment_finished or job.segmenter is None or job.log is None:
            return
        if job.finish_target is None:
            target = min(job.end_target or job.durable, job.durable)
            if job.fed < target:
                return
            if job.segmenter.finished:
                return
            try:
                closed = job.segmenter.finish(end_sample=target)
            except SegmenterError:
                job.state = "failed"
                job.reason = "audio_unavailable"
                return
            job.finish_target = target
        else:
            target = job.finish_target
            closed = []
        # The finish is retried independently of new closes: flush any deferred segment records even when
        # ``closed`` is empty. The end record is written only after every closed range is durable.
        if not await self._append_closed(job, closed):
            return
        if job.deferred_segments or job.corrupt:
            return
        reason = "interrupted" if job.end_state == "interrupted" else "stopped"
        try:
            await self._call(self._guarded, job.token, True, job.log.append, {
                "k": "end", "generation": job.generation, "end_sample": target, "reason": reason,
            })
        except asyncio.CancelledError:
            raise
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return
        except Exception as exc:  # noqa: BLE001 - raw OSError/fsync must not escape or lose the finish
            job.log_failures += 1
            if not isinstance(exc, RecorderError):
                log.warning("recorder transcript end append failed for %s (%s)",
                            job.rid, type(exc).__name__)
            if job.log_failures >= 3:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
            return
        job.log_failures = 0
        job.finish_target = None
        job.segment_finished = True
        job.finished_durable = target
        job.checkpoint_dirty = True
        job.cursor_exhausted = False
        job.audio_fault = False

    async def _append_closed(self, job: _RecordingJob, closed: list[Segment]) -> bool:
        """Flush deferred and new closed ranges durably; independent of whether new ranges arrived.

        Deferred records survive every append failure (including a raw ``OSError`` from a write/fsync).
        If an append may have landed before raising, the next attempt reconciles against the log first, so
        records are neither lost nor counted twice and the ``end`` record waits for a durable flush.
        """
        if not closed and not job.deferred_segments and not job.deferred_uncertain:
            return True
        if job.transcript is None or job.log is None:
            return False
        if job.deferred_uncertain and not await self._reconcile_deferred(job):
            return False
        if not closed and not job.deferred_segments:
            return True
        documents: list[dict[str, Any]] = []
        pending: list[Segment] = []
        try:
            for segment in closed:
                if not await self._ensure_capacity(job, segment.segment_id):
                    raise _Capacity
                revision = job.transcript.next_revision(segment.segment_id)
                documents.append({
                    "k": "seg",
                    "generation": job.generation,
                    "segment_id": segment.segment_id,
                    "start": segment.start,
                    "end": segment.end,
                    "window_start": segment.window_start,
                    "window_end": segment.window_end,
                    "close_reason": segment.close_reason,
                    "final_revision": revision,
                })
                pending.append(segment)
        except (_Capacity, TranscriptError):
            remaining = [
                self._segment_document(job, segment) for segment in closed[len(documents):]
                if job.transcript is not None
            ]
            additions = documents + remaining
            if len(job.deferred_segments) + len(additions) > job.deferred_segments.maxlen:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
                return False
            job.deferred_segments.extend(additions)
            return False
        batch = list(job.deferred_segments) + documents
        if len(batch) > job.deferred_segments.maxlen:
            job.state = "failed"
            job.reason = "storage"
            job.terminal = True
            return False
        try:
            await self._call(self._guarded, job.token, True, job.log.append, batch)
        except asyncio.CancelledError:
            # Close/shutdown cancellation: keep the records for a restart or a later retry.
            self._restore_deferred(job, batch)
            job.deferred_uncertain = True
            raise
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return False
        except RecorderCorrupt:
            job.state = "failed"
            job.reason = "transcript_corrupt"
            job.corrupt = True
            job.terminal = True
            return False
        except Exception as exc:  # noqa: BLE001 - OSError, RecorderError and any raw write/fsync failure
            self._restore_deferred(job, batch)
            job.deferred_uncertain = True
            job.log_failures += 1
            if not isinstance(exc, RecorderError):
                log.warning("recorder transcript append failed for %s (%s)", job.rid, type(exc).__name__)
            if job.log_failures >= 3:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
            return False
        job.deferred_segments.clear()
        job.deferred_uncertain = False
        job.log_failures = 0
        job.checkpoint_dirty = True
        job.cursor_exhausted = False
        if job.cursor_next_start is None and batch:
            job.cursor_next_start = int(batch[0].get("start", 0))
        job.counts["pending"] += len(batch)
        for document in batch:
            job.resume_sample = max(job.resume_sample, int(document.get("end", 0)))
        closed_ids = {int(document["segment_id"]) for document in batch}
        if job.preview_work is not None and job.preview_work.segment_id in closed_ids:
            job.preview_work = None
            if job.preview_task is not None:
                job.preview_task.cancel()
                job.preview_task = None
        return True

    def _restore_deferred(self, job: _RecordingJob, batch: list[dict[str, Any]]) -> None:
        """Put an attempted batch back, preserving every record for a later durable retry."""
        if len(batch) > job.deferred_segments.maxlen:
            job.state = "failed"
            job.reason = "storage"
            job.terminal = True
            return
        job.deferred_segments.clear()
        job.deferred_segments.extend(batch)

    async def _reconcile_deferred(self, job: _RecordingJob) -> bool:
        """After an ambiguous append failure, learn which segment records actually landed.

        A write may succeed and its fsync raise, so retrying the same batch blindly could double-count
        pending segments. Replaying the authoritative log lets us drop the landed prefix, keep the rest for
        retry and restore counts/revisions to the log's state.
        """
        if job.log is None:
            return False
        try:
            transcript, cursors = await self._call(self._guarded, job.token, True, job.log.replay, None)
            last_end = await self._call(self._guarded, job.token, True, self._last_seg_end, job.log)
        except asyncio.CancelledError:
            raise
        except (RecorderNotFound, _StaleJob):
            self._cancel_job(job)
            return False
        except Exception:  # noqa: BLE001 - replay is the recovery; a failure keeps deferred state
            job.log_failures += 1
            if job.log_failures >= 3:
                job.state = "failed"
                job.reason = "storage"
                job.terminal = True
            return False
        if cursors.end_offset == 0 and cursors.final_cursor is None:
            # An empty log has no generation yet; adopt the recording generation before any apply.
            transcript.generation = job.generation
        job.transcript = transcript
        job.counts = {
            "final": cursors.counts.final,
            "error": cursors.counts.error,
            "skipped": cursors.counts.skipped,
            "pending": cursors.counts.pending,
        }
        job.transcript_revision = cursors.transcript_revision
        job.resume_sample = max(job.resume_sample, last_end)
        next_segment = cursors.segmentation.next_segment_id
        kept = deque(
            (document for document in job.deferred_segments
             if int(document.get("segment_id", -1)) >= next_segment),
            maxlen=job.deferred_segments.maxlen,
        )
        if len(kept) != sum(1 for document in job.deferred_segments
                           if int(document.get("segment_id", -1)) >= next_segment):
            job.state = "failed"
            job.reason = "storage"
            job.terminal = True
            return False
        job.deferred_segments = kept
        job.deferred_uncertain = False
        job.log_failures = 0
        # A tail checkpoint boundary from before the failure need not be trusted now.
        job.log_offset = 0
        job.checkpoint_log_offset = 0
        if job.preview_task is not None:
            job.preview_task.cancel()
            job.preview_task = None
            job.preview_work = None
        if not job.outstanding and not job.results:
            if cursors.final_cursor is not None:
                job.cursor_offset = cursors.final_cursor.log_offset
                job.cursor_exhausted = False
            else:
                job.cursor_offset = cursors.end_offset
                job.cursor_exhausted = True
        return True

    def _segment_document(self, job: _RecordingJob, segment: Segment) -> dict[str, Any]:
        revision = 1
        if job.transcript is not None:
            try:
                revision = job.transcript.next_revision(segment.segment_id)
            except TranscriptError:
                revision = 1
        return {
            "k": "seg",
            "generation": job.generation,
            "segment_id": segment.segment_id,
            "start": segment.start,
            "end": segment.end,
            "window_start": segment.window_start,
            "window_end": segment.window_end,
            "close_reason": segment.close_reason,
            "final_revision": revision,
        }

    async def _preview(self, job: _RecordingJob, now: float) -> None:
        if (job.state == "unavailable" or job.ended or job.preview_task is not None
                or job.segmenter is None or job.transcript is None or job.audio_fault):
            return
        if job.probe_code is not None:
            return
        try:
            window = job.segmenter.preview(available_end=job.durable)
        except SegmenterError:
            return
        if window is None:
            return
        multiplier = self._cadence_multiplier(job)
        cadence = self.config.preview_cadence_samples * multiplier
        if window.end - job.last_preview_end < cadence:
            return
        if not await self._ensure_capacity(job, window.segment_id):
            return
        try:
            revision = job.transcript.next_revision(window.segment_id)
        except TranscriptError:
            return
        work = _PreviewWork(
            segment_id=window.segment_id,
            revision=revision,
            start=window.start,
            end=window.end,
            window_start=window.window_start,
            window_end=window.window_end,
        )
        job.last_preview_end = window.end
        job.preview_work = work
        job.preview_task = self._spawn(job, "preview", work, self._window_loader(job, work))

    def _cadence_multiplier(self, job: _RecordingJob) -> int:
        lag = self._lag(job)
        if lag >= _DEGRADED_LAG:
            return 4
        if lag >= _CATCHUP_LAG:
            return 2
        return 1

    def _window_loader(self, job: _RecordingJob, work: _PreviewWork | _FinalWork) -> Callable[[], Awaitable[bytes]]:
        async def load() -> bytes:
            end = work.window_end
            pad_to = None
            if work.end - work.start < samples_for(MIN_INFERENCE_SECONDS):
                end = max(end, work.window_start + samples_for(MIN_INFERENCE_SECONDS))
                pad_to = end - work.window_start
            return await self._call(self._guarded, job.token, True, self._store.read_window_wav,
                                    job.rid, work.window_start, end, pad_to_samples=pad_to)
        return load

    async def _finals(self, job: _RecordingJob, now: float) -> None:
        if job.state == "unavailable" or job.probe_code is not None or job.audio_fault or job.log is None:
            return
        voice = self.voice
        while len(job.outstanding) < self.limits.max_outstanding_finals and not self._closed:
            backlog = int(getattr(voice, "recorder_final_backlog", 0))
            maximum = int(getattr(voice, "max_recorder_finals", 1))
            if backlog >= maximum:
                job.final_wait_until = now + self.limits.retry_soon
                return
            job.final_wait_until = 0.0
            work = await self._take_work(job, now)
            if work is None:
                return
            if work.isolated is None and not await self._ensure_capacity(job, work.segment_id):
                self._requeue(job, work, now, self.limits.retry_soon)
                return
            target = work.isolated or job.transcript
            if target is None:
                return
            try:
                existing = target.segment(work.segment_id)
                if existing is not None and existing.revision >= work.revision:
                    work.revision = target.next_revision(work.segment_id)
                else:
                    work.revision = target.reserve_through(work.segment_id, work.revision)
            except TranscriptError:
                self._requeue(job, work, now, self.limits.retry_soon)
                return
            if work.end - work.start < self.config.frame_samples:
                await self._skip_segment(job, work, target)
                continue
            job.final_work[work.segment_id] = work
            job.outstanding[work.segment_id] = self._spawn(
                job, "final", work, self._window_loader(job, work))

    async def _skip_segment(self, job: _RecordingJob, work: _FinalWork, target: Transcript) -> None:
        segment = TranscriptSegment(
            segment_id=work.segment_id,
            revision=work.revision,
            start=work.start,
            end=work.end,
            status="skipped",
            raw_text="",
            window_start=work.window_start,
            attempt=1,
            generation=job.generation,
            retranscription_token=work.retranscription_token,
        )
        await self._commit_segment(job, work, segment, target, terminal=True)

    async def _take_work(self, job: _RecordingJob, now: float) -> _FinalWork | None:
        due: list[_FinalWork] = []
        for work in job.retry_heap:
            if work.due <= now:
                due.append(work)
        if due:
            work = min(due, key=lambda item: (item.due, item.segment_id))
            job.retry_heap.remove(work)
            return work
        if (job.retry_heap
                and len(job.retry_heap) + len(job.outstanding) >= self.limits.max_retry_heap):
            # Never grow the heap or outstanding work without bound: pause the cursor while retries wait.
            return None
        if job.retry_targets:
            return job.retry_targets.popleft()
        if job.cursor_exhausted or job.cursor_offset < 0:
            return None
        return await self._next_cursor_work(job)

    async def _next_cursor_work(self, job: _RecordingJob) -> _FinalWork | None:
        """Read the next durable seg record. Returns None when the cursor is exhausted."""
        if job.log is None:
            return None
        while True:
            try:
                record = await self._call(self._guarded, job.token, True, self._next_seg,
                                          job.log, job.cursor_offset)
            except (RecorderNotFound, _StaleJob):
                self._cancel_job(job)
                return None
            except RecorderError:
                job.state = "failed"
                job.reason = "storage"
                return None
            if record is None:
                job.cursor_exhausted = True
                job.cursor_next_start = None
                return None
            document = record[2]
            job.cursor_offset = record[1]
            segment_id = int(document["segment_id"])
            if job.transcript is not None:
                if segment_id <= job.transcript.paged_through:
                    continue
                existing = job.transcript.segment(segment_id)
                if existing is not None and Transcript.terminal(existing):
                    continue
            job.cursor_next_start = int(document["start"])
            return _FinalWork(
                segment_id=segment_id,
                revision=int(document["final_revision"]),
                attempt=1,
                start=int(document["start"]),
                end=int(document["end"]),
                window_start=int(document["window_start"]),
                window_end=int(document["window_end"]),
                close_reason=str(document.get("close_reason", "silence")),
                offset=record[0],
                previous_status="pending",
            )

    @staticmethod
    def _next_seg(transcript_log: TranscriptLog, offset: int) -> tuple[int, int, dict[str, Any]] | None:
        for record in transcript_log.iter_seg(offset):
            return record.offset, record.end_offset, dict(record.document)
        return None

    @staticmethod
    def _last_seg_end(transcript_log: TranscriptLog) -> int:
        """Resume point for an unfinished segmentation: the last durable segment's content end."""
        last = 0
        for record in transcript_log.iter_seg(0):
            last = max(last, int(record.document["end"]))
        return last

    def _requeue(self, job: _RecordingJob, work: _FinalWork, now: float, delay: float) -> None:
        # A work item taken from the cursor is requeued at its own offset so it is not lost.
        if work.retranscription_token is not None and job.retry_pass:
            work.due = now + delay
            if len(job.retry_targets) < MAX_RETRY_TARGETS:
                job.retry_targets.append(work)
            return
        if work.offset and job.cursor_offset >= work.offset:
            job.cursor_offset = work.offset
            job.cursor_exhausted = False
            job.cursor_next_start = work.start
            return
        work.due = now + delay
        if len(job.retry_heap) < self.limits.max_retry_heap:
            job.retry_heap.append(work)

    # -- paging, state, progress, checkpoint -------------------------------------------------------------------

    async def _ensure_capacity(self, job: _RecordingJob, segment_id: int) -> bool:
        transcript = job.transcript
        if transcript is None:
            return False
        try:
            if segment_id in transcript.active_segment_ids:
                return True
            if segment_id <= transcript.paged_through:
                return False
            if len(transcript) < transcript.max_active_segments:
                return True
        except Exception:  # noqa: BLE001 - conservative fallback
            return False
        self._page(job, aggressive=True)
        return len(transcript) < transcript.max_active_segments

    def _maybe_page(self, job: _RecordingJob) -> None:
        self._page(job, aggressive=False)

    def _page(self, job: _RecordingJob, *, aggressive: bool) -> None:
        transcript = job.transcript
        if transcript is None or not job.ready:
            return
        ids = transcript.active_segment_ids
        if not ids:
            return
        protected = set(job.outstanding) | {work.segment_id for work in job.retry_heap}
        if job.preview_work is not None:
            protected.add(job.preview_work.segment_id)
        if job.segmenter is not None and job.segmenter.active_start is not None:
            protected.add(job.segmenter.next_segment_id)
        keep = 0 if aggressive else self.limits.active_keep
        terminal: list[int] = []
        for segment_id in ids:
            if segment_id in protected:
                break
            segment = transcript.segment(segment_id)
            if segment is None or not Transcript.terminal(segment):
                break
            terminal.append(segment_id)
        if len(terminal) <= keep:
            return
        target = terminal[len(terminal) - keep - 1]
        try:
            page = transcript.page_out_through(target)
        except TranscriptError:
            return
        job.checkpoint_dirty = True
        # The current totals no longer describe the old prefix boundary, and the log exposes no
        # per-record offset from a live append. A full-replay checkpoint is correct and bounded;
        # a future log API can restore tail checkpoints cheaply.
        job.log_offset = 0
        job.checkpoint_log_offset = 0
        # Paged records are already durable in the log; the page object is not needed in memory.
        del page

    def _processed_through(self, job: _RecordingJob) -> int:
        candidates = [work.start for work in job.final_work.values()]
        candidates.extend(work.start for work in job.retry_heap)
        candidates.extend(work.start for work in job.retry_targets)
        if job.cursor_next_start is not None:
            candidates.append(job.cursor_next_start)
        if candidates:
            return min(candidates)
        if job.segmenter is not None and job.segmenter.active_start is not None:
            return min(job.fed, job.segmenter.active_start)
        return job.fed

    def _lag(self, job: _RecordingJob) -> int:
        return max(0, job.durable - self._processed_through(job))

    def _update_state(self, job: _RecordingJob) -> None:
        if job.state == "unavailable" or job.probe_code is not None:
            return
        if job.state == "failed" and (job.corrupt or job.audio_fault or job.terminal):
            return
        if job.terminal:
            return
        if job.retry_pass:
            pending_retry = any(work.retranscription_token is not None
                                for work in job.final_work.values())
            if job.retry_targets or pending_retry:
                job.state = "catching_up"
                return
            job.retry_pass = False
        lag = self._lag(job)
        if job.ended:
            if self._all_done(job):
                job.state = "failed" if job.counts.get("error", 0) else "complete"
                job.reason = "transcript_failed" if job.counts.get("error", 0) else None
                job.terminal = True
            else:
                job.state = "catching_up"
            return
        if not job.first_success:
            job.state = "preparing"
            job.reason = None
            return
        if lag >= _CATCHUP_LAG:
            job.state = "catching_up"
        elif job.state == "catching_up" and lag < _LIVE_LAG:
            job.state = "live"
        else:
            job.state = "live"

    def _all_done(self, job: _RecordingJob) -> bool:
        return (
            job.ended
            and job.segment_finished
            and not job.outstanding
            and not job.retry_heap
            and not job.retry_targets
            and not job.results
            and job.cursor_exhausted
            and not job.deferred_segments
            and not job.deferred_uncertain
            and not job.retry_pass
            and job.counts.get("pending", 0) == 0
            and not (job.finished_durable is not None and job.durable > job.finished_durable)
        )

    async def _publish_progress(self, job: _RecordingJob, *, force: bool = False) -> None:
        service = getattr(self._host, "service", None)
        if service is None or job.progress_task is not None:
            return
        now = self.clock()
        if not force and now - job.last_progress_at < self.limits.progress_interval:
            return
        job.last_progress_at = now
        progress = {
            "processed_through": self._processed_through(job),
            "lag_samples": self._lag(job),
            "preview_through": job.preview_through,
            "pending_finals": job.counts.get("pending", 0),
            "failed": job.counts.get("error", 0),
            "transcript_revision": job.transcript_revision,
        }
        if job.reason:
            progress["reason"] = job.reason

        async def publish() -> None:
            try:
                await self._call(self._guarded, job.token, True,
                                 service.set_transcription_progress, job.rid, job.state, progress)
            except (RecorderNotFound, _StaleJob, RecorderUnavailable, RecorderError, SchedulerError):
                return
            except Exception:
                log.debug("recorder transcription progress publish failed", exc_info=True)

        task = asyncio.create_task(publish(), name=f"recorder-progress-{job.rid}")
        job.progress_task = task
        self._background.add(task)

        def done(_: asyncio.Task[Any]) -> None:
            self._background.discard(task)
            if job.progress_task is task:
                job.progress_task = None
            self._notify()

        task.add_done_callback(done)

    async def _maybe_checkpoint(self, job: _RecordingJob, now: float) -> None:
        if not job.checkpoint_dirty or job.checkpoint_task is not None or job.log is None:
            return
        if now - job.last_checkpoint_at < self.limits.checkpoint_interval:
            return
        job.checkpoint_dirty = False
        job.last_checkpoint_at = now
        checkpoint = self._checkpoint(job)
        if checkpoint is None:
            return

        async def write() -> None:
            try:
                await self._call(self._guarded, job.token, True, job.log.write_checkpoint, checkpoint)
            except (RecorderNotFound, _StaleJob, RecorderUnavailable, RecorderError):
                return
            except Exception:
                log.debug("recorder transcript checkpoint failed", exc_info=True)

        task = asyncio.create_task(write(), name=f"recorder-checkpoint-{job.rid}")
        job.checkpoint_task = task
        self._background.add(task)

        def done(_: asyncio.Task[Any]) -> None:
            self._background.discard(task)
            if job.checkpoint_task is task:
                job.checkpoint_task = None
            self._notify()

        task.add_done_callback(done)

    def _checkpoint(self, job: _RecordingJob) -> Checkpoint | None:
        if job.transcript is None or job.segmenter is None:
            return None
        tail = job.checkpoint_log_offset > 0
        counts = job.prefix_counts if tail else job.counts
        revision = job.prefix_transcript_revision if tail else job.transcript_revision
        try:
            return Checkpoint(
                generation=job.generation,
                transcript=job.transcript.checkpoint(),
                log_offset=max(0, job.checkpoint_log_offset),
                final_cursor=None,
                segmentation=Segmentation(
                    next_segment_id=job.segmenter.next_segment_id,
                    resume_sample=job.resume_sample,
                    ended_at=job.finished_durable if job.segment_finished else None,
                ),
                transcript_revision=revision,
                counts=Counts(**counts),
                state=job.state,
                reason=job.reason,
            )
        except (ValueError, TypeError):
            return None

    async def _final_checkpoints(self) -> None:
        if self._host is None:
            return
        for job in list(self._jobs.values()):
            if not job.ready or job.log is None:
                continue
            checkpoint = self._checkpoint(job)
            if checkpoint is None:
                continue
            try:
                await self._host.call_background(job.log.write_checkpoint, checkpoint)
            except (RecorderError, RecorderUnavailable):
                continue
            except Exception:
                log.debug("final recorder checkpoint failed", exc_info=True)
                continue

    # -- reconciliation and queues -----------------------------------------------------------------------------

    async def _maybe_reconcile(self, now: float) -> None:
        if now - self._last_reconcile_at < self.limits.reconcile_interval:
            return
        self._last_reconcile_at = now
        service = getattr(self._host, "service", None)
        if service is None:
            return
        try:
            items = await self._call(self._guarded, None, False, service.list, limit=50)
        except (RecorderUnavailable, RecorderError, _StaleJob, SchedulerError):
            return
        except Exception:  # noqa: BLE001 - reconciliation is a safety net, not a failure path
            return
        seen = set()
        for item in items:
            rid = item.get("id")
            if not isinstance(rid, str):
                continue
            seen.add(rid)
            if rid in self._cancelled:
                continue
            with self._signal_lock:
                signal = self._signals.get(rid)
                if signal is None:
                    signal = _Signal()
                    self._signals[rid] = signal
                self._merge_state_locked(signal, item)
                needs_job = (signal.token is None and not signal.cancelled
                             and rid not in self._jobs and rid not in self._queue
                             and rid not in self._complete_hints)
            if (needs_job and rid not in self._queue
                    and (signal.durable_offset > 0 or signal.ended or signal.recovered)):
                self._queue.append(rid)
        # ``service.list`` is bounded to the newest recordings. An existing job outside that window still
        # needs the authoritative state, so reconcile each one directly and treat only a missing row as a
        # deletion (a transient read error never deletes a job).
        for rid in list(self._jobs):
            if rid in seen:
                continue
            try:
                state = await self._call(self._guarded, None, False, service.get, rid)
            except RecorderNotFound:
                self.cancel(rid)
                continue
            except (RecorderUnavailable, RecorderError, _StaleJob, SchedulerError):
                continue
            with self._signal_lock:
                signal = self._signals.get(rid)
                if signal is not None and not signal.cancelled:
                    self._merge_state_locked(signal, state)
            seen.add(rid)
        await self._enqueue_enumerated(service)
        if len(items) < 50:
            # The list was exhaustive: a job for a recording that is no longer present was deleted.
            for rid in list(self._jobs):
                if rid not in seen and rid not in self._cancelled:
                    self.cancel(rid)

    async def _enqueue_enumerated(self, service: Any) -> None:
        """Queue recordings found by the host's bounded cursor enumeration.

        The recent list is capped at 50 and startup reconcile skips sealed/stopped recordings, so this is
        the only way an old unfinished transcript is resumed. Pages are scanned up to a fixed per-reconcile
        budget; reaching the end starts a fresh scan next cycle, which picks up newly added ids below the
        old cursor.
        """
        host = self._host
        enumerate_recordings = getattr(host, "enumerate_recordings", None)
        if not callable(enumerate_recordings):
            return
        for _ in range(MAX_ENUM_PAGES_PER_CYCLE):
            try:
                page = await enumerate_recordings(after=self._enum_after, limit=ENUM_PAGE_LIMIT)
            except (RecorderUnavailable, RecorderError, _StaleJob, SchedulerError):
                return
            if not isinstance(page, dict):
                return
            recordings = page.get("recordings")
            if not isinstance(recordings, list):
                return
            with self._signal_lock:
                if self._closed:
                    return
                queued = set(self._queue)
                room = True
                for item in recordings:
                    if not isinstance(item, dict) or "error" in item:
                        continue
                    rid = item.get("id")
                    if not isinstance(rid, str):
                        continue
                    if rid in self._cancelled or rid in self._jobs or rid in queued:
                        continue
                    if rid in self._complete_hints:
                        continue
                    if len(self._queue) >= self.limits.max_queued:
                        room = False
                        break
                    self._queue.append(rid)
                    queued.add(rid)
            if not room:
                # Keep the cursor at this page; the next reconcile fills the freed queue slots.
                return
            next_after = page.get("next_after")
            if (not isinstance(next_after, str) or not next_after
                    or next_after == self._enum_after or len(recordings) < ENUM_PAGE_LIMIT):
                self._enum_after = None
                return
            self._enum_after = next_after

    def _merge(self, recording_id: str, *, generation: int | None = None, durable_offset: int | None = None,
               ended: bool | None = None, end_offset: int | None = None, state: str | None = None) -> None:
        if not isinstance(recording_id, str):
            return
        with self._signal_lock:
            if self._closed or recording_id in self._cancelled:
                return
            signal = self._signals.get(recording_id)
            if signal is None:
                signal = _Signal()
                self._signals[recording_id] = signal
            stale_generation = False
            if generation is not None:
                if not signal.generation_known:
                    signal.generation = int(generation)
                    signal.generation_known = True
                elif int(generation) != signal.generation:
                    # A notification from a previous recording incarnation must not move any offset.
                    stale_generation = True
                signal.merged += 1
            if not stale_generation:
                if durable_offset is not None and signal.generation_known:
                    signal.durable_offset = max(signal.durable_offset, int(durable_offset))
                    self._complete_hints.pop(recording_id, None)
                if ended:
                    signal.ended = True
                    if end_offset is not None:
                        signal.end_offset = max(signal.end_offset, int(end_offset))
                    if state is not None:
                        signal.end_state = state
            if (not stale_generation and not signal.cancelled and signal.token is None
                    and recording_id not in self._jobs and recording_id not in self._queue):
                self._queue.append(recording_id)
                while len(self._queue) > self.limits.max_queued:
                    dropped = self._queue.popleft()
                    self._signals.pop(dropped, None)
        self._notify()

    def _merge_state_locked(self, signal: _Signal, state: dict[str, Any]) -> None:
        generation = state.get("generation")
        if isinstance(generation, int):
            signal.generation = generation
            signal.generation_known = True
        durable = state.get("durable_offset")
        if isinstance(durable, int):
            signal.durable_offset = max(signal.durable_offset, durable)
        active = bool(state.get("active"))
        sealed = bool(state.get("sealed"))
        capture_state = state.get("capture_state")
        final = state.get("final_sample_count")
        if not active and (sealed or capture_state in {"stopped", "interrupted"}):
            signal.ended = True
            target = durable if not isinstance(final, int) else final
            signal.end_offset = max(signal.end_offset, int(target or 0))
            signal.end_state = capture_state if isinstance(capture_state, str) else "interrupted"
        elif active:
            signal.ended = False
            signal.end_state = "recording"
        # Reconciliation is not a hook delivery: it must not revive a terminal job every interval.

    # -- helper plumbing ---------------------------------------------------------------------------------------

    def _notify(self) -> None:
        loop = None
        with self._signal_lock:
            if self._closed or self._loop is None:
                return
            if self._wake_queued:
                return
            self._wake_queued = True
            loop = self._loop
        try:
            loop.call_soon_threadsafe(self._deliver_wake)
        except RuntimeError:
            pass

    def _deliver_wake(self) -> None:
        with self._signal_lock:
            self._wake_queued = False
            event = self._wake_event
        if event is not None:
            event.set()

    def _cancel_job(self, job: _RecordingJob) -> None:
        job.terminal = True
        job.pumping = False
        job.active_capture = False
        for task in list(job.outstanding.values()):
            task.cancel()
        job.outstanding.clear()
        job.final_work.clear()
        if job.preview_task is not None:
            job.preview_task.cancel()
            job.preview_task = None
        self._jobs.pop(job.rid, None)
        with self._signal_lock:
            self._tokens.pop(job.token, None)
            signal = self._signals.get(job.rid)
            if signal is not None and signal.token is job.token:
                signal.cancelled = True

    def _trim_jobs(self) -> None:
        while len(self._jobs) > self.limits.max_jobs:
            for rid, job in list(self._jobs.items()):
                if not job.pumping and job.terminal:
                    self._jobs.pop(rid, None)
                    with self._signal_lock:
                        self._tokens.pop(job.token, None)
                        signal = self._signals.get(rid)
                        if signal is not None and signal.token is job.token:
                            self._signals.pop(rid, None)
                    break
            else:
                break

    def _drop_stale_preview(self, job: _RecordingJob) -> None:
        if job.preview_work is None or job.transcript is None:
            return
        segment = job.transcript.segment(job.preview_work.segment_id)
        if segment is not None and segment.status == "final":
            job.preview_work = None
            if job.preview_task is not None:
                job.preview_task.cancel()
                job.preview_task = None

    def _spawn(self, job: _RecordingJob, kind: str,
               work: _PreviewWork | _FinalWork,
               loader: Callable[[], Awaitable[bytes]]) -> asyncio.Task[Any]:
        async def run() -> Any:
            return await self.voice.transcribe_window(
                loader, kind="preview" if kind == "preview" else "final", generation=job.generation)

        task = asyncio.create_task(run(), name=f"recorder-{kind}-{job.rid}-{work.segment_id}")

        def done(completed: asyncio.Task[Any]) -> None:
            error: BaseException | None = None
            value: Any = None
            try:
                value = completed.result()
            except asyncio.CancelledError:
                return
            except BaseException as exc:  # noqa: BLE001 - delivered to the driver, never lost
                error = exc
            if self._closed or self._jobs.get(job.rid) is not job or job.token is None:
                return
            job.results.append(_Result(kind, work, value, error))
            self._notify()

        task.add_done_callback(done)
        return task

    def _spawn_background(self, coro: Awaitable[Any]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)

        def done(_: asyncio.Task[Any]) -> None:
            self._background.discard(task)
            self._notify()

        task.add_done_callback(done)

    async def _call(self, guard: Callable[..., Any] | None, token: object | None, guarded: bool,
                    fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._host is None:
            raise RecorderUnavailable("the scheduler is not attached")
        if guarded:
            if token is None:
                raise _StaleJob
            return await self._host.call_background(self._guarded, token, fn, *args, **kwargs)
        return await self._host.call_background(fn, *args, **kwargs)

    def _guarded(self, token: object | None, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Runs on the recorder worker: re-check the cancellation flag before touching files."""
        if token is None:
            return fn(*args, **kwargs)
        with self._signal_lock:
            if self._closed:
                raise _StaleJob
            rid = self._tokens.get(token)
            if rid is None or rid in self._cancelled:
                raise _StaleJob
            signal = self._signals.get(rid)
            if signal is None or signal.cancelled or signal.token is not token:
                raise _StaleJob
        return fn(*args, **kwargs)

    async def _retry_targets(self, job: _RecordingJob, scope: Any) -> list[_FinalWork]:
        if job.log is None or job.transcript is None:
            return []
        explicit = isinstance(scope, (list, tuple))
        if explicit:
            requested = [int(item) for item in scope if isinstance(item, int) and not isinstance(item, bool)]
            requested = requested[:MAX_RETRY_TARGETS]
        else:
            requested = []
        work: list[_FinalWork] = []
        items = await self._call(self._guarded, job.token, True, self._page_all, job.log)
        by_id = {int(item["segment_id"]): item for item in items}
        if explicit:
            # An explicit list, including [], is authoritative: it never falls back to failed/all.
            ids = requested
        else:
            ids = [
                segment_id for segment_id in sorted(by_id)
                if scope == "all" or by_id[segment_id].get("status") == "error"
            ][:MAX_RETRY_TARGETS]
        for segment_id in ids[:MAX_RETRY_TARGETS]:
            document = by_id.get(segment_id)
            if document is None:
                continue
            status = document.get("status")
            if status == "pending":
                continue
            active = job.transcript.segment(segment_id)
            isolated = None
            if active is None:
                try:
                    isolated = Transcript.from_documents([document], generation=job.generation)
                    token = isolated.begin_retranscription(segment_id)
                    revision = isolated.next_revision(segment_id)
                except (TranscriptError, KeyError, TypeError):
                    continue
            else:
                try:
                    token = job.transcript.begin_retranscription(segment_id)
                    revision = job.transcript.next_revision(segment_id)
                except TranscriptError:
                    continue
            work.append(_FinalWork(
                segment_id=segment_id,
                revision=revision,
                attempt=1,
                start=int(document.get("start", 0)),
                end=int(document.get("end", 0)),
                window_start=int(document.get("window_start", document.get("start", 0))),
                window_end=int(document.get("end", 0)),
                close_reason="retry",
                retranscription_token=token,
                isolated=isolated,
                previous_status=str(status) if status in {"final", "error", "skipped"} else None,
                due=0.0,
            ))
        return work

    @staticmethod
    def _page_all(transcript_log: TranscriptLog) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after = -1
        while True:
            page = transcript_log.read_page(after, 200)
            if not page.segments:
                break
            items.extend(dict(item) for item in page.segments)
            if page.next_after is None or page.next_after <= after:
                break
            after = page.next_after
            if len(items) >= MAX_RETRY_TARGETS:
                break
        return items[:MAX_RETRY_TARGETS]

    @staticmethod
    def _drop_error(error: BaseException) -> bool:
        if isinstance(error, _StaleJob):
            return True
        return isinstance(error, VoiceError) and error.code in _DROP_CODES


def _bounded_text(value: str) -> str:
    return value if len(value) <= MAX_TEXT_CHARS else value[:MAX_TEXT_CHARS]


def _logical_status(segment: TranscriptSegment) -> str:
    """Aggregate status used by checkpoints: a retryable error is still pending, not terminal."""
    if segment.status == "error" and segment.attempt < MAX_ATTEMPTS:
        return "pending"
    return segment.status


def _voice_reason(code: str) -> str:
    return {
        VOICE_TIMEOUT: "timeout",
        "voice_bad_audio": "audio_invalid",
        "voice_load_failed": "model_load_failed",
        "voice_model_invalid": "model_invalid",
    }.get(code, "engine_failed")
