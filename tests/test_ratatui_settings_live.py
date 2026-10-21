"""Settings → GPT Live: the [live] options as one page, saved through the config settings path."""
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.config.schema import LiveSection
from nexus.host import protocol as p
from nexus.live.protocol import DEFAULT_VOICE, MODEL, VOICES
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.settings_pages import PAGE_AREAS
from nexus.ui_support import live_settings as ls
from nexus.ui_support import settings_page as sp
from nexus.ui_support.settings_help import SETTINGS_SECTIONS


def _shell(tmp_path, monkeypatch, body="config_version = 2\n"):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    c = shell.client
    c.settings_read = AsyncMock(return_value=p.SettingsReadResult(body=body, rel_path="config.toml", builtin=False, sha256="h"))
    c.settings_write = AsyncMock(return_value=p.SettingsWriteResult(status="saved"))
    return shell


@pytest.fixture
def shell(tmp_path, monkeypatch):
    return _shell(tmp_path, monkeypatch)


def _rows(page):
    return {b["id"]: b for b in page["blocks"] if b.get("t") == "row"}


def _written(shell) -> dict:
    return tomllib.loads(shell.client.settings_write.await_args.args[3])


def test_mirrored_constants_match_the_real_live_config():
    assert ls.LIVE_MODEL == MODEL and ls.LIVE_DEFAULT_VOICE == DEFAULT_VOICE and ls.LIVE_VOICES == VOICES
    defaults = LiveSection()
    assert ls.LIVE_DEFAULTS == {key: getattr(defaults, key) for key in ls.LIVE_DEFAULTS}
    for key, (low, high) in ls.LIVE_RANGES.items():
        LiveSection(**{key: low}), LiveSection(**{key: high})
        for bad in (low - 1, high + 1):
            with pytest.raises(ValueError):
                LiveSection(**{key: bad})


def test_live_sits_next_to_voice_in_navigation():
    keys = [key for key, _ in SETTINGS_SECTIONS]
    assert keys[keys.index("voice") + 1] == "live" and "live" in PAGE_AREAS
    assert dict(SETTINGS_SECTIONS)["live"] == "GPT Live"


def test_reading_keeps_valid_values_and_defaults_the_rest():
    body = '[live]\nmax_minutes = 37\npause_seconds = 99999\nvoice = "nope"\ncarry_turns = 0\ninstructions = "p.md"\nlog_raw = 1\n'
    values = ls._read_values(body)
    assert values["max_minutes"] == 37 and values["carry_turns"] == 0 and values["instructions"] == "p.md"
    assert values["pause_seconds"] == 120 and values["voice"] == "default" and values["log_raw"] is False
    assert ls._read_values("not [toml") == ls.LIVE_DEFAULTS


def test_writing_replaces_only_the_named_key_and_keeps_the_rest():
    body = 'config_version = 2\n[live]\n# keep\nmax_minutes = 37 # mine\n\n[voice]\nenabled = true\n'
    out = ls._set_value(body, "pause_seconds", 60)
    assert tomllib.loads(out)["live"] == {"max_minutes": 37, "pause_seconds": 60} and "# keep" in out
    assert tomllib.loads(out)["voice"] == {"enabled": True}
    assert tomllib.loads(ls._set_value(body, "max_minutes", 45.0))["live"]["max_minutes"] == 45


@pytest.mark.parametrize("key, value", [("max_minutes", 0), ("max_minutes", 241), ("max_minutes", 2.5), ("max_minutes", True),
                                        ("carry_turns", 101), ("pause_seconds", 4), ("voice", "Sol"), ("enabled", 1),
                                        ("instructions", "a\x00b"), ("instructions", "x" * 4097), ("model", "x")])
def test_invalid_values_are_refused(key, value):
    with pytest.raises(ValueError):
        ls.normalize_live_value(key, value)


@pytest.mark.asyncio
async def test_the_page_shows_every_option_with_the_pinned_model_and_default_voice(shell):
    await shell.workflows.settings_area("live")
    page = shell.workflows.settings_page
    assert shell.panel_title == "Settings · GPT Live" and shell.settings_nav == "live"
    assert page["scope"] is None and all(b.get("scope", "") == "" for b in page["blocks"] if b.get("t") == "row")
    rows = _rows(page)
    assert list(rows) == ["enabled", "model", "voice", "max_minutes", "pause_seconds", "carry_turns", "progress", "instructions", "log_raw"]
    assert all(row["description"] for row in rows.values())
    assert rows["model"]["control"]["value"] == MODEL
    voice = rows["voice"]["control"]
    assert voice["value"] == "Default (sol)" and [v for _, v in voice["options"]] == ["default", *VOICES]
    assert rows["max_minutes"]["control"]["display"] == "30 minutes" and rows["carry_turns"]["control"]["value"] == 12
    assert (rows["pause_seconds"]["control"]["min"], rows["pause_seconds"]["control"]["max"]) == (5, 3600)
    assert rows["instructions"]["control"]["c"] == "text"


@pytest.mark.asyncio
async def test_configured_numbers_off_the_step_grid_are_shown_exactly(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch, '[live]\nmax_minutes = 37\npause_seconds = 7\nvoice = "cove"\n')
    await shell.workflows.settings_area("live")
    rows = _rows(shell.workflows.settings_page)
    assert rows["max_minutes"]["control"]["value"] == 37 and rows["pause_seconds"]["control"]["display"] == "7 seconds"
    assert rows["voice"]["control"]["value"] == "cove"


@pytest.mark.asyncio
async def test_each_control_saves_through_the_config_path_and_stays_on_the_page(shell):
    await shell.workflows.settings_area("live")
    rows = _rows(shell.workflows.settings_page)
    for key, value, expected in [("enabled", False, False), ("progress", False, False), ("log_raw", True, True), ("voice", "ember", "ember"),
                                 ("max_minutes", 35, 35), ("carry_turns", 0, 0), ("pause_seconds", 90.0, 90), ("instructions", "docs/live.md", "docs/live.md")]:
        await shell.workflows.operate({**rows[key]["control"]["operation"], "value": value})
        assert _written(shell)["live"][key] == expected
        assert shell.panel_title == "Settings · GPT Live"


@pytest.mark.asyncio
async def test_reset_restores_the_defaults_and_toasts(tmp_path, monkeypatch):
    shell = _shell(tmp_path, monkeypatch, '[live]\nmax_minutes = 37\nvoice = "cove"\n\n[voice]\nenabled = true\n')
    await shell.workflows.settings_area("live")
    button = next(i["operation"] for b in shell.workflows.settings_page["blocks"] if b.get("t") == "buttons" for i in b["items"])
    await shell.workflows.operate(button)
    written = _written(shell)
    assert written["live"] == ls.LIVE_DEFAULTS and written["voice"] == {"enabled": True}
    assert shell.toasts[-1]["title"].startswith("GPT Live settings reset")


@pytest.mark.asyncio
async def test_the_bridge_refuses_out_of_range_values_and_accepts_every_client_operation(shell):
    from settings_page_ops import assert_every_client_op_is_accepted
    await shell.workflows.settings_area("live")
    page = shell.workflows.settings_page
    stepper = _rows(page)["max_minutes"]["control"]["operation"]
    assert sp.accepts(page, {**stepper, "value": 240}) and not sp.accepts(page, {**stepper, "value": 241})
    assert assert_every_client_op_is_accepted(shell) > 8
