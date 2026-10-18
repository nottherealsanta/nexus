from types import SimpleNamespace

from nexus.ui.ratatui.prototype import _agent_color, project
from nexus.ui.ratatui.actions import ShellActions
from nexus.view import initial_state
from nexus.view.model import (
    AgentView,
    BlockView,
    MessageView,
    ToolCallView,
    TurnView,
    UsageTotals,
)


def _make_shell(session_id="s1", defs=None):
    view = initial_state(session_id)
    controller = SimpleNamespace(view=view, session=session_id)
    shell = ShellActions(controller)
    shell.preferences.values["context_preview"] = False
    if defs is not None:
        shell.agent_definitions = defs
    return shell, controller, view


def test_agent_color_stable_across_runtimes_and_sessions():
    shell1, _, _ = _make_shell("session-A")
    shell2, _, _ = _make_shell("session-B")

    color1 = _agent_color(shell1, "explorer")
    color2 = _agent_color(shell2, "explorer")
    assert color1 == color2
    assert color1 != ""

    # Semantic names remain fixed
    assert _agent_color(shell1, "build") == _agent_color(shell2, "build")
    assert _agent_color(shell1, "orchestrator") == _agent_color(shell2, "orchestrator")


def test_explicit_color_in_agent_definition_wins():
    defs = {"custom": {"name": "custom", "color": "#abcdef"}}
    shell, _, _ = _make_shell("s1", defs=defs)
    assert _agent_color(shell, "custom") == "#abcdef"

    # With definition object (non-dict)
    obj_def = SimpleNamespace(name="special", color="#123456")
    shell_obj, _, _ = _make_shell("s1", defs={"special": obj_def})
    assert _agent_color(shell_obj, "special") == "#123456"


