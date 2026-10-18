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
def test_agent_color_marks_the_agent_not_the_prompt_card(tmp_path, monkeypatch, color):
    snapshot = _snapshot(tmp_path, monkeypatch, [_turn(agent={"name": "review", "color": color})])
    user = next(block for block in snapshot["blocks"] if block["kind"] == "user")
    assert "color" not in user, "the prompt card rail is grey (GROK_BUILD_TUI_PLAN §8B)"
    assert snapshot["agent_color"]


def test_handoff_divider_keeps_earlier_cards_and_stable_identity(tmp_path, monkeypatch):
    from nexus.view.model import ContextView
    turn = _turn()
    turn.context = [ContextView(kind="handoff", data={"head_seq": 4})]
    first = _snapshot(tmp_path, monkeypatch, [turn], literal=False)
    second = _snapshot(tmp_path, monkeypatch, [turn], literal=False)
    divider = next(b for b in first["blocks"] if b.get("title") == "Context handed off")
    assert divider["id"] == "t:handoff:5"
    assert any(b["kind"] == "user" for b in first["blocks"])
    assert any(b.get("text") == "Hello!" for b in first["blocks"])
    assert divider in second["blocks"]


def test_no_transcript_block_names_the_agent(tmp_path, monkeypatch):
    turns = [_turn(agent={"name": "review"}), replace(_turn(agent={"name": "plan"}), id="t2", index=1)]
    snapshot = _snapshot(tmp_path, monkeypatch, turns)
    assert not any(block["kind"] == "agent" for block in snapshot["blocks"])
    assert not any(block.get("color") for block in snapshot["blocks"] if block["kind"] in {"user", "tool_group", "context_header"})


@pytest.mark.parametrize("prompt", ["界" * 4000, "\n".join(["line"] * 100)])
def test_user_prompt_renders_beyond_old_limit(tmp_path, monkeypatch, prompt):
    turn = _turn()
    turn.messages[0].blocks = [BlockView(text=prompt)]
    snapshot = _snapshot(tmp_path, monkeypatch, [turn], literal=False)
    user = next(block for block in snapshot["blocks"] if block["kind"] == "user")
    assert user["title"] + ("\n" + user["text"] if user["text"] else "") == prompt


@pytest.mark.parametrize("prompt, preview", [
    ("界" * 16_001, "界" * 16_000),
    ("\n".join(["line"] * 201), "\n".join(["line"] * 200)),
    ("x" * 16_000, "x" * 16_000),
])
def test_user_prompt_announces_display_limits(tmp_path, monkeypatch, prompt, preview):
    turn = _turn()
    turn.messages[0].blocks = [BlockView(text=prompt)]
    snapshot = _snapshot(tmp_path, monkeypatch, [turn], literal=False)
    user = next(block for block in snapshot["blocks"] if block["kind"] == "user")
    expected = preview
    if preview != prompt:
        expected += f"\n... ({len(prompt):,} characters total)"
    assert user["title"] + ("\n" + user["text"] if user["text"] else "") == expected
    assert turn.messages[0].text == prompt


def test_live_activity_has_no_preview_window_the_running_tree_replaced_it(tmp_path, monkeypatch):
    tools = [ToolCallView(call_id=f"read-{i}", name="read", event_seq=i + 2,
                         status="completed", input={"path": f"{i}.py"}) for i in range(8)]
    turn = replace(_turn(), phase="active", messages=[_turn().messages[0]], tools=tools)
    def activity(value):
        return next(b for b in _snapshot(tmp_path, monkeypatch, [value],
                    local_transcript=True)["blocks"] if b["kind"] == "tool_group")
    live = activity(turn)
    assert "preview_limit" not in live
    assert live["text"] == "Read 8", "more than five members drop their nouns"
    assert len(live["members"]) == 8  # Rust bounds the running tree, never the full payload.
    assert "preview_limit" not in activity(replace(turn, phase="completed"))
    assert activity(replace(turn, tools=tools[:2]))["id"] == live["id"]


def test_parallel_tools_render_without_gutter_markers(tmp_path, monkeypatch):
    tools = [
        ToolCallView(call_id="a", name="read", event_seq=2, iteration=1,
                     status="completed", input={"path": "README.md"}),
        ToolCallView(call_id="b", name="subagent", event_seq=3, iteration=1,
                     status="running", input={"description": "Scan"}),
        ToolCallView(call_id="c", name="grep", event_seq=4, iteration=2,
                     status="completed", input={"pattern": "main"}),
    ]
    snapshot = _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=tools)])
    blocks = [b for b in snapshot["blocks"] if b["kind"] in {"task", "tool", "tool_group"}]
    assert [b["kind"] for b in blocks] == ["tool_group", "task", "tool_group"]
    assert [b["count"] for b in blocks if b["kind"] == "tool_group"] == [1, 1]
    assert all(not b["text"].startswith(("┌", "│", "└")) for b in blocks)
    assert blocks[1]["text"] == ""  # task cards keep the compatible empty body
    assert "Subagent — Scan" in blocks[1]["title"]
    assert blocks[1]["gap"] == blocks[2]["gap"] == 1


def test_blocks_follow_event_order_with_textual_gaps(tmp_path, monkeypatch):
    blocks = _snapshot(tmp_path, monkeypatch, [_turn(), replace(_turn(), id="t2", index=1)])["blocks"]
    kinds = [block["kind"] for block in blocks[:4]]
    assert kinds == ["user", "tool_group", "markdown", "summary"]
    user, tool, reply, summary = blocks[:4]
    assert user["number"] == 1 and user["title"] == "hi there" and user["text"] == "second line"
    assert tool["gap"] == 1 and tool["text"] == "Read 1 file"  # margin after the prompt card
    assert reply["gap"] == 1  # a reply after a tool call is set apart
    assert summary["gap"] == 1 and "3.3s" in summary["text"]  # a blank row above the turn footer
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
    assert transcript[0]["text"] == "Read 2 files"
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
    assert all("color" not in block for block in header), "headers are grey, never the agent colour"
    assert "color" not in next(block for block in snapshot["blocks"] if block["kind"] == "context_header")
    assert all(block["gap"] == 0 for block in header), "closed headers stack with no blank rows"
    assert header[4]["operation"] == {"kind": "context_show", "key": "mcp"}
    assert header[4]["inspect_operation"] == {"kind": "context_show", "key": "mcp"}
    # Not loaded yet: every header is inactive, tokens unknown, and the body is marked closed-local.
    assert all(block["tokens"] == "~?" and block["active"] is False for block in header)
    assert all(block["local_ui"] is True and block["local_open"] is False for block in header)


