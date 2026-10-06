"""Settings → Models is one page: default chain and titles above Low/Medium/High tier tabs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.ui_support import settings_page as sp


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    c = shell.client
    c.model_tiers = AsyncMock(return_value=_tiers())
    c.default_model_settings = AsyncMock(return_value=_default())
    c.session_title_settings = AsyncMock(return_value=p.SessionTitleSettingsResult(enabled=True, model="low", resolved="openai/gpt-5-mini"))
    c.list_models = AsyncMock(return_value=[{"provider": "openai", "id": "gpt-5-nano", "name": "GPT-5 nano", "tier": "low"}])
    for name in ("model_tier_set", "model_tier_reset", "default_model_set", "session_title_settings_set", "agent_max_tier_set", "refresh_models"):
        setattr(c, name, AsyncMock(return_value=SimpleNamespace(restart_required=False)))
    return shell


def _tiers(**overrides):
    rows = [
        {"name": "low", "refs": ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], "source": "your list",
         "resolved": "openai/gpt-5-mini", "runnable": True, "editable": True,
         "candidates": [{"ref": "anthropic/claude-haiku-4-5", "connected": False, "reason": "provider not connected"}]},
        {"name": "medium", "refs": [], "source": "by price", "resolved": "openai/gpt-5", "runnable": True, "editable": True},
        {"name": "high", "refs": ["anthropic/claude-opus-5"], "source": "built-in", "resolved": "", "runnable": False, "editable": True},
    ]
    values = dict(order=["low", "medium", "high"], default="medium", tiers=rows, max_tier="high")
    values.update(overrides)
    return p.ModelTiersResult(**values)


def _default():
    return p.DefaultModelSettingsResult(refs=["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], resolved="openai/gpt-5-mini")


def _walk(blocks):
    for block in blocks:
        yield block
        if block.get("t") == "section":
            yield from _walk(block["blocks"])


def _find(page, kind, **match):
    return next(b for b in _walk(page["blocks"]) if b.get("t") == kind and all(b.get(k) == v for k, v in match.items()))


def _headings(page):
    return [b["text"] for b in _walk(page["blocks"]) if b.get("t") == "heading"]


@pytest.mark.asyncio
async def test_models_is_one_page_with_defaults_above_the_tier_tabs(shell):
    await shell.workflows.settings_area("models")
    page = shell.workflows.settings_page
    assert shell.panel_title == "Settings · Models" and shell.settings_nav == "models"
    assert not shell.items, "no menu rows: the page replaces them"
    assert _headings(page) == ["DEFAULT MODEL", "SESSION TITLES", "TIERS", "LIMITS", "CATALOGUE"]
    tabs = _find(page, "tabs")
    assert [label for label, _ in tabs["items"]] == ["Low", "Medium", "High"]
    assert [badge for _, badge in tabs["items"]] == ["", "", "!"], "a tier nothing can run is flagged"
    chain = _find(page, "ordered", id="chain")
    assert [(label, tag) for label, tag, _ in chain["items"]] == [("openai/gpt-5-mini", "in use"), ("anthropic/claude-haiku-4-5", "fallback")]
    low = _find(page, "ordered", id="tier:low")
    assert low["items"][1] == ["anthropic/claude-haiku-4-5", "skipped", "provider not connected"]


@pytest.mark.asyncio
async def test_tab_switch_shows_that_tier_and_is_kept_across_rebuilds(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "tab", value=2))
    page = shell.workflows.settings_page
    assert _find(page, "tabs")["active"] == 2
    assert _find(page, "ordered", id="tier:high")["items"][0][0] == "anthropic/claude-opus-5"
    assert any("No model in this tier can run" in b.get("text", "") for b in page["blocks"] if b.get("t") == "note")
    await shell.workflows.operate(sp.op("models", "titles", value=False))
    assert _find(shell.workflows.settings_page, "tabs")["active"] == 2


@pytest.mark.asyncio
async def test_reordering_a_tier_saves_the_whole_list_and_the_page_follows(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "tier", tier="low", action="down", index=0))
    shell.client.model_tier_set.assert_awaited_once_with("low", ["anthropic/claude-haiku-4-5", "openai/gpt-5-mini"])
    assert shell.notice.startswith("Saved") and shell.toasts[-1]["level"] == "success"
    assert shell.panel_title == "Settings · Models", "the page stays open"


@pytest.mark.asyncio
async def test_removing_the_last_model_is_refused_with_a_way_out(shell):
    only = _tiers(tiers=[{"name": "low", "refs": ["openai/gpt-5-mini"], "source": "your list", "resolved": "openai/gpt-5-mini", "runnable": True, "editable": True}])
    shell.client.model_tiers = AsyncMock(return_value=only)
    with pytest.raises(ValueError, match="Reset to default"):
        await shell.workflows.operate(sp.op("models", "tier", tier="low", action="remove", index=0))
    shell.client.model_tier_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_chain_reorder_and_remove_use_the_default_model_command(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "chain", action="down", index=0))
    shell.client.default_model_set.assert_awaited_once_with(["anthropic/claude-haiku-4-5", "openai/gpt-5-mini"])
    await shell.workflows.operate(sp.op("models", "chain", action="remove", index=1))
    shell.client.default_model_set.assert_awaited_with(["openai/gpt-5-mini"])


@pytest.mark.asyncio
async def test_adding_a_model_goes_through_one_picker_and_returns_to_the_page(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "tier", tier="low", action="add", index=0))
    assert shell.panel_title == "Add model · low", "the picker is the one allowed drill-in"
    assert shell.items[0]["operation"] == sp.op("models", "add", target="tier:low", ref="openai/gpt-5-nano")
    await shell.workflows.operate(shell.items[0]["operation"])
    shell.client.model_tier_set.assert_awaited_once_with("low", ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5", "openai/gpt-5-nano"])
    assert shell.panel_title == "Settings · Models" and shell.workflows.settings_page is not None


@pytest.mark.asyncio
async def test_a_busy_daemon_says_a_restart_is_needed(shell):
    shell.client.model_tier_set = AsyncMock(return_value=SimpleNamespace(restart_required=True))
    await shell.workflows.operate(sp.op("models", "add", target="tier:low", ref="openai/gpt-5-nano"))
    assert "restart the daemon" in shell.notice


@pytest.mark.asyncio
async def test_reset_asks_first_then_uses_the_host_command(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "tier_reset_ask", tier="low"))
    assert shell.panel_title == "Reset low?"
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.client.model_tier_reset.assert_awaited_once_with("low")
    assert shell.panel_title == "Settings · Models"


@pytest.mark.asyncio
async def test_ceiling_titles_and_refresh_use_their_host_commands(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "ceiling", value="medium"))
    shell.client.agent_max_tier_set.assert_awaited_once_with("medium")
    await shell.workflows.operate(sp.op("models", "titles", value=False))
    shell.client.session_title_settings_set.assert_awaited_with(enabled=False)
    await shell.workflows.operate(sp.op("models", "title_model", value="medium"))
    shell.client.session_title_settings_set.assert_awaited_with(model="medium")
    await shell.workflows.operate(sp.op("models", "refresh"))
    shell.client.refresh_models.assert_awaited_once()


@pytest.mark.asyncio
async def test_title_model_other_opens_the_picker_and_a_choice_returns(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "title_model", value="__pick__"))
    assert shell.panel_title == "Title model"
    await shell.workflows.operate(shell.items[0]["operation"])
    shell.client.session_title_settings_set.assert_awaited_with(model="openai/gpt-5-nano")
    assert shell.panel_title == "Settings · Models"


@pytest.mark.asyncio
async def test_escape_closes_settings_and_the_stale_page_is_not_sent(shell):
    await shell.workflows.settings_area("models")
    controller = shell.controller
    controller.view = __import__("nexus.view", fromlist=["initial_state"]).initial_state("s")
    assert project(controller, 1, shell=shell)["settings_page"]["area"] == "models"
    shell.workflows.back()
    snapshot = project(controller, 2, shell=shell)
    assert snapshot["settings_page"] is None and snapshot["nav"] is None


@pytest.mark.asyncio
async def test_a_failing_host_keeps_the_last_page_and_says_why(shell):
    await shell.workflows.settings_area("models")
    before = shell.workflows.settings_page
    shell.client.model_tiers = AsyncMock(side_effect=RuntimeError("daemon down"))
    await shell.workflows.refresh_page()
    assert shell.workflows.settings_page is before
    assert shell.toasts[-1]["level"] == "error" and "daemon down" in shell.toasts[-1]["body"] + shell.toasts[-1]["title"]


def test_pages_are_control_safe_and_bounded():
    hostile = sp.page("x", "T\x1b[31m", [sp.row("a", "Label\x07", sp.readout("v\x1b]0;x"))] + [sp.gap()] * 1000)
    assert "\x1b" not in str(hostile) and "\x07" not in str(hostile)
    assert len(hostile["blocks"]) <= sp.MAX_BLOCKS
