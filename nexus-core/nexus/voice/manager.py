"""Lazy STT model lifecycle, consented cache download and fair serialized inference.

Photon runs in one dedicated child process, keeping torch, Kestrel and its
telemetry reporter out of the daemon's process. The worker is loaded on the
first transcription only and closed after ten idle minutes.

One scheduler admits every caller to the same worker. Interactive dictation and
HTTP requests keep their existing fail-fast busy semantics and priority;
recorder work is admitted without counting against their pending limit, with a
replaceable preview slot and final windows that are only materialised when the
worker reaches them. Recorder finals have a bounded admitted backlog and reject
new work with a retryable `recorder_backlog_full` error instead of growing
queued audio or lazy loaders without limit.

Adapted from old Nexus nexus/voice/manager.py.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .model import TranscribeResult, VoiceError
from .store import EXPECTED_SIZE, EXPECTED_SIZES, MODEL_ID, ModelPaths, ModelStore, unsupported_reason

_PATH = re.compile(r"(?:[A-Za-z]:\\|~?/)[^\s'\",;)]*")
IDLE_UNLOAD_SECONDS = 600
MAX_PENDING = 3  # one active call plus two normal requests waiting for the worker
RECORDER_PREVIEW_FINALS_PER_TURN = 1  # one replaceable preview per final so finals cannot starve
MAX_RECORDER_FINALS = 8  # recorder finals queued or in flight; bounds their bytes/loaders
RECORDER_BACKLOG_FULL = "recorder_backlog_full"  # retryable: keep the record on disk and try later
RECORDER_EXECUTE_TIMEOUT_S = 60.0  # bounds recorder-only inference; interactive calls are untouched
VOICE_TIMEOUT = "voice_timeout"  # recorder execution expired; the shared worker was restarted

RecorderWindowKind = Literal["preview", "final"]


@dataclass(frozen=True, slots=True)
class WindowResult:
    """A recorder window transcription tagged with its scheduling generation."""

    text: str
    duration_s: float
    elapsed_s: float
    generation: int
    kind: RecorderWindowKind


@dataclass(slots=True)
class _Job:
    kind: str
    generation: int
    seq: int
    started: float
    audio: bytes | None = None
    info: Any = None
    loader: Callable[[], Any] | None = None
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class _Completed:
    text: str | None
    info: Any
    elapsed_s: float


def _is_async_callable(function: Callable[..., Any]) -> bool:
    if inspect.iscoroutinefunction(function):
        return True
    return inspect.iscoroutinefunction(type(function).__call__)


def _parse_wav(data: bytes) -> Any:
    from .audio import VoiceAudioError, parse_wav

    try:
        return parse_wav(data)
    except VoiceAudioError as exc:
        raise VoiceError(exc.code, str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise VoiceError("voice_bad_audio", "Invalid voice audio") from exc


def _describe(exc: BaseException) -> str:
    text = " ".join(_PATH.sub("[path]", str(exc)).split())
    text = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return text if len(text) <= 160 else text[:159] + "…"


class Engine(Protocol):
    async def load(self) -> None: ...
    async def transcribe(self, audio: bytes) -> str: ...
    async def close(self) -> None: ...


def _default_engine(paths: ModelPaths) -> Engine:
    from .engine import PhotonWorkerEngine

    return PhotonWorkerEngine(paths)


def _default_available() -> bool:
    from .engine import PhotonWorkerEngine

    return PhotonWorkerEngine.available()


_default_engine.available = staticmethod(_default_available)


def _default_download(store: ModelStore) -> ModelPaths:
    from .download import download_stt

    return download_stt(store)


class VoiceManager:
    """Keep Photon cold until requested, serialize inference, and unload when idle."""

    def __init__(
        self,
        repo_getter: str | Callable[[], str] = MODEL_ID,
        *,
        engine_factory: Callable[[ModelPaths], Engine] | None = None,
        store: ModelStore | None = None,
        download_runner: Callable[[ModelStore], ModelPaths] | None = None,
        idle_unload_seconds: float = IDLE_UNLOAD_SECONDS,
        max_recorder_finals: int = MAX_RECORDER_FINALS,
        recorder_execute_timeout_s: float | None = RECORDER_EXECUTE_TIMEOUT_S,
    ) -> None:
        if max_recorder_finals < 1:
            raise ValueError("max_recorder_finals must be at least 1")
        if recorder_execute_timeout_s is not None and (
            isinstance(recorder_execute_timeout_s, bool)
            or not isinstance(recorder_execute_timeout_s, (int, float))
            or not math.isfinite(recorder_execute_timeout_s)
            or recorder_execute_timeout_s <= 0
        ):
            raise ValueError("recorder_execute_timeout_s must be a positive number or None")
        self.repo_getter = repo_getter
        self.engine_factory = engine_factory or _default_engine
        self.store = store or ModelStore()
        self.download_runner = download_runner or _default_download
        self.idle_unload_seconds = idle_unload_seconds
        self._max_recorder_finals = max_recorder_finals
        self.recorder_execute_timeout_s = recorder_execute_timeout_s
        self._engine: Engine | None = None
        self._lock = asyncio.Lock()
        self._pending = 0
        self._condition = asyncio.Condition()
        self._waiters: list[_Job] = []
        self._active: _Job | None = None
        self._job_seq = 0
        self._finals_since_preview = RECORDER_PREVIEW_FINALS_PER_TURN
        self._idle_task: asyncio.Task[None] | None = None
        self._download_task: asyncio.Task[None] | None = None
        self._download_state = "absent"
        self._download_error: str | None = None
        self._download_bytes = 0
        self._last_error: str | None = None
        self._closed = False

    def _repo(self) -> str:
        return self.repo_getter() if callable(self.repo_getter) else self.repo_getter

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    @property
    def busy(self) -> bool:
        """Interactive callers' pending count; recorder work never counts here."""
        return self._pending > 0

    def _scheduler_busy(self) -> bool:
        return self._pending > 0 or self._active is not None or bool(self._waiters)

    @property
    def max_recorder_finals(self) -> int:
        """Maximum recorder finals admitted (queued or in flight) before retryable backpressure."""
        return self._max_recorder_finals

    @property
    def recorder_final_backlog(self) -> int:
        """Admitted recorder finals waiting or in flight, for recorder backpressure."""
        active = 1 if self._active is not None and self._active.kind == "recorder_final" else 0
        return active + sum(job.kind == "recorder_final" for job in self._waiters)

    async def readiness(self) -> str | None:
        """Non-loading, offline recorder readiness probe.

        Returns ``None`` when a recorder window can be attempted, otherwise a stable
        ``VoiceError`` code: ``voice_unavailable`` (manager closed), ``voice_model_unsupported``,
        ``voice_dependency_missing``, ``voice_model_not_cached`` or ``voice_model_invalid``.
        It never constructs or loads an engine, never downloads, and never contacts the network.
        Callers should check ``downloading`` separately when a download is in progress.
        """
        if self._closed:
            return "voice_unavailable"
        if unsupported_reason(self._repo()) is not None:
            return "voice_model_unsupported"
        if self._engine is not None:
            return None
        availability = getattr(self.engine_factory, "available", None)
        if callable(availability) and not availability():
            return "voice_dependency_missing"
        try:
            await asyncio.to_thread(self.store.verified_paths)
        except FileNotFoundError:
            return "voice_model_not_cached"
        except Exception:  # noqa: BLE001 - any cache verification failure is a stable invalid status
            return "voice_model_invalid"
        return None

    @property
    def downloading(self) -> bool:
        return self._download_state == "downloading"

    @property
    def download_status(self) -> dict[str, Any]:
        return {
            "state": self._download_state,
            "bytes_done": self._download_bytes,
            "bytes_total": EXPECTED_SIZE,
            "error": self._download_error,
        }

    @property
    def error(self) -> str | None:
        return self._download_error or self._last_error

    async def prepare(self) -> None:
        """Load the cached model before microphone capture begins."""
        if self._closed:
            raise VoiceError("voice_unavailable", "Voice input is unavailable")
        if self.downloading or self._pending >= MAX_PENDING:
            raise VoiceError("voice_busy", "Voice is busy")
        self._pending += 1
        try:
            await self._submit(_Job(kind="interactive", generation=0, seq=self._next_seq(), started=time.monotonic()))
        finally:
            self._pending = max(0, self._pending - 1)
            self._maybe_schedule_unload()

    async def transcribe(self, wav: bytes, *, partial: bool = False) -> TranscribeResult:
        """Validate and transcribe a WAV; partial previews never wait behind work."""
        info = _parse_wav(wav)
        if self._closed:
            raise VoiceError("voice_unavailable", "Voice input is unavailable")
        if self.downloading:
            raise VoiceError("voice_busy", "The voice model is downloading")
        if partial and self._scheduler_busy():
            raise VoiceError("voice_busy", "Voice inference is already in progress")
        if self._pending >= MAX_PENDING:
            raise VoiceError("voice_busy", "Voice is busy; try again shortly")

        self._pending += 1
        started = time.monotonic()
        try:
            job = _Job(
                kind="interactive_preview" if partial else "interactive",
                generation=0,
                seq=self._next_seq(),
                started=started,
                audio=wav,
                info=info,
            )
            completed = await self._submit(job)
            assert completed.text is not None
            return TranscribeResult(text=completed.text, duration_s=info.duration_s, elapsed_s=completed.elapsed_s)
        finally:
            self._pending = max(0, self._pending - 1)
            self._maybe_schedule_unload()

    async def transcribe_window(
        self,
        pcm: bytes | Callable[[], bytes | Awaitable[bytes]],
        *,
        kind: RecorderWindowKind | Literal["recorder_preview", "recorder_final"],
        generation: int,
    ) -> WindowResult:
        """Transcribe one bounded recorder window without consuming interactive slots.

        ``pcm`` is a bounded PCM16 WAV window, or a callable returning one. A callable is
        invoked only when the worker is ready for this job, so queued recorder finals can be
        backed by records on disk instead of materialised audio. A preview replaces any older
        preview that has not started; a final takes its turn between previews.

        Loader contract: synchronous callables run in a worker thread through
        ``asyncio.to_thread``, so they must be thread-safe, read-only (or safely repeatable) and
        must not touch loop-bound objects. Cancelling the caller stops waiting but cannot stop a
        synchronous loader already running; its result is discarded. Async functions (including
        async ``__call__``) stay on the loop, and a synchronous callable that returns an
        awaitable has that awaitable awaited on the loop. Only the selected job's loader runs;
        queued loaders are never invoked.

        Recorder-final contract for the recorder scheduler (C3):

        * At most ``max_recorder_finals`` finals are admitted across the active window and the
          waiting queue. The bound covers both byte-backed jobs and lazy loaders, so a caller
          cannot grow pending final work without limit.
        * Only the one active final is ever materialised. Loaders stay on disk records until
          the worker selects that job; at most one is invoked at a time.
        * Admission overflow raises ``VoiceError`` with ``RECORDER_BACKLOG_FULL``. It is
          retryable: keep the segment record on disk, do not load its audio, and resubmit after
          ``recorder_final_backlog`` drops below ``max_recorder_finals`` (the completion,
          failure or cancellation of any final frees a slot).
        * Interactive and prepare jobs are never rejected by this bound and still run before
          queued recorder work. A full final backlog never delays previews or dictation.
        * A queued preview superseded by a newer preview raises ``voice_superseded``; completed
          results carry ``generation`` so C3 can ignore an older result that finished late.
        * Recorder inference is bounded by ``recorder_execute_timeout_s`` (default 60 s, ``None``
          disables it). Expiry closes the shared worker and raises ``voice_timeout``; the next
          caller reloads it. Interactive jobs are never subject to this timeout, and cancelling
          an active recorder job still raises ``CancelledError`` after closing the worker.
        """
        if kind in ("preview", "recorder_preview"):
            job_kind, result_kind = "recorder_preview", "preview"
        elif kind in ("final", "recorder_final"):
            job_kind, result_kind = "recorder_final", "final"
        else:
            raise ValueError("kind must be preview or final")
        if self._closed:
            raise VoiceError("voice_unavailable", "Voice input is unavailable")
        if self.downloading:
            raise VoiceError("voice_busy", "The voice model is downloading")

        loader: Callable[[], Any] | None = None
        audio: bytes | None = None
        info: Any = None
        if callable(pcm):
            loader = pcm
        else:
            if not isinstance(pcm, bytes):
                raise VoiceError("voice_bad_audio", "Invalid voice audio")
            audio = pcm
            info = _parse_wav(pcm)
        job = _Job(
            kind=job_kind,
            generation=int(generation),
            seq=self._next_seq(),
            started=time.monotonic(),
            audio=audio,
            info=info,
            loader=loader,
        )
        completed = await self._submit(job)
        assert completed.text is not None
        return WindowResult(
            text=completed.text,
            duration_s=completed.info.duration_s,
            elapsed_s=completed.elapsed_s,
            generation=job.generation,
            kind=result_kind,
        )

    def _next_seq(self) -> int:
        self._job_seq += 1
        return self._job_seq

    async def _submit(self, job: _Job) -> _Completed:
        async with self._condition:
            if self._closed:
                raise VoiceError("voice_unavailable", "Voice input is unavailable")
            if job.kind == "recorder_final" and self.recorder_final_backlog >= self.max_recorder_finals:
                raise VoiceError(
                    RECORDER_BACKLOG_FULL,
                    "Recorder final backlog is full; retry after a final completes",
                )
            if job.kind == "recorder_preview":
                for previous in self._waiters:
                    if previous.kind == "recorder_preview":
                        previous.superseded = True
                        self._waiters.remove(previous)
                        break
            self._waiters.append(job)
            self._condition.notify_all()
        return await self._run(job)

    async def _run(self, job: _Job) -> _Completed:
        try:
            async with self._condition:
                while True:
                    if job.superseded:
                        raise VoiceError("voice_superseded", "A newer recorder preview replaced this one")
                    if self._closed:
                        raise VoiceError("voice_unavailable", "Voice input is unavailable")
                    if self._active is None and self._select_locked() is job:
                        self._waiters.remove(job)
                        self._active = job
                        if job.kind == "recorder_preview":
                            self._finals_since_preview = 0
                        break
                    await self._condition.wait()
        except BaseException:
            async with self._condition:
                if job in self._waiters:
                    self._waiters.remove(job)
                self._condition.notify_all()
            raise
        try:
            return await self._execute(job)
        finally:
            async with self._condition:
                if self._active is job:
                    self._active = None
                if job.kind == "recorder_final":
                    self._finals_since_preview += 1
                self._condition.notify_all()
            self._maybe_schedule_unload()

    def _select_locked(self) -> _Job | None:
        interactive = next(
            (job for job in self._waiters if job.kind in ("interactive", "interactive_preview")), None
        )
        if interactive is not None:
            return interactive
        preview = next((job for job in self._waiters if job.kind == "recorder_preview"), None)
        final = next((job for job in self._waiters if job.kind == "recorder_final"), None)
        if preview is not None and (
            final is None or self._finals_since_preview >= RECORDER_PREVIEW_FINALS_PER_TURN
        ):
            return preview
        return final if final is not None else preview

    async def _execute(self, job: _Job) -> _Completed:
        wav = job.audio
        info = job.info
        if job.loader is not None:
            if _is_async_callable(job.loader):
                loaded = job.loader()
            else:
                loaded = await asyncio.to_thread(job.loader)
            if inspect.isawaitable(loaded):
                loaded = await loaded
            if not isinstance(loaded, bytes):
                raise VoiceError("voice_bad_audio", "Invalid voice audio")
            wav = loaded
            info = _parse_wav(loaded)

        async with self._lock:
            if self._closed:
                raise VoiceError("voice_unavailable", "Voice input is unavailable")
            self._cancel_idle()
            engine = await self._ensure_engine()
            if wav is None:
                return _Completed(text=None, info=None, elapsed_s=time.monotonic() - job.started)
            timeout = None
            if job.kind.startswith("recorder_") and self.recorder_execute_timeout_s is not None:
                timeout = asyncio.timeout(self.recorder_execute_timeout_s)
            try:
                if timeout is None:
                    text = await engine.transcribe(wav)
                else:
                    async with timeout:
                        text = await engine.transcribe(wav)
            except TimeoutError as exc:
                if timeout is not None and timeout.expired():
                    self._last_error = "Recorder transcription timed out"
                    await self._close_engine()
                    raise VoiceError(VOICE_TIMEOUT, "Recorder transcription timed out") from exc
                self._last_error = _describe(exc)
                await self._close_engine()
                raise VoiceError("voice_failed", "Voice transcription failed") from exc
            except asyncio.CancelledError:
                await self._close_engine()
                raise
            except Exception as exc:
                self._last_error = _describe(exc)
                await self._close_engine()
                raise VoiceError("voice_failed", "Voice transcription failed") from exc
        if not isinstance(text, str):
            self._last_error = "The engine returned an invalid transcript"
            raise VoiceError("voice_failed", "Voice transcription failed")
        self._last_error = None
        return _Completed(text=text, info=info, elapsed_s=time.monotonic() - job.started)

    async def _ensure_engine(self) -> Engine:
        reason = unsupported_reason(self._repo())
        if reason is not None:
            raise VoiceError("voice_model_unsupported", reason)
        if self._engine is not None:
            return self._engine
        availability = getattr(self.engine_factory, "available", None)
        if callable(availability) and not availability():
            raise VoiceError("voice_dependency_missing", "STT needs the voice extra: nexus voice init")
        try:
            paths = await asyncio.to_thread(self.store.verified_paths)
        except FileNotFoundError as exc:
            raise VoiceError("voice_model_not_cached", "The STT model is not downloaded") from exc
        except Exception as exc:
            self._last_error = _describe(exc)
            raise VoiceError("voice_model_invalid", "The cached STT model failed verification") from exc

        engine = self.engine_factory(paths)
        try:
            await engine.load()
        except asyncio.CancelledError:
            await engine.close()
            raise
        except ImportError as exc:
            await engine.close()
            self._last_error = _describe(exc)
            raise VoiceError("voice_dependency_missing", f"STT runtime failed: {self._last_error}. Run: nexus voice init") from exc
        except FileNotFoundError as exc:
            await engine.close()
            raise VoiceError("voice_model_not_cached", "The STT model is not downloaded") from exc
        except Exception as exc:
            await engine.close()
            self._last_error = _describe(exc)
            raise VoiceError("voice_load_failed", "The STT model could not be loaded") from exc
        self._engine = engine
        self._last_error = None
        return engine

    async def start_download(self, publish: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """Start the explicit, consented download and return its initial state."""
        if self._closed:
            raise VoiceError("voice_unavailable", "Voice input is unavailable")
        reason = unsupported_reason(self._repo())
        if reason is not None:
            raise VoiceError("voice_model_unsupported", reason)
        if self._scheduler_busy() or self.downloading:
            raise VoiceError("voice_busy", "Voice is busy")
        self._download_state = "downloading"
        self._download_error = None
        self._download_bytes = 0
        self._download_task = asyncio.create_task(self._run_download(publish), name="nexus-voice-download")
        data = self.download_status
        publish({"model": "stt", **data})
        return data

    async def _run_download(self, publish: Callable[[dict[str, Any]], None]) -> None:
        baseline = self.store.cache_sizes()
        last_done = 0
        try:
            worker = asyncio.create_task(asyncio.to_thread(self.download_runner, self.store))
            while not worker.done():
                await asyncio.sleep(0.5)
                current = self.store.cache_sizes()
                done = sum(
                    min(EXPECTED_SIZES[repo], max(0, current[repo] - baseline.get(repo, 0)))
                    for repo in EXPECTED_SIZES
                )
                if done != last_done:
                    self._download_bytes = done
                    last_done = done
                    publish({"model": "stt", **self.download_status})
            await worker
            self._download_state = "ready"
            self._download_bytes = EXPECTED_SIZE
            self._last_error = None
            publish({"model": "stt", **self.download_status})
        except Exception as exc:  # noqa: BLE001 - a download failure must update status and publish an event
            self._download_state = "error"
            self._download_error = _describe(exc)
            publish({"model": "stt", **self.download_status})

    def _schedule_unload(self) -> None:
        self._cancel_idle()
        if self.idle_unload_seconds <= 0:
            return
        self._idle_task = asyncio.create_task(self._unload_after_idle(), name="nexus-voice-idle-unload")

    def _cancel_idle(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _maybe_schedule_unload(self) -> None:
        if self._pending == 0 and self._active is None and not self._waiters and self.loaded and not self._closed:
            self._schedule_unload()

    async def _unload_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.idle_unload_seconds)
            async with self._lock:
                if self._pending == 0 and self._active is None and not self._waiters:
                    await self._close_engine()
        except asyncio.CancelledError:
            pass

    async def _close_engine(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            await engine.close()

    async def unload(self) -> None:
        """Drop the loaded model now (before its files are removed); the manager stays usable."""
        self._cancel_idle()
        async with self._lock:
            await self._close_engine()

    async def close(self) -> None:
        self._closed = True
        self._cancel_idle()
        async with self._condition:
            self._condition.notify_all()
        if self._download_task is not None and not self._download_task.done():
            await asyncio.gather(self._download_task, return_exceptions=True)
        async with self._lock:
            await self._close_engine()


__all__ = [
    "IDLE_UNLOAD_SECONDS",
    "MAX_PENDING",
    "MAX_RECORDER_FINALS",
    "RECORDER_BACKLOG_FULL",
    "RECORDER_EXECUTE_TIMEOUT_S",
    "RECORDER_PREVIEW_FINALS_PER_TURN",
    "VOICE_TIMEOUT",
    "Engine",
    "RecorderWindowKind",
    "VoiceManager",
    "WindowResult",
]
