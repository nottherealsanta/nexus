"""Textual dictation controls and consent flow (VOICE_PLAN.md §8.2)."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from ..client.protocol import ClientError
from .text import sanitize
from .voice_capture import Recorder, VoiceCaptureError


class VoiceConsentScreen(ModalScreen[bool]):
    """A persistent first-use confirmation/progress/ready dialog."""

    BINDINGS: ClassVar[list[tuple[str, str, str]]] = [("escape", "cancel", "Cancel")]

    def __init__(self, client: Any, *, waiting: bool = False) -> None:
        super().__init__()
        self.client = client
        self.waiting = waiting
        self._started = waiting
        self._poll_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="voice-consent-dialog"):
            yield Static("Voice model · about 178 MB", id="voice-title", markup=False)
            yield Static("Download a local speech model to enable dictation? It runs on this device.", id="voice-status", markup=False)
            with Horizontal(id="voice-actions"):
                yield Button("Download model", id="voice-confirm", variant="warning")
                yield Button("Cancel", id="voice-cancel")
            yield Button("Retry", id="voice-retry", variant="warning")
            yield Button("Start dictation", id="voice-ready")

    def on_mount(self) -> None:
        self._controls("progress" if self.waiting else "confirm")
        self._poll_task = asyncio.create_task(self._poll_loop())

    def _controls(self, mode: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#voice-actions").display = mode == "confirm"
        self.query_one("#voice-confirm", Button).display = mode == "confirm"
        self.query_one("#voice-retry", Button).display = mode == "retry"
        self.query_one("#voice-ready", Button).display = mode == "ready"

    async def _prepare(self) -> None:
        if self._started:
            return
        self._started = True
        self.waiting = True
        self._controls("progress")
        try:
            status = await self.client.voice_prepare()
            if status.state == "ready":
                self._controls("ready")
                self.waiting = False
        except ClientError as exc:
            self.waiting = False
            self.query_one("#voice-status", Static).update(sanitize(str(exc), 180))
            self._controls("retry")

    async def _poll_loop(self) -> None:
        while self.is_mounted:
            try:
                result = await self.client.voice_status()
            except ClientError as exc:
                if self.waiting:
                    self.query_one("#voice-status", Static).update(sanitize(str(exc), 180))
                    self._controls("retry")
                    self.waiting = False
            else:
                if result.state == "ready":
                    self._controls("ready")
                    self.waiting = False
                elif result.state == "error":
                    self.query_one("#voice-status", Static).update(sanitize(result.message or "Voice model could not be loaded.", 180))
                    self._controls("retry")
                    self.waiting = False
                elif result.state in {"absent", "disabled", "unsupported"} and self.waiting:
                    self.query_one("#voice-status", Static).update(
                        sanitize(result.message, 180) if result.state == "unsupported" and result.message
                        else "Voice model is not available yet. Retry to download it."
                    )
                    self._controls("retry")
                    self.waiting = False
            await asyncio.sleep(1)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "voice-cancel":
            self.dismiss(False)
        elif event.button.id == "voice-ready":
            self.dismiss(True)
        elif event.button.id == "voice-confirm":
            self.run_worker(self._prepare(), group="voice-prepare", exclusive=True)
        elif event.button.id == "voice-retry":
            self._started = False
            self.run_worker(self._prepare(), group="voice-prepare", exclusive=True)

    def action_cancel(self) -> None:
        if not self.waiting and not self._started:
            self.dismiss(False)

    def on_unmount(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None


class VoiceController:
    """Coordinate capture, host inference, and safe insertion into the composer."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.recorder: Recorder | None = None
        self.request_id: str | None = None
        self.session: str | None = None
        self.started = 0.0
        self.max_seconds = 120
        self.auto_send = False
        self._timer = None
        self._transcribe_task: asyncio.Task | None = None
        self._active_request_id: str | None = None
        self._last_status = None
        self._status_poll_deadline = time.monotonic() + 5

    @property
    def recording(self) -> bool:
        return self.recorder is not None

    @property
    def transcribing(self) -> bool:
        return self._transcribe_task is not None

    async def refresh_status(self, *, force: bool = False) -> None:
        if not self.app.is_mounted:
            return
        if (
            not force
            and self._last_status is not None
            and self._last_status.state not in {"downloading", "loading"}
            and time.monotonic() >= self._status_poll_deadline
        ):
            return
        try:
            status = await self.app.controller.client.voice_status()
        except Exception:
            return
        self.max_seconds = max(1, min(120, status.max_seconds))
        self.auto_send = bool(status.auto_send)
        self._last_status = status
        if status.state in {"downloading", "loading"}:
            self._status_poll_deadline = float("inf")
        else:
            self._status_poll_deadline = time.monotonic() + 5

    async def toggle(self) -> None:
        if self.recording:
            await self.stop()
        else:
            await self.start_or_confirm()

    async def start_or_confirm(self) -> None:
        try:
            status = await self.app.controller.client.voice_status()
            self._last_status = status
        except Exception as exc:
            self._show_error(sanitize(str(exc), 120))
            return
        if not status.enabled:
            self._show_error("Voice is off · use /voice on")
            return
        self.max_seconds = max(1, min(120, status.max_seconds))
        self.auto_send = bool(status.auto_send)
        if status.state == "ready":
            self._start_recording()
            return
        waiting = status.state in {"downloading", "loading"}
        screen = VoiceConsentScreen(self.app.controller.client, waiting=waiting)
        self.app.push_screen(screen, callback=self._consent_finished)

    def _consent_finished(self, ready: bool | None) -> None:
        if ready:
            self.app.run_worker(self._start_after_consent(), group="voice-ready", exclusive=True)

    async def _start_after_consent(self) -> None:
        await self.refresh_status(force=True)
        if self._last_status is not None and self._last_status.state == "ready":
            self._start_recording()

    async def command(self, args: tuple[str, ...]) -> None:
        action = args[0].casefold() if args else "toggle"
        if action == "status":
            try:
                status = await self.app.controller.client.voice_status()
            except Exception as exc:
                await self._notice(f"Voice status unavailable · {sanitize(str(exc), 140)}")
                return
            await self._notice(f"Voice {status.state} · {'on' if status.enabled else 'off'} · {status.configured_device}")
        elif action in {"on", "off"}:
            await set_voice_config(self.app.controller.client, enabled=action == "on")
            await self.refresh_status(force=True)
            await self._notice(f"Voice {action}")
            if action == "on":
                await self.start_or_confirm()
            elif self.recording:
                await self.cancel()
        elif action == "download":
            try:
                status = await self.app.controller.client.voice_status()
            except Exception as exc:
                await self._notice(f"Voice status unavailable · {sanitize(str(exc), 140)}")
                return
            if status.state == "ready":
                await self._notice("Voice model is ready")
            else:
                screen = VoiceConsentScreen(self.app.controller.client, waiting=status.state in {"downloading", "loading"})
                self.app.push_screen(screen)
        elif action == "toggle":
            await self.toggle()
        else:
            await self._notice("Use /voice [status|download|on|off]")

    async def _notice(self, text: str) -> None:
        method = getattr(self.app, "_show_notice", None)
        if callable(method):
            await method(text)

    def _start_recording(self) -> None:
        editor = self.app.query_one("#chat-editor")
        editor.focus()
        self.session = self.app.controller.session
        self.request_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.recorder = Recorder(self.max_seconds)
        try:
            self.recorder.start()
        except VoiceCaptureError as exc:
            self.recorder = None
            self.request_id = None
            self._show_error(sanitize(str(exc), 140))
            return
        self._set_recording_dot(True)
        self._timer = self.app.set_interval(0.25, self._tick)

    def _tick(self) -> None:
        if not self.recorder:
            return
        elapsed = time.monotonic() - self.started
        if elapsed >= self.max_seconds or self.recorder.full:
            self.app.run_worker(self.stop(), group="voice-transcribe", exclusive=True)

    async def stop(self) -> None:
        recorder, request_id, session = self.recorder, self.request_id, self.session
        if recorder is None or request_id is None:
            return
        self.recorder = None
        self.request_id = None
        self._set_recording_dot(False)
        if self._timer:
            self._timer.stop()
            self._timer = None
        try:
            wav = recorder.stop()
        except Exception:
            self._show_error("Microphone could not stop")
            return
        editor = self.app.query_one("#chat-editor")
        editor.focus()
        self._active_request_id = request_id
        self._transcribe_task = asyncio.current_task()
        result = None
        try:
            result = await self.app.controller.client.voice_transcribe(wav, request_id, session=session or "")
        except ClientError as exc:
            self._show_error(sanitize(str(exc), 140))
            return
        finally:
            self._transcribe_task = None
            self._active_request_id = None
        if (result is None or self.session != self.app.controller.session or editor is not self.app.query_one("#chat-editor")
                or self.app.focused is not editor or request_id != result.request_id):
            self._show_error("Dictation result discarded because the conversation or focus changed")
            return
        text = sanitize(result.text, 100_000)
        if not text.strip():
            return
        rows = editor.text.splitlines() or [""]
        row, column = editor.cursor_location
        before = rows[row][:column] if row < len(rows) else ""
        after = rows[row][column:] if row < len(rows) else ""
        prefix = " " if before and not before[-1].isspace() else ""
        suffix = " " if after and not after[0].isspace() else ""
        editor.insert(prefix + text + suffix)
        editor.focus()
        if self.auto_send:
            editor.post_message(editor.SubmitRequested(editor.text))
            editor.clear()

    async def cancel(self) -> None:
        request_id, recorder = self.request_id or self._active_request_id, self.recorder
        self.request_id = None
        self.recorder = None
        self._set_recording_dot(False)
        if self._timer:
            self._timer.stop()
            self._timer = None
        if recorder is not None:
            recorder.stop()
        if self._transcribe_task is not None and not self._transcribe_task.done():
            self._transcribe_task.cancel()
        if request_id:
            try:
                await self.app.controller.client.voice_cancel(request_id)
            except ClientError:
                pass

    async def close(self) -> None:
        await self.cancel()

    def _set_recording_dot(self, recording: bool) -> None:
        if not self.app.is_mounted:
            return
        try:
            self.app.query_one("#root-agent-recording", Static).display = recording
        except NoMatches:
            return

    def _show_error(self, text: str) -> None:
        if self.app.is_mounted:
            self.app._sync_status(text, error=True)


