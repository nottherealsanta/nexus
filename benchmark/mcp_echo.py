"""Offline stdio MCP echo server for checking extension loading."""
import json
import sys


def main():
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "benchmark-echo", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "echo", "description": "Return the supplied text.",
                "inputSchema": {"type": "object", "properties": {
                    "text": {"type": "string"}}, "required": ["text"]},
                "annotations": {"readOnlyHint": True},
            }]}
        elif method == "tools/call":
            params = request.get("params", {})
            result = {"content": [{"type": "text", "text":
                params.get("arguments", {}).get("text", "")}], "isError": False}
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
