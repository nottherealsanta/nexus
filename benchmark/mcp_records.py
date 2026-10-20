"""Offline stdio MCP server with a large, deterministic issue list.

Used by ``benchmark/codemode_eval.py`` for the MCP-style list-and-filter task:
``list_records`` returns a summary row per record (a big result), ``get_record``
returns one full record.
"""
import json
import sys

RECORDS = [
    {
        "id": i,
        "title": f"Issue {i}: " + ("flaky cache invalidation " if i % 17 == 0 else "routine maintenance ") * 2,
        "state": "open" if i % 5 in (0, 1) else "closed",
        "severity": "critical" if i % 17 == 0 else ("high" if i % 7 == 0 else "low"),
        "labels": ["bug", "area/%d" % (i % 6), "triage"],
        "body": f"Details for issue {i}. " + "Lorem ipsum dolor sit amet. " * 6,
    }
    for i in range(1, 201)
]


def _text(value):
    return {"content": [{"type": "text", "text": value}], "isError": False}


def _call(name, arguments):
    if name == "list_records":
        keys = ("id", "title", "state", "severity")
        return _text(json.dumps([{k: r[k] for k in keys} for r in RECORDS]))
    if name == "get_record":
        for record in RECORDS:
            if record["id"] == arguments.get("id"):
                return _text(json.dumps(record))
        return {"content": [{"type": "text", "text": "not found"}], "isError": True}
    return {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}


TOOLS = [
    {"name": "list_records", "description": "List every record.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_record", "description": "Fetch one record by id.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}},
                     "required": ["id"]},
     "annotations": {"readOnlyHint": True}},
]


def main():
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "benchmark-records", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = request.get("params", {})
            result = _call(params.get("name"), params.get("arguments", {}))
        elif method == "ping":
            result = {}
        elif method in {"resources/list", "resources/templates/list", "prompts/list"}:
            key = {"resources/list": "resources", "resources/templates/list":
                   "resourceTemplates", "prompts/list": "prompts"}[method]
            result = {key: []}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {
                "code": -32601, "message": "Unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
