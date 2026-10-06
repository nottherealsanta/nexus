"""``/speak`` model download in the native client (mirrors ``/voice download``).

A mixin for ``Workflows``: check the host status, ask for consent with the size,
start the download, show progress (Refresh updates it), then speak the latest
answer. The wording and rules come from ``ui_support/speech_download.py``.
"""
from __future__ import annotations

import asyncio

from ...ui_support import speech_download as sd


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


class SpeakPages:
    speak_task = None

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
            self.menu("Local speech", [("Back", {"kind": "back"})], [message])
        elif state == "downloading":
            self.menu("Local speech", [("Refresh status", {"kind": "speak_refresh", "speak": speak_after}),
                                       ("Back", {"kind": "back"})], [sd.progress_text(status)])
        elif state == "ready":
            rows = [("Speak latest answer", {"kind": "speak_now"})]
            self.menu("Local speech", rows, [sd.ready_text()])
        else:
            lines = [sd.CONSENT_PROMPT] + ([message] if state == "error" and message else [])
            self.menu("Local speech", [
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
        elif kind == "speak_settings_prepare":
            await self.client.speech_prepare()
            await self.speech_settings()
        else:
            return False
        return True
