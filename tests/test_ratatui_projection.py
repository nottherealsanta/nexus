"""Native transcript projection follows the native terminal timeline order and spacing."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

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


def _snapshot(tmp_path, monkeypatch, turns, literal=True, **shell_values):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    view.turns = turns
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    for key, value in shell_values.items():
        setattr(shell, key, value)
    return project(controller, 1, shell=shell, literal=literal)


@pytest.mark.parametrize("color", ["", "#ab12cd"])
def test_user_border_matches_agent_color(tmp_path, monkeypatch, color):
    snapshot = _snapshot(tmp_path, monkeypatch, [_turn(agent={"name": "review", "color": color})])
    user = next(block for block in snapshot["blocks"] if block["kind"] == "user")
    assert user["color"] == (color or snapshot["agent_color"])


def test_live_activity_preview_closes_at_reply_and_completion(tmp_path, monkeypatch):
    tools = [ToolCallView(call_id=f"read-{i}", name="read", event_seq=i + 2,
                         status="completed", input={"path": f"{i}.py"}) for i in range(8)]
    turn = replace(_turn(), phase="active", messages=[_turn().messages[0]], tools=tools)
    def activity(value):
        return next(b for b in _snapshot(tmp_path, monkeypatch, [value],
                    local_transcript=True)["blocks"] if b["kind"] == "tool_group")
    live = activity(turn)
    assert live["preview_limit"] == 5
    assert live["text"] == "Read 8 files"
    assert len(live["members"]) == 8  # Rust bounds the preview, never the full payload.
    completed = activity(replace(turn, phase="completed"))
    assert "preview_limit" not in completed
    reply = MessageView(id="reply", role="assistant", event_seq=20,
                        blocks=[BlockView(text="The files look good.")])
    assert "preview_limit" not in activity(replace(turn, messages=turn.messages + [reply]))
    assert activity(replace(turn, tools=tools[:2]))["id"] == live["id"]


def test_parallel_markers_are_separate_from_tool_text(tmp_path, monkeypatch):
    tools = [
        ToolCallView(call_id="a", name="read", event_seq=2, iteration=1,
                     status="completed", input={"path": "README.md"}),
        ToolCallView(call_id="b", name="subagent", event_seq=3, iteration=1,
                     status="running", input={"description": "Scan"}),
        ToolCallView(call_id="c", name="grep", event_seq=4, iteration=2,
                     status="completed", input={"pattern": "main"}),
    ]
    snapshot = _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=tools)])
    blocks = [b for b in snapshot["blocks"] if b["kind"] in {"tool", "tool_group"}]
    assert [b["kind"] for b in blocks] == ["tool_group", "tool", "tool_group"]
    assert [b["count"] for b in blocks if b["kind"] == "tool_group"] == [1, 1]
    assert all(not b["text"].startswith(("┌", "│", "└")) for b in blocks)
    assert blocks[1]["text"].splitlines() == [blocks[1]["text"]]  # one row
    assert "Subagent — Scan" in blocks[1]["text"]


def test_blocks_follow_event_order_with_textual_gaps(tmp_path, monkeypatch):
    blocks = _snapshot(tmp_path, monkeypatch, [_turn(), replace(_turn(), id="t2", index=1)])["blocks"]
    kinds = [block["kind"] for block in blocks[:4]]
    assert kinds == ["user", "tool_group", "markdown", "summary"]
    user, tool, reply, summary = blocks[:4]
    assert user["number"] == 1 and user["title"] == "hi there" and user["text"] == "second line"
    assert tool["gap"] == 1 and tool["text"] == "Read 1 file"  # margin after the prompt card
    assert reply["gap"] == 1  # a reply after a tool call is set apart
    assert summary["gap"] == 0 and "3.3s" in summary["text"]  # reply bottom margin collapses with the footer's
    assert blocks[4]["id"].startswith("u:user") and blocks[4]["gap"] == 1  # turn margin-bottom


@pytest.mark.parametrize("expanded", [False, True])
def test_thought_and_tools_share_one_activity_group(tmp_path, monkeypatch, expanded):
    thought = MessageView(id="thought", role="assistant", event_seq=3,
                          blocks=[BlockView(kind="thinking", text="Consider the next step.")])
    turn = replace(_turn(), messages=[_turn().messages[0], thought], tools=[
        ToolCallView(call_id="before", name="Read", event_seq=2, status="completed",
                     input={"path": "README.md"}),
        ToolCallView(call_id="after", name="Read", event_seq=4, status="completed",
                     input={"path": "docs/README.md"}),
    ])
    blocks = _snapshot(tmp_path, monkeypatch, [turn], verbose=expanded,
                       expanded={"thoughtthinking"} if expanded else set())["blocks"]
    transcript = [b for b in blocks if b["kind"] in {"tool_group", "thought"}]
    assert len(transcript) == 1
    assert transcript[0]["text"] == "Read 2 files · Thought 1 time"
    assert transcript[0]["count"] == 3
    if expanded:
        assert [m["kind"] for m in transcript[0]["members"]] == ["tool", "thought", "tool"]


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
    assert [block["kind"] for block in shown["blocks"]] == []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    shell = ShellActions(SimpleNamespace(view=view, session="s"))
    snapshot = project(SimpleNamespace(view=view, session="s"), 1, shell=shell)
    header = next(block for block in snapshot["blocks"] if block["kind"] == "context_header")["members"]
    assert [block["title"] for block in header] == ["System prompt", "Tools", "AGENTS.md", "Skills", "MCP"]
    assert all(block["color"] == snapshot["agent_color"] for block in header)
    assert header[0]["gap"] == 0
    assert header[4]["operation"] == {"kind": "context_show", "key": "mcp"}


def test_details_panel_matches_textual_sections(tmp_path, monkeypatch):
    panel = _snapshot(tmp_path, monkeypatch, [_turn()], mcp_report={"mcp": {"servers": [
        {"name": "cvc", "health": "ready", "tool_count": 4}]}})["details_panel"]
    labels = [label for label, _ in panel["session"]]
    assert labels[:8] == ["ID", "Title", "Status", "Agent", "Model", "Effort", "Turns", "Tool calls"]
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
    assert card["text"] == "\ue000 Explore Subagent — Scan"  # spinner slot, one row
    assert card["operation"] == {"kind": "agent_page", "id": "a1"}
    assert [block["id"] for block in blocks if block["id"] in {"a1", "a2"}] == ["a2"]


@pytest.mark.parametrize(
    ("status", "spawned", "completed", "suffix"),
    [
        ("completed", 100.0, 165.0, " · 1m 5s"),
        ("running", 100.0, None, ""),
        ("completed", 100.0, 100.25, " · 250ms"),
        ("completed", None, None, ""),
        ("completed", 100.0, None, ""),
        ("failed", 100.0, 105.0, " · 5.0s"),
    ],
)
def test_subagent_elapsed_time_only_when_finished(tmp_path, monkeypatch, status, spawned, completed, suffix):
    from nexus.view.model import AgentView

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    tool = ToolCallView(
        call_id="tc", name="subagent", input={"description": "find code", "subagent_type": "explore"},
        status="running" if status == "running" else "completed", child_agent_ids=["kid"],
    )
    child = AgentView(
        id="kid", type="explore", status="spawned" if status == "running" else status,
        model="openai/gpt-x", spawned_ts=spawned, completed_ts=completed,
    )
    child.body.turns = [TurnView(id="t", reasoning_effort="medium")]
    view = initial_state("s")
    view.turns = [replace(_turn(), tools=[tool])]
    view.agents = {"kid": child}
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    row = next(row for row in project(controller, 1, shell=shell)["blocks"] if row["kind"] == "tool")
    assert row["text"].endswith(f" · gpt-x (medium){suffix}")


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
    from nexus.ui.ratatui.prototype import SPINNER_FRAMES

    running = replace(_turn(phase="running"), tools=[ToolCallView(call_id="c", name="Bash", event_seq=2, status="running", input={"command": "ls"})])
    blocks = _snapshot(tmp_path, monkeypatch, [running])["blocks"]
    tool = next(block for block in blocks if block["kind"] == "tool_group")
    assert tool["status"] == "running" and not any(frame in tool["text"] for frame in SPINNER_FRAMES)
    done = next(block for block in _snapshot(tmp_path, monkeypatch, [_turn()])["blocks"] if block["kind"] == "tool_group")
    assert done["status"] != "running"


def test_empty_session_has_no_shortcut_splash(tmp_path, monkeypatch):
    assert _snapshot(tmp_path, monkeypatch, [])['blocks'] == []
    assert not any(block['kind'] == 'hints' for block in _snapshot(tmp_path, monkeypatch, [_turn()])['blocks'])


def test_modified_file_diff_is_sent_only_while_expanded(tmp_path, monkeypatch):
    edit = ToolCallView(call_id="e", name="Edit", event_seq=2, status="completed",
                        input={"path": "a.py"}, diff={"path": "a.py", "hunk": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new"})
    turn = replace(_turn(), tools=[edit])
    closed = _snapshot(tmp_path, monkeypatch, [turn])["details_panel"]["files"][0]
    assert closed["open"] is False and closed["diff"] == []
    opened = _snapshot(tmp_path, monkeypatch, [turn], open_files={"a.py"})["details_panel"]["files"][0]
    assert opened["open"] is True and opened["diff"] == ["@@ -1 +1 @@", "-old", "+new"]


def test_session_cards_carry_status_words_age_and_the_current_marker():
    from nexus.host.protocol import ProjectSession, ProjectSessionsListResult
    from nexus.session.manager import SessionSummary
    from nexus.ui.ratatui.workflows import session_rows

    def row(id, state, seq, title):
        return ProjectSession("/w", "p", SessionSummary(id=id, title=title, state=state, last_activity=1000.0, last_seq=seq, completion_seq=seq, message_count=3))

    result = ProjectSessionsListResult(sessions=[row("a", "running", 5, "Busy"), row("b", "idle", 5, "Quiet"), row("c", "awaiting_input", 5, "Asks"), row("d", "idle", 0, "")])
    seen = {"b": 2}  # b finished something since it was last viewed
    rows = {r["id"]: r for r in session_rows(result, current="a", seen=seen, now=1000.0 + 300)}
    assert rows["a"]["status"] == "working" and rows["a"]["active"] is True and rows["a"]["sub"] == "3 · 5m"
    assert rows["b"]["status"] == "done" and rows["b"]["sub"] == "3 · 5m"
    assert rows["c"]["status"] == "input" and rows["c"]["sub"] == "3 · 5m"
    assert rows["a"]["title"] == "Busy"
    assert rows["d"]["title"] == "New Session"


def test_tabs_mark_the_current_session_and_its_running_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from nexus.ui.ratatui.prototype import _tab_rows

    controller = SimpleNamespace(view=initial_state("s"), session="s", running=True)
    shell = ShellActions(controller)
    shell.workspace = "/w"
    shell.tabs = [{"id": "s", "title": "one", "workspace": "/w"}, {"id": "t", "title": "two", "workspace": "/w", "status": "done"},
                  {"id": "s", "title": "other project", "workspace": "/x"}]
    rows = _tab_rows(controller, shell)
    assert [(r["active"], r["status"]) for r in rows] == [(True, "working"), (False, "done"), (False, "")]
    controller.running = False
    assert _tab_rows(controller, shell)[0]["status"] == ""


def test_diff_rows_carry_real_line_numbers_pairing_and_gaps():
    from nexus.ui_support.timeline import diff_split_rows

    hunk = "@@ -9,3 +9,3 @@\n keep\n-old\n+new\n tail\n@@ -40,1 +40,2 @@\n ctx\n+extra"
    assert diff_split_rows(hunk) == [
        (9, "keep", 9, "keep", "ctx"), (10, "old", 10, "new", "change"), (11, "tail", 11, "tail", "ctx"),
        (0, "", 0, "", "sep"), (40, "ctx", 40, "ctx", "ctx"), (0, "", 41, "extra", "add"),
    ]
    clipped = diff_split_rows("@@ -1,5 +1,5 @@\n" + "\n".join(f" l{i}" for i in range(5)), limit=2)
    assert clipped[-1] == (0, "… 3 more rows", 0, "", "clip") and len(clipped) == 3


def test_diff_blocks_send_rows_and_counts(tmp_path, monkeypatch):
    edit = ToolCallView(call_id="e", name="Edit", event_seq=2, status="completed", input={"path": "a.py"},
                        diff={"path": "a.py", "hunk": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"})
    group = next(b for b in _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=[edit])], verbose=True)["blocks"] if b["kind"] == "tool_group")
    diff = group["members"][0]["members"][0]
    assert diff["title"] == "a.py" and (diff["added"], diff["removed"]) == (1, 1)
    assert diff["diff_rows"] == [[1, "old", 1, "new", "change"]]


def test_tool_detail_tones_mirror_the_plain_text_and_the_textual_modal():
    from nexus.ui_support.tool_details import DetailRow, DetailSection, sections_to_text, styled_lines

    sections = [DetailSection("Parameters", (DetailRow("path", "a.py"), DetailRow("edits[0]", "", header=True, indent=0),
                                             DetailRow("old", "x\ny", block=True, indent=1))),
                DetailSection("Diff", (DetailRow("hunk", "@@ -1 +1 @@\n-a\n+b\n c", block=True),), kind="diff")]
    lines, tones = styled_lines(sections)
    assert "\n".join(lines) == sections_to_text(sections)
    assert tones == ["title", "kv", "header", "label", "", "", "", "title", "label", "hunk", "del", "add", ""]


def test_queue_lines_and_meter_extras_follow_the_textual_status_row(tmp_path, monkeypatch):
    from nexus.ui.ratatui.prototype import _queue_lines
    from nexus.view.model import QueuedInputView

    view = initial_state("s")
    view.input_queue = [QueuedInputView(queued_id=str(i), mode=mode, content=[{"type": "text", "text": f"msg {i}"}])
                        for i, mode in enumerate(["queue", "steer", "interrupt", "queue", "queue"])]
    assert _queue_lines(view) == ["Queued · msg 0", "Steer · msg 1", "Interrupt · msg 2", "+2 more queued"]


def test_usage_lines_bar_each_window_with_its_tone():
    from nexus.ui_support.usage import usage_lines

    result = {"providers": [{"id": "claude", "label": "Claude", "plan": "Pro", "windows": [
        {"label": "5-hour session", "used_percent": 79.0}, {"label": "Weekly", "used_percent": 12.0}], "notes": ["a note"], "source": "cli"}],
        "not_connected": ["ChatGPT"]}
    lines, tones = usage_lines(result, now=0)
    assert len(lines) == len(tones) and tones[0] == "title"
    window_tones = [t for line, t in zip(lines, tones) if "session" in line or "Weekly" in line]
    assert window_tones == ["warn", "ok"] or window_tones[1] == "ok"
    assert "dim" in tones and lines[-1].startswith("Not connected")
    assert usage_lines({"providers": []})[0][0] == "No connected provider reports usage."


def test_live_projection_skips_the_literal_lines_but_keeps_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    view.turns = [_turn()]
    controller = SimpleNamespace(view=view, session="s")

    def shell():
        value = ShellActions(controller)
        value.preferences.values["context_preview"] = False
        return value

    literal = project(controller, 1, shell=shell())
    live = project(controller, 1, shell=shell(), literal=False)
    assert "Read" in "\n".join(literal["lines"])
    assert live["lines"] == []
    assert [b["kind"] for b in live["blocks"]] == [b["kind"] for b in literal["blocks"]]


def test_agent_page_uses_child_conversation_and_recorded_context(tmp_path, monkeypatch):
    from nexus.view.model import AgentView
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view, body = initial_state("s"), initial_state("child")
    body.turns = [_turn()]
    view.agents = {"child": AgentView(id="child", type="advisor", task="Inspect", body=body)}
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.workflows.agent_page_id = "child"
    shell.workflows.agent_context = {"system_text": "Child instructions", "agent": {"name": "advisor"},
                                    "messages": [{"blocks": [{"text": "Inspect the UI"}]}]}
    snapshot = project(controller, 1, shell=shell)
    assert snapshot["agent_page"] == "child" and snapshot["panel_title"] == ""
    assert not snapshot["sessions_sidebar"] and snapshot["prompt"] is None
    first = snapshot["blocks"][0]  # the task opens the page as a prompt card, no header strip
    assert first["kind"] == "user" and first["title"] == "Inspect the UI" and first["color"]
    assert all(b["kind"] != "context_header" for b in snapshot["blocks"])
    assert shell.workflows.agent_context["system_text"] == "Child instructions"
    assert any(b["kind"] == "markdown" and b["text"] == "Hello!" for b in snapshot["blocks"])
    shell.workflows.back()
    assert shell.workflows.agent_page_id is None


@pytest.mark.parametrize("name, inputs, summary", [
    ("Read", {"path": "private-file.txt"}, "Read 1 file"),
    ("Bash", {"command": "echo private-output"}, "Ran 1 command"),
])
def test_single_tool_keeps_group_summary_and_expands_details(tmp_path, monkeypatch, name, inputs, summary):
    tool = ToolCallView(call_id="only", name=name, event_seq=2, status="completed",
                        input=inputs, display="private-output")
    turn = replace(_turn(), tools=[tool])
    def group(expanded):
        return next(b for b in _snapshot(tmp_path, monkeypatch, [turn], literal=False,
                                        expanded=expanded)["blocks"] if b["kind"] == "tool_group")
    closed = group(set())
    assert closed["text"] == summary and closed["count"] == 1
    assert closed["collapsed"] and closed["members"] == []
    opened = group({"t:activity:only", "only:detail"})
    assert opened["text"] == summary and not opened["collapsed"]
    assert len(opened["members"]) == 1
    assert "private-output" in opened["members"][0]["detail"]
    assert next(iter(inputs.values())) in opened["members"][0]["detail"]


def test_groups_and_tool_detail_have_independent_expansion(tmp_path, monkeypatch):
    long_out = "\n".join(f"line {i}" for i in range(30))
    tools = [ToolCallView(call_id="short", name="Bash", event_seq=2, status="completed", input={"command": "ls"}, display="ok"),
             ToolCallView(call_id="long", name="Bash", event_seq=3, status="completed", input={"command": "ls"}, display=long_out)]
    turn = replace(_turn(), messages=[_turn().messages[0]], tools=tools)
    def group(expanded):
        return next(b for b in _snapshot(tmp_path, monkeypatch, [turn], literal=False, expanded=expanded)["blocks"] if b["kind"] == "tool_group")
    assert group(set())["members"] == []
    opened = group({"t:activity:short"})
    assert opened["count"] == 2 and not opened["members"][1]["detail"]
    folded = group({"t:activity:short", "long:detail"})["members"][1]
    assert "command: ls" in folded["detail"] and "more lines" in folded["detail"]
    assert folded["output_operation"] == {"kind": "block_toggle", "id": "long:output"}
    all_output = group({"t:activity:short", "long:detail", "long:output"})["members"][1]
    assert "line 29" in all_output["detail"]


def test_explored_group_summarises_lookups_and_thought_carries_duration(tmp_path, monkeypatch):
    thinking = BlockView(kind="thinking", text="**Plan**\nlook around", elapsed_ms=671)
    messages = [_turn().messages[0], MessageView(id="a", role="assistant", event_seq=2, blocks=[thinking])]
    tools = [ToolCallView(call_id="g", name="Grep", event_seq=3, status="completed", input={"pattern": "x"}),
             ToolCallView(call_id="r", name="Read", event_seq=3, status="completed", input={"path": "a.py"})]
    blocks = _snapshot(tmp_path, monkeypatch, [replace(_turn(), messages=messages, tools=tools)], literal=False,
                       expanded={"t:activity:athinking"})["blocks"]
    group = next(b for b in blocks if b["kind"] == "tool_group")
    assert group["members"][0]["title"] == "Thought: 671ms"
    assert group["text"] == "Thought 1 time · Searched 1 time · Read 1 file"
    assert [m["heading"].split(" ")[0] for m in group["members"][1:]] == ["✱", "→"]


def test_local_transcript_supplies_hidden_content_without_python_expansion(tmp_path, monkeypatch):
    output = "\n".join(f"line {i}" for i in range(30))
    turn = replace(_turn(), tools=[ToolCallView(call_id="long", name="Bash", event_seq=2,
                   status="completed", input={"command": "ls"}, display=output)])
    def snapshot(expanded):
        return _snapshot(tmp_path, monkeypatch, [turn], literal=False,
                         local_transcript=True, expanded=expanded)
    blocks = snapshot(set())["blocks"]
    group = next(b for b in blocks if b["kind"] == "tool_group")
    assert group["local_ui"] and group["collapsed"]
    member = group["members"][0]
    assert "line 29" in member["local_detail"]
    assert member["fold_lines"] > 0
    assert "local_preview" not in member
    assert member["detail"] == ""
    assert member["members"] == []
    assert all(b["turn_id"] == "t" for b in blocks)
    assert blocks == snapshot({"unused"})["blocks"]


def test_disconnected_flag_tracks_the_notice(tmp_path, monkeypatch):
    connected = _snapshot(tmp_path, monkeypatch, [_turn()])
    assert connected["disconnected"] is False
    offline = _snapshot(
        tmp_path, monkeypatch, [_turn()],
        notice="Disconnected: use /reconnect to replay and reattach",
    )
    assert offline["disconnected"] is True


def test_folded_turn_summary_lists_tool_calls_tokens_and_model():
    from nexus.ui.ratatui.prototype import _fold_summary
    from nexus.view.model import MessageView, ToolCallView, TurnView, UsageTotals

    def turn(tools, usage, model):
        return TurnView(
            id="t", index=0, phase="completed",
            messages=[MessageView(id="a", role="assistant", event_seq=2, model=model, provider="p" if model else None)],
            tools=[ToolCallView(call_id=str(i), name="Read", event_seq=3 + i, status="completed") for i in range(tools)],
            usage=usage,
        )

    assert _fold_summary(turn(2, UsageTotals(input_tokens=1000, output_tokens=234), "gpt")) == "2 tools · 1.2K tokens · gpt"
    assert _fold_summary(turn(1, UsageTotals(), None)) == "1 tool"
    assert _fold_summary(turn(0, UsageTotals(), None)) == "0 tools"
