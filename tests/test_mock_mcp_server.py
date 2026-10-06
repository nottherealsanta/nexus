"""The dummy dev MCP server over real stdio (CONTEXT_SECTIONS_PLAN §5.1)."""
from __future__ import annotations

import json
import subprocess
import sys


def run(profile: str, *requests: dict) -> subprocess.CompletedProcess:
    payload = "".join(json.dumps({"jsonrpc": "2.0", **request}) + "\n" for request in requests)
    return subprocess.run([sys.executable, "-m", "nexus.devtools.mock.mcp_server", "--profile", profile],
                          input=payload, capture_output=True, text=True, timeout=20)


def replies(done: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in done.stdout.splitlines()]


def test_tracker_profile_has_rich_schemas_resources_prompts_and_instructions():
    got = replies(run("tracker", {"id": 1, "method": "initialize"}, {"id": 2, "method": "tools/list"},
                      {"id": 3, "method": "resources/list"}, {"id": 4, "method": "prompts/list"}))
    assert got[0]["result"]["serverInfo"]["name"] == "mock-tracker" and got[0]["result"]["instructions"]
    tools = {tool["name"]: tool for tool in got[1]["result"]["tools"]}
    assert len(tools) == 6 and tools["create_issue"]["inputSchema"]["properties"]["assignee"]["type"] == "object"
    assert tools["list_issues"]["inputSchema"]["properties"]["state"]["enum"] == ["open", "closed", "all"]
    assert tools["search"]["annotations"] == {"readOnlyHint": True}
    assert len(got[2]["result"]["resources"]) == 2 and len(got[3]["result"]["prompts"]) == 1


def test_tracker_calls_run_and_unknown_methods_get_32601():
    got = replies(run("tracker", {"id": 1, "method": "tools/call", "params": {"name": "list_issues", "arguments": {"state": "open"}}},
                      {"id": 2, "method": "nope"}, {"method": "notifications/initialized"}))
    assert len(got) == 2, "notifications get no reply"
    assert json.loads(got[0]["result"]["content"][0]["text"])[0]["state"] == "open"
    assert got[1]["error"]["code"] == -32601


def test_docs_profile_has_24_small_lookup_tools():
    tools = replies(run("docs", {"id": 1, "method": "tools/list"}))[0]["result"]["tools"]
    assert len(tools) == 24 and all(tool["name"].startswith("lookup_") for tool in tools)


def test_broken_profile_exits_3_before_initialize():
    done = run("broken", {"id": 1, "method": "initialize"})
    assert done.returncode == 3 and done.stdout == "" and "start-up failure" in done.stderr