def test_context_header_tokens_and_activity(tmp_path, monkeypatch):
    from nexus.ui.ratatui.prototype import _compact_header
    from nexus.ui_support import context_header
    blocks = [
        context_header.HeaderBlock("system", "System prompt", "You are", "", 348, "#fff"),
        context_header.HeaderBlock("agents", "AGENTS.md", "# AGENTS.md", "", 2500, "#fff"),
        context_header.HeaderBlock("memory", "MEMORY.md", "", "", 0, "#fff"),
        context_header.HeaderBlock("tools", "Tools", "", "", None, "#fff", inventory=("read", "bash")),
        context_header.HeaderBlock("mcp", "MCP", "", "", None, "#fff"),
    ]
    monkeypatch.setattr(context_header, "header_blocks", lambda preview, color: blocks)
    preview = SimpleNamespace(tools=[], skills_index=[], mcp_servers=[], agent={"name": "build"})
    shell = SimpleNamespace(preview=preview, controller=SimpleNamespace(agent_name="build"), agent_definitions={})
    [header] = _compact_header(shell, None)
    rows = {m["title"]: m for m in header["members"]}
    assert [rows[t]["tokens"] for t in rows] == ["~348", "~2.5K", "~0", "~?", "~?"]
    assert [rows[t]["active"] for t in rows] == [True, True, False, True, False]


def test_details_panel_matches_textual_sections(tmp_path, monkeypatch):
    panel = _snapshot(tmp_path, monkeypatch, [_turn()], mcp_report={"mcp": {"servers": [
        {"name": "cvc", "health": "ready", "tool_count": 4}]}})["details_panel"]
    labels = [label for label, _ in panel["session"]]
    assert labels[:8] == ["ID", "Title", "Status", "Agent", "Model", "Effort", "Turns", "Tool calls"]
    assert panel["files"] == [] and panel["files_summary"] == ""
    assert panel["mcp"] == [["success", "cvc", "4 tools · search"]]


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
    assert card["kind"] == "task"
    assert card["title"] == "Explore Subagent — Scan"
    assert (card["model"], card["tier"]) == ("model pending", "")
    assert (card["tool_calls"], card["tokens"]) == (0, 0)
    assert card["status"] == "running"
    assert card["text"] == ""
    assert card["gap"] == 1
    assert card["operation"] == {"kind": "agent_page", "id": "a1"}
    assert [block["id"] for block in blocks if block["id"] in {"a1", "a2"}] == ["a2"]


def _agent(**kwargs):
    from nexus.view.model import AgentView
    return AgentView(id="a1", type="explore", description="Scan", **kwargs)


def test_agent_card_tier_letter_tokens_and_unknown_usage():
    from nexus.ui.ratatui.prototype import _agent_card_metrics, _agent_card_model
    from nexus.view.model import UsageTotals
    for tier in ("low", "medium", "high", None):
        assert _agent_card_model(_agent(tier=tier))[2] == (tier or "")
    assert _agent_card_model(_agent(requested_tier="high"))[2] == "high"
    agent = _agent(tier="low", usage=UsageTotals(input_tokens=1000, output_tokens=234))
    agent.body.turns = [TurnView(id="ct", index=0, tools=[ToolCallView(call_id="x", name="read", event_seq=2)])]
    assert _agent_card_metrics(agent) == (1, 1234)
    agent.usage_unknown = True  # an external worker reports no accounting: unknown, not zero
    assert _agent_card_metrics(agent) == (1, None)
    assert _agent_card_metrics(None) == (None, None)


def _external_agent_events(provider, *, finish):
    from nexus.events import Event
    meta = {"id": "s1/sub/1", "parent": "s1", "depth": 1, "type": provider, "task": "task-1", "session": "s1/sub/1"}
    prov = f"{provider} (own tools)"
    child = "s1/sub/1"
    rows = [("agent.spawned", {"agent": meta, **meta}, "s1"),
            ("external.started", {"agent": meta, "provider": provider}, child)]
    for call in ("a", "b"):
        for phase in ("requested", "completed"):
            rows.append(("external.tool", {"agent": meta, "phase": phase, "call_id": call, "tool": "Read",
                                           "provenance": prov, **({"is_error": False, "content": []} if phase == "completed" else {})}, child))
    if finish:
        rows.append(("external.finished", {"agent": meta, "status": "completed", "tool_calls": 2, "usage": "unknown"}, child))
        rows.append(("agent.completed", {"agent": meta, **meta, "status": "completed", "ok": True, "is_error": False,
                                         "usage": {}, "usage_unknown": True}, "s1"))
    return [Event(type=t, data=d, seq=i + 1, ts=float(i + 1), session=s, turn="t1", id=f"e{i:03d}")
            for i, (t, d, s) in enumerate(rows)]


def test_external_agent_card_counts_own_tools_separately_for_both_providers():
    from nexus.ui.ratatui.prototype import _agent_card_external_tools, _agent_card_metrics
    from nexus.view import fold
    for provider in ("claude-code", "antigravity"):
        for finish in (False, True):
            events = _external_agent_events(provider, finish=finish)
            for log in (events, events + events):  # replay is idempotent: no double counting
                agent = fold(log).agents["s1/sub/1"]
                assert _agent_card_external_tools(agent) == 2
                assert _agent_card_metrics(agent)[0] is None  # native count stays unreported
    assert _agent_card_external_tools(_agent()) is None  # native agents keep `tool_calls`
    assert _agent_card_external_tools(None) is None
    started_only = fold(_external_agent_events("claude-code", finish=False)[:2]).agents["s1/sub/1"]
    assert _agent_card_external_tools(started_only) is None


