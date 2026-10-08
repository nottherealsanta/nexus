"""``/speak`` model download in the native client (mirrors ``/voice download``).

A mixin for ``Workflows``: check the host status, ask for consent with the size,
start the download, show progress (Refresh updates it), then speak the latest
answer. The wording and rules come from ``ui_support/speech_download.py``.

When ``/speak`` finds a download already running (the host fetches a missing
model on its own once the user has consented, e.g. after ``nexus update``), the
progress page refreshes itself and speaks when the model is ready, so an
upgrade never asks the user to do anything again.
"""
from __future__ import annotations

import asyncio

from ...ui_support import speech_download as sd


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


_TITLE = "Local speech"


class SpeakPages:
    speak_task = None
    speak_wait_task = None
    speak_poll_seconds = 1.0
    speak_wait_seconds = 600.0

    async def speak_command(self, *, download_only):
        """Entry point for ``/speak`` and ``/speak download``."""
        status = await self.client.speech_status()
        step = sd.next_step(status, download_only=download_only)
        if step == "speak":
            await self.speak_now()
        elif step == "ready":
            self.shell.notice = "Speech model is ready"
        else:
            self.speak_page(status, speak_after=not download_only)
            if step == "progress" and not download_only:
                self._wait_then_speak()

    def _wait_then_speak(self):
        """Poll a running download (bounded) and speak when it is ready, while the page stays open."""
        if self.speak_wait_task and not self.speak_wait_task.done():
            return
        self.speak_wait_task = asyncio.create_task(self._wait_run())

    async def _wait_run(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.speak_wait_seconds
        try:
            while loop.time() < deadline:
                await asyncio.sleep(self.speak_poll_seconds)
                if self.shell.panel_title != _TITLE:
                    return  # the user left the page: they can /speak again later
                status = await self.client.speech_status()
                if _get(status, "state") == "ready":
                    self.shell.panel_title = ""
                    self.stack.clear()
                    await self.speak_now()
                    return
                self.speak_page(status, speak_after=True)
                if self.shell.on_update:
                    await self.shell.on_update()
                if _get(status, "state") != "downloading":
                    return
        except Exception as exc:  # noqa: BLE001 - a failure is a labelled notice
            self.shell.flash(str(exc), "error")

    async def speak_now(self):
        """Start speaking in the background so Esc (``speak_stop``) is still read meanwhile."""
        if self.speak_task and not self.speak_task.done():
            return
        self.speak_task = asyncio.create_task(self._speak_run())

    async def _speak_run(self):
        try:
            await self.client.speak(self.shell.controller.session)
            pass  # speaking shows no notice; only a failure does
        except Exception as exc:  # noqa: BLE001 - a failure is a labelled notice
            self.shell.flash(str(exc), "error")
        if self.shell.on_update:
            await self.shell.on_update()

    async def speak_stop(self):
        """Esc: stop the speech; ``False`` when nothing is playing."""
        if not self.speak_task or self.speak_task.done():
            return False
        await self.client.speak_stop()
        return True

    def speak_page(self, status, *, speak_after):
        state = _get(status, "state", "")
        message = _get(status, "message", "")
        if state == "unsupported":
            self.menu(_TITLE, [("Back", {"kind": "back"})], [message])
        elif state == "downloading":
            self.menu(_TITLE, [("Refresh status", {"kind": "speak_refresh", "speak": speak_after}),
                                       ("Back", {"kind": "back"})], [sd.progress_text(status)])
        elif state == "ready":
            rows = [("Speak latest answer", {"kind": "speak_now"})]
            self.menu(_TITLE, rows, [sd.ready_text()])
        else:
            lines = [sd.CONSENT_PROMPT] + ([message] if state == "error" and message else [])
            self.menu(_TITLE, [
                ("Download speech model…", {"kind": "confirm", "label": f"{sd.CONSENT_TITLE}. Download it now?",
                                            "lines": [sd.CONSENT_PROMPT],
                                            "next": {"kind": "speak_prepare", "speak": speak_after}}),
                ("Back", {"kind": "back"}),
            ], lines)

    async def speak_operate(self, operation):
        """Handle one ``speak_*`` operation; ``False`` when it is not ours."""
        kind = operation["kind"]
        if kind == "speak_prepare":
            self.speak_page(await self.client.speech_prepare(), speak_after=operation.get("speak", True))
        elif kind == "speak_refresh":
            status = await self.client.speech_status()
            if _get(status, "state") == "ready" and operation.get("speak"):
                self.shell.panel_title = ""
                self.stack.clear()
                await self.speak_now()
            else:
                self.speak_page(status, speak_after=operation.get("speak", True))
        elif kind == "speak_now":
            self.shell.panel_title = ""
            self.stack.clear()
            await self.speak_now()
        else:
            return False
        return True
