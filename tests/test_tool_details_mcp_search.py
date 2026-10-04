"""Readable MCP proxy and search disclosures after replay."""
from nexus.ui_support.tool_details import tool_detail_sections, sections_to_text
from nexus.ui_support.timeline import tool_heading
from nexus.view import ToolCallView, fold
from nexus.events import Event


def test_search_queries_and_schema_rows():
    tool = ToolCallView(name="McpSearch", result=[{"type": "text", "text":
        'Query 1 · server fs · read · 1 of 2 tools\n1. fs/read (read only)\n   Description: Read a file\n   Input schema: {"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}\nQuery 2 · server missing · x · Unknown MCP server'}])
    text = sections_to_text(tool_detail_sections(tool))
    assert "QUERY 1" in text and "QUERY 2" in text
    assert "Input schema.properties.path.type: string" in text
    assert '{"type"' not in text


def test_proxy_heading_and_durable_target():
    tool = ToolCallView(name="McpCall", target="mcp__fs__read_file", input={"tool": "fs/read_file", "arguments": {"path": "hello.txt"}})
    assert "fs · read_file" in tool_heading(tool)
    text = sections_to_text(tool_detail_sections(tool))
    assert "Target: mcp__fs__read_file" in text and "arguments.path: hello.txt" in text
    view = fold([Event(type="tool.started", session="s", turn="t", data={"call_id": "c", "tool": "McpCall", "target": "mcp__fs__read_file"})])
    assert view.turns[0].tools[0].target == "mcp__fs__read_file"


def test_denied_target_survives_replay_without_resolved_target():
    events = [
        Event(type="tool.requested", session="s", turn="t", data={"call_id": "c", "tool": "McpCall", "input": {"tool": "fs/write_file", "arguments": {"path": "x"}}}),
        Event(type="tool.failed", session="s", turn="t", data={"call_id": "c", "tool": "McpCall", "error": "Target mcp__fs__write_file is not permitted by this agent's tool restrictions"}),
    ]
    tool = fold(events).turns[0].tools[0]
    assert tool.target is None
    assert "fs · write_file" in tool_heading(tool)
    text = sections_to_text(tool_detail_sections(tool))
    assert "not permitted by this agent's tool restrictions" in text
    assert "fs/write_file" in text
