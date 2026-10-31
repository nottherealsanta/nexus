"""Lazy STT model lifecycle, consented cache download and serialized inference.

Photon runs in one dedicated child process, keeping torch, Kestrel and its
telemetry reporter out of the daemon's process. The worker is loaded on the
first transcription only and closed after ten idle minutes.

Adapted from old Nexus nexus/voice/manager.py.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from .model import TranscribeResult, VoiceError
from .store import EXPECTED_SIZE, EXPECTED_SIZES, MODEL_ID, ModelPaths, ModelStore, unsupported_reason

_PATH = re.compile(r"(?:[A-Za-z]:\\|~?/)[^\s'\",;)]*")
IDLE_UNLOAD_SECONDS = 600
MAX_PENDING = 3  # one active call plus two normal requests waiting for the worker


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
    ) -> None:
        self.repo_getter = repo_getter
        self.engine_factory = engine_factory or _default_engine
        self.store = store or ModelStore()
        self.download_runner = download_runner or _default_download
        self.idle_unload_seconds = idle_unload_seconds
        self._engine: Engine | None = None
        self._lock = asyncio.Lock()
        self._pending = 0
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
        return self._pending > 0

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
            async with self._lock:
                self._cancel_idle()
                await self._ensure_engine()
        finally:
            self._pending -= 1
            if self._pending == 0 and self.loaded and not self._closed:
                self._schedule_unload()

    async def transcribe(self, wav: bytes, *, partial: bool = False) -> TranscribeResult:
        """Validate and transcribe a WAV; partial previews never wait behind work."""
        from .audio import VoiceAudioError, parse_wav

        try:
            info = parse_wav(wav)
        except VoiceAudioError as exc:
            raise VoiceError(exc.code, str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise VoiceError("voice_bad_audio", "Invalid voice audio") from exc
        if self._closed:
            raise VoiceError("voice_unavailable", "Voice input is unavailable")
        if self.downloading:
            raise VoiceError("voice_busy", "The voice model is downloading")
        if partial and self._pending:
            raise VoiceError("voice_busy", "Voice inference is already in progress")
        if self._pending >= MAX_PENDING:
            raise VoiceError("voice_busy", "Voice is busy; try again shortly")

        self._pending += 1
        started = time.monotonic()
        try:
            async with self._lock:
                self._cancel_idle()
                engine = await self._ensure_engine()
                try:
                    text = await engine.transcribe(wav)
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
            return TranscribeResult(text=text, duration_s=info.duration_s, elapsed_s=time.monotonic() - started)
        finally:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0 and self.loaded and not self._closed:
                self._schedule_unload()

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
            raise VoiceError("voice_dependency_missing", "STT needs the voice extra: nexus voice init") from exc
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
        if self._pending or self.downloading:
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

    async def _unload_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.idle_unload_seconds)
            async with self._lock:
                if self._pending == 0:
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
        if self._download_task is not None and not self._download_task.done():
            await asyncio.gather(self._download_task, return_exceptions=True)
        async with self._lock:
            await self._close_engine()


__all__ = ["IDLE_UNLOAD_SECONDS", "Engine", "VoiceManager"]
