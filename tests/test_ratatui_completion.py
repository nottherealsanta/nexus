"""Native completion and model picker share the native terminal shell's logic."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.ui_support.completion import command_matches, complete
from nexus.ui_support.model_choice import model_groups, preselected_effort, selection_effort

MODELS = [
    {"provider": "a", "id": "old", "name": "Old", "last_updated": "2026-06-01", "supported_efforts": ["low", "high"]},
    {"provider": "b", "id": "new", "name": "New", "last_updated": "2026-09-15"},
    {"provider": "a", "id": "mid", "name": "Mid", "last_updated": "2026-07-01"},
]


def client():
    return SimpleNamespace(
        list_models=AsyncMock(return_value=MODELS), list_agents=AsyncMock(return_value=[{"name": "Coder", "contexts": ["root"]}, {"name": "Child", "contexts": ["subagent"]}]),
        list_sessions=AsyncMock(return_value=[SimpleNamespace(id="abc123")]),
        search_files=AsyncMock(return_value=["src/a.py"]))


def test_command_matches_hide_hidden_sort_and_use_aliases():
    assert command_matches("/QUIT") == ["/exit"]
    assert command_matches("/mo") == ["/model"]
    assert command_matches("/") == sorted(command_matches("/"))


@pytest.mark.asyncio
async def test_argument_providers():
    c = client()
    assert await complete(c, "/model a/", "a/") == ["a/old", "a/mid"]
    assert await complete(c, "/agent ", "c") == ["current", "Coder"]
    assert await complete(c, "/reasoning ", "", efforts=["low"]) == ["default", "low"]
    assert await complete(c, "/theme ", "D") == ["dark"]
    assert await complete(c, "/export ", "m") == ["markdown"]
    assert await complete(c, "/voice ", "o") == ["on", "off"]
    assert await complete(c, "/sessions ", "ab") == ["abc123"]
    assert await complete(c, "/settings ", "") == []
    assert await complete(c, "/attach ", "c") == ["clear", "src/a.py"]
    assert await complete(c, "@sr", "@sr") == ["src/a.py"]
    c.search_files.assert_awaited_with("sr", limit=30)
    assert await complete(c, "hi", "hi") == []


@pytest.mark.asyncio
async def test_host_failure_gives_no_candidates():
    c = client()
    c.list_models.side_effect = ValueError("boom")
    assert await complete(c, "/model ", "") == []


def test_model_groups_use_canonical_order():
    groups, _ = model_groups(MODELS, favorites=["a/mid"], recent=["a/mid", "b/new"])
    assert [(t, [r["id"] for r in rows]) for t, rows in groups] == [
        ("Favorites", ["mid"]), ("Recent", ["new"]), ("Recently updated", ["old"])]
    ranked, marks = model_groups(MODELS, query="nw")
    assert ranked[0][0].startswith("Best matches") and ranked[0][1][0]["id"] == "new" and marks["b/new"]


def test_effort_selection_is_atomic_only_when_needed():
    row = MODELS[0]
    base = dict(current="a/old", current_effort="high", stored_override=None)
    assert preselected_effort(row, **base) == "high"
    assert selection_effort(row, effort_source=None, pending=None, touched=False, **base) == (None, False)
    assert selection_effort(row, effort_source="agent", pending=None, touched=False, **base) == ("high", True)
    assert selection_effort(row, effort_source=None, pending="low", touched=True, **base) == ("low", True)


@pytest.mark.asyncio
async def test_agent_model_picker_details_preserve_original_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nexus.ui.ratatui.actions import ShellActions
    c = client()
    row = {"provider": "other", "id": "internal", "name": "Display",
           "context": 128000, "max_output": 8192, "cost": {"input": 0},
           "extra": {"future": [1, 2]}}
    c.list_models.return_value = [row]
    shell = ShellActions(SimpleNamespace(client=c))
    await shell.workflows.agent_model_picker("fallback", 0)
    item = shell.items[0]
    assert item["label"] == "Display · other"
    assert item["operation"]["ref"] == "other/internal"
    await shell.workflows.operate(item["info_operation"])
    assert not shell.items
    text = "\n".join(shell.panel_lines)
    assert "Context tokens: 128000" in text and "Max output tokens: 8192" in text
    import json
    assert json.loads(text.split("Original metadata:\n", 1)[1]) == row
    shell.workflows.back()
    assert shell.items[0] == item


@pytest.mark.asyncio
async def test_native_model_picker_orders_and_commits_effort(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nexus.ui.ratatui.actions import ShellActions
    c = client()
    controller = SimpleNamespace(client=c, session="s", provider="a", model="old", reasoning_effort="high",
        stored_override=None, reasoning_effort_source=None, supported_levels=[], select_model_and_effort=AsyncMock())
    shell = ShellActions(controller)
    shell.preferences.set("model_favorites", ["a/mid"])
    await shell.submit("/model")
    assert shell.items[0]["search"] == "a/mid"
    assert all(i["detail"] == "" for i in shell.items)
    assert shell.items[0]["label"] == "Mid · a"
    assert shell.panel_title == "Select model" and shell.panel_layout == "modal"
    assert shell.items[0]["group"] == "Favorites"  # shown as a heading above the first favorite
    first = next(i for i in shell.items if i["search"] == "a/old")
    assert first["current"] and first["operation"]["selected"] == "high"
    assert any(i["search"] == "b/new" and not i["current"] for i in shell.items)
    original = dict(first["info_operation"]["model"])
    await shell.workflows.operate(first["info_operation"])
    assert shell.panel_title == "Model details"
    text = "\n".join(shell.panel_lines)
    assert "Model ID: old" in text and "Max input tokens: unknown" in text
    import json
    assert json.loads(text.split("Original metadata:\n", 1)[1]) == original
    shell.workflows.back()
    assert shell.panel_title == "Select model"
    assert first in shell.items
    controller.select_model_and_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_picker_sort_toggle_is_named_in_the_title(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nexus.ui.ratatui.actions import ShellActions
    controller = SimpleNamespace(client=client(), session="s", provider="a", model="old", reasoning_effort="high",
        stored_override=None, reasoning_effort_source=None, supported_levels=[], select_model_and_effort=AsyncMock())
    shell = ShellActions(controller)
    await shell.submit("/model")
    assert "Updated ↓" in shell.panel_hint
    shell.model_sort = "name"
    await shell.submit("/model")
    assert "Name A–Z" in shell.panel_hint and all("group" in item for item in shell.items)


@pytest.mark.asyncio
async def test_effort_and_followup_menus_stay_above_composer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nexus.ui.ratatui.actions import ShellActions
    controller = SimpleNamespace(client=client(), session="s", supported_levels=["low", "high"])
    shell = ShellActions(controller)
    await shell.submit("/effort")
    assert shell.panel_layout == "drawer"
    assert [item["command"] for item in shell.items] == ["/effort default", "/effort low", "/effort high"]
    shell.workflows.menu("Follow-up", [("Choose", {"kind": "dismiss"})])
    assert shell.panel_layout == "drawer"
