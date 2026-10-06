"""Dummy stdio MCP server for the dev sandbox (plan CONTEXT_SECTIONS_PLAN §5.1).

Extends the ``benchmark/mcp_echo.py`` protocol subset: ``initialize`` (with
``instructions``), ``tools/list``, ``tools/call``, ``resources/list|read``,
``prompts/list|get`` and ``ping``. It never touches the network and reads only its
own fixtures. Bounded: one JSON line per reply, request lines at most 1 MB, unknown
methods get ``-32601``. ``--profile`` picks the identity:

* ``tracker``: six rich tools, two resources, one prompt, instructions (``tool_loading: all``).
* ``docs``: 24 small ``lookup_*`` tools (``tool_loading: search``), for the deferred figure.
* ``broken``: writes to stderr and exits with code 3 before ``initialize`` (failed status).
"""
from __future__ import annotations

import argparse
import json
import sys

MAX_LINE = 1_000_000
PROTOCOL = "2025-06-18"

_ISSUES = [
    {"id": 1, "title": "Login fails on Safari", "state": "open", "labels": ["bug"]},
    {"id": 2, "title": "Add dark mode", "state": "open", "labels": ["feature"]},
    {"id": 3, "title": "Upgrade dependencies", "state": "closed", "labels": ["chore"]},
]


def _tool(name, description, properties=None, required=(), annotations=None):
    tool = {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": list(required)}}
    if annotations:
        tool["annotations"] = annotations
    return tool


def tracker_tools() -> list[dict]:
    return [
        _tool("list_issues", "List issues, optionally filtered by state and labels.", {
            "state": {"type": "string", "enum": ["open", "closed", "all"], "description": "Which issues to return"},
            "limit": {"type": "integer", "default": 20, "description": "Maximum number of issues"},
            "labels": {"type": "array", "items": {"type": "string"}, "description": "Only issues with all these labels"},
        }, annotations={"readOnlyHint": True}),
        _tool("get_issue", "Fetch one issue by id.", {"id": {"type": "integer"}}, ["id"], {"readOnlyHint": True}),
        _tool("create_issue", "Create an issue.", {
            "title": {"type": "string", "description": "Short summary"},
            "body": {"type": "string"},
            "assignee": {"type": "object", "description": "Who owns it", "properties": {
                "name": {"type": "string"}, "email": {"type": "string"}}},
        }, ["title"]),
        _tool("add_comment", "Comment on an issue.", {"id": {"type": "integer"}, "text": {"type": "string"}}, ["id", "text"]),
        _tool("close_issue", "Close an issue.", {"id": {"type": "integer"}}, ["id"]),
        _tool("search", "Search issue titles.", {"query": {"type": "string"}}, ["query"], {"readOnlyHint": True}),
    ]


def docs_tools() -> list[dict]:
    topics = ["install", "config", "tools", "skills", "mcp", "agents", "sessions", "models", "hooks", "voice",
              "web", "desktop", "release", "testing", "security", "context", "loop", "events", "daemon", "cli",
              "storage", "providers", "worktrees", "permissions"]
    return [_tool(f"lookup_{topic}", f"Look up the {topic} page of the mock docs.",
                  {"query": {"type": "string"}}, ["query"], {"readOnlyHint": True}) for topic in topics]


PROFILES = {
    "tracker": {
        "info": {"name": "mock-tracker", "version": "1.2.0"},
        "instructions": "Mock issue tracker. Issue state lives in memory for this process only.",
        "tools": tracker_tools,
        "resources": [
            {"uri": "mock://tracker/labels", "name": "labels", "mimeType": "application/json"},
            {"uri": "mock://tracker/readme", "name": "readme", "mimeType": "text/markdown"},
        ],
        "prompts": [{"name": "triage", "description": "Triage the open issues."}],
    },
    "docs": {
        "info": {"name": "mock-docs", "version": "0.4.0"},
        "instructions": "Mock documentation index. Use the search tool to find lookup_* tools.",
        "tools": docs_tools, "resources": [], "prompts": [],
    },
}


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(profile: str, name: str, arguments: dict) -> dict:
    if profile == "docs":
        return _text(f"{name}: no page found for {arguments.get('query', '')!r} (mock)")
    if name == "list_issues":
        state = arguments.get("state", "all")
        rows = [i for i in _ISSUES if state == "all" or i["state"] == state][: int(arguments.get("limit", 20))]
        return _text(json.dumps(rows))
    if name == "get_issue":
        found = next((i for i in _ISSUES if i["id"] == arguments.get("id")), None)
        return _text(json.dumps(found)) if found else _text("no such issue", True)
    if name == "create_issue":
        issue = {"id": len(_ISSUES) + 1, "title": arguments.get("title", ""), "state": "open", "labels": []}
        _ISSUES.append(issue)
        return _text(json.dumps(issue))
    if name == "add_comment":
        return _text("comment added")
    if name == "close_issue":
        for issue in _ISSUES:
            if issue["id"] == arguments.get("id"):
                issue["state"] = "closed"
                return _text(json.dumps(issue))
        return _text("no such issue", True)
    if name == "search":
        query = str(arguments.get("query", "")).lower()
        return _text(json.dumps([i for i in _ISSUES if query in i["title"].lower()]))
    return _text(f"unknown tool {name}", True)


def handle(profile: str, request: dict) -> dict | None:
    """One JSON-RPC reply for a request, or ``None`` for notifications."""
    if "id" not in request:
        return None
    spec = PROFILES[profile]
    method, params = request.get("method"), request.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "serverInfo": spec["info"], "instructions": spec["instructions"],
                  "capabilities": {"tools": {}, "resources": {}, "prompts": {}}}
    elif method == "tools/list":
        result = {"tools": spec["tools"]()}
    elif method == "tools/call":
        result = call_tool(profile, str(params.get("name", "")), params.get("arguments") or {})
    elif method == "resources/list":
        result = {"resources": spec["resources"]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": []}
    elif method == "resources/read":
        uri = params.get("uri", "")
        result = {"contents": [{"uri": uri, "mimeType": "text/plain", "text": f"mock content of {uri}"}]}
    elif method == "prompts/list":
        result = {"prompts": spec["prompts"]}
    elif method == "prompts/get":
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": "List the open issues and rank them."}}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Unknown method"}}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=["tracker", "docs", "broken"], default="tracker")
    args = parser.parse_args(argv)
    if args.profile == "broken":
        print("mock-broken: simulated start-up failure", file=sys.stderr, flush=True)
        return 3
    for line in sys.stdin:
        if len(line) > MAX_LINE:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(args.profile, request) if isinstance(request, dict) else None
        if reply is not None:
            print(json.dumps(reply), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
