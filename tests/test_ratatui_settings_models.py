"""Settings → Models is one page: default chain and titles above all subagent tiers."""
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
    c.session_title_settings = AsyncMock(return_value=p.SessionTitleSettingsResult(enabled=True, model="openai/gpt-5-mini", resolved="openai/gpt-5-mini"))
    c.list_models = AsyncMock(return_value=[{"provider": "openai", "id": "gpt-5-nano", "name": "GPT-5 nano", "tier": "low"}])
    c.claude_code_settings = AsyncMock(return_value=p.ClaudeCodeSettingsResult(
        main_enabled=False, message="Main selection uses the Agent SDK. External subagents are configured per role."))
    for name in ("claude_code_settings_set", "model_tier_set", "model_tier_reset", "default_model_set", "session_title_settings_set", "agent_max_tier_set", "refresh_models"):
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
async def test_models_is_one_page_with_all_subagent_tiers(shell):
    await shell.workflows.settings_area("models")
    page = shell.workflows.settings_page
    assert shell.panel_title == "Settings · Models" and shell.settings_nav == "models"
    assert not shell.items, "no menu rows: the page replaces them"
    assert _headings(page) == ["Default Model", "Session Titles", "Claude Code", "Subagent Tiers", "Low", "Medium", "High", "Catalogue"]
    assert not any(b.get("t") == "tabs" for b in page["blocks"])
    assert not any(b.get("id") == "ceiling" for b in page["blocks"])
    chain = _find(page, "ordered", id="chain")
    assert [(label, tag) for label, tag, _ in chain["items"]] == [("openai/gpt-5-mini", "in use"), ("anthropic/claude-haiku-4-5", "fallback")]
    low = _find(page, "ordered", id="tier:low")
    assert low["items"][1] == ["anthropic/claude-haiku-4-5", "skipped", "provider not connected · effort: provider default"]
    assert len(low["controls"]) == len(low["items"])
    assert low["controls"][0]["control"]["operation"]["key"] == "effort"
    assert not any(block.get("id", "").startswith("effort:") for block in page["blocks"])
    notes = [b["text"] for b in page["blocks"] if b.get("t") == "note"]
    assert not any("Your own list" in text for text in notes), "a user's own list needs no note"
    assert any("built-in choice" in text for text in notes) and any("sorted into this tier by price" in text for text in notes)


@pytest.mark.asyncio
async def test_all_tiers_remain_visible_across_rebuilds(shell):
    await shell.workflows.settings_area("models")
    page = shell.workflows.settings_page
    assert _find(page, "ordered", id="tier:high")["items"][0][0] == "anthropic/claude-opus-5"
    assert any("No model in this tier can run" in b.get("text", "") for b in page["blocks"] if b.get("t") == "note")
    await shell.workflows.operate(sp.op("models", "titles", value=False))
    assert all(_find(shell.workflows.settings_page, "ordered", id=f"tier:{name}") for name in ("low", "medium", "high"))


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
@pytest.mark.parametrize("tier", ["low", "medium", "high"])
async def test_tier_picker_uses_subagent_models_and_preserves_model_metadata(shell, tier):
    agy = {"provider": "antigravity", "id": "gemini-3-pro", "name": "Gemini 3 Pro — Subagents only",
           "selectable": True, "scope": "subagents",
           "route_metadata": {"worker": "agy"}}
    claude = {"provider": "anthropic", "id": "claude-sonnet-4", "name": "Claude Sonnet 4 — Subagents only",
              "selectable": True, "scope": "subagents",
              "route_metadata": {"worker": "external"}}
    main_only = {"provider": "github-copilot", "id": "gpt-6-luna", "name": "GPT-6 Luna",
                 "selectable": True, "scope": "main"}
    shell.client.list_models = AsyncMock(side_effect=lambda **kwargs: [
        agy, claude] if kwargs["scope"] == "subagents" else [main_only])

    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "tier", tier=tier, action="add", index=0))
    assert shell.panel_title == f"Add model · {tier}"
    assert shell.client.list_models.await_args_list[-1].kwargs == {
        "selectable_only": True, "scope": "subagents"}
    assert [item["label"] for item in shell.items] == [
        "Claude Sonnet 4 — Subagents only · anthropic/claude-sonnet-4",
        "Gemini 3 Pro — Subagents only · antigravity/gemini-3-pro",
    ]
    agy_item = next(item for item in shell.items if "antigravity/gemini-3-pro" in item["label"])
    assert agy_item["operation"] == sp.op(
        "models", "add", target=f"tier:{tier}", ref="antigravity/gemini-3-pro")
    await shell.workflows.operate(agy_item["operation"])
    assert shell.client.model_tier_set.await_args.args[0] == tier
    assert shell.client.model_tier_set.await_args.args[1][-1] == "antigravity/gemini-3-pro"
    assert shell.client.model_tier_set.await_args.args[1][:-1] == {
        "low": ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"],
        "medium": [],
        "high": ["anthropic/claude-opus-5"],
    }[tier]
    assert shell.panel_title == "Settings · Models"


