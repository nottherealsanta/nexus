"""MCP: servers configured in `config.toml`, reached through `mcp_tool_search` and `mcp_call`.

See `manager.py` for the lifecycle, `bridge.py` for how server text becomes fenced
context and `config.py` for the entry dialects. PLAN.md item G.
"""
from .manager import McpManager, McpUnavailable

MCP_SEARCH = "mcp_tool_search"
MCP_CALL = "mcp_call"
MCP_TOOLS = (MCP_SEARCH, MCP_CALL)
DISABLED_PREFIX = "mcp:"  # in a session's disabled list: `mcp:<server>`

__all__ = ["DISABLED_PREFIX", "MCP_CALL", "MCP_SEARCH", "MCP_TOOLS", "McpManager", "McpUnavailable"]
