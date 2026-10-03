"""``/speak`` model download in the Textual shell (mirrors ``tui_voice.VoiceConsentScreen``).

``SpeechConsentScreen`` asks for consent with the size, starts the download
(``SpeechPrepare``), polls ``SpeechStatus`` for progress and ends ready, failed
or unsupported (packages missing: it says how to install them). ``speak_command``
is the ``/speak [download]`` entry point. Rules and wording are shared with the
native client in ``speech_download.py``.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, ProgressBar, Static

from ..client.protocol import ClientError
from . import speech_download as sd
from .text import sanitize

_POLL_SECONDS = 1.0


class SpeechConsentScreen(ModalScreen[bool]):
    """Confirm, download with progress, then report ready (``True``) or leave (``False``)."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def __init__(self, client: Any, *, status: Any = None, speak_after: bool = True) -> None:
        super().__init__()
        self.client = client
        self._status = status
        self._speak_after = speak_after
        self.waiting = str(sd._get(status, "state", "")) == "downloading"
        self._poll_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="speech-consent-dialog"):
            yield Static(sd.CONSENT_TITLE, id="speech-title", markup=False)
            yield Static(sd.CONSENT_PROMPT, id="speech-status", markup=False)
            yield ProgressBar(total=100, show_eta=False, id="speech-progress")
            with Horizontal(id="speech-actions"):
                yield Button("Download model", id="speech-confirm", variant="warning")
                yield Button("Cancel", id="speech-cancel")
            yield Button("Retry", id="speech-retry", variant="warning")
            yield Button("Speak latest answer" if self._speak_after else "Done", id="speech-ready")
            yield Button("Close", id="speech-close")

    def on_mount(self) -> None:
        state = str(sd._get(self._status, "state", "absent"))
        if state == "unsupported":
            self._show("Speech is not installed", sanitize(str(sd._get(self._status, "message", "")), 400), "close")
        elif state == "ready":
            self._show(sd.CONSENT_TITLE, sd.ready_text(), "ready")
        elif state == "error":
            self._show(sd.CONSENT_TITLE, sanitize(str(sd._get(self._status, "message", "")), 240), "retry")
        elif self.waiting:
            self._show(sd.CONSENT_TITLE, sd.progress_text(self._status), "progress")
        else:
            self._show(sd.CONSENT_TITLE, sd.CONSENT_PROMPT, "confirm")
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _show(self, title: str, text: str, mode: str) -> None:
        self.query_one("#speech-title", Static).update(title)
        self.query_one("#speech-status", Static).update(text)
        self.query_one("#speech-actions").display = mode == "confirm"
        self.query_one("#speech-confirm", Button).display = mode == "confirm"
        self.query_one("#speech-retry", Button).display = mode == "retry"
        self.query_one("#speech-ready", Button).display = mode == "ready"
        self.query_one("#speech-close", Button).display = mode == "close"
        self.query_one("#speech-progress", ProgressBar).display = mode == "progress"

    def _apply(self, status: Any) -> None:
        state = str(sd._get(status, "state", ""))
        if state == "ready":
            self.waiting = False
            self._show(sd.CONSENT_TITLE, sd.ready_text(), "ready")
        elif state == "downloading":
            self.waiting = True
            self.query_one("#speech-progress", ProgressBar).update(progress=round(float(sd._get(status, "progress", 0.0)) * 100))
            self._show(sd.CONSENT_TITLE, sd.progress_text(status), "progress")
        elif state in {"error", "unsupported"}:
            self.waiting = False
            self._show(sd.CONSENT_TITLE, sanitize(str(sd._get(status, "message", "")) or "The speech model could not be downloaded.", 240),
                       "retry" if state == "error" else "close")
        elif self.waiting:  # a download stopped without finishing
            self.waiting = False
            self._show(sd.CONSENT_TITLE, "The speech model is not available yet. Retry to download it.", "retry")

    async def _prepare(self) -> None:
        self.waiting = True
        self._show(sd.CONSENT_TITLE, sd.progress_text({"progress": 0.0}), "progress")
        try:
            self._apply(await self.client.speech_prepare())
        except ClientError as exc:
            self.waiting = False
            self._show(sd.CONSENT_TITLE, sanitize(str(exc), 180), "retry")

    async def _poll_loop(self) -> None:
        while self.is_mounted:
            if self.waiting:
                try:
                    self._apply(await self.client.speech_status())
                except ClientError as exc:
                    self.waiting = False
                    self._show(sd.CONSENT_TITLE, sanitize(str(exc), 180), "retry")
            await asyncio.sleep(_POLL_SECONDS)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button.id
        if button in {"speech-cancel", "speech-close"}:
            self.dismiss(False)
        elif button == "speech-ready":
            self.dismiss(True)
        elif button in {"speech-confirm", "speech-retry"}:
            self.run_worker(self._prepare(), group="speech-prepare", exclusive=True)

    def action_cancel(self) -> None:
        if not self.waiting:
            self.dismiss(False)

    def on_unmount(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None


async def speak_command(app: Any, args: tuple[str, ...]) -> None:
    """``/speak`` and ``/speak download``: speak now, or run the consent flow first."""
    client = app.controller.client
    download_only = args == ("download",)
    status = await client.speech_status()
    step = sd.next_step(status, download_only=download_only)
    if step == "speak":
        start_speaking(app)
    elif step == "ready":
        await app._show_notice("Speech model is ready")
    else:
        screen = SpeechConsentScreen(client, status=status, speak_after=not download_only)
        app.push_screen(screen, callback=lambda ready: _after_consent(app, ready, download_only))


def _after_consent(app: Any, ready: bool | None, download_only: bool) -> None:
    if ready and not download_only:
        start_speaking(app)


def start_speaking(app: Any) -> None:
    """Speak in a worker so the UI (and Esc) stay live while the answer plays."""
    if not getattr(app, "speaking", False):
        app.run_worker(_speak(app), group="speak", exclusive=True)


async def stop_speaking(app: Any) -> None:
    """Esc: stop the speech that is playing."""
    with contextlib.suppress(ClientError):
        await app.controller.client.speak_stop()


async def _speak(app: Any) -> None:
    app.speaking = True
    try:
        await app.controller.client.speak(app.controller.session)
    except ClientError as exc:
        await app._show_notice(sanitize(str(exc), 200))
        return
    finally:
        app.speaking = False
    # No notice while speaking or after; only a failure is shown.
