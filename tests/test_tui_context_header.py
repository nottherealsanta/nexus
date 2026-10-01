"""The context header summarizes the host snapshot without losing detail."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_context_header import (
    ContextHeader,
    format_schema_type,
    group_tools,
    prompt_preview,
    render_columns,
    schema_param_rows,
)


def test_context_summary_helpers_group_and_bound_data():
    groups, mcp = group_tools([
        {"name": "bash", "group": "bash"},
        {"name": "BashOutput", "group": "bash"},
        {"name": "read"},
        {"name": "mcp__docs__search"},
    ])
    assert list(groups) == ["bash", "read"]
    assert len(groups["bash"]) == 2
    assert list(mcp) == ["docs"]
    assert render_columns(["bash(2)", "read", "write", "edit"]).splitlines()[1].strip() == "edit"
    assert prompt_preview("\n".join(str(n) for n in range(7))) == "0\n1\n2\n3\n4\n… +2 more lines"
    assembled = (
        "You are Nexus\n\n<environment>\nworkspace: /elsewhere\n</environment>\n"
        "--- Selected agent instructions ---\nYou are Build, the coding agent.\n"
        "Verify your work.\nMore instructions"
    )
    preview = prompt_preview(assembled)
    assert "You are Nexus" in preview and "You are Build" in preview
    assert "workspace: /elsewhere" not in preview
    assert "… open for full prompt" in preview
    assert format_schema_type({"type": "array", "items": {"type": "string"}}) == "array<string>"
    assert schema_param_rows({"properties": {"path": {"type": "string", "description": "File"}}, "required": ["path"]}) == ["* path  string  — File"]


@pytest.mark.asyncio
async def test_context_header_is_first_and_opens_prompt_modal():
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        timeline = app.query_one("#conversation")
        assert isinstance(timeline.children[0], ContextHeader)
        assert "Selected system prompt" in app.query_one("#context-prompt").render().plain
        prompt = "Selected system prompt\n\n<environment>\nworkspace: /tmp/project\nplatform: darwin\nprofile: coding\n</environment>\n[literal] **prompt**"
        app.query_one("#context-prompt").detail = prompt
        await pilot.click("#context-prompt")
        await pilot.pause()
        # The system prompt is literal text, including XML environment tags.
        assert app.screen.query_one("#context-modal-body").render().plain == prompt
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


@pytest.mark.asyncio
async def test_context_blocks_align_rows_and_grey_out_empty_parts():
    from nexus.host import protocol as p
    from nexus.ui_support.tui_context_header import ContextBlock
    from nexus.ui_support.tui_widgets import agent_color

    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        tools = [{"name": name} for name in ("apply_patch", "bash", "edit", "glob")]
        app.query_one(ContextHeader).set_data(p.ContextInspectResult(
            session="s", agent={"name": "build"}, system_text="one\ntwo", tools=tools,
            skills_index=[], mcp_servers=[],
        ))
        await pilot.pause()
        text = app.query_one("#context-tools", ContextBlock).render().plain.splitlines()
        assert text[1].startswith("  apply_patch") and text[2].startswith("  glob")
        assert app.query_one("#context-prompt", ContextBlock).render().plain.splitlines()[1:] == ["  one", "  two"]
        for slug in ("skills", "mcp"):
            block = app.query_one(f"#context-{slug}", ContextBlock)
            assert "Project 0 | Global 0" in block.render().plain
            assert block.has_class("-empty") and block.label_color == "$nx-label-neutral"
        assert app.query_one("#context-tools", ContextBlock).label_color == agent_color("build")


def _grouped_result():
    from nexus.host import protocol as p

    return p.ContextInspectResult(
        session="s", agent={"name": "build"}, provider="scripted", model="m",
        system_text="identity text\nsoul text",
        included_parts=[{"name": "identity", "text": "identity text"}, {"name": "soul", "text": "soul text"}],
        tools=[
            {"name": "Read", "group": "read", "description": "Read a file.\nLonger help.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string", "description": "File"}}, "required": ["path"]}},
            {"name": "Bash", "group": "bash", "description": "Run a command", "input_schema": {"type": "object"}},
            {"name": "BashOutput", "group": "bash", "description": "Read output", "input_schema": {"type": "object"}},
            {"name": "mcp__docs__search", "description": "Search docs", "input_schema": {}},
        ],
        messages=[
            {"role": "user", "blocks": [{"type": "text", "text": "first prompt"}]},
            {"role": "assistant", "blocks": [{"type": "text", "text": "reading"}, {"type": "tool_use", "id": "c1", "name": "Read", "input": {"path": "a.py"}}]},
            {"role": "user", "blocks": [{"type": "tool_result", "tool_use_id": "c1", "is_error": True, "content": [{"type": "text", "text": "missing"}]}]},
            {"role": "assistant", "blocks": [{"type": "text", "text": "done"}]},
            {"role": "user", "blocks": [{"type": "text", "text": "second prompt"}]},
        ],
        request_context={"used_tokens": 40, "input_budget": 1000},
    )


def test_context_groups_follow_wire_order_and_pair_tool_results():
    from nexus.ui_support.context import (
        context_groups,
        context_summary,
        estimate_tokens,
        tool_groups,
    )

    groups = context_groups(_grouped_result())
    assert [group.title for group in groups] == ["System prompt", "Tools", "Turn 1", "Turn 2", "Request details"]
    system, tools, first, second, _request = groups
    assert [entry.title for entry in system.entries] == ["identity", "soul", "Full system prompt"]
    assert system.tokens == estimate_tokens("identity text\nsoul text")
    assert [entry.title for entry in tools.entries] == ["Read", "Bash", "BashOutput", "mcp__docs__search"]
    assert tools.entries[0].detail == "1 param · Read a file."
    assert "- `path` string · required — File" in tools.entries[0].body
    assert [entry.title for entry in first.entries] == ["User message", "Assistant", "Tool · Read", "Assistant"]
    call = first.entries[2]
    assert call.error and "**Error**" in call.body and "missing" in call.body and '"path": "a.py"' in call.body
    assert first.detail == "1 tool call(s)"
    assert [entry.title for entry in second.entries] == ["User message"]
    summary = context_summary(_grouped_result(), "Context: 4% · 40 of 1,000 tokens (estimated)")
    assert "build · scripted/m" in summary and "2 turn(s)" in summary
    assert [group.title for group in tool_groups(_grouped_result().tools)] == ["bash", "read", "MCP · docs"]


@pytest.mark.asyncio
async def test_tools_block_opens_grouped_tools_dialog():
    from nexus.ui_support.tui_context_header import ToolsModal

    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.query_one(ContextHeader).set_data(_grouped_result())
        await pilot.pause()
        await pilot.click("#context-tools")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ToolsModal)
        assert screen.query_one("#context-modal-title").render().plain.startswith("Tools · 4 definitions · ~")
        # Only a family of several tools gets a header; a family of one is just its row.
        groups = [str(group._title.label).split("  ")[0] for group in screen.query("Collapsible.context-group")]
        assert groups == ["bash"]
        assert not any(group.collapsed for group in screen.query("Collapsible.context-group"))
        rows = list(screen.query("Collapsible.context-entry"))
        assert [str(row._title.label).split("  ")[0] for row in rows] == ["Bash", "BashOutput", "Read", "mcp__docs__search"]
        assert all(row.collapsed for row in rows)
        assert "1 param · Read a file." in str(rows[2]._title.label)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is app.screen_stack[0]


@pytest.mark.asyncio
async def test_context_header_has_agents_block_between_tools_and_skills():
    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        header = app.query_one(ContextHeader)
        ids = [child.id for child in header.children]
        assert ids.index("context-tools") < ids.index("context-agents") < ids.index("context-skills")

@pytest.mark.asyncio
async def test_prompt_dialog_excludes_separately_displayed_agents_md():
    from nexus.host.protocol import ContextInspectResult
    from nexus.ui_support.tui_context_header import ContextBlock

    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        result = ContextInspectResult(
            session="s", system_text="Coding assistant\n\nProject rules\n\nMemory",
            included_parts=[{"name": "soul", "text": "Coding assistant"},
                            {"name": "agents_md", "text": "Project rules"},
                            {"name": "memory", "text": "Memory"}],
        )
        app.query_one(ContextHeader).set_data(result)
        assert app.query_one("#context-agents", ContextBlock).detail == "Project rules"
        await pilot.click("#context-prompt")
        await pilot.pause()
        assert app.screen.query_one("#context-modal-body").render().plain == "Coding assistant\n\nMemory"


def test_header_prompt_preserves_incomplete_snapshots_and_overrides():
    from nexus.host.protocol import ContextInspectResult
    from nexus.ui_support.context import header_system_prompt

    for parts in ([], [{"name": "system_override", "text": "Full prompt"}],
                  [{"name": "agents_md", "text": "Clipped rules"}]):
        assert header_system_prompt(ContextInspectResult(
            session="s", system_text="Full prompt", included_parts=parts)) == "Full prompt"