def test_child_with_no_type_gets_definition_name_color_not_id(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # Child has empty type and a unique run-specific ID
    child = AgentView(id="run-987654321", type="", status="running", spawned_ts=100.0)
    tool = ToolCallView(
        call_id="task-call",
        name="task",
        input={"subagent_type": "explorer", "description": "find things"},
        child_agent_ids=["run-987654321"],
    )
    shell, controller, view = _make_shell("s1")
    view.turns = [
        TurnView(
            id="t1",
            index=0,
            messages=[MessageView(id="m1", role="user", blocks=[BlockView(text="start")])],
            tools=[tool],
        )
    ]
    view.agents = {"run-987654321": child}

    blocks = project(controller, 1, shell=shell)["blocks"]
    card = next(b for b in blocks if b["id"] == "task-call")

    assert card["agent_name"] == "explorer"
    assert card["color"] == _agent_color(shell, "explorer")
    # Color must NOT be hashed from the random id
    assert card["color"] != _agent_color(shell, "run-987654321")


def test_child_fallback_agent_argument_and_task_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # Tool with "agent" argument instead of "subagent_type"
    child1 = AgentView(id="id-1", type="", status="running", spawned_ts=10.0)
    tool1 = ToolCallView(
        call_id="call-1",
        name="task",
        input={"agent": "advisor", "description": "advise"},
        child_agent_ids=["id-1"],
    )

    # Tool with neither argument: falls back to literal "task"
    child2 = AgentView(id="id-2", type="", status="running", spawned_ts=20.0)
    tool2 = ToolCallView(
        call_id="call-2",
        name="task",
        input={"description": "generic work"},
        child_agent_ids=["id-2"],
    )

    shell, controller, view = _make_shell("s1")
    view.turns = [
        TurnView(
            id="t1",
            index=0,
            messages=[MessageView(id="m1", role="user", blocks=[BlockView(text="go")])],
            tools=[tool1, tool2],
        )
    ]
    view.agents = {"id-1": child1, "id-2": child2}

    blocks = project(controller, 1, shell=shell)["blocks"]
    card1 = next(b for b in blocks if b["id"] == "call-1")
    card2 = next(b for b in blocks if b["id"] == "call-2")

    assert card1["agent_name"] == "advisor"
    assert card1["color"] == _agent_color(shell, "advisor")

    assert card2["agent_name"] == "task"
    assert card2["color"] == _agent_color(shell, "task")


def test_task_block_carries_structured_step2_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    child = AgentView(
        id="child-1",
        type="explorer",
        tier="low",
        resolved_model="openai/gpt-6-mini",
        status="completed",
        spawned_ts=100.0,
        completed_ts=103.2,
        usage=UsageTotals(input_tokens=8000, output_tokens=2000),
    )
    child.body.turns = [
        TurnView(
            id="ct-1",
            index=0,
            reasoning_effort="medium",
            tools=[
                ToolCallView(call_id="sub-1", name="read"),
                ToolCallView(call_id="sub-2", name="search"),
            ],
        )
    ]

    tool = ToolCallView(
        call_id="main-task",
        name="task",
        input={"subagent_type": "explorer", "description": "map call sites"},
        child_agent_ids=["child-1"],
    )

    shell, controller, view = _make_shell("s1")
    view.turns = [
        TurnView(
            id="t1",
            index=0,
            messages=[MessageView(id="m1", role="user", blocks=[BlockView(text="run")])],
            tools=[tool],
        )
    ]
    view.agents = {"child-1": child}

    blocks = project(controller, 1, shell=shell)["blocks"]
    card = next(b for b in blocks if b["id"] == "main-task")

    assert card["agent_name"] == "explorer"
    assert card["task"] == "map call sites"
    assert card["model"] == "gpt-6-mini"
    assert card["effort"] == "medium"
    assert card["tier"] == "low"
    assert card["tool_calls"] == 2
    assert card["tokens"] == 10000
    assert card["started_ms"] == 100000
    assert card["elapsed_ms"] == 3200


def test_task_block_null_when_usage_unknown_or_external(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # Child with usage_unknown
    child = AgentView(
        id="child-unk",
        type="explorer",
        tier="low",
        resolved_model="openai/gpt-6",
        status="running",
        usage_unknown=True,
        spawned_ts=50.0,
    )
    tool = ToolCallView(
        call_id="task-unk",
        name="task",
        input={"subagent_type": "explorer", "description": "run"},
        child_agent_ids=["child-unk"],
    )

    shell, controller, view = _make_shell("s1")
    view.turns = [
        TurnView(
            id="t1",
            index=0,
            messages=[MessageView(id="m1", role="user", blocks=[BlockView(text="run")])],
            tools=[tool],
        )
    ]
    view.agents = {"child-unk": child}

    blocks = project(controller, 1, shell=shell)["blocks"]
    card = next(b for b in blocks if b["id"] == "task-unk")

    assert card["tokens"] is None
    assert card["tool_calls"] == 0
    assert card["started_ms"] == 50000
    assert card["elapsed_ms"] == 0


def test_task_block_external_worker_note_and_omits_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    external_info = {
        "provider": "Antigravity",
        "model": "claude-3-7-sonnet",
        "command": ["agy", "run"],
    }
    child = AgentView(
        id="child-ext",
        type="worker",
        status="running",
        spawned_ts=10.0,
    )
    child.body.external = external_info

    tool = ToolCallView(
        call_id="task-ext",
        name="task",
        input={"subagent_type": "worker", "description": "implement feature"},
        child_agent_ids=["child-ext"],
    )

    shell, controller, view = _make_shell("s1")
    view.turns = [
        TurnView(
            id="t1",
            index=0,
            messages=[MessageView(id="m1", role="user", blocks=[BlockView(text="run")])],
            tools=[tool],
        )
    ]
    view.agents = {"child-ext": child}

    blocks = project(controller, 1, shell=shell)["blocks"]
    card = next(b for b in blocks if b["id"] == "task-ext")

    assert card["tool_calls"] is None
    assert "· Antigravity (agy) (external, own tools)" in card["task"]
    assert "· Antigravity (agy) (external, own tools)" in card["title"]
