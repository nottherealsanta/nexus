"""Pilot coverage for the Ctrl+X leader keys and any-key-stops-dictation."""

from __future__ import annotations

import pytest

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_command_palette import KEYBOARD_SHORTCUTS, LEADER_SHORTCUTS
from nexus.ui_support.tui_model_picker import ModelPickerScreen
from test_tui_voice import VoiceTransport
from test_ui_tui import _client


class FakeRecorder:
    full = False

    def __init__(self, *_args, **_kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return b"wav"


def _app(monkeypatch):
    transport = VoiceTransport()
    transport.voice_state = "ready"
    monkeypatch.setattr("nexus.ui_support.tui_voice.Recorder", FakeRecorder)
    return transport, NexusTextualApp(_client(transport), session="leader")


def test_shortcut_reference_lists_leader_keys():
    text = "\n".join(KEYBOARD_SHORTCUTS)
    assert "Ctrl+X M" in text and "Ctrl+X V" in text
    assert {"m", "v"} <= {letter for letter, _, _ in LEADER_SHORTCUTS}


@pytest.mark.asyncio
async def test_leader_m_opens_model_picker(monkeypatch):
    _, app = _app(monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        editor = app.query_one("#chat-editor")
        await pilot.press("ctrl+x")
        assert app.leader.armed
        await pilot.press("m")
        await pilot.pause()
        assert isinstance(app.screen, ModelPickerScreen)
        assert editor.text == ""


@pytest.mark.asyncio
async def test_leader_v_records_and_any_key_stops(monkeypatch):
    transport, app = _app(monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+x", "v")
        await pilot.pause()
        assert app.voice.recording
        await pilot.press("a")
        await pilot.pause()
        await pilot.pause()
        assert not app.voice.recording
        assert transport.transcribe_calls == 1
        assert "a" not in app.query_one("#chat-editor").text.replace("spoken words", "")


@pytest.mark.asyncio
async def test_escape_discards_recording(monkeypatch):
    transport, app = _app(monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+x", "v")
        await pilot.pause()
        assert app.voice.recording
        await pilot.press("escape")
        await pilot.pause()
        assert not app.voice.recording
        assert transport.transcribe_calls == 0


@pytest.mark.asyncio
async def test_unbound_leader_key_is_swallowed(monkeypatch):
    _, app = _app(monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+x", "z")
        assert not app.leader.armed
        assert app.query_one("#chat-editor").text == ""
        await pilot.press("ctrl+x", "b")
        await pilot.pause()
        assert not app.leader.armed


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_navigation_returns_from_nested_settings(monkeypatch, key):
    from textual.screen import ModalScreen

    _, app = _app(monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.action_open_settings()
        await pilot.pause()
        app.push_screen(ModalScreen())
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause()
        assert app._is_main_screen()
        assert app.focused is app.query_one("#chat-editor")
        assert not app._last_ctrl_c


@pytest.mark.asyncio
async def test_double_escape_cancels_work_but_single_escape_does_not(monkeypatch):
    _, app = _app(monkeypatch)
    calls = []

    async def cancel():
        calls.append(True)
        return p.SessionCancelResult(session="leader", cancelled=True, dropped=2,
                                     returned_messages=["message 1", "message 2"])

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.controller, "cancel", cancel)
        app.controller.running = True
        editor = app.query_one("#chat-editor")
        editor.text = "unsent draft"
        await pilot.press("escape")
        assert not calls
        await pilot.press("escape")
        assert calls == [True]
        assert editor.text == "message 1\n\nmessage 2\n\nunsent draft"
        app.controller.running = False


@pytest.mark.asyncio
async def test_escape_pair_expires_and_other_keys_reset_it(monkeypatch):
    _, app = _app(monkeypatch)
    calls = []

    async def cancel():
        calls.append(True)
        return p.SessionCancelResult(session="leader", cancelled=True)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app.controller, "cancel", cancel)
        app.controller.running = True
        await pilot.press("escape")
        app.leader._last_escape -= 2
        await pilot.press("escape")
        assert not calls
        await pilot.press("a", "escape")
        assert not calls
        await pilot.press("escape")
        assert calls == [True]
        app.controller.running = False
