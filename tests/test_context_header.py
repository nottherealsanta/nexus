"""Shared context-header MCP block: mode chips and deferred tokens (MCP_SEARCH_PLAN section 9)."""
from types import SimpleNamespace

from nexus.ui_support.context_header import header_blocks


def inspected(servers):
    return SimpleNamespace(tools=[], included_parts=[], skills_index=[], mcp_index="", mcp_servers=servers,
                           system_text="")


def mcp_block(servers):
    return next(block for block in header_blocks(inspected(servers), "green") if block.key == "mcp")


def test_mcp_block_shows_mode_per_server_and_deferred_tokens():
    block = mcp_block([
        {"name": "github", "status": "ready", "tool_count": 41, "tool_loading": "search", "schema_tokens": 9000, "tools": ["create_issue"]},
        {"name": "filesystem", "status": "ready", "tool_count": 6, "tool_loading": "all", "schema_tokens": 800, "tools": ["read_file"]},
        {"name": "off", "status": "ready", "tool_count": 3, "tool_loading": "search", "schema_tokens": 500, "enabled": False, "tools": []},
    ])
    assert "github(41 · search)" in block.body and "filesystem(6 · all)" in block.body
    assert "off(3 · search) (off)" in block.body
    # Only enabled search-mode servers count as deferred.
    assert "~9000 tokens deferred" in block.body and "~9000 tokens deferred" in block.detail
    assert "create_issue" in block.detail


def test_mcp_block_without_deferred_servers_has_no_deferred_line():
    block = mcp_block([{"name": "fs", "status": "ready", "tool_count": 2, "tool_loading": "all", "schema_tokens": 800, "tools": []}])
    assert "deferred" not in block.body and "deferred" not in block.detail
