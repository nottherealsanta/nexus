"""Bounded search-mode names, counts and loading chips."""

from nexus.context.parts import freeze_mcp_index
from nexus.host.protocol import ContextInspectResult
from nexus.mcp.manager import MCPServerSnapshot, MCPHealth
from nexus.ui_support.context_header import header_blocks
from test_mcp_search_rank import tool


def test_index_names_only_and_clipping():
    tools = tuple(tool(f"read_file_{i}") for i in range(100))
    index = freeze_mcp_index({"s": MCPServerSnapshot(name="s", health=MCPHealth.READY, tools=tools)})
    assert "mode: search" in index and "100 tools" in index
    assert "read_file_0" in index and "more; use McpSearch" in index
    assert '"properties"' not in index


def test_header_shows_modes_and_deferred_tokens():
    result = ContextInspectResult(session="s", mcp_servers=[{"name": "s", "tool_loading": "search", "tool_count": 100, "schema_tokens": 1234}])
    block = next(block for block in header_blocks(result, "green") if block.key == "mcp")
    assert "100 · search" in block.body
    assert "1234 tokens deferred" in block.detail
    assert block.tokens == 0
