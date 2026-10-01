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
        assert app.screen.query_one("#voice-confirm").display
        assert not app.screen.query_one("#voice-retry").display
        assert not app.screen.query_one("#voice-ready").display
        assert transport.prepare_calls == 0
        await pilot.click("#voice-confirm")
        await pilot.pause()
        assert transport.prepare_calls == 1
        assert "downloading" not in app.screen.query_one("#voice-status").render().plain.lower()
        assert "loading" not in app.screen.query_one("#voice-status").render().plain.lower()
        transport.voice_state = "ready"
        await pilot.pause(1.1)
        assert "local voice model is available" in app.screen.query_one("#voice-status").render().plain
        assert not app.screen.query_one("#voice-confirm").display
        assert not app.screen.query_one("#voice-retry").display
        assert app.screen.query_one("#voice-ready").display
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


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["error", "unsupported"])
async def test_voice_dialog_shows_failure_without_download_prompt(state):
    transport = VoiceTransport()
    transport.voice_state = state
    app = NexusTextualApp(_client(transport), session="voice-failure")
    async with app.run_test(size=(100, 30)) as pilot:
        await app.voice.start_or_confirm()
        await pilot.pause()
        assert isinstance(app.screen, VoiceConsentScreen)
        assert "could not be loaded" in app.screen.query_one("#voice-status").render().plain
        assert not app.screen.query_one("#voice-actions").display
        assert app.screen.query_one("#voice-retry").display
        assert not app.screen.query_one("#voice-ready").display
        assert transport.prepare_calls == 0


class LiveVoiceTransport(VoiceTransport):
    def __init__(self) -> None:
        super().__init__()
        self.voice_state = "ready"
        self.partials: list[str] = []

    async def request(self, command):
        if isinstance(command, p.VoiceTranscribe) and command.partial:
            self.partials.append(command.request_id)
            return p.VoiceTranscribeResult(
                request_id=command.request_id, text="hello there", duration_s=1, elapsed_s=0.05
            )
        return await super().request(command)


@pytest.mark.asyncio
async def test_live_dictation_streams_previews_into_a_floating_strip(monkeypatch):
    from nexus.ui_support.tui_voice import VoiceStrip

    transport = LiveVoiceTransport()
    app = NexusTextualApp(_client(transport), session="voice-live")

    class GrowingRecorder:
        full = False

        def __init__(self, *_args, on_level=None, **_kwargs):
            self.on_level = on_level
            self.duration = 0.0

        def start(self):
            pass

        def snapshot(self):
            return b"partial-wav"

        def stop(self):
            return b"wav"

    monkeypatch.setattr("nexus.ui_support.tui_voice.Recorder", GrowingRecorder)
    monkeypatch.setattr("nexus.ui_support.tui_voice._PREVIEW_MIN_INTERVAL", 0.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one("#chat-input")
        before = composer.region
        await app.voice.start_or_confirm()
        await pilot.pause()
        strip = app.query_one("#voice-strip", VoiceStrip)
        assert strip.display and app.query_one("#chat-input").region == before
        app.voice.recorder.on_level(0.2)
        app.voice.recorder.duration = 1.2
        await pilot.pause(0.3)
        assert transport.partials and all(rid.startswith(app.voice.request_id) for rid in transport.partials)
        rendered = strip.render().plain
        assert "hello there" in rendered and "0:0" in rendered
        assert strip.region.y + strip.region.height <= composer.region.y
        await app.voice.stop()
        await pilot.pause()
        assert not strip.display
        assert "spoken words" in app.query_one("#chat-editor").text


def test_voice_strip_frame_highlights_only_new_words_and_announces_clipping():
    from nexus.ui_support.tui_voice import common_prefix, render_voice_strip

    assert common_prefix("hello wor", "hello world") == 9
    frame = render_voice_strip(
        phase="recording", levels=[0.1, 0.9], text="one two three " * 20, fresh_from=270,
        fresh_age=0.0, elapsed=65, frame=1, width=60, colors={},
    )
    status, transcript = frame.plain.split("\n", 1)
    assert status.startswith("● 1:05 ") and len(status) <= 60
    assert transcript.startswith("…") and len(transcript) <= 60 * 3
    idle = render_voice_strip(
        phase="transcribing", levels=[], text="", fresh_from=0, fresh_age=9, elapsed=0, frame=0, width=60, colors={},
    )
    assert "transcribing" in idle.plain
