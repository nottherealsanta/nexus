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
    assert set(rows) == {"details_sidebar", "context_preview", "centered_layout"}
    assert [key for key, row in rows.items() if not row["control"]["on"]] == ["centered_layout"]  # off by default
    assert all(row["scope"] == "" for row in rows.values())
    await shell.workflows.operate({**rows["centered_layout"]["control"]["operation"], "value": True})
    assert shell.preferences.values["centered_layout"] is True
    await shell.workflows.operate({**rows["details_sidebar"]["control"]["operation"], "value": False})
    assert shell.preferences.values["details_sidebar"] is False
    assert _rows(shell.workflows.settings_page)["details_sidebar"]["control"]["on"] is False
    await shell.workflows.operate(sp.op("layout", "reset"))
    assert shell.preferences.values["details_sidebar"] is True
    assert shell.preferences.values["centered_layout"] is False
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
    # Three leader keys live only on this page: Ctrl+X X, Ctrl+X A and Ctrl+X Shift+N.
    assert sum(len(t["rows"]) for t in tables[:2]) == len(SHORTCUTS) + len(LEADER_SHORTCUTS) + 3
    assert "Ctrl+X Shift+N" in keys
    listed = [row[1] for table in tables for row in table["rows"]]
    assert len(listed) == len(set(listed)), "a shortcut is listed twice"
    with pytest.raises(ValueError, match="read-only"):
        await shell.workflows.operate(sp.op("keys", "anything"))


@pytest.mark.asyncio
async def test_these_pages_need_no_scope_and_say_where_they_save(shell):
    for area in ("appearance", "layout", "keys"):
        await shell.workflows.settings_area(area)
        page = shell.workflows.settings_page
        assert page["scope"] is None and page["footer"]


@pytest.mark.asyncio
async def test_every_operation_the_client_can_send_is_accepted_by_the_bridge(shell):
    from settings_page_ops import assert_every_client_op_is_accepted
    for area in ("appearance", "layout", "keys"):
        await shell.workflows.settings_area(area)
        assert_every_client_op_is_accepted(shell)



@pytest.mark.asyncio
async def test_root_overview_makes_no_provider_call_but_providers_page_refreshes():
    from types import SimpleNamespace

    from nexus.ui.ratatui.settings_pages import build_overview, page_module

    calls = []

    class Client:
        async def providers_status(self):
            calls.append(1)
            return SimpleNamespace(providers=[{"id": "a", "name": "A", "connected": True}])

    # Match the real preference defaults used by the peer overview test.
    prefs = SimpleNamespace(values={"theme": "nexus-dark", "details_sidebar": True, "context_preview": True, "centered_layout": False})
    workflows = SimpleNamespace(client=Client(), shell=SimpleNamespace(preferences=prefs), page_state={}, login=None)
    overview = await build_overview(workflows)
    assert calls == [] and "providers" not in overview["summaries"]

    await page_module("providers").build(workflows)
    assert len(calls) == 1
    overview = await build_overview(workflows)
    assert len(calls) == 1 and overview["summaries"]["providers"] == "1 of 1 connected"


@pytest.mark.asyncio
async def test_overview_inlines_appearance_and_layout_rows_with_their_own_operations(tmp_path, monkeypatch):
    """The root list's rows come from the area builders, so every operation is one the drilled page also offers."""
    from types import SimpleNamespace

    from nexus.ui.ratatui.settings_pages import build_overview, page_module
    from nexus.ui_support import settings_page as sp

    prefs = SimpleNamespace(values={"theme": "nexus-light", "details_sidebar": True, "context_preview": False, "centered_layout": False})
    workflows = SimpleNamespace(shell=SimpleNamespace(preferences=prefs))
    overview = await build_overview(workflows)
    headings = [b["text"] for b in overview["blocks"] if b["t"] == "heading"]
    assert headings == ["Appearance", "Layout"]
    labels = [b["label"] for b in overview["blocks"] if b["t"] == "row"]
    assert labels[0] == "Theme" and "Show context header" in labels
    assert overview["summaries"]["keys"].endswith("shortcuts")
    theme = await page_module("appearance").build(workflows)
    op = next(b for b in theme["blocks"] if b["t"] == "row")["control"]["operation"]
    assert sp.accepts(overview, {**op, "value": "nexus-dark"})
