"""Focused pilot coverage for Textual dictation and voice consent."""

from __future__ import annotations

import tomllib

import pytest

from nexus.host import protocol as p
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_voice import VoiceConsentScreen, _set_toml_voice_value
from test_ui_tui import FakeTransport, _client


class VoiceTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.voice_state = "absent"
        self.prepare_calls = 0
        self.transcribe_calls = 0
        self.cancel_calls = 0

    async def request(self, command):
        if isinstance(command, p.VoiceStatus):
            return p.VoiceStatusResult(state=self.voice_state, enabled=True, auto_send=False)
        if isinstance(command, p.VoicePrepare):
            self.prepare_calls += 1
            self.voice_state = "loading"
            return p.VoiceStatusResult(state=self.voice_state, enabled=True)
        if isinstance(command, p.VoiceTranscribe):
            self.transcribe_calls += 1
            self.voice_state = "ready"
            return p.VoiceTranscribeResult(
                request_id=command.request_id, text="spoken words", duration_s=1, elapsed_s=0.1
            )
        if isinstance(command, p.VoiceCancel):
            self.cancel_calls += 1
            return p.VoiceCancelResult(cancelled=True)
        if isinstance(command, p.SettingsRead):
            return p.SettingsReadResult(body="config_version = 2\n", sha256="voice-sha")
        if isinstance(command, p.SettingsWrite):
            self.settings_body = command.body
            return p.SettingsWriteResult(status="saved", sha256="voice-sha-2")
        return await super().request(command)


def test_voice_config_edit_is_valid_v2_toml_and_quotes_strings():
    body = _set_toml_voice_value("[model]\nname = 'test'\n", "device", 'm"ps\\cpu')
    doc = tomllib.loads(body)
    assert doc["config_version"] == 2
    assert doc["model"]["name"] == "test"
    assert doc["voice"]["device"] == 'm"ps\\cpu'
    assert _set_toml_voice_value(body, "auto_send", True).endswith("auto_send = true\n")


@pytest.mark.asyncio
async def test_first_voice_use_waits_for_confirmation_and_ready_ack(monkeypatch):
    transport = VoiceTransport()
    app = NexusTextualApp(_client(transport), session="voice-consent")

    class FakeRecorder:
        full = False

        def __init__(self, *_args, **_kwargs):
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            return b"wav"

    monkeypatch.setattr("nexus.ui_support.tui_voice.Recorder", FakeRecorder)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await app.voice.start_or_confirm()
        await pilot.pause()
        assert isinstance(app.screen, VoiceConsentScreen)
        assert not app.query_one("#root-agent-recording").display
        assert transport.prepare_calls == 0
        await pilot.click("#voice-confirm")
        await pilot.pause()
        assert transport.prepare_calls == 1
        assert "downloading" not in app.screen.query_one("#voice-status").render().plain.lower()
        assert "loading" not in app.screen.query_one("#voice-status").render().plain.lower()
        transport.voice_state = "ready"
        await pilot.pause(1.1)
        assert "Download a local speech model" in app.screen.query_one("#voice-status").render().plain
        assert "ready" not in app.screen.query_one("#voice-status").render().plain.lower()
        name = app.query_one("#root-agent-name")
        model = app.query_one("#root-model")
        composer = app.query_one("#chat-input")
        bottom = app.query_one("#bottom-info")
        context = app.query_one("#context-usage")
        geometry = lambda: (
            (name.region.x, name.region.y, name.region.width, name.region.height),
            (model.region.x, model.region.y, model.region.width, model.region.height),
            (composer.region.x, composer.region.y, composer.region.width, composer.region.height),
            (bottom.region.x, bottom.region.y, bottom.region.width, bottom.region.height),
            (context.region.x, context.region.y, context.region.width, context.region.height),
        )
        before = geometry()
        await pilot.click("#voice-ready")
        await pilot.pause()
        assert app.voice.recording
        dot = app.query_one("#root-agent-recording")
        assert dot.display
        assert dot.region.x == bottom.region.x
        assert dot.region.y == bottom.region.y == context.region.y
        assert geometry() == before
        assert (dot.styles.color.r, dot.styles.color.g, dot.styles.color.b) == (245, 167, 66)
        await app.voice.stop()
        await pilot.pause()
        assert not app.voice.recording
        assert not dot.display
        assert geometry() == before

        await app.voice.start_or_confirm()
        await pilot.pause()
        assert app.voice.recording
        assert dot.display
        assert dot.region.x == bottom.region.x
        assert dot.region.y == bottom.region.y == context.region.y
        assert geometry() == before
        await app.voice.cancel()
        await pilot.pause()
        assert not app.voice.recording
        assert not dot.display
        assert geometry() == before
        assert transport.cancel_calls == 1
        assert transport.transcribe_calls == 1


@pytest.mark.asyncio
async def test_voice_slash_status_and_draft_insertion(monkeypatch):
    transport = VoiceTransport()
    transport.voice_state = "ready"
    app = NexusTextualApp(_client(transport), session="voice-draft")

    class FakeRecorder:
        full = False

        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            return None

        def stop(self):
            return b"wav"

    monkeypatch.setattr("nexus.ui_support.tui_voice.Recorder", FakeRecorder)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not app.query("#composer-voice")
        assert not app.query("#voice-indicator")
        # Voice status refresh remains safe without a composer mic control.
        transport.voice_state = "downloading"
        await app.voice.refresh_status(force=True)
        assert not app.query_one("#root-agent-recording").display
        assert str(app.query_one("#connection-status").render()).strip() == ""
        transport.voice_state = "ready"
        await app._dispatch_chat_command("/voice status")
        assert "Voice ready" in app.query_one("#connection-status").render().plain
        editor = app.query_one("#chat-editor")
        editor.text = "hello"
        editor.move_cursor((0, 5))
        await app.voice.start_or_confirm()
        await app.voice.stop()
        await pilot.pause()
        assert editor.text == "hello spoken words"
        assert transport.last_input is None
        assert "/voice" in {spec.name for spec in __import__("nexus.ui.cli.commands", fromlist=["SPECS"]).SPECS}
