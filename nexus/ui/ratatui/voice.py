"""Bounded host-backed native dictation (VOICE_PLAN §8.2).

Reuse the neutral Recorder; previews are paced and never inserted as final text.
Stopping keys finish; Escape discards. Closing the shell always releases capture.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

from ...ui_support.voice_capture import Recorder


class Voice:
    def __init__(self, shell):
        self.shell = shell
        self.recorder = None
        self.request = ""
        self.preview_request = ""
        self.preview = ""
        self.phase = "idle"
        self.task = None
        self.level = 0.0
        self.generation = 0
        self.finish_task = None
        self.auto_send = False

    async def open(self, *, download=False, prepare=True):
        status = await self.shell.client.voice_status()
        ready = getattr(status, "state", "") in {"ready", "loaded"} or getattr(status, "loaded", False)
        if (
            prepare and not download and status.enabled and status.cached
            and not ready and status.state not in {"loading", "downloading"}
        ):
            status = await self.shell.client.voice_prepare(allow_download=False)
            ready = getattr(status, "state", "") in {"ready", "loaded"} or getattr(status, "loaded", False)
        if ready and status.enabled and not download:
            await self.start()
            return
        if not status.enabled and not download:
            rows = [("Enable voice", {"kind": "voice_enable"})]
        elif ready:
            rows = [("Start dictation", {"kind": "voice_start"})]
        elif status.state in {"loading", "downloading"}:
            rows = [("Refresh model status", {"kind": "voice"})]
        elif status.cached:
            rows = [("Retry loading voice model", {"kind": "voice_prepare", "allow_download": False})]
        else:
            rows = [("Download voice model…", {"kind": "confirm", "label": "Download the local voice model (~179 MB)?",
                     "next": {"kind": "voice_prepare"}})]
        if status.message:
            rows = [(f"{label} · {status.message}", operation) for label, operation in rows]
        self.shell.workflows.menu("Local dictation", rows, [f"{key}: {getattr(status, key)}" for key in status.__struct_fields__])

    async def start(self):
        if self.recorder:
            return
        status = await self.shell.client.voice_status()
        if not status.enabled:
            raise ValueError("Voice is off · use /voice on")
        if status.state not in {"ready", "loaded"} and not getattr(status, "loaded", False):
            raise ValueError("Voice model is not ready · use /voice download")
        self.auto_send = bool(status.auto_send)
        self.request = uuid.uuid4().hex
        self.generation = self.shell.generation
        self.recorder = Recorder(max_seconds=max(1, min(120, status.max_seconds)), on_level=lambda value: setattr(self, "level", value))
        try:
            await asyncio.to_thread(self.recorder.start)
        except Exception:
            self.recorder = None
            raise
        self.preview = ""
        self.phase = "recording"
        self.shell.panel_title = ""
        self.shell.items = []
        self.task = asyncio.create_task(self._previews())

    async def _previews(self):
        previous = 0.0
        count = 0
        delay = .7
        try:
            while self.recorder:
                await asyncio.sleep(delay)
                recorder = self.recorder
                if not recorder:
                    break
                if recorder.full:
                    await self.stop()
                    break
                if recorder.duration - previous < .4:
                    continue
                previous = recorder.duration
                count += 1
                self.preview_request = f"{self.request}-p{count}"
                start = time.monotonic()
                try:
                    result = await self.shell.client.voice_transcribe(recorder.snapshot(), self.preview_request,
                        session=self.shell.controller.session, partial=True)
                    if self.recorder is recorder:
                        self.preview = getattr(result, "text", "")
                except Exception:
                    pass
                delay = max(.7, 1.5 * (time.monotonic() - start))
        except asyncio.CancelledError:
            raise

    async def stop(self, discard=False, *, send=False):
        recorder, self.recorder = self.recorder, None
        if not recorder:
            return
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.preview_request:
            await self.shell.client.voice_cancel(self.preview_request)
        audio = await asyncio.to_thread(recorder.stop)
        if discard:
            await self.shell.client.voice_cancel(self.request)
        else:
            self.phase = "transcribing"
            try:
                result = await self.shell.client.voice_transcribe(audio, self.request, session=self.shell.controller.session)
                if (self.generation == self.shell.generation and self.phase == "transcribing"
                        and not self.shell.panel_title and result.request_id == self.request):
                    self.shell.composer_insert = getattr(result, "text", "")
                    self.shell.composer_auto_send = send or self.auto_send
                    self.shell.composer_insert_kind = "voice"
            finally:
                self.phase = "idle"
        self.phase = "idle"
        self.preview = ""

    async def discard(self):
        self.phase = "idle"
        if self.finish_task and not self.finish_task.done():
            await self.shell.client.voice_cancel(self.request)
            self.finish_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.finish_task
        if self.recorder:
            await self.stop(discard=True)
