"""Native projection of an external Claude Code worker: live and replayed, from the same events."""
from types import SimpleNamespace

import pytest

from nexus.events import Event
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import fold

PROV = "Claude Code (own tools)"
CHILD = "s/sub/1"
META = {"id": CHILD, "parent": "s", "depth": 1, "type": "claude", "task": "task-1", "session": CHILD,
        "parent_call_id": "task1", "agent_id": CHILD, "description": "audit the repo"}
MANUAL = "Claude Code is waiting at its folder-trust prompt; run `cd /w && claude` once and trust it"


def _ev(seq, type_, data=None, *, child=False):
    data = dict(data or {})
    if child:
        data["agent"] = META
    return Event(type=type_, data=data, seq=seq, ts=float(seq), session=CHILD if child else "s",
                 turn="c1" if child else "t1", id=f"e{seq:03d}")


def _events(status="completed", attention=None):
    return [
        _ev(1, "turn.started", {"limits": {}}),
        _ev(2, "tool.requested", {"call_id": "task1", "tool": "Task",
                                  "input": {"prompt": "audit", "subagent_type": "claude"}}),
        _ev(3, "agent.spawned", {"agent": META, **META, "model": "claude-code/claude-opus-5.5"}),
        _ev(4, "turn.started", {"limits": {}}, child=True),
        _ev(5, "external.started", {
            "provider": "claude-code", "model": "claude-opus-5.5", "session": "sess-1", "workspace": "/w",
            "permission_mode": "dontAsk", "tools": ["Read", "Bash"], "allowed_tools": ["Read"],
            "disallowed_tools": [], "restricted": True, "setting_sources": "", "mcp": "none",
            "provenance": PROV,
            "authority": "Claude Code ran its own tools (mode dontAsk) in /w; Nexus permissions/PathGuard did not mediate them",
        }, child=True),
        _ev(6, "external.tool", {"phase": "requested", "call_id": "k1", "tool": "Bash",
                                 "input": {"command": "ls"}, "provenance": PROV}, child=True),
        _ev(7, "external.tool", {"phase": "completed", "call_id": "k1", "tool": "Bash", "is_error": False,
                                 "content": [{"type": "text", "text": "a.py"}], "provenance": PROV}, child=True),
        _ev(8, "external.finished", {
            "status": status, "usage": "unknown", "transcript_path": "/home/u/.claude/projects/w/sess-1.jsonl",
            "tool_calls": 1, "stop_event": "Stop", "attention": attention,
            "error": attention, "elapsed_seconds": 2.5, "clip_notes": ["answer clipped at 8000 characters"],
        }, child=True),
        _ev(9, "agent.completed", {"agent": META, **META, "status": status, "ok": status == "completed",
                                   "is_error": status != "completed", "usage": {}, "usage_unknown": True}),
    ]


def _page(tmp_path, monkeypatch, events, *, on_page=True, verbose=False):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = fold(events)
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    shell.verbose = verbose
    if on_page:
        shell.workflows.agent_page_id = CHILD
        shell.workflows.agent_context = {}
    return project(controller, 1, shell=shell, literal=False)


def _external(snapshot):
    return [block for block in snapshot["blocks"] if block["kind"] == "external"]


def test_agent_page_states_authority_tools_and_marks_claude_tool_rows(tmp_path, monkeypatch):
    snapshot = _page(tmp_path, monkeypatch, _events())
    head, foot = _external(snapshot)
    assert head["title"].startswith("Claude Code worker · claude-opus-5.5")
    for line in ("Provider: Claude Code (external worker)", "Model: claude-opus-5.5", "Workspace: /w",
                 "Permission mode: dontAsk", "Tools: Read, Bash", "Allowed tools: Read", "Restricted: yes"):
        assert line in head["text"].splitlines()
    assert "Nexus permissions/PathGuard did not mediate them" in head["text"]
    assert "Nexus did not execute them" in head["text"]
    assert foot["status"] == "done" and foot["title"] == "Claude Code completed"
    for line in ("Usage: unknown (Claude Code reports none to Nexus; this is not zero)",
                 "Transcript: /home/u/.claude/projects/w/sess-1.jsonl", "Claude tool calls: 1",
                 "Clipped: answer clipped at 8000 characters"):
        assert line in foot["text"].splitlines()
    assert "0 tokens" not in foot["text"]
    opened = _page(tmp_path, monkeypatch, _events(), verbose=True)
    rows = [m for b in opened["blocks"] if b["kind"] == "tool_group" for m in b["members"]]
    assert rows and all(PROV in (r.get("heading") or "") + r["text"] for r in rows)
    group = next(b for b in snapshot["blocks"] if b["kind"] == "tool_group")
    assert group["text"] == "Claude Code ran 1 tool"  # never "Ran 1 command"
    session = dict(map(tuple, snapshot["details_panel"]["session"]))
    assert session["Tokens"].startswith("unknown")