def test_project_task_card_carries_external_tool_calls_for_external_workers(tmp_path, monkeypatch):
    from nexus.view import fold
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for provider in ("claude-code", "antigravity"):
        view = fold(_external_agent_events(provider, finish=True))
        task = ToolCallView(call_id="task-1", name="Task", event_seq=2, status="completed",
                            input={"subagent_type": provider, "description": "Review"},
                            child_agent_ids=["s1/sub/1"])
        view.turns = [TurnView(id="t", index=0, phase="completed", messages=[
            MessageView(id="u", role="user", event_seq=1, blocks=[BlockView(text="go")])], tools=[task])]
        controller = SimpleNamespace(view=view, session="s1")
        shell = ShellActions(controller)
        shell.preferences.values["context_preview"] = False
        blocks = project(controller, 1, shell=shell)["blocks"]
        card = next(block for block in blocks if block["id"] == "task-1")
        assert card["kind"] == "task"
        assert card["external_tool_calls"] == 2
        assert card["tool_calls"] is None  # native count stays unreported

    # A native agent keeps `tool_calls` and reports no external count.
    native = _snapshot(tmp_path, monkeypatch, [_turn()])["blocks"]
    assert all(block.get("external_tool_calls") is None for block in native)


def test_agent_model_label_prefers_actual_over_tier_and_spawn_reference():
    from nexus.ui_support.timeline import agent_model_label
    actual = _agent(tier="low", model="openai/spawn-ref", resolved_model="anthropic/claude-x")
    assert agent_model_label(actual) == "tier low · anthropic/claude-x"
    # A tier name is never presented as the model; the spawn reference is only "requested".
    pending = _agent(tier="low", model="openai/spawn-ref")
    assert agent_model_label(pending) == "tier low · model pending (requested openai/spawn-ref)"
    assert agent_model_label(_agent(tier="low", model="low")) == "tier low · model pending"
    assert agent_model_label(_agent(tier="low", status="completed")) == "tier low · model unavailable"
    assert agent_model_label(_agent(requested_tier="high")) == "tier high · model pending"
    assert agent_model_label(None) == "tier unknown · model pending"


def test_agent_model_label_falls_back_to_child_turn_metadata():
    from nexus.ui_support.timeline import agent_model_label
    agent = _agent(tier="medium", status="completed")
    agent.body.turns = [TurnView(id="ct", index=0, messages=[
        MessageView(id="m", role="assistant", event_seq=2, provider="openai", model="gpt-6")])]
    assert agent_model_label(agent) == "tier medium · openai/gpt-6"


def _with_assistant(**message):
    agent = _agent(tier="medium", status="completed")
    agent.body.turns = [TurnView(id="ct", index=0, messages=[
        MessageView(id="m", role="assistant", event_seq=2, **message)])]
    return agent


def test_agent_actual_model_fallback_edge_cases():
    from nexus.ui_support.timeline import agent_actual_model, agent_model_label
    # Provider alone is not a concrete model.
    provider_only = _with_assistant(provider="openai")
    assert agent_actual_model(provider_only) is None
    assert agent_model_label(provider_only) == "tier medium · model unavailable"
    # An already provider-qualified model is not prefixed twice.
    assert agent_actual_model(_with_assistant(provider="openai", model="openai/gpt-6")) == "openai/gpt-6"
    # A bare model keeps working with or without a provider.
    assert agent_actual_model(_with_assistant(model="gpt-6")) == "gpt-6"
    # Provider-only newest message falls through to an older concrete one.
    agent = _with_assistant(provider="openai", model="gpt-5")
    agent.body.turns[0].messages.append(MessageView(id="m2", role="assistant", event_seq=3, provider="openai"))
    assert agent_actual_model(agent) == "openai/gpt-5"


def test_agent_model_label_running_status_is_pending():
    from nexus.ui_support.timeline import agent_model_label
    assert agent_model_label(_agent(tier="low", status="running")) == "tier low · model pending"
    assert agent_model_label(_agent(tier="low", status="failed")) == "tier low · model unavailable"


