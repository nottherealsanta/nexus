"""Textual dictation controls and consent flow (VOICE_PLAN.md §8.2).

While recording, the controller sends growing snapshots of the audio as
``partial`` transcriptions (at most one in flight, paced by how long the last
one took) and shows the running transcript in :class:`VoiceStrip`, a floating
row above the composer with a live waveform. The final transcript of the whole
recording is still what gets inserted into the composer.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from collections import deque
from typing import Any, ClassVar

from rich.color import Color, blend_rgb
from rich.text import Text

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
                    self.query_one("#voice-status", Static).update("The local voice model is available. Start dictation when you are ready.")
                    self._controls("ready")
                    self.waiting = False
                elif result.state in {"error", "unsupported"}:
                    self.query_one("#voice-status", Static).update(sanitize(result.message or "Voice model could not be loaded.", 180))
                    self._controls("retry")
                    self.waiting = False
                elif result.state in {"absent", "disabled"} and self.waiting:
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


WAVE_BARS = 96
TRANSCRIPT_ROWS = 3
_BAR_GLYPHS = "▁▂▃▄▅▆▇█"
_FRESH_SECONDS = 0.9
_PREVIEW_MIN_INTERVAL = 0.7
_PREVIEW_MIN_GROWTH = 0.4


def level_height(rms: float) -> float:
    """Map microphone RMS (speech is roughly 0.01–0.2) to a 0–1 bar height."""
    return max(0.0, min(1.0, (max(0.0, rms) * 9) ** 0.6))


def common_prefix(old: str, new: str) -> int:
    """Length of the shared prefix, so only newly heard text is highlighted."""
    limit = min(len(old), len(new))
    index = 0
    while index < limit and old[index] == new[index]:
        index += 1
    return index


def render_voice_strip(
    *,
    phase: str,
    levels: list[float],
    text: str,
    fresh_from: int,
    fresh_age: float,
    elapsed: float,
    frame: int,
    width: int,
    colors: dict[str, str],
) -> Text:
    """One frame of the dictation strip.

    Row one is the status and a full-width waveform (newest sample on the
    right); below it the transcript wraps to at most three rows, its head
    replaced by "…" when clipped. Pure so it can be tested; ``frame`` drives
    the pulse, the idle ripple and the "transcribing" sweep.
    """
    accent = Color.parse(colors.get("accent", "#fab283"))
    quiet = Color.parse(colors.get("quiet", "#6f6f6f"))
    body = Color.parse(colors.get("text", "#eeeeee"))
    muted = colors.get("muted", "#a3a3a3")

    def mix(low: Color, high: Color, amount: float) -> str:
        blended = blend_rgb(low.get_truecolor(), high.get_truecolor(), max(0.0, min(1.0, amount)))
        return Color.from_triplet(blended).name

    width = max(24, width)
    line = Text(overflow="fold")
    recording = phase == "recording"
    if recording:
        line.append("● ", style=f"bold {mix(quiet, accent, 0.55 + 0.45 * math.sin(frame * 0.35))}")
        minutes, seconds = divmod(int(elapsed), 60)
        line.append(f"{minutes}:{seconds:02d}  ", style=muted)
    else:
        line.append(f"{'◐◓◑◒'[frame // 2 % 4]} ", style=f"bold {accent.name}")
        line.append("transcribing  ", style=muted)
    bars = max(8, min(WAVE_BARS, width - line.cell_len))
    samples = list(levels[-bars:])
    samples = [0.0] * (bars - len(samples)) + samples
    for index, value in enumerate(samples):
        # A slow travelling ripple keeps the wave alive through silence.
        ripple = 0.07 + 0.05 * math.sin(frame * 0.4 - index * 0.35)
        height = max(ripple, value) if recording else ripple * (1 + math.sin(frame * 0.5 + index * 0.3))
        glyph = _BAR_GLYPHS[min(len(_BAR_GLYPHS) - 1, int(height * len(_BAR_GLYPHS)))]
        line.append(glyph, style=mix(quiet, accent, 0.25 + height))
    line.append("\n")
    words = " ".join(text.split())
    if not words:
        hint = "Listening… any key stops · Esc discards" if recording else "Finishing the transcript…"
        line.append(hint[:width], style=f"italic {muted}")
        return line
    room = width * TRANSCRIPT_ROWS - 2
    start = 0
    if len(words) > room:
        start = len(words) - (room - 1)
        line.append("…", style=muted)
    fresh = max(0.0, 1 - fresh_age / _FRESH_SECONDS)
    sweep = (frame * 2) % max(1, len(words) - start + 12) + start - 6
    for index in range(start, len(words)):
        if not recording and abs(index - sweep) <= 3:
            style = f"bold {mix(body, accent, 1 - abs(index - sweep) / 4)}"
        elif index >= fresh_from and fresh > 0:
            style = f"bold {mix(body, accent, fresh)}"
        else:
            style = body.name
        line.append(words[index], style=style)
    if recording and frame // 4 % 2 == 0:
        line.append("▍", style=accent.name)
    return line


class VoiceStrip(Static):
    """Floating live-dictation row above the composer; it never shifts layout."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.display = False

    def place(self) -> None:
        """Sit just above the composer inside ``#main-column``."""
        try:
            composer = self.app.query_one("#chat-input")
            column = self.app.query_one("#main-column")
        except NoMatches:
            return
        height = self.outer_size.height or 3
        self.styles.offset = (0, max(0, composer.region.y - column.region.y - height))


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
        # Live preview state (reset per recording).
        self._levels: deque[float] = deque(maxlen=WAVE_BARS)
        self._peak = 0.0
        self._frame = 0
        self._phase = "recording"
        self._preview_text = ""
        self._fresh_from = 0
        self._fresh_at = 0.0
        self._preview_task: asyncio.Task | None = None
        self._preview_count = 0
        self._preview_request_id: str | None = None
        self._preview_audio = 0.0
        self._next_preview = 0.0

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
        self._levels.clear()
        self._peak = 0.0
        self._preview_text = ""
        self._fresh_from = 0
        self._preview_count = 0
        self._preview_audio = 0.0
        self._next_preview = self.started + _PREVIEW_MIN_INTERVAL
        self.recorder = Recorder(self.max_seconds, on_level=self._on_level)
        try:
            self.recorder.start()
        except VoiceCaptureError as exc:
            self.recorder = None
            self.request_id = None
            self._show_error(sanitize(str(exc), 140))
            return
        self._set_recording_dot(True)
        self._show_strip("recording")
        self._timer = self.app.set_interval(1 / 12, self._tick)

    def _on_level(self, level: float) -> None:
        # Called from the audio thread; the animation samples the peak per frame.
        self._peak = max(self._peak, level)

    def _tick(self) -> None:
        if not self.recorder:
            self._paint_strip()
            return
        peak, self._peak = self._peak, 0.0
        self._levels.append(level_height(peak))
        self._paint_strip()
        elapsed = time.monotonic() - self.started
        if elapsed >= self.max_seconds or self.recorder.full:
            self.app.run_worker(self.stop(), group="voice-transcribe", exclusive=True)
            return
        self._maybe_preview()

    def _maybe_preview(self) -> None:
        recorder, request_id = self.recorder, self.request_id
        if recorder is None or request_id is None or self._preview_task is not None:
            return
        duration = getattr(recorder, "duration", 0.0)
        if time.monotonic() < self._next_preview or duration - self._preview_audio < _PREVIEW_MIN_GROWTH or duration < 0.5:
            return
        self._preview_count += 1
        self._preview_audio = duration
        preview_id = f"{request_id}-p{self._preview_count}"
        self._preview_request_id = preview_id
        self._preview_task = asyncio.create_task(self._preview(recorder.snapshot(), request_id, preview_id))

    async def _preview(self, wav: bytes, request_id: str, preview_id: str) -> None:
        started = time.monotonic()
        try:
            result = await self.app.controller.client.voice_transcribe(
                wav, preview_id, session=self.session or "", partial=True
            )
        except (ClientError, asyncio.CancelledError):
            # Busy or failed previews are skipped; the final transcript reports errors.
            return
        finally:
            took = time.monotonic() - started
            self._next_preview = time.monotonic() + max(_PREVIEW_MIN_INTERVAL, took * 1.5)
            if self._preview_request_id == preview_id:
                self._preview_task = None
                self._preview_request_id = None
        if self.request_id != request_id or result.request_id != preview_id:
            return
        self._set_preview(sanitize(result.text, 100_000))

    def _set_preview(self, text: str) -> None:
        text = " ".join(text.split())
        if text == self._preview_text:
            return
        self._fresh_from = common_prefix(self._preview_text, text)
        self._fresh_at = time.monotonic()
        self._preview_text = text

    async def _stop_preview(self) -> None:
        task, preview_id = self._preview_task, self._preview_request_id
        self._preview_task = None
        self._preview_request_id = None
        if task is not None and not task.done():
            task.cancel()
            if preview_id:
                try:
                    await self.app.controller.client.voice_cancel(preview_id)
                except ClientError:
                    pass

    def _show_strip(self, phase: str) -> None:
        self._phase = phase
        strip = self._strip()
        if strip is None:
            return
        strip.display = True
        self._paint_strip()
        strip.call_after_refresh(strip.place)

    def _hide_strip(self) -> None:
        strip = self._strip()
        if strip is not None:
            strip.display = False
            strip.update("")

    def _strip(self) -> VoiceStrip | None:
        if not self.app.is_mounted:
            return None
        try:
            return self.app.query_one("#voice-strip", VoiceStrip)
        except NoMatches:
            return None

    def _paint_strip(self) -> None:
        strip = self._strip()
        if strip is None or not strip.display:
            return
        self._frame += 1
        variables = self.app.theme_variables
        colors = {name: str(variables.get(f"nx-{name}", "")) or default for name, default in (
            ("accent", "#fab283"), ("quiet", "#6f6f6f"), ("text", "#eeeeee"), ("muted", "#a3a3a3"))}
        width = strip.content_region.width or strip.size.width or 80
        strip.update(render_voice_strip(
            phase=self._phase, levels=list(self._levels), text=self._preview_text,
            fresh_from=self._fresh_from, fresh_age=time.monotonic() - self._fresh_at,
            elapsed=time.monotonic() - self.started, frame=self._frame, width=width, colors=colors,
        ))
        strip.place()

    async def stop(self) -> None:
        recorder, request_id, session = self.recorder, self.request_id, self.session
        if recorder is None or request_id is None:
            return
        self.recorder = None
        self.request_id = None
        self._set_recording_dot(False)
        try:
            wav = recorder.stop()
        except Exception:
            self._end_animation()
            self._show_error("Microphone could not stop")
            return
        await self._stop_preview()
        self._show_strip("transcribing")
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
            self._end_animation()
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
        self._end_animation()
        await self._stop_preview()
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

    def _end_animation(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
        self._hide_strip()

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


__all__ = ["VoiceConsentScreen", "VoiceController", "VoiceStrip", "common_prefix", "level_height", "render_voice_strip", "_set_toml_voice_value", "set_voice_config"]
