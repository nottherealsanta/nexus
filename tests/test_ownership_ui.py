"""P3 native ownership workflows use only the durable host contract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import msgspec
import pytest

from nexus.events import Event
from nexus.host import protocol as p
from nexus.host_support.session_inspect import inspect_session
from nexus.session import SessionManager
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.ownership import tree_menu, transcript_lines, request_lines
from nexus.view import apply, initial_state


@pytest.mark.parametrize("kind", ["todo.updated", "handoff.created", "handoff.accepted", "handoff.dismissed"])
def test_metadata_events_do_not_mutate_conversation(kind):
    state = initial_state("root")
    for data in ({"items": []}, {"agent": {"id": "child"}, "items": []}):
        updated = apply(state, Event(type=kind, data=data, ts=1, seq=1))
        assert not updated.turns
        assert not updated.agents
        assert not updated.diagnostics
        assert not updated.errors


def test_durable_child_inspection_survives_manager_restart(tmp_path):
    from test_session_ownership import managers
    manager, child_manager = managers(tmp_path)
    from nexus.model.message import Message, Text
    manager.open("root").append_message(Message(role="user", content=[Text(text="root")]))
    child = child_manager.open("child", ownership={
        "kind": "subagent", "root_id": "root", "owner_namespace": "main",
        "owner_id": "root", "owner_turn_id": "turn", "owner_spawn_seq": 1,
        "agent_id": "agent-1",
    })
    child.append_message(Message(role="user", content=[Text(text="Durable assignment")]))
    events = [
        ("turn.started", {}),
        ("input.started", {"content": [{"type": "text", "text": "Durable assignment"}], "input_id": "input-1"}),
        ("context.assembled", {"model": "provider/model", "request": {"messages": [{"role": "user", "content": "Durable assignment"}]}}),
        ("text", {"text": "Durable answer"}),
        ("turn.completed", {}),
    ]
    for seq, (kind, data) in enumerate(events, 1):
        child.append_event(Event(type=kind, data=data, ts=seq, turn="turn"))
    manager.close_all()
    restarted = SessionManager(tmp_path, db=manager.store.db, project=tmp_path, lock_dir=tmp_path / "locks")
    assert not restarted._handles
    result = inspect_session(restarted, "child", "agents")
    assert not restarted._handles  # never opened a handle, recovered or locked a log
    response = p.SessionInspectResult(session="child", namespace="agents", **result)
    text = "\n".join(transcript_lines(response))
    assert "Durable assignment" in text
    assert "Durable answer" in text
    assert "provider/model" in "\n".join(request_lines(response))
    assert restarted.tree("root")["nodes"][0]["children"][0]["status"] == "completed"
    assert not restarted._handles
    assert msgspec.json.decode(msgspec.json.encode(response), type=p.Result) == response


@pytest.mark.asyncio
async def test_native_tree_child_read_only_navigation(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    node = {"id": "root", "namespace": "main", "status": "idle", "children": [
        {"id": "child", "namespace": "agents", "agent_type": "task", "status": "interrupted",
         "task": "a" * 3000, "children": []},
    ]}
    client = SimpleNamespace(session_tree=AsyncMock(return_value={"nodes": [node]}),
                             inspect_session=AsyncMock(return_value=p.SessionInspectResult(
                                 session="child", namespace="agents", view={"turns": []}, request={})))
    shell = ShellActions(SimpleNamespace(session="root", client=client))
    workflow = shell.workflows
    await workflow.operate({"kind": "ownership_tree", "id": "root"})
    client.session_tree.assert_awaited_once_with("root")
    child_op = shell.items[-1]["operation"]
    assert child_op["id"] == "child"
    assert len(child_op["facts"][2]) < 2100
    await workflow.operate(child_op)
    assert all(item["operation"]["kind"] != "switch" for item in shell.items)
    transcript_op = shell.items[0]["operation"]
    await workflow.operate(transcript_op)
    client.inspect_session.assert_awaited_once_with("child", "agents")
    assert shell.panel_layout == "detail"
    workflow.back()
    assert shell.panel_title == "Session details · read only"
    await workflow.operate(shell.items[-1]["operation"])
    assert shell.panel_title == "Session ownership"
    root_op = shell.items[1]["operation"]
    await workflow.operate(root_op)
    assert shell.items[0]["operation"] == {"kind": "switch", "id": "root"}


def test_tree_marks_uncertainty_and_bounds_readable_fields():
    rows, lines = tree_menu({"unknown_ownership": 2, "truncated": True, "nodes": [
        {"id": "root", "namespace": "main", "status": "unknown", "children": []},
    ]})
    assert "unknown" in rows[0][0]
    assert any("unknown ownership" in line for line in lines)
    assert any("truncated" in line for line in lines)

@pytest.mark.asyncio
async def test_desktop_sessions_menu_reaches_shared_ownership_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    client = SimpleNamespace(list_sessions=AsyncMock(return_value=[SimpleNamespace(id="root", title="Root")]),
                             session_tree=AsyncMock(return_value={"nodes": []}))
    shell = ShellActions(SimpleNamespace(session="root", client=client))
    shell.local_transcript = False
    shell.workspace = str(tmp_path)
    await shell.workflows.sessions()
    await shell.workflows.operate(shell.items[0]["operation"])
    assert shell.items[0]["operation"] == {"kind": "ownership_tree", "id": "root"}
    await shell.workflows.operate(shell.items[0]["operation"])
    client.session_tree.assert_awaited_once_with("root")


def test_tree_projects_bounded_spawn_facts_and_usage(tmp_path):
    from test_session_ownership import managers, message
    manager, children = managers(tmp_path)
    root = manager.open("root")
    root.append_message(message())
    spawn = root.append_event(Event(type="agent.spawned", data={"agent": {
        "id": "agent-1", "type": "task", "role": "subagent", "task": "bounded work " * 500,
        "description": "Do bounded work", "model": "p/m", "tier": "medium",
        "branch": "feature", "worktree_path": "/tmp/worktree",
    }}))
    child = children.open("child", ownership={
        "kind": "subagent", "root_id": "root", "owner_namespace": "main", "owner_id": "root",
        "owner_turn_id": "turn", "owner_spawn_seq": spawn.seq, "agent_id": "agent-1",
    })
    child.append_message(message())
    for kind, data in [("turn.started", {}), ("model.usage", {"input": 10, "output": 3}),
                       ("text", {"text": "Result"}), ("turn.completed", {})]:
        child.append_event(Event(type=kind, data=data, turn="turn"))
    node = manager.tree("root")["nodes"][0]["children"][0]
    assert len(node["task"]) == 2000
    assert node["agent_type"] == "task"
    assert node["resolved_model"] == "p/m"
    assert node["tier"] == "medium"
    assert node["branch"] == "feature"
    assert node["worktree"] == "/tmp/worktree"
    assert node["tokens"] == 13
    assert node["usage_bounded"] is True
    assert node["cost_usd"] is None
    assert node["result"] == "Result"
    assert node["status"] == "completed"


def test_inspection_preserves_paragraphs_and_marks_display_truncation(tmp_path):
    from test_session_ownership import managers, message
    manager, children = managers(tmp_path)
    manager.open("root").append_message(message())
    child = children.open("child", ownership={
        "kind": "subagent", "root_id": "root", "owner_namespace": "main", "owner_id": "root",
        "owner_turn_id": "turn", "owner_spawn_seq": 1, "agent_id": "agent-1",
    })
    child.append_message(message())
    child.append_event(Event(type="turn.started", turn="t"))
    child.append_event(Event(type="text", turn="t", data={"text": "line 1\nline 2\n" + "bounded paragraph " * 2000}))
    result = inspect_session(manager, "child", "agents")
    assert result["truncated"] is True
    assert "line 1\nline 2" in str(result["view"]["turns"][0]["messages"][0]["blocks"][0]["text"])

@pytest.mark.asyncio
async def test_poll_refreshes_visible_tree_without_growing_stack(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    root = {"id": "root", "namespace": "main", "status": "idle", "children": []}
    client = SimpleNamespace(session_tree=AsyncMock(return_value={"nodes": [root]}))
    shell = ShellActions(SimpleNamespace(session="root", client=client))
    await shell.workflows.operate({"kind": "ownership_tree", "id": "root"})
    stack = len(shell.workflows.stack)
    client.session_tree.return_value = {"nodes": [{**root, "status": "running"}]}
    assert await shell.workflows.refresh_ownership()
    assert "running" in shell.items[1]["label"]
    assert len(shell.workflows.stack) == stack
    await shell.workflows.operate(shell.items[1]["operation"])
    calls = client.session_tree.await_count
    assert not await shell.workflows.refresh_ownership()
    assert client.session_tree.await_count == calls
