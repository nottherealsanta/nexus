"""Isolated official SDK call; private bounded JSON worker (plan section 8).

The parent supplies an OS-only environment and kills this process group on
cancellation. SDK stderr and exceptions never cross the provider boundary. Nexus MCP schemas
collect intentions only; workspace execution belongs to the outer harness.
"""
from __future__ import annotations

import asyncio
import json
import sys

from .claude_agent import MAX_BYTES
from .claude_agent_auth import subscription_connected


async def run(payload, sdk=None):
    if sdk is None:
        import claude_agent_sdk as sdk
    instruction = (
        "You are the model in the Nexus harness. Use the supplied JSON schema for final prose. "
        "Request tools through the nexus MCP server. These are intentions only: Nexus will "
        "approve and execute them outside this SDK and send real results on the next request. "
        "Never invent tool results. Do not call SDK built-in tools. "
        "An empty tool_calls array means you are finished. History follows as JSON."
    )
    # Give the SDK real callable schemas, rather than tool names buried in prose.
    # Handlers never execute work: all execution stays in the outer Nexus loop.
    pending = []
    tools = []
    for definition in payload["tools"]:
        name = definition["name"]

        async def queue(arguments, name=name):
            if len(pending) >= 32:
                raise ValueError("too many tool intentions")
            pending.append({"name": name, "arguments": json.dumps(arguments)})
            return {"content": [{"type": "text", "text":
                "Queued for Nexus approval; no execution has occurred. End this response now."}]}

        tools.append(sdk.tool(name, definition["description"], definition["input_schema"])(queue))
    servers = {"nexus": sdk.create_sdk_mcp_server("nexus", tools=tools)} if tools else {}
    options = sdk.ClaudeAgentOptions(
        model=payload["model"], cli_path=payload["executable"],
        system_prompt=payload["system"] + "\n\n" + instruction,
        tools=[], mcp_servers=servers, setting_sources=[],
        allowed_tools=["mcp__nexus__" + tool["name"] for tool in payload["tools"]],
        settings=json.dumps({"disableAllHooks": True}),
        permission_mode="dontAsk", max_turns=3,
        extra_args={"strict-mcp-config": None, "no-session-persistence": None},
        output_format={"type": "json_schema", "schema": payload["schema"]},
        max_buffer_size=MAX_BYTES, stderr=lambda _: None,
    )
    if payload.get("effort") in {"low", "medium", "high", "xhigh", "max"}:
        options.effort = payload["effort"]
    prompt = json.dumps({"history": payload["history"]})
    result = {"error": True}
    declared = {"mcp__nexus__" + tool["name"]: tool["name"] for tool in payload["tools"]}
    async with sdk.ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, sdk.AssistantMessage):
                calls = []
                for block in message.content:
                    if isinstance(block, sdk.ToolUseBlock):
                        if block.name == "StructuredOutput":
                            continue
                        if block.name not in declared:
                            # Do not silently turn an unavailable SDK call into final prose.
                            return {"error": True}
                        calls.append({"name": declared[block.name],
                                      "arguments": json.dumps(block.input)})
                if calls:
                    # Stop before any SDK follow-up can treat a queued call as a result.
                    # Nexus records these calls and supplies actual results next iteration.
                    return {"output": {"text": "", "tool_calls": calls}, "usage": {}}
            if isinstance(message, sdk.ResultMessage):
                if pending:
                    result = {"output": {"text": "", "tool_calls": pending}, "usage": message.usage}
                elif not message.is_error and message.subtype == "success":
                    result = {"output": message.structured_output, "usage": message.usage}

    return result


def main():
    try:
        line = sys.stdin.buffer.readline(MAX_BYTES + 2)
        if len(line) > MAX_BYTES + 1:
            raise ValueError("oversized request")
        payload = json.loads(line)
        if not asyncio.run(subscription_connected(payload.get("executable"))):
            result = {"error": True}
        else:
            result = asyncio.run(run(payload))
        encoded = json.dumps(result)
        if len(encoded.encode()) > MAX_BYTES - 1:
            result = {"error": True}
        print(json.dumps(result), flush=True)
    except Exception:
        print('{"error": true}', flush=True)


if __name__ == "__main__":
    main()
