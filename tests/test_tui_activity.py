"""Focused tests for inline reducer-backed subagent activity."""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.widgets import Static

from nexus.ui.tui.agent_transcript import AgentTranscriptScreen
from nexus.ui.tui.messages import AgentOpenRequested
from nexus.ui.tui.timeline import (
    AgentActivityLink,
    TaskActivityWidget,
    ThoughtLine,
    ToolActivityWidget,
    _agent_metrics,
    _latest_activity,
)
from nexus.ui_support.timeline import tool_output
from nexus.view import (
    AgentView,
    BlockView,
    ConversationView,
    MessageView,
    ToolCallView,
    TurnView,
)


def _agent(*, tools=(), messages=(), **kwargs) -> AgentView:
    return AgentView(
        id="child",
        type="explore",
        task="inspect repository",
        body=ConversationView(
            turns=[
                TurnView(
                    id="child-turn",
                    started_ts=kwargs.pop("turn_started_ts", None),
                    updated_ts=kwargs.pop("turn_updated_ts", None),
                    tools=list(tools),
                    messages=list(messages),
                )
            ]
        ),
        **kwargs,
    )


def test_running_child_card_shows_latest_actual_tool_and_elapsed_activity():
    agent = _agent(
        spawned_ts=10.0,
        turn_updated_ts=13.25,
        tools=[
            ToolCallView(
                call_id="old",
                event_seq=2,
                name="Read",
                status="completed",
                finished_ts=11.0,
            ),
            ToolCallView(
                call_id="live",
                event_seq=5,
                name="Bash",
                status="running",
                progress=["reading files"],
            ),
        ],
    )

    assert _latest_activity(agent) == "Bash: reading files"
    assert _agent_metrics(agent) == "1 tool · 3.2s"


def test_completed_child_card_counts_finished_calls_from_event_timestamps():
    agent = _agent(
        status="completed",
        spawned_ts=4.0,
        completed_ts=9.5,
        tools=[
            ToolCallView(call_id="ok", name="Read", status="completed"),
            ToolCallView(call_id="failed", name="Bash", status="failed"),
            ToolCallView(call_id="live", name="Task", status="running"),
        ],
    )

    assert _agent_metrics(agent) == "2 tools · 5.5s"


def test_missing_child_timestamps_do_not_invent_elapsed_time():
    agent = _agent(tools=[ToolCallView(call_id="done", status="completed")])
    assert _agent_metrics(agent) == "1 tool"


class _LinkApp(App[None]):
    def compose(self) -> ComposeResult:
        yield AgentActivityLink(AgentView(id="child", type="explore"), id="child-link")

    def on_mount(self) -> None:
        self.query_one(AgentActivityLink).focus()

    @on(AgentOpenRequested)
    def opened(self, message: AgentOpenRequested) -> None:
        self.opened_agent = message.agent_id


async def test_child_card_is_clickable_and_keyboard_activatable():
    app = _LinkApp()
    async with app.run_test() as pilot:
        link = app.query_one("#child-link", AgentActivityLink)
        assert link.can_focus
        assert app.focused is link
        await pilot.press("enter")
        await pilot.pause()
        assert app.opened_agent == "child"
        app.opened_agent = None
        await pilot.pause(0.25)
        await pilot.click("#child-link")
        await pilot.pause()
        assert app.opened_agent == "child"


async def test_child_modal_shows_provider_thought_but_never_signature():
    agent = _agent(
        messages=[
            MessageView(
                role="assistant",
                blocks=[
                    BlockView(kind="thinking", text="Checking the requested files", signature="opaque-signature"),
                    BlockView(text="Visible answer"),
                ],
            )
        ]
    )
    async with App().run_test() as pilot:
        screen = AgentTranscriptScreen(agent)
        await pilot.app.push_screen(screen)
        await pilot.pause(0.2)
        (thought,) = screen.query(ThoughtLine)
        rendered = str(thought.render())
        assert "Checking the requested files" in rendered
        assert "opaque-signature" not in rendered


def test_child_inspector_redacts_and_escapes_hostile_errors():
    hostile = ToolCallView(call_id="bad", name="Bash", status="failed", error="api_key=supersecret ``` injected")
    rendered = tool_output(hostile)
    assert "supersecret" not in rendered
    assert "api_key=…" in rendered
    escaped = tool_output(ToolCallView(call_id="c", name="Bash", status="failed", error="failure\x1b[31mboom"))
    assert "\\x1b" in escaped


