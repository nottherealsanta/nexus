"""One isolated Agent SDK request (PLAN.md step 5b). Private to `claude.py`.

Reads one JSON line (the provider body plus `executable`) on stdin and writes one
JSON line on stdout: `{"output", "usage"}` or `{"error", "detail"}`. Tool handlers
never execute anything; the worker returns at the first tool call so Nexus runs it.
"""
from __future__ import annotations

import asyncio
import json
import sys

MAX_BYTES = 8 * 1024 * 1024


async def run(payload: dict, sdk=None) -> dict:
    if sdk is None:
        import claude_agent_sdk as sdk
    pending: list[dict] = []
    tools = []
    for definition in payload["tools"]:
        name = definition["name"]

        async def queue(arguments, name=name):
            if len(pending) >= 32:
                raise ValueError("too many tool intentions")
            pending.append({"name": name, "arguments": json.dumps(arguments)})
            return {"content": [{"type": "text", "text": "Queued for Nexus approval; no execution has occurred. End this response now."}]}

        tools.append(sdk.tool(name, definition["description"], definition["input_schema"])(queue))
    servers = {"nexus": sdk.create_sdk_mcp_server("nexus", tools=tools)} if tools else {}
    stderr: list[str] = []
    options = sdk.ClaudeAgentOptions(
        model=payload["model"], cli_path=payload.get("executable"), system_prompt=payload["system"],
        tools=[], mcp_servers=servers, setting_sources=[],
        allowed_tools=["mcp__nexus__" + t["name"] for t in payload["tools"]],
        settings=json.dumps({"disableAllHooks": True}), permission_mode="dontAsk", max_turns=3,
        extra_args={"strict-mcp-config": None, "no-session-persistence": None},
        output_format={"type": "json_schema", "schema": payload["schema"]},
        # Without this the CLI makes a second, hidden model request per query to title the session, and that request
        # contains the whole history. Measured 2026-10-30 against a local capture server.
        env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        max_buffer_size=MAX_BYTES, stderr=lambda line: stderr.append(line[:300]) if len(stderr) < 20 else None,
    )
    if payload.get("effort"):
        options.effort = payload["effort"]
    declared = {"mcp__nexus__" + t["name"]: t["name"] for t in payload["tools"]}
    result: dict = {"error": "the SDK ended without a result", "detail": "\n".join(stderr)}
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(json.dumps({"history": payload["history"]}))
        async for message in client.receive_response():
            if isinstance(message, sdk.AssistantMessage):
                calls = []
                for block in message.content:
                    if isinstance(block, sdk.ToolUseBlock) and block.name != "StructuredOutput":
                        if block.name not in declared:
                            return {"error": f"the SDK called a tool that was not offered: {block.name}"}
                        calls.append({"name": declared[block.name], "arguments": json.dumps(block.input)})
                if calls:
                    # Stop before the SDK treats a queued call as a result; Nexus supplies the real one next request.
                    return {"output": {"text": "", "tool_calls": calls}, "usage": message.usage or {}}
            elif isinstance(message, sdk.ResultMessage):
                if message.is_error or message.subtype != "success":
                    return {"error": "the SDK reported an error", "detail": "; ".join([str(message.subtype), *(map(str, message.errors or []))])[:1000]
                            + ("\n" + "\n".join(stderr) if stderr else "")}
                result = {"output": message.structured_output, "usage": message.usage}
    return result


def main() -> None:
    try:
        line = sys.stdin.buffer.readline(MAX_BYTES + 2)
        if len(line) > MAX_BYTES + 1:
            raise ValueError("oversized request")
        payload = json.loads(line)
        out = asyncio.run(run(payload))
    except ModuleNotFoundError:
        out = {"error": "claude-agent-sdk is not installed"}
    except Exception as exc:  # noqa: BLE001 - nothing but a short class name crosses the process boundary
        out = {"error": f"the SDK worker failed: {type(exc).__name__}"}
    encoded = json.dumps(out)
    if len(encoded.encode()) > MAX_BYTES - 1:
        encoded = json.dumps({"error": "the SDK result exceeds 8 MiB"})
    print(encoded, flush=True)


if __name__ == "__main__":
    main()