def test_task_card_and_child_page_disclose_actual_model(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view = initial_state("s")
    task = ToolCallView(call_id="task-1", name="Task", event_seq=2, status="running",
                        input={"subagent_type": "explore", "description": "Scan"}, child_agent_ids=["a1"])
    view.turns = [TurnView(id="t", index=0, phase="active", messages=[
        MessageView(id="u", role="user", event_seq=1, blocks=[BlockView(text="go")])], tools=[task])]
    child = _agent(tier="low", status="spawned", resolved_model="openai/gpt-6")
    child.body.turns = [TurnView(id="ct", index=0, reasoning_effort="high")]
    view.agents = {"a1": child}
    shell = ShellActions(SimpleNamespace(view=view, session="s"))
    shell.preferences.values["context_preview"] = False
    controller = SimpleNamespace(view=view, session="s")
    card = next(b for b in project(controller, 1, shell=shell)["blocks"] if b["id"] == "task-1")
    assert card["title"] == "Explore Subagent — Scan"
    assert (card["model"], card["effort"], card["tier"]) == ("gpt-6", "high", "low")
    shell.workflows.agent_page_id = "a1"
    page = project(controller, 2, shell=shell)
    assert page["model"] == "tier low · openai/gpt-6"
    assert ("Model", "tier low · openai/gpt-6") in [tuple(r) for r in page["details_panel"]["session"]]
    assert ("Effort", "high") in [tuple(r) for r in page["details_panel"]["session"]]


def test_cli_details_and_link_label_disclose_model():
    from nexus.ui.cli.details import detail_lines
    from nexus.ui_support.timeline import _agent_link_label
    view = initial_state("s")
    done = _agent(tier="low", status="completed", resolved_model="openai/gpt-6")
    view.agents = {"a1": done}
    view.agent_order = ["a1"]
    assert any("tier low · openai/gpt-6" in line for line in detail_lines("s", view))
    assert "tier low · openai/gpt-6" in _agent_link_label(done)
    view.agents = {"a1": _agent(tier="low", status="completed")}
    assert any("model unavailable" in line for line in detail_lines("s", view))


@pytest.mark.parametrize(
    ("status", "spawned", "completed"),
    [
        ("completed", 100.0, 165.0),
        ("running", 100.0, None),
        ("completed", None, None),
        ("failed", 100.0, 105.0),
    ],
)
def test_subagent_card_shows_counts_without_elapsed_time(tmp_path, monkeypatch, status, spawned, completed):
    from nexus.view.model import AgentView

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    tool = ToolCallView(
        call_id="tc", name="subagent", input={"description": "find code", "subagent_type": "explore"},
        status="running" if status == "running" else "completed", child_agent_ids=["kid"],
    )
    child = AgentView(
        id="kid", type="explore", status="spawned" if status == "running" else status,
        tier="low", resolved_model="openai/gpt-x", spawned_ts=spawned, completed_ts=completed,
    )
    child.body.turns = [TurnView(id="t", reasoning_effort="medium")]
    view = initial_state("s")
    view.turns = [replace(_turn(), tools=[tool])]
    view.agents = {"kid": child}
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    row = next(row for row in project(controller, 1, shell=shell)["blocks"] if row["kind"] == "task")
    assert row["title"] == "Explore Subagent — find code"
    assert (row["model"], row["effort"], row["tier"]) == ("gpt-x", "medium", "low")
    assert row["status"] == ("running" if status == "running" else "done")
    assert row["text"] == ""
    assert (row["tool_calls"], row["tokens"]) == (0, 0)


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
    assert rows["a"]["age"] == "5m"
    assert rows["a"]["last_user"] == "" and rows["a"]["last_agent"] == ""
    assert rows["d"]["title"] == "New Session"
    # The sidebar colors project and day separately in each "project · day" heading.
    assert rows["a"]["project"] == "w" and rows["a"]["group"] == f"w · {rows['a']['day']}"


def test_worktree_sessions_group_under_their_repository_after_the_main_checkout():
    from nexus.host.protocol import ProjectSession, ProjectSessionsListResult
    from nexus.session.manager import SessionSummary
    from nexus.ui.ratatui.workflows import session_rows

    def row(id, workspace, at, repo="", worktree=""):
        return ProjectSession(workspace, "p", SessionSummary(id=id, title=id, state="idle", last_activity=at),
                              repo=repo, worktree=worktree)

    result = ProjectSessionsListResult(sessions=[
        row("tree", "/w/nexus-feat", 900.0, repo="/w/nexus", worktree="feat/x"),
        row("main", "/w/nexus", 800.0),
        row("other", "/w/site", 850.0),
    ])
    rows = session_rows(result, now=1000.0)
    assert [r["id"] for r in rows] == ["main", "tree", "other"], "main checkout first, worktree beside it"
    tree = rows[1]
    assert tree["project"] == "nexus" and tree["worktree"] == "feat/x" and tree["repo"] == "/w/nexus"
    assert tree["workspace"] == "/w/nexus-feat", "opening still targets the worktree"
    assert tree["group"] == f"nexus › feat/x · {tree['day']}"
    assert rows[0]["repo"] == "/w/nexus" and rows[0]["worktree"] == ""


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
    change = next(b for b in _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=[edit])], verbose=True)["blocks"] if b["kind"] == "change")
    assert change["path"] == "a.py" and (change["added"], change["removed"]) == (1, 1)
    assert change["diff_lines"] == [["del", 1, "old"], ["add", 1, "new"]]


def test_tool_detail_tones_mirror_the_plain_text_and_the_textual_modal():
    from nexus.ui_support.tool_details import DetailRow, DetailSection, sections_to_text, styled_lines

    sections = [DetailSection("Parameters", (DetailRow("path", "a.py"), DetailRow("edits[0]", "", header=True, indent=0),
                                             DetailRow("old", "x\ny", block=True, indent=1))),
                DetailSection("Diff", (DetailRow("hunk", "@@ -1 +1 @@\n-a\n+b\n c", block=True),), kind="diff")]
    lines, tones = styled_lines(sections)
    assert "\n".join(lines) == sections_to_text(sections)
    assert tones == ["title", "kv", "header", "label", "", "", "", "title", "label", "hunk", "del", "add", ""]


def _running_view(queue, user=True):
    from types import SimpleNamespace
    turn = SimpleNamespace(messages=[SimpleNamespace(role="user")] if user else [])
    return SimpleNamespace(input_queue=queue, phase="running", active_turn=turn)


def test_idle_send_never_projects_as_a_queued_or_steer_item():
    """The head input of an idle send starts the turn; it is not waiting behind one."""
    from types import SimpleNamespace
    from nexus.ui.ratatui.prototype import _queue_items, _queue_lines
    from nexus.view.model import QueuedInputView

    def item(i):
        return QueuedInputView(queued_id=f"id{i}", mode="steer", content=[{"type": "text", "text": f"m{i}"}])

    idle = SimpleNamespace(input_queue=[item(0)], phase="idle", active_turn=None)
    starting = _running_view([item(0)], user=False)  # turn.started seen, input.consumed not yet
    for view in (idle, starting):
        assert _queue_items(view) == [] and _queue_lines(view) == []
    # A steer sent while the turn already has its user message is listed.
    assert [i["id"] for i in _queue_items(_running_view([item(1)]))] == ["id1"]


def test_session_parked_on_the_global_turn_cap_keeps_its_head_entry():
    """Idle + slot wait: nothing started, so the head is a waiting message, not the turn's own input."""
    from types import SimpleNamespace
    from nexus.ui.ratatui.prototype import _queue_items, _queue_lines
    from nexus.view.model import QueuedInputView

    head = QueuedInputView(queued_id="id0", mode="queue", content=[{"type": "text", "text": "held"}])
    idle = SimpleNamespace(input_queue=[head], phase="idle", active_turn=None)
    wait = {"waiting": 1, "running": 4, "limit": 4}
    assert [i["id"] for i in _queue_items(idle, wait)] == ["id0"]
    assert _queue_lines(idle, wait) == ["Queued · held"]
    assert _queue_items(idle, None) == [] and _queue_items(idle, {"waiting": 0}) == []


def test_controller_tracks_slot_wait_from_daemon_events_and_clears_it():
    from nexus.events import Event
    from nexus.ui_support.session_controller import SessionController

    controller = SessionController(object(), "s1")
    wait = {"waiting": 1, "running": 4, "limit": 4}
    controller.ingest(Event(type="daemon.session_queued", session="s1", data={"session": "s1", "slot_wait": wait}))
    assert controller.slot_wait == wait
    controller.ingest(Event(type="daemon.session_queued", session="other", data={"session": "other", "slot_wait": None}))
    assert controller.slot_wait == wait
    controller.ingest(Event(type="daemon.session_scheduled", session="s1", data={"session": "s1", "slot_wait": None}))
    assert controller.slot_wait is None


