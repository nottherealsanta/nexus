"""``/speak`` model download in both terminal clients (mirrors ``/voice download``).

Check the host status, ask for consent with the size, start the download, show
progress, then speak the latest answer. Rules and wording are shared
(``ui_support/speech_download.py``).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ui_tui import FakeTransport, _client
from textual.widgets import Button, ProgressBar, Static

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support import speech_download as sd
from nexus.ui_support import tui_speech
from nexus.ui_support.tui_speech import SpeechConsentScreen


def _status(state, **values):
    return p.SpeechStatusResult(state=state, bytes_total=345_000_000, **values)


# -- shared rules -------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "download_only", "step"),
    [
        ("ready", False, "speak"), ("ready", True, "ready"), ("absent", False, "consent"),
        ("absent", True, "consent"), ("error", False, "consent"), ("downloading", False, "progress"),
        ("unsupported", False, "unsupported"), ("unsupported", True, "unsupported"),
    ],
)
def test_next_step(state, download_only, step):
    assert sd.next_step(_status(state), download_only=download_only) == step


def test_progress_text_shows_percent_and_sizes():
    text = sd.progress_text(_status("downloading", progress=0.42, bytes_done=145_000_000))
    assert text == "Downloading the local speech model… 42% · 145 / 345 MB"
    assert sd.progress_text({"progress": 0.0}) == "Downloading the local speech model… 0%"
    assert "345 MB" in sd.CONSENT_TITLE and "never sent to a speech service" in sd.CONSENT_PROMPT


# -- native client -------------------------------------------------------------


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))


def _labels(shell):
    return [item["label"] for item in shell.items]


@pytest.mark.asyncio
async def test_native_speak_with_a_ready_model_speaks_at_once(shell):
    shell.client.speech_status = AsyncMock(return_value=_status("ready"))
    shell.client.speak = AsyncMock(return_value=p.SpeakResult(message="Finished speaking the latest answer", backend="kokoro-cpu"))
    await shell.workflows.speak_command(download_only=False)
    assert shell.notice == ""  # speaking shows no notice
    await shell.workflows.speak_task
    shell.client.speak.assert_awaited_once_with("s")
    assert shell.notice == ""  # removed once speaking ends


@pytest.mark.asyncio
async def test_native_esc_stops_speaking_and_clears_the_notice(shell):
    import asyncio
    release = asyncio.Event()

    async def slow_speak(session):
        await release.wait()
        return p.SpeakResult(message="Stopped speaking", backend="")

    shell.client.speech_status = AsyncMock(return_value=_status("ready"))
    shell.client.speak = slow_speak
    shell.client.speak_stop = AsyncMock(side_effect=lambda: release.set())
    assert await shell.workflows.speak_stop() is False  # nothing playing yet
    await shell.workflows.speak_command(download_only=False)
    assert await shell.workflows.speak_stop() is True
    await shell.workflows.speak_task
    shell.client.speak_stop.assert_awaited_once()
    assert shell.notice == ""


@pytest.mark.asyncio
async def test_native_download_with_a_ready_model_only_says_so(shell):
    shell.client.speech_status = AsyncMock(return_value=_status("ready"))
    shell.client.speak = AsyncMock()
    await shell.workflows.speak_command(download_only=True)
    assert shell.notice == "Speech model is ready"
    shell.client.speak.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_missing_model_asks_for_consent_then_downloads_and_speaks(shell):
    shell.client.speech_status = AsyncMock(side_effect=[_status("absent"), _status("downloading", progress=0.5, bytes_done=172_000_000), _status("ready")])
    shell.client.speech_prepare = AsyncMock(return_value=_status("downloading", progress=0.0))
    shell.client.speak = AsyncMock(return_value=p.SpeakResult(message="Finished speaking the latest answer", backend="kokoro-cpu"))
    await shell.workflows.speak_command(download_only=False)
    assert shell.panel_title == "Local speech"
    assert _labels(shell) == ["Download speech model…", "Back"]
    assert "never sent to a speech service" in " ".join(shell.panel_lines)
    await shell.workflows.operate(shell.items[0]["operation"])  # the confirm page
    assert "345 MB" in shell.panel_title
    await shell.workflows.operate(shell.items[1]["operation"])  # Continue
    shell.client.speech_prepare.assert_awaited_once()
    assert _labels(shell) == ["Refresh status", "Back"]
    await shell.workflows.operate(shell.items[0]["operation"])  # still downloading
    assert "50%" in " ".join(shell.panel_lines)
    await shell.workflows.operate(shell.items[0]["operation"])  # now ready: speaks
    await shell.workflows.speak_task
    shell.client.speak.assert_awaited_once_with("s")
    assert shell.notice == "" and shell.panel_title == ""


@pytest.mark.asyncio
async def test_native_missing_packages_explain_how_to_install(shell):
    message = "Missing kokoro. Install the speak extra in the daemon's environment (uv sync --extra speak)."
    shell.client.speech_status = AsyncMock(return_value=_status("unsupported", message=message))
    await shell.workflows.speak_command(download_only=False)
    assert _labels(shell) == ["Back"] and "uv sync --extra speak" in " ".join(shell.panel_lines)


@pytest.mark.asyncio
async def test_native_failed_download_shows_the_error_and_offers_a_retry(shell):
    shell.client.speech_status = AsyncMock(return_value=_status("error", message="The speech model could not be downloaded."))
    await shell.workflows.speak_command(download_only=True)
    assert "Download speech model…" in _labels(shell)
    assert "could not be downloaded" in " ".join(shell.panel_lines)


@pytest.mark.asyncio
async def test_native_speech_settings_show_the_model_and_a_download_row(shell):
    shell.client.speech_status = AsyncMock(return_value=_status("absent", message="The speech model is not downloaded yet."))
    shell.client.speech_prepare = AsyncMock(return_value=_status("downloading"))
    shell.client.settings_read = AsyncMock(return_value=p.SettingsReadResult(body="config_version = 2\n", rel_path="nexus.toml", builtin=False, sha256="h"))
    await shell.workflows.operate({"kind": "speech_settings"})
    assert "Download speech model…" in _labels(shell)
    assert any(line.startswith("Model: The speech model is not downloaded yet") for line in shell.panel_lines)
    await shell.workflows.operate({"kind": "speak_settings_prepare"})
    shell.client.speech_prepare.assert_awaited_once()


# -- Textual -------------------------------------------------------------------


class SpeechTransport(FakeTransport):
    def __init__(self, *states) -> None:
        super().__init__()
        self.states = list(states)
        self.calls: list[str] = []

    def _next(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def request(self, command):
        if isinstance(command, p.SpeechStatus):
            self.calls.append("status")
            return self._next()
        if isinstance(command, p.SpeechPrepare):
            self.calls.append("prepare")
            return _status("downloading", progress=0.0)
        if isinstance(command, p.Speak):
            self.calls.append("speak")
            return p.SpeakResult(message="Finished speaking the latest answer", backend="kokoro-cpu")
        return await super().request(command)


async def _settle(pilot, times=8):
    for _ in range(times):
        await pilot.pause()


def _text(widget):
    return widget.render().plain


def _button(root, id_):
    return root.query_one(f"#{id_}", Button)


@pytest.mark.asyncio
async def test_textual_speak_asks_consent_downloads_with_progress_then_speaks(monkeypatch):
    monkeypatch.setattr(tui_speech, "_POLL_SECONDS", 0.01)
    transport = SpeechTransport(_status("absent"), _status("downloading", progress=0.5, bytes_done=172_000_000), _status("ready"))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await tui_speech.speak_command(app, ())
        await _settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, SpeechConsentScreen)
        assert _text(dialog.query_one("#speech-title", Static)) == sd.CONSENT_TITLE
        assert _text(dialog.query_one("#speech-status", Static)) == sd.CONSENT_PROMPT
        assert _button(dialog, "speech-confirm").display and not _button(dialog, "speech-ready").display
        assert "prepare" not in transport.calls  # consent comes first
        _button(dialog, "speech-confirm").press()
        await _settle(pilot, 20)
        assert "prepare" in transport.calls
        assert _button(dialog, "speech-ready").display and not _button(dialog, "speech-confirm").display
        assert _text(dialog.query_one("#speech-status", Static)) == sd.ready_text()
        _button(dialog, "speech-ready").press()
        await _settle(pilot, 20)
        assert transport.calls[-1] == "speak"


@pytest.mark.asyncio
async def test_textual_progress_bar_follows_the_host(monkeypatch):
    monkeypatch.setattr(tui_speech, "_POLL_SECONDS", 0.01)
    transport = SpeechTransport(_status("downloading", progress=0.5, bytes_done=172_000_000))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await tui_speech.speak_command(app, ())
        await _settle(pilot, 12)
        dialog = app.screen
        assert dialog.query_one("#speech-progress", ProgressBar).display
        assert dialog.query_one("#speech-progress", ProgressBar).progress == 50
        assert "50% · 172 / 345 MB" in _text(dialog.query_one("#speech-status", Static))
        dialog.action_cancel()  # not while a download runs
        await _settle(pilot)
        assert isinstance(app.screen, SpeechConsentScreen)


@pytest.mark.asyncio
async def test_textual_ready_model_speaks_without_a_dialog():
    transport = SpeechTransport(_status("ready"))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await tui_speech.speak_command(app, ())
        await _settle(pilot)
        assert transport.calls == ["status", "speak"] and not isinstance(app.screen, SpeechConsentScreen)
        transport.calls.clear()
        await tui_speech.speak_command(app, ("download",))
        await _settle(pilot)
        assert transport.calls == ["status"]  # already present: just says so


@pytest.mark.asyncio
async def test_textual_unsupported_shows_how_to_install_with_only_a_close_button():
    message = "Missing kokoro. Install the speak extra in the daemon's environment (uv sync --extra speak)."
    transport = SpeechTransport(_status("unsupported", message=message))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await tui_speech.speak_command(app, ())
        await _settle(pilot)
        dialog = app.screen
        assert "uv sync --extra speak" in _text(dialog.query_one("#speech-status", Static))
        assert _button(dialog, "speech-close").display and not _button(dialog, "speech-confirm").display
        _button(dialog, "speech-close").press()
        await _settle(pilot)
        assert not isinstance(app.screen, SpeechConsentScreen)


@pytest.mark.asyncio
async def test_textual_failed_download_offers_retry(monkeypatch):
    monkeypatch.setattr(tui_speech, "_POLL_SECONDS", 0.01)
    transport = SpeechTransport(_status("error", message="The speech model could not be downloaded. Check the network and retry."))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await tui_speech.speak_command(app, ("download",))
        await _settle(pilot)
        dialog = app.screen
        assert "could not be downloaded" in _text(dialog.query_one("#speech-status", Static))
        assert _button(dialog, "speech-retry").display
        assert _button(dialog, "speech-ready").display is False


class SettingsSpeechTransport(SpeechTransport):
    async def request(self, command):
        if isinstance(command, p.SettingsRead):
            return p.SettingsReadResult(body="config_version = 2\n", rel_path="nexus.toml", builtin=False, sha256="h")
        if isinstance(command, p.SettingsInventory):
            return p.SettingsInventoryResult(scope=command.scope, root_display="~/.nexus", categories=[], items=[])
        return await super().request(command)


@pytest.mark.asyncio
async def test_textual_speech_settings_show_the_model_and_open_the_download_dialog(monkeypatch):
    monkeypatch.setattr(tui_speech, "_POLL_SECONDS", 0.01)
    transport = SettingsSpeechTransport(_status("absent", message="The speech model is not downloaded yet."))
    app = NexusTextualApp(_client(transport), session="s")
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        app.action_open_settings("speech")
        await _settle(pilot, 12)
        screen = app.screen
        assert _text(screen.query_one("#settings-speech-model", Static)) == "Model · The speech model is not downloaded yet."
        button = screen.query_one("#settings-speech-download", Button)
        assert button.display and str(button.label) == "Download model"
        button.press()
        await _settle(pilot, 12)
        assert isinstance(app.screen, SpeechConsentScreen)
        assert _button(app.screen, "speech-ready").label.plain == "Done"  # from Settings nothing is spoken
        _button(app.screen, "speech-cancel").press()
        await _settle(pilot, 12)
        assert app.screen is screen
