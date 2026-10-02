"""Native transcript projection follows the Textual timeline order and spacing."""
from dataclasses import replace
from types import SimpleNamespace

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import BlockView, MessageView, ToolCallView, TurnView


def _turn(**kwargs):
    kwargs.setdefault("phase", "completed")
    return TurnView(
        id="t", index=0, elapsed_ms=3300,
        messages=[
            MessageView(id="u", role="user", event_seq=1, blocks=[BlockView(text="hi there\nsecond line")]),
            MessageView(id="a", role="assistant", event_seq=3, model="gpt-6", provider="openai", blocks=[BlockView(text="Hello!")]),
        ],
        tools=[ToolCallView(call_id="c", name="Read", event_seq=2, status="completed", input={"path": "README.md"})],
        **kwargs,
    )


def _snapshot(tmp_path, monkeypatch, turns, **shell_values):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    view.turns = turns
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    for key, value in shell_values.items():
        setattr(shell, key, value)
    return project(controller, 1, shell=shell)


def test_blocks_follow_event_order_with_textual_gaps(tmp_path, monkeypatch):
    blocks = _snapshot(tmp_path, monkeypatch, [_turn(), replace(_turn(), id="t2", index=1)])["blocks"]
    kinds = [block["kind"] for block in blocks[:4]]
    assert kinds == ["user", "tool", "markdown", "summary"]
    user, tool, reply, summary = blocks[:4]
    assert user["number"] == 1 and user["title"] == "hi there" and user["text"] == "second line"
    assert tool["gap"] == 1 and "Read" in tool["text"]  # margin after the prompt card
    assert reply["gap"] == 1  # a reply after a tool call is set apart
    assert summary["gap"] == 1 and "3.3s" in summary["text"]  # reply bottom margin collapses with the footer's
    assert blocks[4]["id"].startswith("u:user") and blocks[4]["gap"] == 1  # turn margin-bottom


def test_collapsed_turn_keeps_prompt_and_counts_tools(tmp_path, monkeypatch):
    blocks = _snapshot(tmp_path, monkeypatch, [_turn()], collapsed_turns={"t"})["blocks"]
    assert [block["kind"] for block in blocks] == ["user", "collapsed"]
    assert blocks[0]["collapsed"] and blocks[0]["title"] == "hi there …" and blocks[0]["text"] == ""
    assert blocks[1]["title"].startswith("1 tools · Hello!")


def test_error_only_for_terminal_turns_and_context_header_placeholders(tmp_path, monkeypatch):
    failed = _snapshot(tmp_path, monkeypatch, [_turn(error="boom", phase="failed")])["blocks"]
    assert any(block["kind"] == "error" and block["text"] == "Error: boom" for block in failed)
    running = _snapshot(tmp_path, monkeypatch, [_turn(error="boom", phase="active")])["blocks"]
    assert not any(block["kind"] == "error" for block in running)
    shown = _snapshot(tmp_path, monkeypatch, [])
    assert [block["kind"] for block in shown["blocks"]] == ["hints"]  # only the empty-session tips
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    shell = ShellActions(SimpleNamespace(view=view, session="s"))
    snapshot = project(SimpleNamespace(view=view, session="s"), 1, shell=shell)
    header = [block for block in snapshot["blocks"] if block["kind"] == "context"]
    assert [block["title"] for block in header] == ["System prompt", "Tools", "AGENTS.md", "Skills", "MCP"]
    assert all(block["color"] == "$nx-label-neutral" for block in header)
    assert header[4]["operation"] == {"kind": "context_show", "key": "mcp"}


def test_details_panel_matches_textual_sections(tmp_path, monkeypatch):
    panel = _snapshot(tmp_path, monkeypatch, [_turn()], mcp_report={"mcp": {"servers": [
        {"name": "cvc", "health": "ready", "tool_count": 4}]}})["details_panel"]
    labels = [label for label, _ in panel["session"]]
    assert labels[:6] == ["Status", "Agent", "Model", "Effort", "Turns", "Tool calls"]
    assert panel["files"] == [] and panel["files_summary"] == ""
    assert panel["mcp"] == [["success", "cvc", "4 tools"]]