def test_queue_items_carry_durable_ids_and_s_q_tags():
    from nexus.ui.ratatui.prototype import _queue_items
    from nexus.view.model import QueuedInputView

    view = _running_view([])
    view.input_queue = [QueuedInputView(queued_id=f"id{i}", mode=mode, content=[{"type": "text", "text": f"msg\x1b {i}"}])
                        for i, mode in enumerate(["steer", "queue", "interrupt"])]
    items = _queue_items(view)
    assert [(item["id"], item["tag"]) for item in items] == [("id0", "S"), ("id1", "Q"), ("id2", "I")]
    assert all("\x1b" not in item["text"] and item["text"].endswith(str(i)) for i, item in enumerate(items))


def test_queue_items_move_flags_only_for_queued_items_between_queued_neighbours():
    from nexus.ui.ratatui.prototype import _queue_items
    from nexus.view.model import QueuedInputView

    view = _running_view([])
    view.input_queue = [QueuedInputView(queued_id=f"id{i}", mode=mode, content=[{"type": "text", "text": str(i)}])
                        for i, mode in enumerate(["queue", "steer", "interrupt", "queue", "steer", "queue", "queue"])]
    # The head is the turn's own input (unlisted) in an idle-without-user-message view; use a running
    # turn with a user message so every item waits.
    from nexus.view.model import MessageView
    view.active_turn.messages.append(MessageView(id="m", role="user", event_seq=1))
    flags = [(i["id"], i["can_up"], i["can_down"]) for i in _queue_items(view)]
    # Steering and interrupt items are never reordered by this client: no arrows at all.
    assert flags == [("id0", False, False), ("id1", False, False), ("id2", False, False), ("id3", False, False),
                     ("id4", False, False), ("id5", False, True), ("id6", True, False)]


async def test_queue_edit_promotes_and_restores_only_steering_content():
    from types import SimpleNamespace
    from nexus.ui.ratatui.prototype import queue_edit
    from nexus.view.model import MessageView, QueuedInputView

    calls = []

    class Client:
        async def queue_move(self, session, queued_id, offset):
            calls.append(("move", queued_id, offset))

        async def queue_remove(self, session, queued_id):
            calls.append(("remove", queued_id))

        async def queue_remove_content(self, session, queued_id):
            calls.append(("remove_content", queued_id))
            return True, [{"type": "text", "text": "full " + "x" * 300}]

        async def queue_set_mode(self, session, queued_id, mode):
            calls.append(("mode", queued_id, mode))
            return True

    view = _running_view([])
    view.active_turn.messages.append(MessageView(id="m", role="user", event_seq=1))
    view.input_queue = [QueuedInputView(queued_id=f"id{i}", mode=mode, content=[{"type": "text", "text": str(i)}])
                        for i, mode in enumerate(["steer", "queue", "queue"])]
    controller = SimpleNamespace(view=view, session="s", slot_wait=None)
    shell = SimpleNamespace(client=Client(), composer_restore="", composer_restore_if_empty="", composer_restore_note="")

    await queue_edit(shell, controller, "id1", "steer")
    await queue_edit(shell, controller, "id0", "interrupt")
    await queue_edit(shell, controller, "id1", "down")
    await queue_edit(shell, controller, "id0", "down")  # steering rows are never reordered
    await queue_edit(shell, controller, "gone", "remove")
    await queue_edit(shell, controller, "id1", "bogus")
    assert calls == [("mode", "id1", "steer"), ("mode", "id0", "interrupt"), ("move", "id1", 1)]

    calls.clear()
    await queue_edit(shell, controller, "id2", "remove")  # queued: removed, not restored
    assert calls == [("remove", "id2")] and shell.composer_restore == ""
    await queue_edit(shell, controller, "id0", "remove")
    assert calls[-1] == ("remove_content", "id0")
    assert shell.composer_restore == "full " + "x" * 300  # full text, not the 160-char cut
    assert shell.composer_restore_if_empty == shell.composer_restore
    assert shell.composer_restore_note == "restored from steer #1"


def test_queue_lines_and_meter_extras_follow_the_textual_status_row(tmp_path, monkeypatch):
    from nexus.ui.ratatui.prototype import _queue_lines
    from nexus.view.model import QueuedInputView

    view = _running_view([])
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
    assert window_tones == ["meter-warn", "meter-ok"]
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
    assert first["kind"] == "user" and first["title"] == "Inspect the UI" and "color" not in first
    assert snapshot["agent_color"]
    assert all(b["kind"] != "context_header" for b in snapshot["blocks"])
    assert shell.workflows.agent_context["system_text"] == "Child instructions"
    assert any(b["kind"] == "markdown" and b["text"] == "Hello!" for b in snapshot["blocks"])
    shell.workflows.back()
    assert shell.workflows.agent_page_id is None


def test_agent_page_never_repeats_root_context_counters(tmp_path, monkeypatch):
    from nexus.view.model import AgentView
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    view, body = initial_state("s"), initial_state("child")
    view.context = {"context": {"measured_tokens": 5000, "context_window": 10000}}
    body.turns = [_turn()]
    view.agents = {"child": AgentView(id="child", type="advisor", task="Inspect", body=body)}
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    assert project(controller, 1, shell=shell)["context_used"] == 5000
    shell.workflows.agent_page_id = "child"
    unknown = project(controller, 2, shell=shell)
    assert unknown["context_used"] is None and unknown["context_window"] is None
    assert unknown["context_usage"] == "0 (0%)" and unknown["context_label"] == ""
    body.context = {"context": {"measured_tokens": 300, "context_window": 1000}}
    known = project(controller, 3, shell=shell)
    assert known["context_used"] == 300 and known["context_window"] == 1000


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
    assert group["members"][0]["title"] == "Thought for 671ms"
    assert group["text"] == "Searched 1 pattern, Read 1 file"
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


