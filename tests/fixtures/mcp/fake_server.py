"""A tiny, deterministic fake MCP stdio server for client tests.

It speaks newline-delimited JSON-RPC 2.0 over stdin/stdout, exactly as the MCP
stdio transport specifies. Behaviour is selected by ``MCP_FIXTURE_MODE`` so one
script can act as a well-behaved server, a server that hangs mid-request, one
that dies mid-call, one that emits malformed framing, one that leaks a secret
into stderr or an error reply, one that spawns a grandchild (to prove
process-group cleanup), and one that paginates.

No test reaches the network; this process is the only server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

SECRET = os.environ.get("MCP_FIXTURE_SECRET", "sk-fixture-secret-value-123456")
MODE = os.environ.get("MCP_FIXTURE_MODE", "normal")
PID_FILE = os.environ.get("MCP_FIXTURE_PID_FILE", "")
ARGV_FILE = os.environ.get("MCP_FIXTURE_ARGV_FILE", "")


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def reply(request_id, result) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def reply_error(request_id, code, message) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def announce(message: str) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {"note": message},
        }
    )


TOOL_ECHO = {
    "name": "echo",
    "description": "Echo the supplied text.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    },
    "annotations": {"title": "Echo", "readOnlyHint": True},
}

TOOL_BOOM = {
    "name": "boom",
    "description": "Always reports a tool failure.",
    "inputSchema": {"type": "object"},
}


def tools_list(cursor):
    if MODE == "paginate":
        if not cursor:
            return {"tools": [TOOL_ECHO], "nextCursor": "page-2"}
        return {"tools": [TOOL_BOOM]}
    return {"tools": [TOOL_ECHO, TOOL_BOOM]}


def handle(message: dict):
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        if MODE == "exit_after_init":
            reply(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1.2.3"},
                    "instructions": "fixture",
                },
            )
            os._exit(3)
        reply(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {},
                    "prompts": {},
                },
                "serverInfo": {"name": "fake", "version": "1.2.3"},
                "instructions": "fixture",
            },
        )
        return

    if method == "notifications/initialized":
        announce("ready")
        return

    if method == "ping":
        reply(request_id, {})
        return

    if method == "tools/list":
        if MODE == "hang_tools_list":
            while True:
                time.sleep(3600)
        if MODE == "malformed_tools_list":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            return
        reply(request_id, tools_list(message.get("params", {}).get("cursor")))
        return

    if method == "tools/call":
        if MODE in ("die_mid_call", "hang_grandchild"):
            if MODE == "hang_grandchild":
                while True:
                    time.sleep(3600)
            os._exit(7)
        name = message.get("params", {}).get("name")
        arguments = message.get("params", {}).get("arguments", {})
        if name == "echo":
            reply(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": str(arguments.get("text", ""))}
                    ],
                    "isError": False,
                },
            )
            return
        if name == "boom":
            reply(
                request_id,
                {"content": [{"type": "text", "text": "tool failed"}], "isError": True},
            )
            return
        if name == "leak" and MODE == "leak_error":
            reply_error(request_id, -32000, f"upstream said api_key={SECRET}")
            return
        reply_error(request_id, -32602, f"unknown tool {name!r}")
        return

    if method == "resources/list":
        reply(
            request_id,
            {
                "resources": [
                    {
                        "uri": "file:///a.txt",
                        "name": "a",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
        return

    if method == "resources/templates/list":
        if MODE == "no_templates":
            reply_error(request_id, -32601, "method not found")
            return
        reply(
            request_id,
            {
                "resourceTemplates": [
                    {
                        "uriTemplate": "file:///{path}",
                        "name": "file",
                        "mimeType": "text/plain",
                    }
                ]
            },
        )
        return

    if method == "resources/read":
        reply(
            request_id,
            {
                "contents": [
                    {
                        "uri": message["params"]["uri"],
                        "mimeType": "text/plain",
                        "text": "hello",
                    }
                ]
            },
        )
        return

    if method == "prompts/list":
        reply(
            request_id,
            {
                "prompts": [
                    {
                        "name": "greet",
                        "description": "A greeting",
                        "arguments": [{"name": "who", "required": True}],
                    }
                ]
            },
        )
        return

    if method == "prompts/get":
        reply(
            request_id,
            {
                "description": "A greeting",
                "messages": [
                    {"role": "user", "content": {"type": "text", "text": "hi"}}
                ],
            },
        )
        return

    if request_id is not None:
        reply_error(request_id, -32601, f"method not found: {method!r}")


def spawn_grandchild() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    if PID_FILE:
        with open(PID_FILE, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))


def main() -> None:
    if ARGV_FILE:
        with open(ARGV_FILE, "w", encoding="utf-8") as fh:
            json.dump(sys.argv[1:], fh)
    if MODE in ("stderr_secret",):
        sys.stderr.write(f"boot token={SECRET}\n")
        sys.stderr.flush()
    if MODE in ("grandchild", "hang_grandchild"):
        spawn_grandchild()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        try:
            handle(message)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - a fixture should keep going
            sys.stderr.write(f"fixture error: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
