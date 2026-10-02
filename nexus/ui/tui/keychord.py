"""Navigation, Ctrl+X leader keys, and dictation keys for the Textual shell.

Contract: navigation and leader interception see keys before bindings or widgets.
Escape/Ctrl+C return from pushed screens; double Escape cancels active work.
While recording, Esc discards and any other key stops and transcribes (the key is
swallowed). Otherwise Ctrl+X on the main screen arms the leader for a short
window and the next key runs its row of ``LEADER_SHORTCUTS``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from textual.css.query import NoMatches
from textual.timer import Timer

from ...ui_support.tui_command_palette import LEADER_KEY, LEADER_SHORTCUTS

if TYPE_CHECKING:
    from .app import NexusTextualApp

LEADER_TIMEOUT = 3.0
LEADER_HINT = "Ctrl+X — M model · V voice · ? all keys"
_MODIFIER_KEYS = {"shift", "ctrl", "alt", "meta", "super", "hyper", "control"}
_ACTIONS = {letter: action for letter, action, _ in LEADER_SHORTCUTS}


class LeaderKeys:
    def __init__(self, app: NexusTextualApp) -> None:
        self.app = app
        self.armed = False
        self._last_escape = 0.0
        self._escape_session = None
        self._timer: Timer | None = None

    def intercept(self, event) -> bool:
        """Handle ``event`` if it belongs to voice-stop or the leader; True when consumed."""
        key = event.key
        if key in _MODIFIER_KEYS:
            return False
        voice = self.app.voice
        if voice.recording:
            self._consume(event)
            worker = voice.cancel() if key == "escape" else voice.stop(send=key == "enter")
            self.app.run_worker(worker, group="voice", exclusive=True)
            return True
        if self.armed:
            self._consume(event)
            self.disarm()
            if key == "escape":
                return True
            letter = self._letter(event)
            action = _ACTIONS.get(letter)
            if action is None:
                self._hint(f"Ctrl+X {letter} is not bound")
                self._timer = self.app.set_timer(1.5, lambda: self._hint(""))
            else:
                self.app.run_worker(self.app.run_action(action), group="leader", exclusive=True)
            return True
        if key == LEADER_KEY and self.app._is_main_screen():
            self._consume(event)
            self.arm()
            return True
        return False

    async def intercept_navigation(self, event) -> bool:
        """Dismiss pushed screens first; two Escapes within 1.5s stop work."""
        key = event.key
        if key not in {"escape", "ctrl+c"}:
            self._last_escape = 0.0
            return False
        app = self.app
        if len(app.screen_stack) > 1:
            self._consume(event)
            self.disarm()
            self._last_escape = 0.0
            app._last_ctrl_c = 0.0
            while len(app.screen_stack) > 1:
                await app.screen.dismiss(None)
            if app.focused is None:
                app.query_one("#chat-editor").focus()
            return True
        if app._logs_open or app._sessions_overlay:
            self._consume(event)
            self._last_escape = 0.0
            if app._logs_open:
                app.action_close_logs()
            app._close_sessions_overlay()
            return True
        if app._inline_picker_kind is not None:
            self._consume(event)
            self._last_escape = 0.0
            app._close_inline_picker()
            return True
        if app.voice.recording or app.voice.transcribing or self.armed:
            self._last_escape = 0.0
            return False
        if key != "escape" or not app.controller.running:
            self._last_escape = 0.0
            return False
        self._consume(event)
        now = time.monotonic()
        session = app.controller.session
        if self._escape_session == session and self._last_escape and now - self._last_escape <= 1.5:
            self._last_escape = 0.0
            await app.action_cancel_turn()
        else:
            self._last_escape = now
            self._escape_session = session
            app._sync_status("Press Escape again to stop")
        return True

    def arm(self) -> None:
        self.disarm()
        self.armed = True
        self._hint(LEADER_HINT)
        self._timer = self.app.set_timer(LEADER_TIMEOUT, self.disarm)

    def disarm(self) -> None:
        self.armed = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._hint("")

    @staticmethod
    def _letter(event) -> str:
        key = event.key
        if key.startswith("ctrl+") and len(key) == 6:
            return key[-1]
        char = event.character
        if char and char.isprintable():
            return char.lower()
        return key

    @staticmethod
    def _consume(event) -> None:
        event.stop()
        event.prevent_default()

    def _hint(self, text: str) -> None:
        if not self.app.is_mounted:
            return
        try:
            hint = self.app.query_one("#leader-hint")
        except NoMatches:
            return
        hint.update(text)
        hint.display = bool(text)