def test_task_latest_activity_metrics_and_error_detail(tmp_path, monkeypatch):
    from nexus.ui_support.timeline import _task_child_activity, tool_heading
    from nexus.view.model import AgentView

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = ToolCallView(call_id="old", name="read", event_seq=2, status="completed", input={"path": "old.py"})
    latest = ToolCallView(call_id="new", name="bash", event_seq=3,
                          input={"command": "echo latest"},
                          progress=["stale", "fresh\n" + "x" * 150 + "\x1b[31m"])
    child = AgentView(id="kid", type="explore", status="spawned", error="private failure")
    child.body.turns = [TurnView(id="child-turn", tools=[latest, old])]
    activity = _task_child_activity(child)
    assert activity.startswith(tool_heading(latest) + " · fresh ")
    assert "x" * 150 in activity
    assert "stale" not in activity and "old.py" not in activity
    assert "\x1b" not in activity and "\n" not in activity
    task = ToolCallView(call_id="task", name="Task", event_seq=2, status="running",
                        input={"description": "Scan"}, child_agent_ids=["kid"])
    view = initial_state("s")
    view.turns = [replace(_turn(), tools=[task])]
    view.agents = {"kid": child}
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    blocks = project(controller, 1, shell=shell)["blocks"]
    card = next(b for b in blocks if b["kind"] == "task")
    assert card["text"] == "" and "fresh" not in str(card.values())  # the latest tool is never shown
    assert (card["tool_calls"], card["tokens"]) == (2, 0)
    assert card["detail"] == "Error: private failure"  # only the error, never the tool details
    shell.expanded.add("task:detail")
    shell.verbose = True
    card = next(b for b in project(controller, 1, shell=shell)["blocks"] if b["kind"] == "task")
    assert card["detail"] == "Error: private failure"
    assert "private failure" not in card["text"] + card["title"] + card["metrics"]
    child.status = "failed"
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    shell.expanded.add("task:detail")
    card = next(b for b in project(controller, 2, shell=shell)["blocks"] if b["kind"] == "task")
    assert card["status"] == "done" and card["text"] == ""
    assert "private failure" in card["detail"]


def test_edits_stand_alone_with_line_counts(tmp_path, monkeypatch):
    hunk = "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n-old\n+new\n+more\n keep"
    tools = [
        ToolCallView(call_id="r1", name="Read", event_seq=2, status="completed", input={"path": "a.py"}),
        ToolCallView(call_id="e", name="Edit", event_seq=3, status="completed", input={"path": "a.py"},
                     diff={"path": "a.py", "hunk": hunk}),
        ToolCallView(call_id="r2", name="Read", event_seq=5, status="completed", input={"path": "b.py"}),
        ToolCallView(call_id="r3", name="Grep", event_seq=6, status="completed", input={"pattern": "x"}),
    ]
    turn = replace(_turn(), tools=tools)
    blocks = [b for b in _snapshot(tmp_path, monkeypatch, [turn], local_transcript=True)["blocks"] if b["kind"] in {"tool_group", "change"}]
    assert [b["kind"] for b in blocks] == ["tool_group", "change", "tool_group"], "a lone edit is its own row"
    edit = blocks[1]
    assert edit["text"] == "Edit a.py" and edit["path"] == "a.py" and "members" not in edit
    assert (edit["added"], edit["removed"]) == (2, 1)
    assert edit["diff_lines"] == [["del", 1, "old"], ["add", 1, "new"], ["add", 2, "more"], ["ctx", 3, "keep"]]
    assert edit["local_open"] is False and edit["local_detail"] == "", "changes start closed"
    assert "_member" not in edit


def test_consecutive_edits_form_one_change_group(tmp_path, monkeypatch):
    hunk = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new"
    tools = [ToolCallView(call_id=f"e{i}", name="Edit", event_seq=4 + i, status="completed", input={"path": "a.py"},
                          diff={"path": "a.py", "hunk": hunk}) for i in range(3)]
    blocks = [b for b in _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=tools)], local_transcript=True)["blocks"]
              if b["kind"] in {"tool_group", "change"}]
    assert [b["kind"] for b in blocks] == ["tool_group"]
    group = blocks[0]
    assert group["id"].endswith(":activity:e0")
    assert group["text"] == "Edited 3 files" and group["count"] == 3
    assert [m["id"] for m in group["members"]] == ["e0", "e1", "e2"]
    assert all(m["kind"] == "tool" for m in group["members"])


def test_write_and_question_stand_alone(tmp_path, monkeypatch):
    tools = [
        ToolCallView(call_id="w", name="Write", event_seq=2, status="completed",
                     input={"path": "t.md", "content": "hello\nworld"}),
        ToolCallView(call_id="q", name="question", event_seq=3, status="completed",
                     input={"questions": [{"question": "Which?"}]}),
    ]
    blocks = [b for b in _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=tools)])["blocks"] if b["kind"] in {"tool_group", "change"}]
    assert [b["kind"] for b in blocks] == ["change", "change"]
    write, question = blocks
    assert (write["added"], write["removed"]) == (2, 0)
    assert write["collapsed"] and write["diff_lines"] == [], "a file change starts closed"
    assert not question["collapsed"] and question["diff_lines"] == [] and "Which?" in question["detail"]
    opened = [b for b in _snapshot(tmp_path, monkeypatch, [replace(_turn(), tools=tools)], expanded={"w:change"})["blocks"]
              if b["kind"] == "change"][0]
    assert not opened["collapsed"]
    assert opened["diff_lines"] == [["add", 1, "hello"], ["add", 2, "world"]]


def test_multi_file_patch_stands_alone_with_file_count(tmp_path, monkeypatch):
    patch = ("--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,3 @@\n-old\n+new\n+more\n keep\n"
             "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-gone\n+fresh")
    tools = [
        ToolCallView(call_id="r1", name="Read", event_seq=2, status="completed", input={"path": "a.py"}),
        ToolCallView(call_id="p", name="apply_patch", event_seq=3, status="completed", input={},
                     diff={"path": "a.py", "hunk": patch}),
    ]
    turn = replace(_turn(), tools=tools)
    blocks = [b for b in _snapshot(tmp_path, monkeypatch, [turn], local_transcript=True)["blocks"] if b["kind"] in {"tool_group", "change"}]
    assert [b["kind"] for b in blocks] == ["tool_group", "change"], "the patch is its own block, not merged with the read"
    patch_block = blocks[1]
    assert patch_block["files"] == 2 and patch_block["path"] == "a.py, b.py"
    assert (patch_block["added"], patch_block["removed"]) == (3, 2)
    assert ["file", 0, "a.py"] in patch_block["diff_lines"] and ["file", 0, "b.py"] in patch_block["diff_lines"]