@pytest.mark.asyncio
async def test_default_chain_picker_keeps_main_model_scope(shell):
    shell.client.list_models = AsyncMock(return_value=[
        {"provider": "github-copilot", "id": "gpt-6-luna", "name": "GPT-6 Luna",
         "selectable": True, "scope": "main"}])
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "chain", action="add", index=0))
    assert shell.client.list_models.await_args_list[-1].kwargs == {
        "selectable_only": True, "scope": "main"}
    assert shell.items[0]["operation"] == sp.op(
        "models", "add", target="chain", ref="github-copilot/gpt-6-luna")


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
async def test_titles_and_refresh_use_their_host_commands(shell):
    await shell.workflows.settings_area("models")
    await shell.workflows.operate(sp.op("models", "titles", value=False))
    shell.client.session_title_settings_set.assert_awaited_with(enabled=False)
    await shell.workflows.operate(sp.op("models", "title_model", value="openai/gpt-5-mini"))
    shell.client.session_title_settings_set.assert_awaited_with(model="openai/gpt-5-mini")
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


@pytest.mark.asyncio
async def test_every_operation_the_client_can_send_is_accepted_by_the_bridge(shell):
    from settings_page_ops import assert_every_client_op_is_accepted
    await shell.workflows.settings_area("models")
    assert assert_every_client_op_is_accepted(shell) > 15
    assert_every_client_op_is_accepted(shell)


@pytest.mark.asyncio
async def test_the_bridge_gate_runs_effort_operations_only_while_page_is_open(shell):
    from nexus.ui.ratatui.prototype import operation_allowed
    shell.client.list_models = AsyncMock(return_value=[{"provider": "openai", "id": "gpt-5-mini", "supported_efforts": ["low", "high"]}])
    await shell.workflows.settings_area("models")
    effort = sp.op("models", "effort", tier="low", model="openai/gpt-5-mini", value="high")
    assert operation_allowed(shell, set(), effort)
    assert not operation_allowed(shell, set(), {**effort, "value": "invalid"})
    await shell.workflows.operate(effort)
    shell.client.model_tier_set.assert_awaited_with("low", ["openai/gpt-5-mini", "anthropic/claude-haiku-4-5"], efforts={"openai/gpt-5-mini": "high"})
    shell.workflows.back()
    assert not operation_allowed(shell, set(), effort)


@pytest.mark.asyncio
async def test_claude_code_toggle_is_discoverable_explained_and_saved(shell):
    await shell.workflows.settings_area("models")
    row = _find(shell.workflows.settings_page, "row", id="claude-main")
    assert row["label"] == "Allow Claude Code as a main model"
    assert "Agent SDK" in row["description"] and "per role" in row["description"]
    assert row["control"]["on"] is False
    await shell.workflows.operate(sp.op("models", "claude_main", value=True))
    shell.client.claude_code_settings_set.assert_awaited_with(main_enabled=True)


@pytest.mark.asyncio
async def test_claude_code_subagent_backend_choice_is_explained_and_saved(shell):
    shell.client.claude_code_settings.return_value = p.ClaudeCodeSettingsResult(
        subagent_backend="worker", backend_message="Claude runs its own tools; Nexus does not mediate them.")
    await shell.workflows.settings_area("models")
    row = _find(shell.workflows.settings_page, "row", id="claude-backend")
    assert row["label"] == "Claude Code subagents run via"
    assert row["control"]["options"] == ["Interactive worker (ZMX)", "Agent SDK"]
    assert row["control"]["values"] == ["worker", "sdk"] and row["control"]["active"] == 0
    assert "own tools" in row["description"]
    await shell.workflows.operate(sp.op("models", "claude_backend", value="sdk"))
    shell.client.claude_code_settings_set.assert_awaited_with(subagent_backend="sdk")


def test_claude_code_route_is_labelled_for_subagents_not_skipped():
    from nexus.ui.ratatui.settings_pages.models import _items

    candidates = [{"ref": "claude-code/claude-haiku-5-5", "connected": True, "scope": "subagents"},
                  {"ref": "codex/m", "connected": True}]
    items = _items(["claude-code/claude-haiku-5-5", "codex/m"], "codex/m", candidates)
    assert items[0][1] == "subagents" and "subagents only" in items[0][2]
    assert items[1][1] == "in use"
