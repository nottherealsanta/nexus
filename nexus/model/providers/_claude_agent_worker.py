"""Isolated official SDK call; private bounded JSON worker (plan section 8).

The parent supplies an OS-only environment and kills this process group on
cancellation. SDK stderr and exceptions never cross the provider boundary.
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
        "You are the model in the Nexus harness. Respond using the supplied JSON schema. "
        "Put user-facing prose in text. To request tools, put their names and JSON-encoded "
        "argument objects in tool_calls. Nexus will approve and execute them and send results "
        "on the next request. Never execute tools yourself or invent tool results. "
        "Use an empty tool_calls array when finished. History and tool definitions follow as JSON."
    )
    options = sdk.ClaudeAgentOptions(
        model=payload["model"], cli_path=payload["executable"],
        system_prompt=payload["system"] + "\n\n" + instruction,
        tools=[], mcp_servers={}, setting_sources=[],
        settings=json.dumps({"disableAllHooks": True}),
        permission_mode="dontAsk", max_turns=3,
        extra_args={"strict-mcp-config": None, "no-session-persistence": None},
        output_format={"type": "json_schema", "schema": payload["schema"]},
        max_buffer_size=MAX_BYTES, stderr=lambda _: None,
    )
    if payload.get("effort") in {"low", "medium", "high", "xhigh", "max"}:
        options.effort = payload["effort"]
    prompt = json.dumps({"history": payload["history"], "tool_definitions": payload["tools"]})
    result = {"error": True}
    async for message in sdk.query(prompt=prompt, options=options):
        if isinstance(message, sdk.ResultMessage):
            if message.is_error or message.subtype != "success":
                result = {"error": True}
            else:
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