def test_turn_numbers_survive_queue_steer_and_interrupt_and_replay(tmp_path, monkeypatch):
    """#N counts user-initiated turns: a steer folded into a turn and shell turns take no number."""
    from nexus.events import Event
    from nexus.view import apply, apply_many

    def ev(kind, seq, data, turn):
        return Event(type=kind, data=data, seq=seq, session="s", turn=turn, ts=1_759_000_000.0 + seq)

    def cards(snapshot):
        return [(b["title"], b["number"]) for b in snapshot["blocks"] if b["kind"] == "user"]

    events = [
        ev("input.queued", 1, {"queued_id": "a", "content": [{"text": "first"}]}, "t1"),
        ev("turn.started", 2, {}, "t1"),
        ev("input.consumed", 3, {"queued_id": "a", "turn": "t1"}, "t1"),
        ev("input.queued", 4, {"queued_id": "s", "content": [{"text": "steer"}], "mode": "steer"}, "t1"),
        ev("input.consumed", 5, {"queued_id": "s", "turn": "t1", "mode": "steer"}, "t1"),
        ev("turn.cancelled", 6, {"reason": "interrupted"}, "t1"),
        ev("input.queued", 7, {"queued_id": "i", "content": [{"text": "interrupt"}], "mode": "interrupt"}, "t2"),
        ev("turn.started", 8, {}, "t2"),
        ev("input.consumed", 9, {"queued_id": "i", "turn": "t2"}, "t2"),
        ev("turn.completed", 10, {"stop_reason": "end_turn"}, "t2"),
        ev("input.queued", 11, {"queued_id": "q", "content": [{"text": "queued"}]}, "t3"),
        ev("turn.started", 12, {}, "t3"),
        ev("input.consumed", 13, {"queued_id": "q", "turn": "t3"}, "t3"),
    ]
    expected = [("first", 1), ("steer", 0), ("interrupt", 2), ("queued", 3)]
    state = initial_state("s")
    live = []
    for event in events:
        state = apply(state, event)
        live.append(state)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def project_state(view):
        controller = SimpleNamespace(view=view, session="s")
        shell = ShellActions(controller)
        shell.preferences.values["context_preview"] = False
        return project(controller, 1, shell=shell, literal=False)

    assert cards(project_state(state)) == expected
    replay = apply_many(initial_state("s"), events)
    assert cards(project_state(replay)) == expected, "live and replay agree"

    def statuses(view):
        return [(b["title"], b.get("status", "")) for b in project_state(view)["blocks"] if b["kind"] == "user"]

    marked = [("first", ""), ("steer", "steer"), ("interrupt", ""), ("queued", "")]
    assert statuses(state) == marked, "only the consumed steer is tagged"
    assert statuses(replay) == marked


def test_user_card_shows_its_own_send_time_live_and_on_replay(tmp_path, monkeypatch):
    """Send time is the queued event's time, not the consume time, per message (steers too)."""
    from datetime import datetime
    from nexus.events import Event
    from nexus.view import apply, apply_many

    base = 1_759_000_000.0

    def ev(kind, seq, data, turn, at):
        return Event(type=kind, data=data, seq=seq, session="s", turn=turn, ts=base + at)

    def clock(at):
        return datetime.fromtimestamp(base + at).strftime("%I:%M %p").lstrip("0")

    events = [
        ev("input.queued", 1, {"queued_id": "a", "content": [{"text": "first"}]}, "t1", 0),
        ev("turn.started", 2, {}, "t1", 7200),
        ev("input.consumed", 3, {"queued_id": "a", "turn": "t1"}, "t1", 7200),
        ev("input.queued", 4, {"queued_id": "s", "content": [{"text": "steer"}], "mode": "steer"}, "t1", 10800),
        ev("input.consumed", 5, {"queued_id": "s", "turn": "t1", "mode": "steer"}, "t1", 14400),
        ev("input.started", 6, {"input_id": "d", "content": [{"text": "direct"}]}, "t2", 18000),
    ]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def sent(view):
        controller = SimpleNamespace(view=view, session="s")
        shell = ShellActions(controller)
        shell.preferences.values["context_preview"] = False
        blocks = project(controller, 1, shell=shell, literal=False)["blocks"]
        return [(b["title"], b["sent_at"]) for b in blocks if b["kind"] == "user"]

    expected = [("first", clock(0)), ("steer", clock(10800)), ("direct", clock(18000))]
    live = initial_state("s")
    for event in events:
        live = apply(live, event)
    assert sent(live) == expected
    assert sent(apply_many(initial_state("s"), events)) == expected

    from msgspec.structs import replace as msg_replace

    late = [
        msg_replace(events[2], seq=1),
        msg_replace(events[0], seq=2),
    ]  # a follower sees the consume before the queued event in its stream
    view = apply_many(initial_state("s"), late)
    assert view.turns[0].messages[0].sent_ts == base


def test_pending_queue_rows_tag_steer_apart_from_queue_and_stay_out_of_the_transcript(tmp_path, monkeypatch):
    from nexus.events import Event
    from nexus.view import apply_many
    from nexus.ui.ratatui.prototype import _queue_items, _queue_lines

    def ev(kind, seq, data, turn):
        return Event(type=kind, data=data, seq=seq, session="s", turn=turn, ts=1_759_000_000.0 + seq)

    events = [
        ev("input.queued", 1, {"queued_id": "a", "content": [{"text": "first"}]}, "t1"),
        ev("turn.started", 2, {}, "t1"),
        ev("input.consumed", 3, {"queued_id": "a", "turn": "t1"}, "t1"),
        ev("input.queued", 4, {"queued_id": "q", "content": [{"text": "later"}], "mode": "queue"}, "t1"),
        ev("input.queued", 5, {"queued_id": "s", "content": [{"text": "nudge"}], "mode": "steer"}, "t1"),
    ]
    view = apply_many(initial_state("s"), events)
    assert [(i["id"], i["tag"], i["text"]) for i in _queue_items(view)] == [("q", "Q", "later"), ("s", "S", "nudge")]
    assert _queue_lines(view) == ["Queued · later", "Steer · nudge"]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(view=view, session="s"))
    shell.preferences.values["context_preview"] = False
    snapshot = project(SimpleNamespace(view=view, session="s"), 1, shell=shell, literal=False)
    assert [b["title"] for b in snapshot["blocks"] if b["kind"] == "user"] == ["first"], "pending rows are not transcript cards"


