"""An agent's Tiers row in the native client (the Models and titles pages are in test_ratatui_settings_models.py).

Plan: plans/done/SESSION_TITLE_PLAN.md. The pages come from shared rows and help text
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