def test_task_card_links_child_and_hides_duplicate_agent_entry(tmp_path, monkeypatch):
    from nexus.view.model import AgentView
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    task = ToolCallView(call_id="task-1", name="Task", event_seq=2, status="running",
                        input={"subagent_type": "explore", "description": "Scan"}, child_agent_ids=["a1"])
    view.turns = [TurnView(id="t", index=0, phase="active", messages=[
        MessageView(id="u", role="user", event_seq=1, blocks=[BlockView(text="go")])], tools=[task])]
    view.agents = {"a1": AgentView(id="a1", type="explore", description="Scan", status="spawned"),
                   "a2": AgentView(id="a2", description="Loose", status="spawned")}
    shell = ShellActions(SimpleNamespace(view=view, session="s"))
    shell.preferences.values["context_preview"] = False
    blocks = project(SimpleNamespace(view=view, session="s"), 1, shell=shell)["blocks"]
    card = next(block for block in blocks if block["id"] == "task-1")
    assert " Explore · " in card["text"].splitlines()[0]
    assert len(card["text"].splitlines()) == 2
    assert card["operation"] == {"kind": "agent_page", "id": "a1"}
    assert [block["id"] for block in blocks if block["id"] in {"a1", "a2"}] == ["a2"]


def test_details_mcp_accepts_doctor_report_dict_not_struct(tmp_path, monkeypatch):
    """The host returns a DoctorResult; the shell must hand the sidebar its `report` dict."""
    from nexus.host.protocol import DoctorResult
    assert "report" in DoctorResult.__struct_fields__
    report = {"mcp": {"servers": [{"name": "cvc", "health": "failed", "tool_count": 0, "last_error": "boom"}]}}
    panel = _snapshot(tmp_path, monkeypatch, [], mcp_report=report)["details_panel"]
    assert panel["mcp"][0][:2] == ["error", "cvc"] and panel["mcp"][1][0] == "plain-error"


def test_failing_section_becomes_a_notice_and_other_sections_render(tmp_path, monkeypatch):
    import nexus.ui.ratatui.prototype as prototype

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(prototype, "_details_panel", broken)
    original = prototype._project_turn
    monkeypatch.setattr(prototype, "_project_turn",
                        lambda turn, *rest: broken() if turn.id == "bad" else original(turn, *rest))
    snapshot = _snapshot(tmp_path, monkeypatch, [replace(_turn(), id="bad"), replace(_turn(), id="good", index=1)])
    ids = [block["id"] for block in snapshot["blocks"]]
    assert "bad" in ids and any(block["kind"] == "markdown" for block in snapshot["blocks"])
    notice = next(block for block in snapshot["blocks"] if block["id"] == "notice")["text"]
    assert "Turn bad could not be shown" in notice and "Details sidebar could not be shown" in notice
    assert snapshot["details_panel"] == {}


def test_running_tool_carries_the_animation_slot_and_finished_does_not(tmp_path, monkeypatch):
    from nexus.ui.ratatui.prototype import SPINNER_FRAMES, SPINNER_SLOT

    running = replace(_turn(phase="running"), tools=[ToolCallView(call_id="c", name="Bash", event_seq=2, status="running", input={"command": "ls"})])
    blocks = _snapshot(tmp_path, monkeypatch, [running])["blocks"]
    tool = next(block for block in blocks if block["kind"] == "tool")
    assert SPINNER_SLOT in tool["text"] and not any(frame in tool["text"] for frame in SPINNER_FRAMES)
    done = next(block for block in _snapshot(tmp_path, monkeypatch, [_turn()])["blocks"] if block["kind"] == "tool")
    assert SPINNER_SLOT not in done["text"]


def test_empty_session_hints_are_aligned_and_vanish_with_the_first_turn(tmp_path, monkeypatch):
    hints = _snapshot(tmp_path, monkeypatch, [])["blocks"][0]
    rows = [row.split("\t") for row in hints["text"].splitlines()]
    assert hints["kind"] == "hints" and hints["gap"] == 4 and len(rows) == 4
    assert len({len(keys) for keys, _ in rows}) == 1 and len({len(text) for _, text in rows}) == 1
    assert not any(block["kind"] == "hints" for block in _snapshot(tmp_path, monkeypatch, [_turn()])["blocks"])