def _question_block(tmp_path, monkeypatch, tool, **shell_values):
    turn = replace(_turn(), tools=[tool])
    return next(b for b in _snapshot(tmp_path, monkeypatch, [turn], **shell_values)["blocks"] if b["kind"] == "change")


def test_question_card_marks_the_selected_option(tmp_path, monkeypatch):
    tool = ToolCallView(call_id="q", name="question", event_seq=3, status="completed",
                        input={"question": "Which db?", "options": ["SQLite", "Postgres"]},
                        result=[{"type": "text", "text": "The user answered: Postgres"}],
                        display="Postgres", metrics={"answer": "Postgres"})
    block = _question_block(tmp_path, monkeypatch, tool)
    assert block["detail"] == "Which db?\n  ○ SQLite\n  ● Postgres"
    assert not block["collapsed"] and block["inspect_operation"] == {"kind": "tool_details", "id": "q"}
    assert block["heading"] == "", "no redundant answered summary beside the options"
    raw = _question_block(tmp_path, monkeypatch, tool, verbose=True)
    assert "The user answered" in raw["detail"], "/verbose keeps the labelled details"


def test_question_card_unanswered_and_free_text(tmp_path, monkeypatch):
    running = ToolCallView(call_id="q", name="question", event_seq=3, status="running",
                           input={"question": "Which db?", "options": ["SQLite", "Postgres"]})
    block = _question_block(tmp_path, monkeypatch, running)
    assert block["detail"] == "Which db?\n  ○ SQLite\n  ○ Postgres\n  … awaiting answer"
    typed = ToolCallView(call_id="q", name="question", event_seq=3, status="completed",
                         input={"question": "Which db?", "options": ["SQLite"]}, metrics={"answer": "DuckDB"})
    assert _question_block(tmp_path, monkeypatch, typed)["detail"] == "Which db?\n  ○ SQLite\n  ✎ DuckDB"
    free = ToolCallView(call_id="q", name="question", event_seq=3, status="completed",
                        input={"question": "Name it?"}, metrics={"answer": "nexus"})
    assert _question_block(tmp_path, monkeypatch, free)["detail"] == "Name it?\n  ✎ nexus"


def test_turn_footer_starts_with_the_local_start_time(tmp_path, monkeypatch):
    from datetime import datetime
    turn = _turn(started_ts=1_759_000_000.0)
    snapshot = _snapshot(tmp_path, monkeypatch, [turn], literal=False)
    footer = next(b for b in snapshot["blocks"] if b["kind"] == "summary")["text"]
    clock = datetime.fromtimestamp(1_759_000_000.0).strftime("%I:%M %p").lstrip("0")
    assert footer.startswith(f"{clock} · ")


async def test_steer_removal_restores_attachments_into_an_empty_draft():
    import base64
    from types import SimpleNamespace
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.prototype import queue_edit
    from nexus.view.model import MessageView, QueuedInputView

    prepared = []

    class Client:
        async def queue_remove_content(self, session, queued_id):
            return True, [
                {"type": "text", "text": "look [image 1] and [document 1]"},
                {"type": "text", "text": "\n\nAttachment: image 1 · a b.png · image/png · 3 bytes\n"},
                {"type": "image", "media_type": "image/png", "data": base64.b64encode(b"\x89PN").decode()},
                {"type": "text", "text": "\n\nAttachment: document 1 · notes.pdf\n\n# Notes"},
            ]

        async def prepare_attachment(self, path="", *, name="", data=b""):
            prepared.append((name, data))
            return SimpleNamespace(attachment_id=f"tok{len(prepared)}", name=name,
                                   kind="image" if name.endswith(".png") else "markdown")

    view = _running_view([])
    view.active_turn.messages.append(MessageView(id="m", role="user", event_seq=1))
    view.input_queue = [QueuedInputView(queued_id="id0", mode="steer", content=[{"type": "text", "text": "x"}])]
    controller = SimpleNamespace(view=view, session="s", slot_wait=None)
    shell = SimpleNamespace(
        client=Client(), composer_restore="", composer_restore_if_empty="", composer_restore_note="",
        composer_restore_attachments=0, attachments=[], flash=lambda *a, **k: None)
    for name in ("attachment_label", "attachment_marker", "reconcile_attachment_markers"):
        setattr(shell, name, getattr(ShellActions, name).__get__(shell))
    shell.attachment_labels, shell.marker_attachments = {}, {}
    shell.attachment_label_counters = {"image": 0, "document": 0}

    await queue_edit(shell, controller, "id0", "remove")
    assert prepared == [("a b.png", b"\x89PN"), ("notes.md", b"# Notes")]
    assert shell.composer_restore == "look [image 1] and [document 1]"
    assert shell.composer_restore_attachments == 2 and len(shell.attachments) == 2
    # Dropping a marker (the Rust side declined the restore) removes its attachment again.
    shell.reconcile_attachment_markers("")
    assert shell.attachments == []

    # A draft that already holds attachments keeps them; nothing is re-attached.
    prepared.clear()
    shell.attachments = [SimpleNamespace(attachment_id="mine", name="m", kind="image")]
    shell.composer_restore = shell.composer_restore_if_empty = ""
    shell.composer_restore_attachments = 0
    await queue_edit(shell, controller, "id0", "remove")
    assert prepared == [] and shell.composer_restore_attachments == 0
    assert [a.attachment_id for a in shell.attachments] == ["mine"]


def test_wire_sends_restore_attachments_with_the_one_shot_restore():
    from nexus.ui.ratatui.wire import TerminalWire

    wire = TerminalWire()
    base = {"revision": 1, "generation": 1, "blocks": [], "restore": "t", "restore_if_empty": True,
            "restore_attachments": 2}
    assert wire.encode(base)["restore_attachments"] == 2
    again = wire.encode({**base, "revision": 2})
    assert again is not None and again["restore_attachments"] == 2