class _ActivityApp(App[None]):
    def __init__(self, tool: ToolCallView) -> None:
        super().__init__()
        self.tool = tool

    def compose(self) -> ComposeResult:
        yield ToolActivityWidget(self.tool, id="tool")


async def test_tool_row_running_label_spinner_and_terminal_cleanup():
    tool = ToolCallView(
        call_id="live", name="Bash", status="running", input={"command": "pytest -q"}
    )
    async with _ActivityApp(tool).run_test() as pilot:
        widget = pilot.app.query_one("#tool", ToolActivityWidget)
        header = widget.query_one("#tool-header", Static)
        timer = widget._spinner
        assert timer is not None
        # Shell calls read as "$ command" with a live spinner while running.
        assert str(header.render())[0] in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        assert "$ pytest -q" in str(header.render())
        before = str(header.render())
        await pilot.pause(0.45)
        assert str(header.render()) != before

        await widget.set_tool(
            ToolCallView(
                call_id="live", name="Bash", status="completed", duration_ms=25,
                display="tests passed", result=[{"text": "42 passed"}],
            )
        )
        assert widget._spinner is None
        terminal = str(header.render())
        assert terminal.startswith("$ · tests passed") and not any(ch in terminal for ch in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
        await pilot.pause(0.45)
        assert str(header.render()) == terminal
        assert "42 passed" not in terminal
        await widget.remove()
        assert widget._spinner is None


async def test_tool_row_activation_opens_modal_and_keeps_transcript_compact():
    tool = ToolCallView(
        call_id="inspect", name="Glob", status="completed",
        input={"pattern": "**/*.py"}, result=[{"text": "one.py\ntwo.py"}],
    )
    async with _ActivityApp(tool).run_test() as pilot:
        widget = pilot.app.query_one("#tool", ToolActivityWidget)
        assert len(widget.children) == 1
        assert "one.py" not in str(widget.query_one("#tool-header", Static).render())
        widget.focus()
        await pilot.press("enter")
        await pilot.pause()
        from nexus.ui.tui.tool_details import ToolDetailsScreen

        screen = pilot.app.screen
        assert isinstance(screen, ToolDetailsScreen)
        body = str(screen.query_one("#tool-details-body", Static).render())
        assert "**/*.py" in body and "one.py" in body and "two.py" in body
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is pilot.app.screen_stack[0]
        assert widget.is_mounted


async def test_failed_tool_stops_spinner_without_inventing_output():
    async with _ActivityApp(
        ToolCallView(call_id="live", name="Read", status="requested")
    ).run_test() as pilot:
        widget = pilot.app.query_one("#tool", ToolActivityWidget)
        assert widget._spinner is not None
        await widget.set_tool(
            ToolCallView(
                call_id="live", name="Read", status="failed", error="missing file"
            )
        )
        assert widget._spinner is None
        assert "missing file" in str(widget.query_one("#tool-header", Static).render())
        await widget.open_details()
        from nexus.ui.tui.tool_details import ToolDetailsScreen

        assert isinstance(pilot.app.screen, ToolDetailsScreen)
        assert "missing file" in str(pilot.app.screen.query_one("#tool-details-body", Static).render())


async def test_nested_task_call_and_child_inspector_activity_refresh_live():
    child = _agent(
        tools=[
            ToolCallView(
                call_id="child-tool",
                event_seq=3,
                name="Read",
                status="running",
                input={"path": "src/main.py"},
                progress=["reading current file"],
            )
        ]
    )
    child.body.turns.append(
        TurnView(
            id="child-turn-later",
            tools=[
                ToolCallView(
                    call_id="newest-child-tool",
                    event_seq=8,
                    name="Glob",
                    status="running",
                    input={"pattern": "src/**/*.py"},
                    progress=["checking source paths"],
                )
            ],
            messages=[
                MessageView(
                    role="assistant",
                    event_seq=9,
                    blocks=[BlockView(text="This must not replace tool activity")],
                )
            ],
        )
    )
    task = ToolCallView(
        call_id="task",
        name="Task",
        status="running",
        input={"description": "inspect implementation"},
        child_agent_ids=[child.id],
    )
    task_widget = TaskActivityWidget(task, {child.id: child})
    async with _ActivityApp(task).run_test() as pilot:
        # Exercise the same nested Task projection used in the timeline.
        host = pilot.app.query_one("#tool", ToolActivityWidget)
        await host.remove()
        await pilot.app.mount(task_widget)
        await task_widget.set_task(task, {child.id: child})
        assert task_widget._spinner is not None
        assert child.id in task_widget.tool.child_agent_ids
        header = str(task_widget.query_one("#tool-header", Static).render())
        assert "Explore · inspect implementation" in header
        assert "This must not replace tool activity" not in header
        assert "Glob" not in header
        metrics = task_widget.query_one("#task-metrics", Static)
        assert "Read: src/main.py" in str(metrics.render())
        assert "Glob: pattern=src/**/*.py · checking source paths" in str(metrics.render())
        assert "explore" in task_widget._details_text()

        done = _agent(
            status="completed",
            description="Child fallback description",
            spawned_ts=10,
            completed_ts=13.25,
            tools=[
                ToolCallView(
                    call_id="child-tool",
                    event_seq=3,
                    name="Read",
                    status="completed",
                    input={"path": "src/main.py"},
                )
            ],
            messages=[
                MessageView(
                    role="assistant",
                    event_seq=4,
                    blocks=[BlockView(text="Read complete. Additional details.")],
                    done=True,
                )
            ],
        )
        completed_task = ToolCallView(
            call_id="task",
            name="Task",
            status="completed",
            input={"description": "Review the repository structure. Extra detail."},
            child_agent_ids=[done.id],
        )
        await task_widget.set_task(completed_task, {done.id: done})
        assert str(task_widget.query_one("#tool-header", Static).render()).endswith(
            "Explore · Review the repository structure."
        )
        rendered = str(task_widget.query_one("#tool-header", Static).render())
        assert "Read complete" not in rendered
        assert "Child fallback description" not in rendered
        assert metrics.styles.display != "none"
        assert str(metrics.render()) == "1 tool call · 3.2s"


async def test_completed_task_without_child_uses_result_without_metrics():
    task = ToolCallView(
        call_id="task",
        name="Task",
        status="completed",
        display="Task: Finished the requested scan. More output.",
    )
    async with _ActivityApp(task).run_test() as pilot:
        original = pilot.app.query_one("#tool", ToolActivityWidget)
        await original.remove()
        widget = TaskActivityWidget(task, {}, id="task")
        await pilot.app.mount(widget)
        await widget.set_task(task, {})
        header = str(widget.query_one("#tool-header", Static).render())
        assert header == "✓ General · Finished the requested scan."
        metrics = widget.query_one("#task-metrics", Static)
        assert str(metrics.render()) == "0 tool calls"


async def test_subagent_named_call_uses_two_line_task_card():
    task = ToolCallView(
        call_id="subagent-call",
        name="subagent",
        status="running",
        input={"subagent_type": "explore", "description": "Inspect the code"},
    )
    async with _ActivityApp(task).run_test() as pilot:
        original = pilot.app.query_one("#tool", ToolActivityWidget)
        await original.remove()
        widget = TaskActivityWidget(task, {}, id="task")
        await pilot.app.mount(widget)
        await widget.set_task(task, {})
        assert "Explore · Inspect the code" in str(widget.query_one("#tool-header", Static).render())
        assert str(widget.query_one("#task-metrics", Static).render()) == "Starting…"


async def test_completed_task_falls_back_to_redacted_child_description():
    child = _agent(
        status="completed",
        description="Review credentials.\napi_key=supersecret",
        messages=[
            MessageView(
                role="assistant",
                blocks=[BlockView(text="Assistant reply must not be shown.")],
            )
        ],
    )
    task = ToolCallView(
        call_id="task",
        name="Task",
        status="completed",
        input={},
        child_agent_ids=[child.id],
    )
    async with _ActivityApp(task).run_test() as pilot:
        original = pilot.app.query_one("#tool", ToolActivityWidget)
        await original.remove()
        widget = TaskActivityWidget(task, {child.id: child}, id="task")
        await pilot.app.mount(widget)
        await widget.set_task(task, {child.id: child})
        header = str(widget.query_one("#tool-header", Static).render())
        assert header.endswith("Explore · Review credentials.")
        assert "Assistant reply must not be shown" not in header
        assert "supersecret" not in header
        assert "\n" not in header

    nested = _agent()
    parent = _agent()
    parent.body.agents["nested"] = nested
    parent.body.agent_order.append("nested")
    screen = AgentTranscriptScreen(parent)
    async with App().run_test() as pilot:
        await pilot.app.push_screen(screen)
        live = _agent(
            tools=[
                ToolCallView(
                    call_id="child-tool",
                    event_seq=3,
                    name="Read",
                    status="running",
                    input={"path": "src/main.py"},
                    progress=["reading current file"],
                )
            ]
        )
        live.body.agents["nested"] = _agent(
            tools=[
                ToolCallView(
                    call_id="nested-tool",
                    event_seq=4,
                    name="Bash",
                    status="running",
                    input={"command": "pytest tests"},
                    progress=["running tests"],
                )
            ]
        )
        live.body.agent_order.append("nested")
        screen.refresh_agent(live)
        await pilot.pause()
        assert screen._spinner is not None
        activity = str(screen.query_one("#agent-inspector-activity", Static).render())
        assert (
            "Read" in activity and "src/main.py" in activity and "running" in activity
        )
        (tool,) = screen.query("#agent-timeline ToolActivityWidget")
        assert tool.tool.call_id == "child-tool" and tool.tool.status == "running"

        done = _agent(
            tools=[
                ToolCallView(
                    call_id="child-tool", event_seq=3, name="Read", status="completed"
                )
            ]
        )
        done.body.agents["nested"] = _agent(
            tools=[
                ToolCallView(
                    call_id="nested-tool",
                    event_seq=4,
                    name="Bash",
                    status="completed",
                    result=[{"text": "2 passed"}],
                )
            ]
        )
        done.body.agent_order.append("nested")
        screen.refresh_agent(done)
        await pilot.pause()
        assert screen._spinner is None
        activity = str(screen.query_one("#agent-inspector-activity", Static).render())
        assert "completed" in activity and "running" not in activity
        (tool,) = screen.query("#agent-timeline ToolActivityWidget")
        assert tool.tool.status == "completed"


def test_diff_sections_split_files_and_pad_to_real_line_numbers():
    from nexus.ui_support.timeline import diff_sections, split_diff_files

    diff = {
        "path": "2 files",
        "hunk": (
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -5,2 +5,2 @@\n keep\n-old\n+new\n"
            "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye"
        ),
    }

    assert [path for path, _ in split_diff_files(diff)] == ["src/a.py", "gone.py"]
    first, second = diff_sections(diff)
    # Four blank lines stand in for lines 1-4 so the viewer numbers from 5.
    assert (first.path, first.before, first.after) == ("src/a.py", "\n\n\n\nkeep\nold", "\n\n\n\nkeep\nnew")
    assert (first.added, first.removed) == (1, 1)
    assert (second.path, second.before, second.after, second.removed) == ("gone.py", "bye", "", 1)
    # A single-file hunk without headers belongs to the artifact's own path.
    (only,) = diff_sections({"path": "x.md", "hunk": "@@ -1 +1 @@\n-a\n+b"})
    assert (only.path, only.before, only.after) == ("x.md", "a", "b")


async def test_patch_row_names_its_files_and_shows_each_file_diff_inline():
    from textual_diff_view import DiffView

    tool = ToolCallView(
        call_id="patch", name="apply_patch", status="completed",
        input={"patch": "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b.py\n+z\n*** End Patch\n"},
        display="apply_patch: 2 file change(s)",
        diff={"path": "2 files", "added_lines": 2, "removed_lines": 1, "truncated": False,
              "hunk": "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n--- /dev/null\n+++ b/b.py\n@@ -0,0 +1 @@\n+z"},
    )
    async with _ActivityApp(ToolCallView(call_id="patch", name="apply_patch", status="running",
                                         input=tool.input)).run_test() as pilot:
        widget = pilot.app.query_one("#tool", ToolActivityWidget)
        assert "Patch a.py, b.py" in str(widget.query_one("#tool-header", Static).render())
        assert not widget.query(DiffView)
        await widget.set_tool(tool)
        await pilot.pause()
        views = list(widget.query(DiffView))
        assert [(view.path_modified, view.code_modified) for view in views] == [("a.py", "y"), ("b.py", "z")]
        # An unchanged artifact keeps the mounted views.
        await widget.set_tool(tool)
        assert list(widget.query(DiffView)) == views
