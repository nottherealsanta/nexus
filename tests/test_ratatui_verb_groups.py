"""Verb groups: membership pinned, labels, failures, stable identity (GROK_BUILD_TUI_PLAN §4)."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project
from nexus.view import initial_state
from nexus.view.model import BlockView, MessageView, ToolCallView, TurnView


def _snapshot(tmp_path, monkeypatch, turn, **shell_values):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    view.turns = [turn]
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    for key, value in shell_values.items():
        setattr(shell, key, value)
    return project(controller, 1, shell=shell, literal=False)


def _turn(tools, messages=(), phase="completed"):
    user = MessageView(id="u", role="user", event_seq=1, blocks=[BlockView(text="go")])
    return TurnView(id="t", index=0, phase=phase, messages=[user, *messages], tools=list(tools))


def _tool(call_id, name, seq, status="completed", **kwargs):
    return ToolCallView(call_id=call_id, name=name, event_seq=seq, status=status, **kwargs)


def _thought(message_id, seq, ms=None, text="Consider."):
    return MessageView(id=message_id, role="assistant", event_seq=seq,
                       blocks=[BlockView(kind="thinking", text=text, elapsed_ms=ms)])


def _reply(message_id, seq):
    return MessageView(id=message_id, role="assistant", event_seq=seq, blocks=[BlockView(text="Done.")])


def _shape(blocks):
    """Kind, id and member ids of each transcript block after the prompt card."""
    shape = []
    for block in blocks:
        if block["kind"] in {"user", "summary"}:
            continue
        members = [m["id"] for m in block.get("members", [])] if block["kind"] == "tool_group" else []
        shape.append((block["kind"], block["id"], block.get("count"), members))
    return shape


def test_membership_is_pinned(tmp_path, monkeypatch):
    """Which blocks group together; recorded before the code moved out of prototype.py (D2)."""
    tools = [
        _tool("r1", "read", 2, input={"path": "a"}),
        _tool("b1", "bash", 4, input={"command": "ls"}),
        _tool("sub", "subagent", 5, input={"description": "scan"}),
        _tool("e1", "edit", 6, input={"path": "a"}),
        _tool("g1", "grep", 8, input={"pattern": "x"}),
        _tool("q1", "question", 9, input={"questions": [{"question": "?"}]}),
        _tool("w1", "webfetch", 11, input={"url": "http://x"}),
    ]
    messages = [_thought("th", 3), _reply("rep", 7), _thought("th2", 10)]
    blocks = _snapshot(tmp_path, monkeypatch, _turn(tools, messages), verbose=True)["blocks"]
    assert _shape(blocks) == [
        ("tool_group", "t:activity:r1", 3, ["r1", "ththinking", "b1"]),
        ("task", "sub", None, []),
        ("change", "e1", None, []),
        ("markdown", "reptext", None, []),
        ("tool_group", "t:activity:g1", 1, ["g1"]),
        ("change", "q1", None, []),
        ("tool_group", "t:activity:th2thinking", 2, ["th2thinking", "w1"]),
    ]


def _group(tmp_path, monkeypatch, tools, messages=(), phase="completed", **shell_values):
    blocks = _snapshot(tmp_path, monkeypatch, _turn(tools, messages, phase), **shell_values)["blocks"]
    return next(b for b in blocks if b["kind"] == "tool_group")


@pytest.mark.parametrize("name, args, past, present", [
    ("bash", {"command": "ls"}, "Ran 1 command", "Running 1 command"),
    ("read", {"path": "a"}, "Read 1 file", "Reading 1 file"),
    ("grep", {"pattern": "x"}, "Searched 1 pattern", "Searching 1 pattern"),
    ("glob", {"pattern": "*.py"}, "Searched 1 pattern", "Searching 1 pattern"),
    ("ls", {"path": "."}, "Listed 1 dir", "Listing 1 dir"),
    ("webfetch", {"url": "http://x"}, "Fetched 1 website", "Fetching 1 website"),
    ("websearch", {"query": "q"}, "Searched 1 website", "Searching 1 website"),
    ("McpSearch", {"queries": ["q"]}, "Searched 1 MCP tool", "Searching 1 MCP tool"),
    ("McpCall", {"tool": "s/t"}, "Called 1 MCP tool", "Calling 1 MCP tool"),
    ("frobnicate", {}, "Frobnicate 1 call", "Frobnicate 1 call"),
])
def test_label_is_past_when_finished_and_present_while_running(tmp_path, monkeypatch, name, args, past, present):
    done = _group(tmp_path, monkeypatch, [_tool("c", name, 2, input=args)])
    assert done["text"] == past and done["running_label"] == "" and done["status"] == "completed"
    live = _group(tmp_path, monkeypatch, [_tool("c", name, 2, status="running", input=args)], phase="active")
    assert live["status"] == "running" and live["running_label"] == "Working"
    assert live["text"] == past, "text is the stable past-tense label"


def test_label_joins_categories_with_commas_and_a_running_group_says_working(tmp_path, monkeypatch):
    tools = [_tool("a", "bash", 2, input={"command": "ls"}), _tool("b", "bash", 3, input={"command": "pwd"}),
             _tool("c", "read", 4, status="running", input={"path": "x"})]
    thought = _thought("th", 5, ms=1200)
    group = _group(tmp_path, monkeypatch, tools, [thought], phase="active")
    assert group["text"] == "Ran 2 commands, Read 1 file"
    assert group["running_label"] == "Working"


def _reads(count, **kwargs):
    return [_tool(f"r{i}", "read", 2 + i, input={"path": f"f{i}"}, **kwargs) for i in range(count)]


def test_label_drops_nouns_above_five_members_and_keeps_them_at_five(tmp_path, monkeypatch):
    five = _reads(3) + [_tool("g1", "grep", 20, input={"pattern": "x"}), _tool("b1", "bash", 21, input={"command": "ls"})]
    assert _group(tmp_path, monkeypatch, five)["text"] == "Read 3 files, Searched 1 pattern, Ran 1 command"
    six = _reads(4) + [_tool("g1", "grep", 20, input={"pattern": "x"}), _tool("g2", "glob", 21, input={"pattern": "*"}),
                       _tool("b1", "bash", 22, input={"command": "ls"})]
    assert _group(tmp_path, monkeypatch, six)["text"] == "Read 4, Searched 2, Ran 1"


def test_change_tools_never_share_a_run_with_other_tools(tmp_path, monkeypatch):
    tools = [_tool("r1", "read", 2, input={"path": "a"}), _tool("e1", "edit", 3, input={"path": "a"}),
             _tool("e2", "write", 4, input={"path": "b", "content": "x"}), _tool("e3", "apply_patch", 5, input={}),
             _tool("g1", "grep", 6, input={"pattern": "x"}), _tool("e4", "edit", 7, input={"path": "c"}),
             _tool("b1", "bash", 8, input={"command": "ls"})]
    blocks = _snapshot(tmp_path, monkeypatch, _turn(tools), verbose=True)["blocks"]
    assert _shape(blocks) == [
        ("tool_group", "t:activity:r1", 1, ["r1"]),
        ("tool_group", "t:activity:e1", 3, ["e1", "e2", "e3"]),
        ("tool_group", "t:activity:g1", 1, ["g1"]),
        ("change", "e4", None, []),
        ("tool_group", "t:activity:b1", 1, ["b1"]),
    ]
    changes = blocks[2]
    assert changes["text"] == "Edited 1 file, Wrote 1 file, Applied 1 patch"
    assert all(m["kind"] == "tool" and "_member" not in m for m in changes["members"])
    assert not any("_member" in b for b in blocks)


def test_a_thought_ends_a_change_run(tmp_path, monkeypatch):
    tools = [_tool("e1", "edit", 2, input={"path": "a"}), _tool("e2", "edit", 4, input={"path": "b"})]
    blocks = _snapshot(tmp_path, monkeypatch, _turn(tools, [_thought("th", 3)]))["blocks"]
    assert [b["kind"] for b in blocks if b["kind"] in {"change", "thought", "tool_group"}] == ["change", "thought", "change"]


def test_running_group_sends_the_last_five_members_and_hidden(tmp_path, monkeypatch):
    tools = _reads(6)
    tools[-1] = replace(tools[-1], status="running")
    group = _group(tmp_path, monkeypatch, tools, phase="active")
    assert [m["id"] for m in group["members"]] == ["r1", "r2", "r3", "r4", "r5"]
    assert group["hidden"] == 1 and group["count"] == 6 and group["collapsed"]
    done = _group(tmp_path, monkeypatch, _reads(6))
    assert done["members"] == [] and done["hidden"] == 0
    opened = _group(tmp_path, monkeypatch, tools, phase="active", expanded={"t:activity:r0"})
    assert len(opened["members"]) == 6 and opened["hidden"] == 0


def test_streaming_flag_marks_only_an_unfinished_assistant_text(tmp_path, monkeypatch):
    live = MessageView(id="a", role="assistant", event_seq=2, blocks=[BlockView(text="Hel")])
    blocks = _snapshot(tmp_path, monkeypatch, _turn([], [live], phase="active"))["blocks"]
    assert next(b for b in blocks if b["kind"] == "markdown")["streaming"] is True
    finished = replace(live, done=True)
    blocks = _snapshot(tmp_path, monkeypatch, _turn([], [finished], phase="active"))["blocks"]
    assert "streaming" not in next(b for b in blocks if b["kind"] == "markdown")
    blocks = _snapshot(tmp_path, monkeypatch, _turn([], [live]))["blocks"]
    assert "streaming" not in next(b for b in blocks if b["kind"] == "markdown")

def test_member_summaries_are_concise_first_line_with_an_ellipsis(tmp_path, monkeypatch):
    tools = [_tool("a", "bash", 2, input={"command": "cd src\nmake all\nmake test"}),
             _tool("b", "read", 3, input={"path": "a.py", "offset": 10, "limit": 20})]
    from nexus.ui_support.timeline import tool_heading
    assert tool_heading(tools[0]) == "$ cd src…"
    assert tool_heading(tools[1]).endswith("a.py (offset=10, limit=20)")
    assert tool_heading(_tool("c", "bash", 4, input={"command": "ls"})) == "$ ls"


def test_external_worker_tools_keep_their_owner_label(tmp_path, monkeypatch):
    tool = _tool("c", "bash", 2, input={"command": "ls"}, provenance="Claude Code (own tools)")
    assert _group(tmp_path, monkeypatch, [tool])["text"] == "Claude Code ran 1 tool"


def test_wall_time_and_failures_are_separate_fields(tmp_path, monkeypatch):
    tools = [_tool("a", "read", 2, input={"path": "a"}, started_ts=100.0, finished_ts=101.0),
             _tool("b", "bash", 3, status="failed", error="boom", input={"command": "false"},
                   started_ts=101.0, finished_ts=101.8)]
    group = _group(tmp_path, monkeypatch, tools)
    assert group["text"] == "Read 1 file, Ran 1 command"
    assert (group["started_ms"], group["elapsed_ms"], group["elapsed_label"]) == (100_000, 1_800, "1.8s")
    assert group["failures"] == 1
    assert group["collapsed"] and group["members"] == []
    # A failure never opens the group; Rust draws only the header count while folded.
    assert group["collapsed"] and group["failures"] == 1
    assert [m["id"] for m in group["failed_members"]] == ["b"]
    assert group["failed_members"][0]["inspect_operation"] == {"kind": "tool_details", "id": "b"}


def test_running_group_has_no_elapsed_and_names_the_live_member(tmp_path, monkeypatch):
    tools = [_tool("a", "read", 2, input={"path": "a"}, started_ts=10.0, finished_ts=11.0),
             _tool("b", "read", 3, status="running", input={"path": "b"}, started_ts=11.0)]
    group = _group(tmp_path, monkeypatch, tools, phase="active")
    assert group["started_ms"] == 10_000 and group["elapsed_ms"] == 0 and group["elapsed_label"] == ""
    assert group["live_member"]["id"] == "b" and group["failed_members"] == []


def test_group_identity_is_stable_while_streaming(tmp_path, monkeypatch):
    tools = [_tool("a", "read", 2, input={"path": "a"}), _tool("b", "read", 3, status="running", input={"path": "b"})]
    first = _group(tmp_path, monkeypatch, tools[:1], phase="active")
    later = _group(tmp_path, monkeypatch, tools, phase="active")
    done = _group(tmp_path, monkeypatch, [tools[0], replace(tools[1], status="completed")])
    assert first["id"] == later["id"] == done["id"] == "t:activity:a"
    assert (first["count"], later["count"]) == (1, 2)


def test_opened_group_keeps_failed_members_in_members_too(tmp_path, monkeypatch):
    tools = [_tool("a", "read", 2, input={"path": "a"}), _tool("b", "bash", 3, status="failed", error="x", input={"command": "q"})]
    group = _group(tmp_path, monkeypatch, tools, expanded={"t:activity:a"})
    assert not group["collapsed"] and [m["id"] for m in group["members"]] == ["a", "b"]
    assert [m["id"] for m in group["failed_members"]] == ["b"]


def test_closed_members_name_the_target_and_not_the_output(tmp_path, monkeypatch):
    text = lambda value: [{"type": "text", "text": value}]
    command = "cargo test --workspace --all-features --no-fail-fast -- --nocapture"
    tools = [_tool("r", "read", 2, input={"path": "a.py"}, result=text("import os\nx = 1\ny = 2")),
             _tool("b", "bash", 3, input={"command": command}, result=text("job_id: j\nstatus: completed\nexit_code: 0\nstdout:\nok"),
                   started_ts=10.0, finished_ts=12.5),
             _tool("g", "bash", 4, status="running", input={"command": "sleep 9"}, started_ts=20.0)]
    group = _group(tmp_path, monkeypatch, tools, expanded={"t:activity:r"})
    read, bash, live = group["members"]
    assert read["text"] == "a.py (lines 1–3)" and "import os" not in read["text"]
    assert bash["text"].startswith("cargo test --workspace") and bash["text"].endswith("…")
    assert bash["text"] != command and "ok" not in bash["text"] and "·" not in bash["text"]
    assert bash["elapsed_ms"] == 2_500 and bash["started_ms"] == 10_000
    assert live["text"] == "sleep 9" and live["status"] == "running" and live["started_ms"] == 20_000
    assert live["elapsed_ms"] == 0


def test_thoughts_alone_are_not_a_group(tmp_path, monkeypatch):
    blocks = _snapshot(tmp_path, monkeypatch, _turn([], [_thought("th", 2, ms=3200), _reply("rep", 3)]))["blocks"]
    assert not any(b["kind"] == "tool_group" for b in blocks)
    thought = next(b for b in blocks if b["kind"] == "thought")
    assert thought["id"] == "ththinking" and thought["title"] == "Thought for 3.2s"
    assert (thought["status"], thought["elapsed_ms"]) == ("completed", 3200)


def test_live_thought_is_thinking_with_start_time(tmp_path, monkeypatch):
    message = MessageView(id="th", role="assistant", event_seq=2,
                          blocks=[BlockView(kind="thinking", text="hmm", started_ts=50.5)])
    blocks = _snapshot(tmp_path, monkeypatch, _turn([], [message], phase="active"))["blocks"]
    thought = next(b for b in blocks if b["kind"] == "thought")
    assert thought["status"] == "running" and thought["title"] == "Thinking…"
    assert thought["started_ms"] == 50_500 and thought["elapsed_ms"] == 0


def test_a_thought_in_a_run_with_tools_stays_a_member(tmp_path, monkeypatch):
    group = _group(tmp_path, monkeypatch, [_tool("a", "read", 3, input={"path": "a"})], [_thought("th", 2, ms=90_000)],
                   verbose=True)
    thought = group["members"][0]
    assert thought["kind"] == "thought" and thought["title"] == "Thought for 1m30s"


def test_tool_blocks_offer_the_full_details_operation(tmp_path, monkeypatch):
    from nexus.ui.ratatui.prototype import _block_operations
    snapshot = _snapshot(tmp_path, monkeypatch, _turn([_tool("a", "read", 2, status="failed", error="no", input={"path": "a"}),
                                                       _tool("e", "edit", 3, input={"path": "a"})]))
    operations = list(_block_operations(snapshot["blocks"]))
    assert {"kind": "tool_details", "id": "a"} in operations, "allowed even through failed_members"
    assert {"kind": "tool_details", "id": "e"} in operations


def test_expanded_tool_carries_relevant_only_signal_and_keeps_full_details(tmp_path, monkeypatch):
    tool = _tool("a", "bash", 2, input={"command": "echo hi"}, display="hi", started_ts=1.0, finished_ts=2.0)
    group = _group(tmp_path, monkeypatch, [tool], local_transcript=True)
    member = group["members"][0]
    assert member["signal"] == [["code", "$ echo hi"], ["out", "hi"]]
    assert "Call ID: a" in member["local_detail"], "the full labelled details are unchanged"
    assert all("Call ID" not in text for _, text in member["signal"])

@pytest.mark.parametrize("boundary", ["none", "reply", "changes", "completed"])
def test_completed_tool_keeps_trailing_active_group_working(tmp_path, monkeypatch, boundary):
    tools = [_tool("r1", "read", 2, input={"path": "a"})]
    messages = [_reply("rep", 3)] if boundary == "reply" else []
    if boundary == "changes":
        tools.append(_tool("e1", "edit", 3, input={"path": "a"}))
    turn = _turn(tools, messages, phase="completed" if boundary == "completed" else "active")
    blocks = _snapshot(tmp_path, monkeypatch, turn)["blocks"]
    group = next(b for b in blocks if b["id"] == "t:activity:r1")
    assert group["status"] == ("running" if boundary == "none" else "completed")
    assert group["running_label"] == ("Working" if boundary == "none" else "")