async def set_voice_config(client: Any, **updates: object) -> None:
    """Persist voice fields as a valid v2 TOML config via the host settings path."""
    for attempt in range(2):
        current = await client.settings_read("global", "config", "config")
        body = current.body
        if not re.search(r"(?m)^\s*config_version\s*=", body):
            body = "config_version = 2\n" + body.lstrip("\n")
        for key, value in updates.items():
            body = _set_toml_voice_value(body, key, value)
        try:
            await client.settings_write("global", "config", "config", body, current.sha256)
            return
        except ClientError:
            if attempt:
                raise


def _set_toml_voice_value(body: str, key: str, value: object) -> str:
    if key not in {"enabled", "auto_send", "device", "max_seconds"}:
        raise ValueError("unsupported voice setting")
    rendered = str(value).lower() if isinstance(value, bool) else str(value)
    if isinstance(value, str):
        rendered = json.dumps(value, ensure_ascii=True)
    lines = body.splitlines()
    version = next((i for i, line in enumerate(lines) if re.match(r"^\s*config_version\s*=", line)), None)
    if version is None:
        lines.insert(0, "config_version = 2")
    else:
        lines[version] = "config_version = 2"
    start = next((i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[voice\]\s*(?:#.*)?", line)), None)
    if start is None:
        prefix = "\n".join(lines).rstrip()
        return prefix + ("\n\n" if prefix else "") + f"[voice]\n{key} = {rendered}\n"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$")
    existing = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
    if existing is None:
        lines.insert(end, f"{key} = {rendered}")
    else:
        lines[existing] = f"{key} = {rendered}"
    return "\n".join(lines) + "\n"


__all__ = ["VoiceConsentScreen", "VoiceController", "_set_toml_voice_value", "set_voice_config"]
