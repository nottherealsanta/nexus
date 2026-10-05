"""Settings -> Models, Session titles and an agent's Tiers row in the native client.

Plan: plans/SESSION_TITLE_PLAN.md. The pages come from shared rows and help text
(``ui_support/tier_settings.py``) and every change goes through a host command.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))


def _tiers(**overrides):
    rows = [
        {"name": "low", "refs": ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], "source": "your list",
         "resolved": "openai/gpt-5-mini", "runnable": True, "editable": True},
        {"name": "medium", "refs": [], "source": "by price", "resolved": "openai/gpt-5",
         "runnable": True, "editable": True},
        {"name": "high", "refs": ["anthropic/claude-opus-5"], "source": "built-in", "resolved": "",
         "runnable": False, "editable": True},
    ]
    values = dict(order=["low", "medium", "high"], default="medium", tiers=rows, max_tier="high")
    values.update(overrides)
    return p.ModelTiersResult(**values)


def _labels(shell):
    return [item["label"] for item in shell.items]


@pytest.mark.asyncio
async def test_models_page_lists_every_tier_with_source_and_the_model_it_runs_on(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    await shell.workflows.settings_area("models")
    labels = _labels(shell)
    assert shell.panel_title == "Models" and shell.settings_nav == "models"
    assert labels[0] == "low · your list · 2 models · runs on openai/gpt-5-mini"
    assert labels[1] == "medium · by price · runs on openai/gpt-5"
    assert labels[2] == "high · built-in · 1 model · no runnable model"
    assert labels[3] == "Highest tier for subagents · high"
    assert "first model in a list that can run" in " ".join(shell.panel_lines)


@pytest.mark.asyncio
async def test_settings_home_and_area_list_offer_models_and_session_titles(shell):
    from nexus.ui_support.settings_help import SETTINGS_SECTIONS

    shell.client.settings_inventory = AsyncMock(
        return_value=p.SettingsInventoryResult(scope="global", categories=[], items=[], root_display="~/.nexus"))
    title, rows, _lines = await shell.workflows.settings_menu("global", "")
    assert {"Models", "Session titles"} <= {label for label, _ in rows}
    keys = [key for key, _ in SETTINGS_SECTIONS]
    assert keys.index("models") == keys.index("providers") + 1 and "titles" in keys


@pytest.mark.asyncio
async def test_reordering_a_tier_saves_the_whole_ordered_list(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.model_tier_set = AsyncMock(return_value=_tiers())
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(shell.items[0]["operation"])
    assert shell.panel_title == "Tier · low"
    assert _labels(shell)[:2] == ["1. openai/gpt-5-mini · in use", "2. anthropic/claude-haiku-4-5 · fallback"]
    await shell.workflows.operate(shell.items[1]["operation"])  # the second model
    labels = _labels(shell)
    assert labels[:3] == ["Move up", "Move down", "Remove from tier"]
    await shell.workflows.operate(shell.items[0]["operation"])  # Move up
    shell.client.model_tier_set.assert_awaited_once_with("low", ["anthropic/claude-haiku-4-5", "openai/gpt-5-mini"])
    assert shell.panel_title == "Tier · low"


@pytest.mark.asyncio
async def test_removing_the_last_model_is_refused_with_a_way_out(shell):
    only = _tiers(tiers=[{"name": "low", "refs": ["openai/gpt-5-mini"], "source": "your list", "resolved": "openai/gpt-5-mini",
                          "runnable": True, "editable": True}])
    shell.client.model_tiers = AsyncMock(return_value=only)
    shell.client.model_tier_set = AsyncMock()
    with pytest.raises(ValueError, match="Reset to default"):
        await shell.workflows.operate({"kind": "tier_edit", "action": "remove", "name": "low", "index": 0})
    shell.client.model_tier_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_adding_a_model_appends_it_and_reset_uses_the_host_command(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.model_tier_set = AsyncMock(return_value=_tiers())
    shell.client.model_tier_reset = AsyncMock(return_value=_tiers())
    await shell.workflows.operate({"kind": "tier_add", "name": "low", "ref": "openai/gpt-5-nano"})
    shell.client.model_tier_set.assert_awaited_once_with(
        "low", ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5", "openai/gpt-5-nano"])
    await shell.workflows.operate({"kind": "tier_reset", "name": "low"})
    shell.client.model_tier_reset.assert_awaited_once_with("low")


@pytest.mark.asyncio
async def test_a_busy_daemon_says_a_restart_is_needed(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.model_tier_set = AsyncMock(return_value=_tiers(restart_required=True))
    await shell.workflows.operate({"kind": "tier_add", "name": "low", "ref": "openai/gpt-5-nano"})
    assert "restart the daemon" in shell.notice


@pytest.mark.asyncio
async def test_subagent_ceiling_is_a_choice_of_tiers(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.agent_max_tier_set = AsyncMock(return_value=_tiers(max_tier="medium"))
    await shell.workflows.operate({"kind": "tier_ceiling_choices"})
    assert _labels(shell) == ["low", "medium", "high · selected"]
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.client.agent_max_tier_set.assert_awaited_once_with("medium")


# -- Session titles ---------------------------------------------------------


def _titles(**overrides):
    values = dict(enabled=True, model="low", resolved="openai/gpt-5-mini", message="")
    values.update(overrides)
    return p.SessionTitleSettingsResult(**values)


@pytest.mark.asyncio
async def test_session_titles_page_explains_and_toggles(shell):
    shell.client.session_title_settings = AsyncMock(return_value=_titles())
    shell.client.session_title_settings_set = AsyncMock(return_value=_titles(enabled=False))
    await shell.workflows.settings_area("titles")
    assert shell.panel_title == "Session titles" and shell.settings_nav == "titles"
    assert _labels(shell) == ["Generate titles automatically · on", "Title model · low → openai/gpt-5-mini"]
    text = " ".join(shell.panel_lines)
    assert "first message to a fast, low-cost model (low → openai/gpt-5-mini)" in text
    assert "Turn this off to use the first line of your first message as the title." in text
    await shell.workflows.operate(shell.items[0]["operation"])
    shell.client.session_title_settings_set.assert_awaited_once_with(enabled=False)


@pytest.mark.asyncio
async def test_session_titles_page_says_when_the_model_cannot_run(shell):
    shell.client.session_title_settings = AsyncMock(return_value=_titles(
        resolved="", message="The low tier has no runnable model; titles use your first message."))
    await shell.workflows.operate({"kind": "title_settings"})
    assert "no runnable model; titles use your first message" in " ".join(shell.panel_lines)


@pytest.mark.asyncio
async def test_title_model_choices_offer_tiers_and_models(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.list_models = AsyncMock(return_value=[{"provider": "openai", "id": "gpt-5-mini", "name": "GPT-5 mini", "tier": "low"}])
    shell.client.session_title_settings = AsyncMock(return_value=_titles(model="openai/gpt-5-mini"))
    shell.client.session_title_settings_set = AsyncMock(return_value=_titles(model="openai/gpt-5-mini"))
    await shell.workflows.operate({"kind": "title_model_pick"})
    labels = _labels(shell)
    assert labels[:3] == ["low tier → openai/gpt-5-mini", "medium tier → openai/gpt-5", "high tier · no runnable model"]
    await shell.workflows.operate(shell.items[0]["operation"])
    shell.client.session_title_settings_set.assert_awaited_once_with(model="low")


# -- an agent's Tiers row ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("pick_model", [False, True])
async def test_title_save_preserves_parent_and_returns_from_picker(shell, pick_model):
    shell.client.session_title_settings = AsyncMock(return_value=_titles())
    shell.client.session_title_settings_set = AsyncMock(return_value=_titles())
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.list_models = AsyncMock(return_value=[])
    shell.workflows.menu("Settings parent", [("Titles", {"kind": "title_settings"})])
    shell.settings_nav = "titles"
    await shell.workflows.operate({"kind": "title_settings"})
    if pick_model:
        await shell.workflows.operate({"kind": "title_model_pick"})
        await shell.workflows.operate({"kind": "title_model_set", "model": "low"})
    else:
        await shell.workflows.operate({"kind": "title_set", "enabled": False})
    assert shell.panel_title == "Session titles"
    assert shell.settings_nav == "titles"
    shell.workflows.back()
    assert shell.panel_title == "Settings parent"

AGENT = "---\nname: helper\ndescription: Helps.\ncontexts: [subagent]\ntiers: [low, medium]\n---\nWork.\n"


def _open_agent(shell, body=AGENT):
    shell.workflows.agent_draft = {"target": {"scope": "global", "category": "agents", "id": "helper", "sha256": "h",
                                              "builtin": False}, "body": body, "path": "agents/helper.md"}
    shell.workflows.agent_page()


@pytest.mark.asyncio
async def test_agent_page_has_a_tiers_row_for_subagents(shell):
    _open_agent(shell)
    assert "Tiers · low (default), medium" in _labels(shell)
    assert any("Which model tiers the calling agent may run" in line for line in shell.panel_lines)


@pytest.mark.asyncio
async def test_root_only_agents_have_no_tiers_row(shell):
    _open_agent(shell, "---\nname: lead\ndescription: Leads.\ncontexts: [root]\n---\nLead.\n")
    assert not any(label.startswith("Tiers") for label in _labels(shell))


@pytest.mark.asyncio
async def test_an_agent_without_tiers_says_it_uses_the_parents_model(shell):
    _open_agent(shell, "---\nname: helper\ndescription: Helps.\n---\nWork.\n")
    assert _labels(shell)[0] == "Run on · Session model"


@pytest.mark.asyncio
async def test_toggling_tiers_writes_the_file_and_keeps_one_checked(shell):
    shell.client.settings_write = AsyncMock(return_value=p.SettingsWriteResult(status="saved", sha256="h2"))
    shell.client.settings_inventory = AsyncMock(
        return_value=p.SettingsInventoryResult(scope="global", categories=[], items=[], root_display="~/.nexus"))
    _open_agent(shell)
    shell.workflows.tier_cache = {"order": ["low", "medium", "high"], "ceiling": "high", "model_tier": ""}
    await shell.workflows.operate({"kind": "agent_tiers"})
    assert shell.panel_title == "Agent tiers"
    assert _labels(shell)[:3] == ["[x] low · default", "[x] medium", "[ ] high"]
    await shell.workflows.operate({"kind": "agent_tier_toggle", "tier": "high"})
    written = shell.client.settings_write.await_args.args[3]
    assert "tiers: [low, medium, high]" in written
    await shell.workflows.operate({"kind": "agent_tier_default", "tier": "medium"})
    assert "tiers: [medium, low, high]" in shell.client.settings_write.await_args.args[3]
    # Down to one tier, then the last one cannot be unchecked.
    shell.workflows.agent_draft["body"] = AGENT.replace("[low, medium]", "[low]")
    await shell.workflows.operate({"kind": "agent_tier_toggle", "tier": "low"})
    assert shell.notice == "At least one tier must stay checked."


@pytest.mark.asyncio
async def test_a_tier_above_the_ceiling_is_called_out_on_the_agent_page(shell):
    shell.workflows.tier_cache = {"order": ["low", "medium", "high"], "ceiling": "medium", "model_tier": ""}
    _open_agent(shell, AGENT.replace("[low, medium]", "[medium, high]"))
    assert any("high is above the global limit (medium): it runs as medium." in line for line in shell.panel_lines)


@pytest.mark.asyncio
async def test_a_pinned_model_outside_the_tiers_is_called_out(shell):
    shell.workflows.tier_cache = {"order": ["low", "medium", "high"], "ceiling": "high", "model_tier": "high"}
    _open_agent(shell, AGENT.replace("tiers:", "model: openai/gpt-6\ntiers:"))
    assert any("also lists tiers; choosing a mode removes the other" in line for line in shell.panel_lines)
    assert _labels(shell)[0] == "Run on · Specific model"


def test_new_agents_start_with_tiers_both_clients_share():
    from nexus.ui.ratatui.workflows import new_file_body
    from nexus.ui_support.tier_settings import new_agent_template

    body = new_file_body("agents", "reviewer")
    assert body == new_agent_template("reviewer") and "tiers: [low, medium]" in body
    from nexus.agents.model import parse_frontmatter

    assert parse_frontmatter(body).tiers == ("low", "medium")


@pytest.mark.asyncio
async def test_models_default_row_survives_tier_parent_refresh(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.default_model_settings = AsyncMock(return_value=p.DefaultModelSettingsResult(
        refs=["openai/gpt-5-mini"], resolved="openai/gpt-5-mini"))
    await shell.workflows.settings_area("models")
    assert shell.items[0]["operation"]["kind"] == "models_default"
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.workflows.back()
    assert shell.panel_title == "Models"
    assert shell.items[0]["operation"]["kind"] == "models_default"


@pytest.mark.asyncio
async def test_tier_inline_move_uses_same_order_as_submenu(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    shell.client.model_tier_set = AsyncMock(return_value=_tiers())
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(shell.items[0]["operation"])
    await shell.workflows.operate(shell.items[1]["move_up"])
    shell.client.model_tier_set.assert_awaited_once_with(
        "low", ["anthropic/claude-haiku-4-5", "openai/gpt-5-mini"])
    assert shell.panel_title == "Tier · low"


@pytest.mark.asyncio
async def test_default_chain_reorder_refreshes_parent_and_keeps_settings(shell):
    shell.client.model_tiers = AsyncMock(return_value=_tiers())
    state = p.DefaultModelSettingsResult(refs=["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], resolved="openai/gpt-5-mini")
    shell.client.default_model_settings = AsyncMock(return_value=state)
    shell.client.default_model_set = AsyncMock(return_value=state)
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(shell.items[0]["operation"])
    await shell.workflows.operate(shell.items[1]["move_up"])
    shell.client.default_model_set.assert_awaited_once_with(
        ["anthropic/claude-haiku-4-5", "openai/gpt-5-mini"])
    assert shell.panel_title == "Default model" and shell.settings_nav == "models"
    shell.workflows.back()
    assert shell.items[0]["operation"]["kind"] == "models_default"