def test_needs_attention_is_a_distinct_state_with_the_manual_instruction_in_full(tmp_path, monkeypatch):
    snapshot = _page(tmp_path, monkeypatch, _events("needs_attention", MANUAL))
    head, foot = _external(snapshot)
    assert foot["status"] == "attention" and head["status"] == "attention"
    assert "NEEDS ATTENTION" in foot["title"]
    assert f"Needs attention: {MANUAL}" in foot["text"].splitlines()
    assert "claude --resume" not in foot["text"]


def test_multiline_values_stay_on_their_labelled_row():
    from nexus.ui_support.external_worker import external_blocks

    header, footer = external_blocks("a", {
        "finished": True, "status": "failed", "error": "line one\nline two\r\nline three",
        "workspace": "/w\nInjected: row"})
    labels = ("Provider", "Model", "Workspace", "Permission mode", "Tools", "Settings sources", "MCP",
              "Authority", "Tool rows", "Status", "Error", "Usage", "Transcript")
    for block in (*header, *footer):
        for line in block["text"].splitlines():
            assert line.split(": ")[0] in labels, line  # no unlabelled continuation rows
    assert "Error: line one line two line three" in footer[0]["text"].splitlines()
    assert "Workspace: /w Injected: row" in header[0]["text"].splitlines()


def test_parent_task_card_names_the_external_worker_and_attention(tmp_path, monkeypatch):
    snapshot = _page(tmp_path, monkeypatch, _events("needs_attention", MANUAL), on_page=False)
    card = next(block for block in snapshot["blocks"] if block["kind"] == "task")
    assert "Claude Code (external, own tools)" in card["title"]
    assert card["detail"].startswith("Needs attention: " + MANUAL)
    plain = _page(tmp_path, monkeypatch, _events(), on_page=False)
    assert "Needs attention" not in next(b for b in plain["blocks"] if b["kind"] == "task")["detail"]


@pytest.mark.parametrize("status", ["failed", "interrupted", "cancelled"])
def test_unsuccessful_statuses_are_not_shown_as_done(tmp_path, monkeypatch, status):
    _, foot = _external(_page(tmp_path, monkeypatch, _events(status, None)))
    assert foot["status"] == "failed" and f"Status: {status}" in foot["text"]


def test_replay_projection_equals_live_projection(tmp_path, monkeypatch):
    events = _events()
    live = _external(_page(tmp_path, monkeypatch, events))
    replay = _external(_page(tmp_path, monkeypatch, events + events))  # replay is idempotent
    assert live == replay


def test_ordinary_agents_render_no_external_blocks(tmp_path, monkeypatch):
    events = [e for e in _events() if e.type not in {"external.started", "external.finished", "external.tool"}]
    snapshot = _page(tmp_path, monkeypatch, events)
    assert _external(snapshot) == []


@pytest.mark.asyncio
async def test_tasks_menu_offers_cancelling_one_running_agent_through_the_host(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from nexus.host import protocol as p

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    running = [e for e in _events() if e.seq < 8]  # spawned, no agent.completed yet
    client = SimpleNamespace(agent_cancel=AsyncMock(return_value=p.AgentCancelResult(
        session="s", agent_id=CHILD, cancelled=True, message="Cancellation requested for this agent only; its parent turn continues.")))
    controller = SimpleNamespace(view=fold(running), session="s", client=client)
    shell = ShellActions(controller)
    await shell.command("/tasks", ())
    cancel = next(item for item in shell.items if item["operation"]["kind"] == "agent_cancel")
    assert CHILD in cancel["label"]
    await shell.workflows.operate(cancel["operation"])
    client.agent_cancel.assert_awaited_once_with("s", CHILD)
    assert "this agent only" in shell.notice
    done = ShellActions(SimpleNamespace(view=fold(_events()), session="s", client=client))
    await done.command("/tasks", ())
    assert not any(item["operation"]["kind"] == "agent_cancel" for item in done.items)
