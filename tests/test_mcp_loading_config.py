"""Strict MCP tool-loading defaults and validation."""
import pytest

from nexus.mcp.client import parse_server_config
from nexus.mcp.errors import MCPConfigError


def test_defaults_and_explicit_modes():
    assert parse_server_config("s", {"command": "server"}).tool_loading == "search"
    config = parse_server_config("s", {"command": "server", "tool_loading": "all"})
    assert config.tool_loading == "all"
    assert config.tool_loading_source == "config"


@pytest.mark.parametrize("mode", [None, False, 1, "ALL", {}, []])
def test_invalid(mode):
    with pytest.raises(MCPConfigError, match="tool_loading"):
        parse_server_config("s", {"command": "server", "tool_loading": mode})
