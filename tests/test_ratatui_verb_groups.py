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
    assert live["status"] == "running" and live["running_label"] == present
    assert live["text"] == past, "text is the stable past-tense label"


def test_label_joins_categories_with_commas_and_present_tense_is_per_category(tmp_path, monkeypatch):
    tools = [_tool("a", "bash", 2, input={"command": "ls"}), _tool("b", "bash", 3, input={"command": "pwd"}),
             _tool("c", "read", 4, status="running", input={"path": "x"})]
    thought = _thought("th", 5, ms=1200)
    group = _group(tmp_path, monkeypatch, tools, [thought], phase="active")
    assert group["text"] == "Ran 2 commands, Read 1 file, Thought 1 time"
    assert group["running_label"] == "Ran 2 commands, Reading 1 file, Thought 1 time"


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
    assert [m["id"] for m in group["failed_members"]] == ["b"], "a failure stays visible under a folded header"
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
