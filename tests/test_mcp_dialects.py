"""mcp.json dialects: entries written for VS Code, Claude Code, Cursor, OpenCode, Zed, Gemini and Cline load as-is."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nexus.ext.manager import ExtensionManager
from nexus.mcp.client import normalize_server_entry, parse_server_config
from nexus.mcp.errors import MCPConfigError
from nexus.mcp.manager import MCPManager

ENV = {"TOKEN": "ghp_fixture_token_1234", "USER_NAME": "someone"}


def test_vscode_type_http_with_env_reference():
    config = parse_server_config("github", {"type": "http", "url": "https://api.githubcopilot.com/mcp/",
                                            "headers": {"Authorization": "Bearer ${env:TOKEN}"}}, environ=ENV)
    assert config.transport == "http"
    assert config.headers["Authorization"] == "Bearer " + ENV["TOKEN"]
    assert ENV["TOKEN"] in config.secrets


def test_opencode_local_command_list_and_environment():
    config = parse_server_config("data", {"type": "local", "command": ["uv", "run", "python", "s.py"],
                                          "environment": {"DIR": "{env:USER_NAME}"}}, environ=ENV)
    assert (config.transport, config.command, config.args) == ("stdio", "uv", ("run", "python", "s.py"))
    assert config.env == {"DIR": "someone"}


def test_opencode_remote_and_timeout_in_milliseconds():
    config = parse_server_config("r", {"type": "remote", "url": "https://h/mcp", "timeout": 5000}, environ=ENV)
    assert (config.transport, config.call_timeout_s) == ("http", 5.0)


def test_roo_timeout_in_seconds():
    assert parse_server_config("r", {"command": "x", "timeout": 30}, environ=ENV).call_timeout_s == 30.0


def test_url_without_type_is_http_or_sse_by_path():
    assert parse_server_config("a", {"url": "https://h/mcp"}, environ=ENV).transport == "http"
    assert parse_server_config("b", {"url": "https://h/sse"}, environ=ENV).transport == "sse"
    assert parse_server_config("c", {"serverUrl": "https://h/mcp"}, environ=ENV).url == "https://h/mcp"
    assert parse_server_config("d", {"httpUrl": "https://h/x"}, environ=ENV).transport == "http"


def test_claude_code_default_expansion_and_editor_variables(tmp_path):
    config = parse_server_config("s", {"command": "${workspaceFolder}/bin/run",
                                       "args": ["${MISSING:-fallback}", "${userHome}", "${/}"]},
                                 environ=ENV, workspace=tmp_path, home="/home/me")
    assert config.command == f"{tmp_path}/bin/run"
    assert config.args == ("fallback", "/home/me", "/")


def test_input_prompts_are_refused_with_a_hint():
    with pytest.raises(MCPConfigError, match="input"):
        parse_server_config("s", {"command": "x", "env": {"K": "${input:token}"}}, environ=ENV)


def test_zed_command_object():
    config = parse_server_config("z", {"source": "custom", "command": {"path": "node", "args": ["a.js"], "env": {"A": "1"}}},
                                 environ=ENV)
    assert (config.command, config.args, dict(config.env), config.ignored_keys) == ("node", ("a.js",), {"A": "1"}, ("source",))


def test_foreign_keys_are_kept_visible_not_fatal():
    data, ignored = normalize_server_entry("c", {"command": "x", "autoApprove": ["t"], "alwaysAllow": [], "disabled": True})
    assert ignored == ("alwaysAllow", "autoApprove")
    assert "disabled" not in data


def test_tool_filters_and_always_load():
    config = parse_server_config("g", {"command": "x", "includeTools": ["a", "b"], "excludeTools": ["b"],
                                       "alwaysLoad": True}, environ=ENV)
    assert [config.allows_tool(name) for name in ("a", "b", "c")] == [True, False, False]
    assert config.tool_loading == "all"


def test_env_file_under_explicit_env(tmp_path):
    (tmp_path / ".env").write_text("export A=from-file\nB='quoted'\n# note\nC=1 # trailing\n")
    config = parse_server_config("e", {"command": "x", "envFile": "${workspaceFolder}/.env", "env": {"A": "explicit"}},
                                 environ=ENV, workspace=tmp_path)
    assert dict(config.env) == {"A": "explicit", "B": "quoted", "C": "1"}


def test_conflicting_transport_keys_are_refused():
    with pytest.raises(MCPConfigError, match="disagree"):
        normalize_server_entry("s", {"type": "sse", "transport": "http", "url": "https://h"})
    with pytest.raises(MCPConfigError, match="not supported"):
        normalize_server_entry("s", {"type": "ws", "url": "wss://h"})


def test_disabled_flag_turns_the_server_off():
    manager = MCPManager(None, environ=ENV)
    parsed, failures = manager._parse({"a": {"command": "x", "disabled": True}, "b": {"command": "y", "enabled": False}})
    assert failures == () and not parsed["a"].enabled and not parsed["b"].enabled


def _extensions(tmp_path: Path, global_doc: object, project_doc: object, project_name: str = ".agents"):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    (workspace / project_name).mkdir(parents=True)
    (home / ".nexus").mkdir(parents=True)
    for path, doc in ((home / ".nexus" / "mcp.json", global_doc), (workspace / project_name / "mcp.json", project_doc)):
        if doc is not None:
            path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    manager = MCPManager(None, environ=ENV, workspace=workspace, home=home)
    return ExtensionManager(workspace, home=home, mcp=manager), manager


def test_global_and_project_merge_with_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home" / ".nexus"))
    ext, manager = _extensions(tmp_path, {"mcpServers": {"g": {"command": "x"}, "both": {"command": "x"}}},
                               {"$schema": "x", "inputs": [], "servers": {"p": {"type": "stdio", "command": "y"},
                                                                         "both": {"command": "z"}}})
    asyncio.run(ext.sync_mcp())
    assert sorted(manager.definitions) == ["both", "g", "p"]
    assert manager.definitions["both"].config.command == "z"
    assert ext.mcp_scopes == {"g": "global", "both": "project", "p": "project"}
    assert ext.mcp_sources["g"] == "~/.nexus/mcp.json" and ext.mcp_sources["p"] == ".agents/mcp.json"
    assert ext.mcp_file_errors == ()


def test_a_broken_project_file_does_not_hide_global_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home" / ".nexus"))
    ext, manager = _extensions(tmp_path, {"mcpServers": {"g": {"command": "x"}}}, "{not json")
    asyncio.run(ext.sync_mcp())
    assert list(manager.definitions) == ["g"]
    assert [row["scope"] for row in ext.mcp_file_errors] == ["project"]


def test_unknown_top_level_keys_warn_but_load(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home" / ".nexus"))
    ext, manager = _extensions(tmp_path, None, {"mcp": {"o": {"type": "local", "command": ["a"]}}, "theme": "dark"})
    asyncio.run(ext.sync_mcp())
    assert list(manager.definitions) == ["o"]
    assert ext.mcp_file_errors[0]["warning"] and "theme" in ext.mcp_file_errors[0]["error"]


def test_invalid_entries_are_reported_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_HOME", str(tmp_path / "home" / ".nexus"))
    ext, manager = _extensions(tmp_path, None, {"servers": {"ok": {"command": "x"}, "bad": {"command": "x", "typo": 1}}})
    asyncio.run(ext.sync_mcp())
    assert list(manager.definitions) == ["ok"]
    assert [failure.name for failure in manager.failures] == ["bad"]


def test_settings_switch_flips_disabled_when_the_entry_uses_it():
    from nexus.host_support.settings_inventory import patch_mcp_enabled
    body = '{"mcpServers": {"c": {"command": "x", "disabled": true}}}'
    assert json.loads(patch_mcp_enabled(body, "c", True))["mcpServers"]["c"] == {"command": "x", "disabled": False}
    vscode = '{"servers": {"v": {"type": "stdio", "command": "x"}}}'
    assert json.loads(patch_mcp_enabled(vscode, "v", False))["servers"]["v"]["enabled"] is False


def test_settings_save_rejects_a_broken_entry_with_its_reason():
    from nexus.errors import ConfigError
    from nexus.host_support.settings_inventory import _validate
    _validate("mcp", "mcp.json", '{"mcp": {"o": {"type": "local", "command": ["a"]}}}')
    with pytest.raises(ConfigError, match="not supported"):
        _validate("mcp", "mcp.json", '{"servers": {"w": {"type": "ws", "url": "wss://h"}}}')
