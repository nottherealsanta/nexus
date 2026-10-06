"""Settings → Appearance, Layout and Keyboard are one page each, with no scope control."""
from types import SimpleNamespace

import pytest

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui_support import settings_page as sp


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))


def _rows(page):
    return {b["id"]: b for b in page["blocks"] if b.get("t") == "row"}


@pytest.mark.asyncio
async def test_appearance_is_one_theme_choice_that_saves_at_once(shell):
    await shell.workflows.settings_area("appearance")
    page = shell.workflows.settings_page
    assert shell.panel_title == "Settings · Appearance" and page["scope"] is None
    theme = _rows(page)["theme"]["control"]
    assert theme["options"] == ["Dark", "Light"] and theme["active"] == 0 and theme["values"] == ["nexus-dark", "nexus-light"]
    await shell.workflows.operate({**theme["operation"], "value": "nexus-light"})
    assert shell.preferences.values["theme"] == "nexus-light"
    assert _rows(shell.workflows.settings_page)["theme"]["control"]["active"] == 1
    assert shell.toasts[-1]["title"] == "Theme saved"
    await shell.workflows.operate(sp.op("appearance", "reset"))
    assert shell.preferences.values["theme"] == "nexus-dark"
    with pytest.raises(ValueError, match="Unknown theme"):
        await shell.workflows.operate(sp.op("appearance", "theme", value="neon"))


@pytest.mark.asyncio
async def test_layout_toggles_each_panel_and_resets(shell):
    await shell.workflows.operate({"kind": "layout"})
    page = shell.workflows.settings_page
    rows = _rows(page)
    assert set(rows) == {"sessions_sidebar", "details_sidebar", "context_preview"}
    assert all(row["control"]["on"] for row in rows.values()) and all(row["scope"] == "" for row in rows.values())
    await shell.workflows.operate({**rows["details_sidebar"]["control"]["operation"], "value": False})
    assert shell.preferences.values["details_sidebar"] is False
    assert _rows(shell.workflows.settings_page)["details_sidebar"]["control"]["on"] is False
    await shell.workflows.operate(sp.op("layout", "reset"))
    assert shell.preferences.values["details_sidebar"] is True
    with pytest.raises(ValueError):
        await shell.workflows.operate(sp.op("layout", "toggle", pref="theme", value=True))


@pytest.mark.asyncio
async def test_keyboard_lists_every_shortcut_read_only(shell):
    from nexus.ui_support.shortcuts import LEADER_SHORTCUTS, SHORTCUTS
    await shell.workflows.operate({"kind": "keyboard"})
    assert shell.panel_title == "Settings · Keyboard" and shell.settings_nav == "keys"
    tables = [b for b in shell.workflows.settings_page["blocks"] if b.get("t") == "table"]
    keys = {row[1] for table in tables for row in table["rows"]}
    assert "Ctrl+B" in keys and "Ctrl+X A" in keys and "Ctrl+X X" in keys
    assert sum(len(t["rows"]) for t in tables[:2]) == len(SHORTCUTS) + len(LEADER_SHORTCUTS) + 2
    with pytest.raises(ValueError, match="read-only"):
        await shell.workflows.operate(sp.op("keys", "anything"))


@pytest.mark.asyncio
async def test_these_pages_need_no_scope_and_say_where_they_save(shell):
    for area in ("appearance", "layout", "keys"):
        await shell.workflows.settings_area(area)
        page = shell.workflows.settings_page
        assert page["scope"] is None and page["footer"]
