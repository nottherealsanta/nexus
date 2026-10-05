"""Minimal JSONC edits, optimistic hashes and path policing."""
import hashlib
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.host_support.settings_inventory import patch_mcp_loading, set_mcp_loading


@pytest.mark.parametrize("body", ['{"servers":{"s":{ /* keep */ "command":"x"}}}',
    '{\n// comment\n"mcpServers": {"s": {"tool_loading": "search", "command": "x",}},\n}'])
def test_comments_and_formatting(body):
    result = patch_mcp_loading(body, "s", "all")
    if '"tool_loading": "search"' in body:
        assert result == body.replace('"tool_loading": "search"', '"tool_loading": "all"')
    else:
        assert result.replace('"tool_loading": "all",', '') == body


@pytest.mark.parametrize("body", ['{"servers":{"s":{},"s":{}}}', '{"servers":{"s":{}},"mcpServers":{"s":{}}}', '{"servers":{"other":{}}}'])
def test_ambiguous_or_missing_refused(body):
    with pytest.raises(ConfigError, match="safely locate"):
        patch_mcp_loading(body, "s", "all")


def test_hash_and_scope(tmp_path):
    directory = tmp_path / ".agents"
    directory.mkdir()
    path = directory / "mcp.json"
    body = '{"servers":{"s":{"command":"x"}}}'
    path.write_text(body)
    runtime = SimpleNamespace(workspace=tmp_path)
    assert set_mcp_loading(runtime, "project", "s", "all", "wrong")["status"] == "conflict"
    assert path.read_text() == body
    result = set_mcp_loading(runtime, "project", "s", "all", hashlib.sha256(body.encode()).hexdigest())
    assert result["status"] == "written"
    with pytest.raises(ConfigError, match="scope"):
        set_mcp_loading(runtime, "bad", "s", "all", result["sha256"])
    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ConfigError, match="symlink"):
        set_mcp_loading(runtime, "project", "s", "all", "")


@pytest.mark.parametrize("body", [
    '{"servers":{"s":{ /* keep */ "command":"x"}}}',
    '{\n// comment\n"mcpServers":{"s":{"enabled":true,"command":"x",}},\n}',
])
def test_enabled_preserves_jsonc(body):
    from nexus.host_support.settings_inventory import patch_mcp_enabled
    result = patch_mcp_enabled(body, "s", False)
    if '"enabled":true' in body:
        assert result == body.replace('"enabled":true', '"enabled":false')
    else:
        assert result.replace('"enabled": false,', '') == body
    with pytest.raises(ConfigError, match="bool"):
        patch_mcp_enabled(body, "s", "false")


def test_enabled_hash_conflict(tmp_path):
    from nexus.host_support.settings_inventory import set_mcp_enabled
    directory = tmp_path / ".agents"
    directory.mkdir()
    path = directory / "mcp.json"
    body = '{"servers":{"s":{"command":"x"}}}'
    path.write_text(body)
    runtime = SimpleNamespace(workspace=tmp_path)
    assert set_mcp_enabled(runtime, "project", "s", False, "wrong")["status"] == "conflict"
    assert path.read_text() == body
    assert set_mcp_enabled(runtime, "project", "s", False, hashlib.sha256(body.encode()).hexdigest())["status"] == "written"
