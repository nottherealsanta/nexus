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
    result.system_text = "actual index"
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


def test_named_prompt_header_order_estimates_and_active_environment():
    from nexus.ui_support.context import estimate_tokens, header_system_prompt
    result = inspected([])
    result.included_parts = [
        {"name": "core_prompt", "text": "Core rules"},
        {"name": "environment", "text": "Workspace facts"},
        {"name": "agents_md", "text": "Project rules"},
        {"name": "memory", "text": "Remember this"},
    ]
    result.system_text = "\n\n".join(part["text"] for part in result.included_parts)
    blocks = header_blocks(result, "green")
    assert [block.key for block in blocks] == ["system", "environment", "agents", "memory", "skills", "tools", "mcp"]
    assert header_system_prompt(result) == "Core rules"
    for key, text in [("system", "Core rules"), ("environment", "Workspace facts"),
                      ("agents", "Project rules"), ("memory", "Remember this")]:
        block = next(block for block in blocks if block.key == key)
        assert block.detail == text and block.tokens == estimate_tokens(text)
    assert blocks[1].color == "green"


def test_prompt_fallback_preserves_unclassified_and_clipped_text_without_double_count():
    from nexus.ui_support.context import estimate_tokens, header_system_prompt
    for parts, text in [
        ([{"name": "core_prompt", "text": "Rules"}, {"name": "future", "text": "New text"}], "Rules\n\nNew text"),
        ([{"name": "core_prompt", "text": "Rules clipped"}, {"name": "environment", "text": "Facts"}], "Rules complete\n\nFacts"),
        ([{"name": "core_prompt", "text": "Rules complete"}], "Rules clip"),
        ([], "Legacy literal"),
    ]:
        result = inspected([])
        result.included_parts = parts
        result.system_text = text
        assert header_system_prompt(result) == text
        blocks = header_blocks(result, "green")
        assert blocks[0].detail == text and blocks[0].tokens == estimate_tokens(text)
        assert all(block.tokens is None for block in blocks if block.key in {"environment", "agents", "memory", "skills"})


def test_scoped_memory_instructions_and_unknown_scope_fallback():
    from nexus.ui_support.context import header_prompt_sections
    result = inspected([])
    instructions = '<instructions scope="soul">Soul</instructions>\n\n<instructions scope="memory">Memory</instructions>'
    result.included_parts = [{"name": "instructions", "text": instructions}]
    result.system_text = instructions
    sections, split = header_prompt_sections(result)
    assert split and sections["system"] == '<instructions scope="soul">Soul</instructions>'
    assert sections["memory"] == '<instructions scope="memory">Memory</instructions>'
    result.system_text += '\nUnclassified'
    assert header_prompt_sections(result)[0]["system"] == result.system_text


def test_context_preview_projection_bounds_skill_card_fields():
    from nexus.host_support.context_preview import project_context_preview
    row = {"name": "s", "description": "d", "frontmatter": {f"k{i}": "word " * 100 for i in range(40)},
           "index_line": "s: d", "context_tokens": 3, "skill_tokens": -1, "resources": True}
    [out] = project_context_preview({"skills_index": [row]})["skills_index"]
    assert len(out["frontmatter"]) == 32 and all(len(v) == 300 for v in out["frontmatter"].values())
    assert out["context_tokens"] == 3 and out["skill_tokens"] == 0 and out["resources"] == 0
    assert out["index_line"] == "s: d"
