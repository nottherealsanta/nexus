"""Keep the shipped examples honest.

Docs rot silently, so these tests exercise the examples the README and
EXTENDING.md point at:

* the facade example runs end to end, offline, with no credentials;
* every config/data example parses through the *same* interface the runtime
  uses (layered config, MCP server config, skill/agent frontmatter, hooks.toml);
* the example tool module loads and runs.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import msgspec

from nexus.agents.model import parse_frontmatter as parse_agent_frontmatter
from nexus.config.schema import ConfigV2
from nexus.hooks.manager import HookManager
from nexus.mcp.client import parse_server_config
from nexus.skills.frontmatter import parse_frontmatter as parse_skill_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"


def test_facade_example_runs_offline_end_to_end():
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "python_api.py")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "turn.completed" in result.stdout
    assert "wrote: 'hello from Nexus'" in result.stdout


def test_example_workspace_config_is_valid():
    text = (EXAMPLES / "nexus.toml").read_text(encoding="utf-8")
    config = msgspec.toml.decode(text.encode("utf-8"), type=ConfigV2)
    assert config.models.default == "medium"
    assert set(config.models.tiers) == {"high", "medium", "low"}
    assert config.providers["groq"].kind == "openai_compatible"
    assert config.agents.max_tier == "medium"


def test_example_mcp_json_is_valid():
    document = json.loads((EXAMPLES / "mcp.json").read_text(encoding="utf-8"))
    environ = {"NEXUS_EXAMPLE_ROOT": "/tmp/data", "MCP_TOKEN": "token"}
    configs = {
        name: parse_server_config(name, entry, environ=environ)
        for name, entry in document["servers"].items()
    }
    assert configs["filesystem"].transport == "stdio"
    assert configs["filesystem"].args[-1] == "/tmp/data"
    assert configs["internal"].transport == "http"


def test_example_skill_frontmatter_is_valid():
    parsed = parse_skill_frontmatter(
        (EXAMPLES / "skills" / "hello-world" / "SKILL.md").read_bytes()
    )
    assert parsed.name == "hello-world"
    assert parsed.bundles == ("fs",)


def test_example_agent_frontmatter_is_valid():
    parsed = parse_agent_frontmatter(
        (EXAMPLES / "agents" / "researcher.md").read_bytes()
    )
    assert parsed.name == "researcher"
    assert parsed.model == "low"
    assert "write" in parsed.excluded_tools


def test_example_tool_module_loads_and_runs():
    spec = importlib.util.spec_from_file_location(
        "nexus_example_greet", EXAMPLES / "tools" / "greet.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.SPEC.name == "Greet"
    assert module.SPEC.input_schema["type"] == "object"

    ok = asyncio.run(module.run({"name": "Nexus"}, None))
    assert ok.is_error is False
    assert "Nexus" in ok.content[0].text

    bad = asyncio.run(module.run({}, None))
    assert bad.is_error is True


def test_example_hooks_toml_loads():
    workspace = Path(tempfile.mkdtemp(prefix="nexus-example-hooks-"))
    try:
        (workspace / ".nexus").mkdir()
        shutil.copy(EXAMPLES / "hooks.toml", workspace / ".nexus" / "hooks.toml")
        manager = HookManager(workspace, home=workspace)
        snapshot = manager.refresh()
        assert snapshot.failures == ()
        assert {spec.event for spec in snapshot.specs} == {"PreToolUse", "SessionStart"}
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
