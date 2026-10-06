"""``/speak`` model download in the native terminal client (mirrors ``/voice download``).

Check the host status, ask for consent with the size, start the download, show
progress, then speak the latest answer. Rules and wording are shared
(``ui_support/speech_download.py``).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui_support import speech_download as sd


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
    shell.client.voice_status = AsyncMock(return_value=p.VoiceStatusResult(enabled=True, state="ready", cached=True))
    shell.client.settings_read = AsyncMock(return_value=p.SettingsReadResult(body="config_version = 2\n", rel_path="nexus.toml", builtin=False, sha256="h"))
    await shell.workflows.operate({"kind": "speech_settings"})  # speech now lives on the Voice & speech page
    blocks = list(shell.workflows.settings_page["blocks"])
    buttons = {i["label"]: i["operation"] for b in blocks if b.get("t") == "buttons" for i in b["items"]}
    assert "Download speech model…" in buttons
    model = next(b for b in blocks if b.get("t") == "row" and b["id"] == "speech_model")
    assert model["control"]["value"] == "The speech model is not downloaded yet."
    await shell.workflows.operate(buttons["Download speech model…"])  # asks first, with the size
    shell.client.speech_prepare.assert_not_awaited()
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.client.speech_prepare.assert_awaited_once()
