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


def test_inventory_limits_keep_complete_details_and_statuses():
    servers = [{"name": f"server{i}", "status": "ready", "tools": [f"tool{i}"]} for i in range(8)]
    block = mcp_block(servers)
    assert len(block.inventory) == 8
    assert "server4" in block.body and "server5" not in block.body
    assert "8 total · 3 omitted" in block.body
    assert "ready" in block.body and "tool7" in block.detail


def test_skills_estimate_uses_included_index_not_available_catalogue():
    result = inspected([])
    result.skills_index = [{"name": f"skill{i}", "description": "long description"} for i in range(14)]
    skill = lambda: next(b for b in header_blocks(result, "green") if b.key == "skills")
    assert skill().tokens is None
    assert "14 total · 4 omitted" in skill().body
    assert "skill13" in skill().detail
    result.included_parts = [{"name": "skills_index", "text": "actual index"}]
    from nexus.ui_support.context import estimate_tokens
    assert skill().tokens == estimate_tokens("actual index")


def test_skill_inventory_labels_actual_included_entry_tokens():
    from nexus.ui_support.context import estimate_tokens
    result = inspected([])
    result.skills_index = [{"name": "nexus-ratatui"}, {"name": "unavailable"}]
    entry = "nexus-ratatui: Native TUI development (skill: /tmp/SKILL.md)"
    result.included_parts = [{"name": "skills_index", "text": "Skills available:\n" + entry}]
    skill = next(b for b in header_blocks(result, "blue") if b.key == "skills")
    assert skill.inventory == (
        f"nexus-ratatui · ~{estimate_tokens(entry)} tokens",
        "unavailable · tokens unknown",
    )
