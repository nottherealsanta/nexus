"""A scriptable fake MCP stdio server for the manager's integration tests.

It speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and is driven by a
JSON *control file* (``MCP_FIXTURE_CONTROL``) so a test can change its behaviour
between launches: let the first N launches fail (to exercise restart/backoff),
die mid-call, hang a listing, announce ``tools/list_changed``, or expose a
different tool set.

Behaviour selected by the control file (re-read per request where it matters):

* ``fail_launches``  -- exit non-zero until this many launches have happened;
* ``version``        -- the reported server version (drives the cache key);
* ``tools``          -- extra tool names to expose;
* ``die_on_call``    -- kill the process on any ``tools/call``;
* ``hang_tools``     -- never answer ``tools/list``;
* ``malformed``      -- emit a non-JSON line for ``tools/list``;
* ``notify_list_changed`` -- announce ``tools/list_changed`` after init and add
  an ``extra`` tool from the *second* ``tools/list`` call on, so a refresh is
  observably different from the first listing.

This process never touches the network. It is intentionally independent of the
manager and the client so an integration test exercises the real stdio
transport end to end.
"""

from __future__ import annotations

import json
import os
import sys
import time

CONTROL = os.environ.get("MCP_FIXTURE_CONTROL", "")
MODE = os.environ.get("MCP_FIXTURE_MODE", "normal")

_list_calls = 0


def read_control() -> dict:
    if not CONTROL:
        return {}
    try:
        with open(CONTROL, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_control(data: dict) -> None:
    if not CONTROL:
        return
    temporary = f"{CONTROL}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, CONTROL)


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


def notify(method: str, params: dict | None = None) -> None:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    send(message)


def tool(name: str) -> dict:
    return {
        "name": name,
        "description": f"Fixture tool {name}.",
        "inputSchema": {"type": "object", "properties": {}},
    }


def handle(message: dict) -> None:
    global _list_calls
    method = message.get("method")
    request_id = message.get("id")
    control = read_control()

    if method == "initialize":
        reply(
            request_id,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {
                    "name": "fixture",
                    "version": str(control.get("version", "1")),
                },
                "instructions": "fixture server",
            },
        )
        return

    if method == "notifications/initialized":
        if control.get("notify_list_changed"):
            notify("notifications/tools/list_changed", {"note": "ready"})
        return

    if method == "ping":
        reply(request_id, {})
        return

    if method == "tools/list":
        if control.get("hang_tools") or MODE == "hang":
            while True:
                time.sleep(3600)
        if control.get("malformed") or MODE == "malformed":
            sys.stdout.write("this is not json\n")
            sys.stdout.flush()
            return
        names = ["echo", "crash"]
        names.extend(str(item) for item in control.get("tools", []) or [])
        if control.get("notify_list_changed") and _list_calls >= 1:
            names.append("extra")
        _list_calls += 1
        reply(request_id, {"tools": [tool(name) for name in names]})
        return

    if method == "tools/call":
        if control.get("die_on_call") or MODE == "die":
            os._exit(9)
        name = (message.get("params") or {}).get("name")
        arguments = (message.get("params") or {}).get("arguments") or {}
        if name == "crash":
            os._exit(9)
        if name == "echo":
            reply(
                request_id,
                {
                    "content": [{"type": "text", "text": str(arguments.get("text", ""))}],
                    "isError": False,
                },
            )
            return
        if name == "extra":
            reply(
                request_id,
                {"content": [{"type": "text", "text": "extra"}], "isError": False},
            )
            return
        reply_error(request_id, -32602, f"unknown tool {name!r}")
        return

    if method == "resources/list":
        reply(
            request_id,
            {
                "resources": [
                    {"uri": "file:///fixture.txt", "name": "fixture", "mimeType": "text/plain"}
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
                        "uri": (message.get("params") or {}).get("uri", "file:///fixture.txt"),
                        "mimeType": "text/plain",
                        "text": "fixture-body",
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
                    {"role": "user", "content": {"type": "text", "text": "hello"}}
                ],
            },
        )
        return

    if request_id is not None:
        reply_error(request_id, -32601, f"method not found: {method!r}")


def main() -> None:
    if CONTROL:
        control = read_control()
        launches = int(control.get("launches", 0)) + 1
        control["launches"] = launches
        write_control(control)
        if launches <= int(control.get("fail_launches", 0) or 0):
            sys.stderr.write(f"fixture launch {launches} configured to fail\n")
            sys.stderr.flush()
            raise SystemExit(3)

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
        except Exception as exc:  # noqa: BLE001 - a fixture keeps running
            sys.stderr.write(f"fixture error: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
