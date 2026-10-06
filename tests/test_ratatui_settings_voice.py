"""Settings → Voice & speech: dictation and local speech on one page; downloads always ask first."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui_support import settings_page as sp


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    c = shell.client
    c.voice_status = AsyncMock(return_value=p.VoiceStatusResult(enabled=True, state="ready", cached=True, configured_device="cpu", max_seconds=60))
    c.speech_status = AsyncMock(return_value=p.SpeechStatusResult(state="absent"))
    c.settings_read = AsyncMock(return_value=p.SettingsReadResult(body="config_version = 2\n", rel_path="config.toml", builtin=False, sha256="h"))
    c.settings_write = AsyncMock(return_value=p.SettingsWriteResult(status="saved"))
    c.voice_prepare = AsyncMock()
    c.speech_prepare = AsyncMock(return_value=p.SpeechStatusResult(state="downloading"))
    return shell


def _walk(blocks):
    for block in blocks:
        yield block
        if block.get("t") == "section":
            yield from _walk(block["blocks"])


def _row(page, id_):
    return next(b for b in _walk(page["blocks"]) if b.get("t") == "row" and b["id"] == id_)


def _buttons(page):
    return {item["label"]: item["operation"] for b in _walk(page["blocks"]) if b.get("t") == "buttons" for item in b["items"]}


@pytest.mark.asyncio
async def test_voice_and_speech_are_one_page_and_opening_it_never_records(shell):
    await shell.workflows.settings_area("voice")
    page = shell.workflows.settings_page
    assert shell.panel_title == "Settings · Voice & speech" and shell.settings_nav == "voice"
    assert shell.voice.phase == "idle", "opening the page never starts capture"
    headings = [b["text"] for b in page["blocks"] if b.get("t") == "heading"]
    assert headings == ["VOICE INPUT · local dictation", "SPEECH · /speak, local Kokoro"]
    assert _row(page, "device")["control"]["value"] == "cpu"
    assert _row(page, "max_seconds")["control"]["value"] == "60 seconds"
    assert page["scope"] is None and all(b.get("scope", "") == "" for b in page["blocks"] if b.get("t") == "row")


@pytest.mark.asyncio
async def test_toggling_auto_send_saves_through_the_config_path_and_stays_on_the_page(shell):
    await shell.workflows.settings_area("voice")
    toggle = _row(shell.workflows.settings_page, "auto_send")["control"]
    await shell.workflows.operate({**toggle["operation"], "value": True})
    assert "auto_send = true" in shell.client.settings_write.await_args.args[3]
    assert shell.panel_title == "Settings · Voice & speech" and shell.voice.phase == "idle"


@pytest.mark.asyncio
async def test_device_and_limit_are_selects_with_their_values(shell):
    await shell.workflows.settings_area("voice")
    page = shell.workflows.settings_page
    limit = _row(page, "max_seconds")["control"]
    assert [v for _, v in limit["options"]] == [15, 30, 60, 90, 120]
    await shell.workflows.operate({**limit["operation"], "value": 90})
    assert "max_seconds = 90" in shell.client.settings_write.await_args.args[3]


@pytest.mark.asyncio
async def test_a_cached_voice_model_loads_without_a_download_consent(shell):
    await shell.workflows.settings_area("voice")
    buttons = _buttons(shell.workflows.settings_page)
    assert "Download local model…" not in buttons and "Load model" in buttons
    await shell.workflows.operate(buttons["Load model"])
    shell.client.voice_prepare.assert_awaited_once_with(allow_download=False)


@pytest.mark.asyncio
async def test_downloading_always_asks_first_then_returns_to_the_page(shell):
    shell.client.voice_status = AsyncMock(return_value=p.VoiceStatusResult(enabled=True, state="absent", cached=False))
    await shell.workflows.settings_area("voice")
    ask = _buttons(shell.workflows.settings_page)["Download local model…"]
    await shell.workflows.operate(ask)
    assert shell.panel_title == "Download the local voice model?"
    shell.client.voice_prepare.assert_not_awaited()
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.client.voice_prepare.assert_awaited_once_with(allow_download=True)
    assert shell.panel_title == "Settings · Voice & speech"


@pytest.mark.asyncio
async def test_speech_settings_save_and_reset_and_the_voice_follows_the_language(shell):
    await shell.workflows.settings_area("voice")
    page = shell.workflows.settings_page
    language = _row(page, "language")["control"]
    assert language["value"] == "English (US)"
    await shell.workflows.operate({**language["operation"], "value": "b"})
    body = shell.client.settings_write.await_args.args[3]
    assert 'language = "b"' in body and 'voice = "bf_emma"' in body, "a language change picks a matching voice"
    await shell.workflows.operate(sp.op("voice", "speech_reset"))
    assert shell.toasts[-1]["title"].startswith("Speech settings reset")


@pytest.mark.asyncio
async def test_speech_model_download_asks_with_the_size_and_shows_progress(shell):
    await shell.workflows.settings_area("voice")
    ask = _buttons(shell.workflows.settings_page)["Download speech model…"]
    await shell.workflows.operate(ask)
    assert "about 345 MB" in shell.panel_title
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.client.speech_prepare.assert_awaited_once()
    shell.client.speech_status = AsyncMock(return_value=p.SpeechStatusResult(state="downloading", progress=0.4, bytes_done=138_000_000, bytes_total=345_000_000))
    await shell.workflows.refresh_page()
    page = shell.workflows.settings_page
    bar = next(b for b in page["blocks"] if b.get("t") == "progress")
    assert 0.39 < bar["fraction"] < 0.41 and "138 / 345 MB" in bar["label"]
    assert "Refresh status" in _buttons(page)
