"""The context header summarizes the host snapshot without losing detail."""

from __future__ import annotations

import pytest
from test_ui_tui import FakeTransport, _client

from nexus.ui.tui.app import NexusTextualApp
from nexus.ui_support.tui_context_header import (
    ContextHeader,
    format_schema_type,
    group_tools,
    one_line_preview,
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
    assert one_line_preview("first\n\nsecond\nthird") == "first  … +2 more lines"
    assert one_line_preview("x" * 200).endswith("…") and len(one_line_preview("x" * 200)) == 100
    assert one_line_preview("  \n") == ""
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
        prompt = app.query_one("#context-prompt", ContextBlock).render().plain.splitlines()
        # One preview line; the token estimate sits beside the label in grey.
        assert prompt == [" System prompt  ~2 tokens", "  one  … +1 more line"]
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
        # A table of families: built-in first, then one row per MCP server.
        from nexus.ui_support.tui_context_header import ToolGroupRow, ToolRow
        sections = [str(row.render()) for row in screen.query(".tools-section")]
        assert sections[0].startswith("BUILT-IN TOOLS  3 tools") and sections[1].startswith("MCP  1 tool")
        groups = list(screen.query(ToolGroupRow))
        assert [group.group.key for group in groups] == ["tools:bash", "tools:read", "mcp:docs"]
        assert "Bash  BashOutput" in str(groups[0].query_one(".tool-group-tools").render())
        rows = list(screen.query(ToolRow))
        assert not any(row.display for row in rows)
        await pilot.click(groups[1])
        await pilot.pause()
        read = next(row for row in rows if row.entry.title == "Read")
        assert read.display and "1 param · Read a file." in str(read.query_one(".tool-row-text").render())
        await pilot.click(read)
        await pilot.pause()
        assert app.screen.query_one("#context-modal-title").render().plain.startswith("Tool · Read · ~")
        assert "**Parameters**" in app.screen.query_one("#context-modal-body").source
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


@pytest.mark.asyncio
async def test_skills_dialog_lists_skills_and_renders_skill_markdown():
    from nexus.host import protocol as p
    from nexus.ui_support.tui_context_header import SkillsModal
    from textual.widgets import Markdown, OptionList

    class Transport(FakeTransport):
        async def request(self, command):
            if isinstance(command, p.SettingsRead):
                assert command.category == "skills"
                if command.id == "broken":
                    raise RuntimeError("unreadable")
                return p.SettingsReadResult(body=f"# {command.id}\n\nUse **{command.id}** wisely.",
                                            rel_path=f"skills/{command.id}/SKILL.md", builtin=False, sha256="x")
            return await super().request(command)

    app = NexusTextualApp(_client(Transport()))
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        result = p.ContextInspectResult(session=app.controller.session, skills_index=[
            {"name": "review", "scope": "project", "description": "Review diffs", "enabled": True},
            {"name": "broken", "scope": "global", "description": "Fallback text", "enabled": False},
        ])
        app.query_one(ContextHeader).set_data(result)
        await pilot.click("#context-skills")
        await pilot.pause(1.0)
        screen = app.screen
        assert isinstance(screen, SkillsModal)
        options = screen.query_one("#skills-list", OptionList)
        assert [str(options.get_option_at_index(i).prompt) for i in range(2)] == [
            "● review  · project", "○ broken  · global"]
        assert "Use **review** wisely." in screen.query_one("#context-modal-body", Markdown).source
        options.highlighted = 1
        await pilot.pause(0.3)
        body = screen.query_one("#context-modal-body", Markdown).source
        assert body.startswith("Fallback text") and "could not be read" in body
        assert str(screen.query_one("#extension-1").label).startswith("Off")


def test_turn_usage_table_and_price_tiers_come_from_the_view():
    from nexus.ui_support.context import (
        context_detail_usage,
        context_pricing_note,
        context_turn_usage,
        price_tier_thresholds,
    )
    from nexus.view.model import ContextView, ConversationView, TurnView, UsageTotals

    view = ConversationView(
        turns=[
            TurnView(id="t1", usage=UsageTotals(input_tokens=1200, output_tokens=300, cache_read_tokens=5000),
                     context=[ContextView(data={"context": {"used_tokens": 6400}})], elapsed_ms=4200),
            TurnView(id="t2", usage=UsageTotals(input_tokens=800, output_tokens=90, reasoning_tokens=40)),
        ],
        context={"context": {"used_tokens": 7000, "context_window": 400_000, "pricing": {
            "input": 0.1, "output": 0.5, "tiers": [{"context": 272_000, "input": 0.2, "output": 0.75}]}}},
    )
    table = context_turn_usage(view).splitlines()
    assert table[0].split() == ["TURN", "CONTEXT", "INPUT", "CACHE", "READ", "CACHE", "WRITE", "OUTPUT", "REASONING", "TIME"]
    assert table[1].split() == ["#1", "6.4K", "1.2K", "5K", "0", "300", "0", "4.2s"]
    assert table[2].split() == ["#2", "–", "800", "0", "0", "90", "40", "–"]
    assert table[3].split()[0] == "Total" and "2K" in table[3]
    assert price_tier_thresholds(view) == [272_000]
    assert context_pricing_note(view) == "Tiered price (input/output per M): $0.1/$0.5 · above 272K: $0.2/$0.75"
    assert "Tiered price" in context_detail_usage(view)
    assert context_turn_usage(ConversationView()) == "No turns yet."
    assert price_tier_thresholds(ConversationView()) == [] and context_pricing_note(ConversationView()) == ""


@pytest.mark.asyncio
async def test_context_meter_marks_where_the_price_rises():
    from nexus.ui_support.tui_widgets import ActivityProgress

    app = NexusTextualApp(_client(FakeTransport()))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        meter = app.query_one(ActivityProgress)
        meter.set_state(used=100_000, budget=400_000, marks=(272_000, 500_000))
        await pilot.pause()
        bar = meter.render().plain
        width = len(bar)
        assert bar.count("┃") == 1 and bar.index("┃") == min(width - 1, round(width * 0.68))
        meter.set_state(used=100_000, budget=400_000)
        assert "┃" not in meter.render().plain
