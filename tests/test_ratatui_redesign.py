"""Native redesign contracts: grouping, identity, accounting and bounded details."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host.protocol import ContextInspectResult
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import _context_label, _context_tiers, project
from nexus.ui_support.timeline import group_tools
from nexus.view import initial_state
from nexus.view.model import BlockView, MessageView, ToolCallView, TurnView


def tool(id, seq, **kw):
    return ToolCallView(call_id=id, event_seq=seq, name=kw.pop("name", "Read"), status=kw.pop("status", "completed"), **kw)


def test_groups_interrupt_on_messages_tasks_and_keep_failure_and_running_tail():
    turn = TurnView(id="t", tools=[tool("a", 1, iteration=1), tool("b", 2, iteration=1, is_error=True),
        tool("c", 3, status="running"), tool("d", 5), tool("task", 6, name="task"), tool("e", 7)],
        messages=[MessageView(event_seq=4, role="assistant", blocks=[BlockView(text="Continue")])])
    groups = group_tools(turn)
    assert [group.id for group in groups] == ["t:ga", "t:gd", "t:gtask", "t:ge"]
    assert len(groups[0].members) == 3 and groups[0].failures == 1 and groups[0].running
    assert groups[0].latest.call_id == "c"
    appended = replace(turn, tools=[*turn.tools[:3], tool("new", 4, status="running")], messages=[])
    assert group_tools(appended)[0].id == groups[0].id


def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    controller = SimpleNamespace(session="s", view=initial_state("s"), agent_name="build", model="test",
                                 running=False, client=SimpleNamespace())
    return ShellActions(controller)


def test_current_agent_color_overrides_stale_preview_everywhere(tmp_path, monkeypatch):
    state = shell(tmp_path, monkeypatch)
    state.preview = ContextInspectResult(session="s", agent={"name": "build", "color": "#112233"})
    state.agent_definitions = {"orchestrator": {"color": "#abcdef"}}
    before = project(state.controller, 1, shell=state)
    state.controller.agent_name = "orchestrator"
    after = project(state.controller, 2, shell=state)
    assert before["agent_color"] == "#112233" and after["agent_color"] == "#abcdef"
    header = next(block for block in after["blocks"] if block["kind"] == "context_header")
    assert header["color"] == after["agent_color"]
    # Empty blocks are greyed; every other chip carries the current agent colour.
    assert all(chip["color"] in {after["agent_color"], "$nx-label-neutral"} for chip in header["members"])


@pytest.mark.asyncio
async def test_selection_refreshes_preview_and_active_refusal_preserves_timestamp(tmp_path, monkeypatch):
    state = shell(tmp_path, monkeypatch)
    preview = ContextInspectResult(session="s", agent={"name": "orchestrator"})
    state.controller.select_agent = AsyncMock()
    state.controller.client.list_agents = AsyncMock(return_value=[{"name": "orchestrator", "color": "#abcdef"}])
    state.controller.client.inspect_context = AsyncMock(return_value=preview)
    await state.select_agent("orchestrator")
    stamp = state.preview_at
    assert stamp is not None and state.preview is preview
    state.controller.client.inspect_context.side_effect = RuntimeError("while the session is active")
    assert not await state.refresh_preview()
    assert state.preview is preview and state.preview_at is stamp
    state.controller.running = True
    state.context_popover()
    assert state.panel_layout == "drawer" and "as of" in state.panel_title
    assert any("before the running turn" in row for row in state.panel_lines)


def test_context_figures_only_use_reported_limits():
    view = SimpleNamespace(context={"used_tokens": 52140, "context_window": 1000000,
        "pricing": {"tiers": [{"context": 200000, "input": 6, "output": 22.5}]}})
    assert _context_tiers(view) == [200000, 1000000]
    assert _context_label(view) == "52.1K · 26.1% · 5.2%"
    view.context.pop("pricing")
    assert _context_tiers(view) == [1000000]
    view.context.pop("context_window")
    assert _context_tiers(view) == [] and _context_label(view) == "52.1K"


def test_revisions_stable_for_history_and_change_for_tail_and_expansion(tmp_path, monkeypatch):
    state = shell(tmp_path, monkeypatch)
    state.preferences.set("context_preview", False)
    first = TurnView(id="t", tools=[tool("a", 1)])
    state.controller.view.turns = [first]
    before = project(state.controller, 1, shell=state, literal=False)["blocks"][0]
    assert before["rev"] == project(state.controller, 2, shell=state, literal=False)["blocks"][0]["rev"]
    state.controller.view.turns = [replace(first, tools=[*first.tools, tool("b", 2, status="running")])]
    after = project(state.controller, 3, shell=state, literal=False)["blocks"][0]
    assert after["id"] == before["id"] and after["rev"] != before["rev"] and after["count"] == 2
    state.expanded.add(after["id"])
    expanded = project(state.controller, 4, shell=state, literal=False)["blocks"][0]
    assert len(expanded["members"]) == 2 and expanded["rev"] != after["rev"]


def test_sidebar_projection_has_full_debug_identity_and_saved_tab(tmp_path, monkeypatch):
    state = shell(tmp_path, monkeypatch)
    state.preferences.set("details_tab", "Logs")
    state.mcp_report = {"daemon": {"pid": 1234, "socket": "/tmp/nexus.sock"}}
    panel = project(state.controller, 1, shell=state)["details_panel"]
    assert panel["tab"] == "Logs"
    assert dict(panel["logs_header"])["Session"] == "s"
    assert dict(panel["logs_header"])["Daemon pid"] == "1234"
    assert dict(panel["logs_header"])["Socket"] == "/tmp/nexus.sock"
    assert state.preferences.__class__(state.preferences.path).values["details_tab"] == "Logs"
