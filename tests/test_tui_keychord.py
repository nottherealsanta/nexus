"""Pilot coverage for the Ctrl+X leader keys and any-key-stops-dictation."""

from __future__ import annotations

import pytest

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
