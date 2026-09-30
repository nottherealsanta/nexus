"""Serialized voice lifecycle and inference (VOICE_PLAN.md §4, §7)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .model import TranscribeResult, VoiceError, VoiceState


class Engine(Protocol):
    async def load(self) -> None: ...
    async def transcribe(self, audio: bytes) -> str: ...
    async def close(self) -> None: ...


class Store(Protocol):
    async def ensure(
        self, progress_cb: Callable[..., None], *, allow_download: bool = True
    ) -> Path: ...
    async def remove(self) -> None: ...


class VoiceManager:
    """Own model preparation and bound inference to one active plus two waiting requests."""

    def __init__(
        self,
        config: Any,
        *,
        engine_factory: Callable[[Path], Engine],
        store: Store,
        request_timeout: float | None = None,
    ) -> None:
        self.config = config
        self.engine_factory = engine_factory
        self.store = store
        self.request_timeout = request_timeout
        self._state = VoiceState(
            state="disabled" if not self._enabled() else "absent",
            revision=str(getattr(config, "revision", "")),
        )
        self._engine: Engine | None = None
        self._prepare_task: asyncio.Task[VoiceState] | None = None
        self._pending = 0
        self._requests: dict[str, asyncio.Task[TranscribeResult]] = {}
        self._workers: set[asyncio.Task[TranscribeResult]] = set()
        self._engine_cleanup_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._worker_lock = asyncio.Lock()
        self._closed = False
        self._removing = False
        self._last_activity = time.monotonic()
        self.real_time_factor: float | None = None
        self._unload_task: asyncio.Task[None] | None = None

    def _enabled(self) -> bool:
        import os

        return (
            bool(getattr(self.config, "enabled", False))
            and os.environ.get("NEXUS_VOICE", "").lower() != "off"
        )

    @property
    def active(self) -> bool:
        return self._state.state in {"downloading", "loading"} or self._pending > 0

    def status(self) -> VoiceState:
        self._sync_enabled()
        return self._state

    def _sync_enabled(self) -> None:
        if self._enabled():
            if self._state.state == "disabled":
                self._state = self._new_state("absent")
        elif self._state.state != "disabled":
            self._state = self._new_state("disabled")

    def configure(self, config: Any) -> None:
        """Apply live settings and prevent a disabled prepare from becoming ready."""
        previous = self.config
        self.config = config
        self._sync_enabled()
        changed = (
            getattr(previous, "device", None) != getattr(config, "device", None)
            or getattr(previous, "revision", None) != getattr(config, "revision", None)
        )
        if changed and self._state.state != "disabled":
            if self._prepare_task and not self._prepare_task.done():
                self._prepare_task.cancel()
            if self._engine is not None and not self._workers:
                asyncio.create_task(self._close_engine())
            self._state = self._new_state("absent")
        if not self._enabled():
            if self._prepare_task and not self._prepare_task.done():
                self._prepare_task.cancel()
            for task in tuple(self._workers):
                task.cancel()
            if self._engine is not None and self._engine_cleanup_task is None:
                self._engine_cleanup_task = asyncio.create_task(self._close_engine())

    async def _close_engine(self) -> None:
        workers = tuple(self._workers)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._lock:
            engine, self._engine = self._engine, None
            if engine is not None:
                await engine.close()

    def schedule_prepare(
        self, force: bool = False, *, allow_download: bool = True
    ) -> VoiceState:
        """Start preparation in the background and return the current snapshot."""
        self._sync_enabled()
        if not self._enabled():
            return self._state
        if self._closed or self._removing:
            return self._set_error("Voice manager is shutting down")
        if self._prepare_task is None or self._prepare_task.done():
            self._prepare_task = asyncio.create_task(
                self._prepare(allow_download=allow_download),
                name="nexus-voice-prepare",
            )
        return self._state

    async def prepare(
        self, force: bool = False, *, allow_download: bool = True
    ) -> VoiceState:
        self._sync_enabled()
        if not self._enabled():
            return self._state
        if self._closed or self._removing:
            return self._set_error("Voice manager is shutting down")
        if self._prepare_task is None or self._prepare_task.done():
            self._prepare_task = asyncio.create_task(
                self._prepare(allow_download=allow_download),
                name="nexus-voice-prepare",
            )
        await asyncio.shield(self._prepare_task)
        return self._state

    async def _prepare(self, *, allow_download: bool = True) -> VoiceState:
        async with self._lock:
            self._sync_enabled()
            if self._closed or self._removing or not self._enabled():
                return self._state
            if self._state.state == "ready" and self._engine is not None:
                return self._state
            try:
                if self._engine is not None:
                    await self._engine.close()
                    self._engine = None
                # The store first verifies an existing cache. Its progress
                # callback switches the snapshot to downloading only when a
                # confirmed prepare actually starts fetching missing weights.
                self._state = self._new_state("loading")
                self._last_activity = time.monotonic()
                if allow_download:
                    factory = self.engine_factory
                    availability = getattr(factory, "available", None)
                    if not callable(availability):
                        availability = getattr(
                            getattr(factory, "func", None), "available", None
                        )
                    if callable(availability) and not availability():
                        self._state = self._new_state(
                            "unsupported",
                            message="Voice runtime is not installed. Install the voice extra (uv sync --extra voice) and restart the daemon.",
                        )
                        return self._state
                path = await self.store.ensure(
                    self._progress, allow_download=allow_download
                )
                self._sync_enabled()
                if self._closed or self._removing or not self._enabled():
                    return self._state
                self._state = self._new_state("loading", progress=1.0)
                engine = self.engine_factory(path)
                try:
                    await engine.load()
                    from .audio import silence

                    warmup_started = time.monotonic()
                    await engine.transcribe(silence(0.5))
                    self.real_time_factor = 0.5 / max(
                        time.monotonic() - warmup_started, 0.001
                    )
                except BaseException:
                    await engine.close()
                    raise
                self._sync_enabled()
                if self._closed or self._removing or not self._enabled():
                    await engine.close()
                    return self._state
                self._engine = engine
                self._state = self._new_state(
                    "ready",
                    progress=1.0,
                    bytes_done=self._state.bytes_done,
                    bytes_total=self._state.bytes_total,
                    device=str(
                        getattr(engine, "device", getattr(self.config, "device", ""))
                    ),
                )
                self._last_activity = time.monotonic()
                self._schedule_unload()
            except asyncio.CancelledError:
                self._sync_enabled()
                if not self._closed and self._state.state != "disabled":
                    self._state = self._new_state("absent")
                raise
            except FileNotFoundError:
                self._sync_enabled()
                if not self._closed and self._state.state != "disabled":
                    self._state = self._new_state("absent")
            except Exception:
                self._sync_enabled()
                if self._enabled() and not self._closed and not self._removing:
                    self._set_error(
                        "Voice model preparation failed. Retry from Voice settings."
                    )
            return self._state

    def _progress(self, done: int = 0, total: int = 0, *_: Any, **__: Any) -> None:
        self._sync_enabled()
        if not self._enabled() or self._closed or self._removing:
            return
        if isinstance(done, (int, float)) and isinstance(total, (int, float)):
            done_i, total_i = max(0, int(done)), max(0, int(total))
            self._state = self._new_state(
                "downloading",
                progress=min(1.0, done_i / total_i) if total_i else 0.0,
                bytes_done=done_i,
                bytes_total=total_i,
            )

    async def transcribe(
        self, audio: bytes, request_id: str = "", *, duration_s: float | None = None
    ) -> TranscribeResult:
        self._sync_enabled()
        if not self._enabled():
            raise VoiceError("voice_unavailable", "Voice input is disabled")
        if self._closed or self._removing:
            raise VoiceError("voice_unavailable", "Voice input is unavailable")
        if self._state.state != "ready" or self._engine is None:
            if self._state.state in {"absent", "error"}:
                self.schedule_prepare(allow_download=False)
            raise VoiceError("voice_not_ready", "Voice model is not ready")
        try:
            from .audio import VoiceAudioError, parse_wav

            duration_s = parse_wav(
                audio, max_seconds=int(getattr(self.config, "max_seconds", 120))
            ).duration_s
        except VoiceAudioError as exc:
            raise VoiceError(exc.code, str(exc)) from exc
        except Exception as exc:
            raise VoiceError("voice_bad_audio", "Invalid voice audio") from exc
        if request_id and (
            len(request_id) > 128 or any(ord(char) < 32 for char in request_id)
        ):
            raise VoiceError("voice_bad_audio", "Invalid voice request identifier")
        if self._pending >= 3 or (request_id and request_id in self._requests):
            raise VoiceError("voice_busy", "Voice is busy; try again shortly")
        self._pending += 1
        task = asyncio.create_task(
            self._run_transcribe(audio, float(duration_s)),
            name="nexus-voice-transcribe",
        )
        self._workers.add(task)

        def finished(done: asyncio.Task[TranscribeResult]) -> None:
            self._workers.discard(done)
            self._pending = max(0, self._pending - 1)
            if request_id and self._requests.get(request_id) is done:
                self._requests.pop(request_id, None)

        task.add_done_callback(finished)
        if request_id:
            self._requests[request_id] = task
        try:
            timeout = self.request_timeout or max(10.0, 2 * float(duration_s))
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            self._sync_enabled()
            if not self._enabled():
                raise VoiceError("voice_unavailable", "Voice input is disabled")
            return result
        except TimeoutError as exc:
            task.cancel()
            raise VoiceError("voice_timeout", "Voice transcription timed out") from exc
        except asyncio.CancelledError:
            # The worker retains its slot and lock until uninterruptible engine work stops.
            task.cancel()
            raise
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError("voice_failed", "Voice transcription failed") from exc
        finally:
            if request_id and task.done() and self._requests.get(request_id) is task:
                self._requests.pop(request_id, None)

    async def _run_transcribe(self, audio: bytes, duration: float) -> TranscribeResult:
        started = time.monotonic()
        assert self._engine is not None
        async with self._worker_lock:
            engine_task = asyncio.create_task(self._engine.transcribe(audio))
            try:
                text = await asyncio.shield(engine_task)
            except asyncio.CancelledError:
                await self._wait_cleanup(engine_task)
                raise
        elapsed = time.monotonic() - started
        self._last_activity = time.monotonic()
        self._schedule_unload()
        return TranscribeResult(text=str(text), duration_s=duration, elapsed_s=elapsed)

    @staticmethod
    async def _wait_cleanup(task: asyncio.Task[Any]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and hasattr(current, "uncancel"):
                    current.uncancel()
            except Exception:
                break

    async def cancel(self, request_id: str) -> bool:
        task = self._requests.get(request_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def remove(self) -> VoiceState:
        if self._pending:
            raise VoiceError("voice_busy", "Voice is busy; try again shortly")
        if self._closed:
            return self._state
        self._removing = True
        if self._prepare_task and not self._prepare_task.done():
            self._prepare_task.cancel()
            try:
                await self._prepare_task
            except asyncio.CancelledError:
                pass
        try:
            async with self._lock:
                if self._engine:
                    await self._engine.close()
                    self._engine = None
                await self.store.remove()
                self._state = self._new_state(
                    "disabled" if not self._enabled() else "absent"
                )
                return self._state
        finally:
            self._removing = False

    async def shutdown(self) -> None:
        self._closed = True
        self._removing = True
        if self._unload_task:
            self._unload_task.cancel()
        if self._prepare_task and not self._prepare_task.done():
            self._prepare_task.cancel()
        for task in tuple(self._workers):
            task.cancel()
        for task in (self._prepare_task, *self._workers):
            if task and not task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self._engine_cleanup_task and not self._engine_cleanup_task.done():
            await asyncio.gather(self._engine_cleanup_task, return_exceptions=True)
        await self._close_engine()

    def _schedule_unload(self) -> None:
        minutes = float(getattr(self.config, "unload_after_minutes", 0) or 0)
        if minutes <= 0:
            return
        if self._unload_task:
            self._unload_task.cancel()
        self._unload_task = asyncio.create_task(self._unload_after(minutes * 60))

    async def _unload_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not self.active and self._engine:
                await self._engine.close()
                self._engine = None
                self._state = self._new_state("absent")
        except asyncio.CancelledError:
            pass

    def _set_error(self, message: str) -> VoiceState:
        self._state = self._new_state("error", message=message)
        return self._state

    def _new_state(self, state: str, **kwargs: Any) -> VoiceState:
        from datetime import datetime, timezone

        kwargs.setdefault("revision", str(getattr(self.config, "revision", "")))
        kwargs.setdefault("device", str(getattr(self.config, "device", "")))
        kwargs.setdefault("since", datetime.now(timezone.utc))
        return VoiceState(state=state, **kwargs)
