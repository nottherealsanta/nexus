"""Claude through the official Agent SDK (PLAN.md step 5b).

The SDK is used as a plain client; Nexus owns the loop. Each request runs
`_claude_worker` in an isolated process with no built-in tools, hooks or project
settings. Nexus tools are offered to the SDK as MCP tools whose handlers only
record the intention, and the worker returns at the first tool call, so Nexus
approves and executes the call and sends the real result in the next request.

What differs from the HTTP dialects, and is announced rather than hidden:

- The reply is buffered: text arrives as one delta when the request finishes.
- Images and documents are refused (`ProviderError`), not silently dropped.
- Thinking is not returned, and reasoning parts in the history are not sent.
- `body()` is everything Nexus gives the SDK: the system prompt including the
  framing sentence added here, the whole history, the tool schemas, the model and
  the effort. The CLI adds a little of its own (`SDK_ADDS`, listed under `sdk_adds`
  in the body). It also makes a second, hidden request to title the session unless
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` is set, which the worker does.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any
from uuid import uuid4

from ..paths import home
from .auth.claude import NOT_INSTALLED, cli_path
from .types import (
    Done,
    Event,
    Image,
    PartDone,
    ProviderError,
    Request,
    Text,
    TextDelta,
    ToolArgsDelta,
    ToolCall,
    ToolCallStarted,
    ToolResult,
    Usage,
    WireEvent,
    WireRequest,
    tool_images,
)

MAX_BYTES = 8 * 1024 * 1024
MAX_CALLS = 32
TIMEOUT_S = 600.0
EFFORTS = {"low", "medium", "high", "xhigh", "max"}

FRAMING = (
    "You are the model in the Nexus harness. Use the supplied JSON schema for final prose. "
    "Request tools through the nexus MCP server. These are intentions only: Nexus will "
    "approve and execute them outside this SDK and send real results on the next request. "
    "Never invent tool results. Do not call SDK built-in tools. "
    "An empty tool_calls array means you are finished. History follows as JSON."
)


# What the SDK's CLI adds to the request on its own, measured 2026-10-30 (CLI 2.1.285) by pointing it at a local capture
# server. Shown in `body()` under `sdk_adds` so the wire request lists everything the model receives that Nexus did not write.
SDK_ADDS = {
    "system_prefix": ["x-anthropic-billing-header: cc_version=...; cc_entrypoint=sdk-py;",
                      "You are a Claude agent, built on Anthropic's Claude Agent SDK."],
    "extra_tools": ["StructuredOutput (the reply schema, about 600 characters)"],
    "first_user_message_prefix": "a <system-reminder> with the signed-in account's email address (userEmail)",
    "environment_message": "# Environment: the worker's working directory, platform, shell, OS version, model, today's date and a token budget",
    "tool_names": "each Nexus tool is offered as mcp__nexus__<name>",
    "request": "thinking adaptive; effort high unless the request sets one; max_tokens 128000",
}


def history(req: Request) -> list[dict[str, Any]]:
    """The conversation as JSON blocks. Reasoning is not sent; images are refused."""
    out: list[dict[str, Any]] = []
    for message in req.messages:
        blocks: list[dict[str, Any]] = []
        for part in message.parts:
            if isinstance(part, Image) or (isinstance(part, ToolResult) and tool_images(part)):
                raise ProviderError("claude: the Agent SDK takes text only; remove the image from this conversation or use another model",
                                    phase="transport")
            if isinstance(part, Text):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolCall):
                blocks.append({"type": "tool_use", "id": part.id, "name": part.name, "input": _loads(part.arguments)})
            elif isinstance(part, ToolResult):
                blocks.append({"type": "tool_result", "tool_use_id": part.call_id, "content": part.output, "is_error": part.is_error})
        if blocks:
            out.append({"role": "user" if message.role in ("user", "tool") else "assistant", "content": blocks})
    return out


def _loads(text: str) -> Any:
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {"_unparsed": text}


def schema_for(names: list[str]) -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "tool_calls": {"type": "array", "maxItems": MAX_CALLS, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"name": {"type": "string", "enum": names or ["__none__"]}, "arguments": {"type": "string"}},
                "required": ["name", "arguments"]}},
        },
        "required": ["text", "tool_calls"],
    }


def build_body(req: Request) -> dict[str, Any]:
    return {
        "model": req.model,
        "system": (req.instructions + "\n\n" if req.instructions else "") + FRAMING,
        "history": history(req),
        "tools": [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools],
        "schema": schema_for([t.name for t in req.tools]),
        "effort": req.reasoning_effort if req.reasoning_effort in EFFORTS else None,
        "sdk_adds": SDK_ADDS,
    }


def events_for_result(value: Any, req: Request, *, origin: str) -> list[Event]:
    """Validate the whole reply, then emit it. Nothing executable is exposed from a bad batch."""
    output = value.get("output") if isinstance(value, dict) else None
    if not isinstance(output, dict) or not isinstance(output.get("text"), str):
        raise ProviderError(f"{origin}: the SDK returned no valid structured response")
    calls = output.get("tool_calls")
    if not isinstance(calls, list) or len(calls) > MAX_CALLS:
        raise ProviderError(f"{origin}: the SDK returned an invalid tool batch")
    declared = {t.name for t in req.tools}
    parsed: list[tuple[str, str, str]] = []
    for call in calls:
        if not isinstance(call, dict) or call.get("name") not in declared or not isinstance(call.get("arguments"), str):
            raise ProviderError(f"{origin}: the SDK requested a tool that was not offered")
        if not isinstance(_loads_strict(call["arguments"]), dict):
            raise ProviderError(f"{origin}: the SDK returned malformed tool arguments")
        parsed.append((f"call_{uuid4().hex[:24]}", call["name"], call["arguments"]))
    out: list[Event] = []
    if output["text"]:
        out += [TextDelta(output["text"]), PartDone(Text(output["text"], origin=origin))]
    for call_id, name, arguments in parsed:
        out += [ToolCallStarted(call_id, name), ToolArgsDelta(call_id, arguments), PartDone(ToolCall(call_id, name, arguments, origin=origin))]
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}

    def count(key: str) -> int:
        n = usage.get(key)
        return n if isinstance(n, int) and n >= 0 else 0

    cached = count("cache_read_input_tokens")
    out.append(Usage(input_tokens=count("input_tokens") + cached + count("cache_creation_input_tokens"),
                     cached_input_tokens=cached, output_tokens=count("output_tokens")))
    out.append(Done("tool_calls" if parsed else "end_turn"))
    return out


def _loads_strict(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


class ClaudeProvider:
    name = "claude"

    def __init__(self, *, timeout: float = TIMEOUT_S) -> None:
        self._timeout = timeout

    def body(self, req: Request) -> dict[str, Any]:
        return build_body(req)

    async def stream(self, req: Request) -> AsyncIterator[Event]:
        body = self.body(req)
        executable = cli_path()
        if executable is None:
            raise ProviderError(f"{self.name}: {NOT_INSTALLED}", phase="transport")
        payload = json.dumps(body | {"executable": executable}).encode()
        if len(payload) > MAX_BYTES:
            raise ProviderError(f"{self.name}: the request exceeds 8 MiB", phase="transport")
        yield WireRequest("SDK", "claude-agent-sdk://query", {}, body)
        process = None
        try:
            async with asyncio.timeout(self._timeout):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-m", "nexus.llmhub._claude_worker",
                    cwd=worker_dir(), env=worker_env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, limit=MAX_BYTES + 1, start_new_session=os.name == "posix")
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
                process.stdin.close()
                line, stderr = await asyncio.gather(process.stdout.readline(), process.stderr.read(4096))
                code = await process.wait()
        except TimeoutError:
            raise ProviderError(f"{self.name}: the SDK request timed out", phase="response") from None
        except OSError as exc:
            raise ProviderError(f"{self.name}: could not start the SDK worker: {type(exc).__name__}", phase="transport") from exc
        finally:
            if process is not None:
                await _stop(process)
        try:
            value = json.loads(line) if line and len(line) <= MAX_BYTES else None
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise ProviderError(f"{self.name}: the SDK worker returned no result (exit {code})", phase="response",
                                body=stderr.decode("utf-8", "replace")[:1000])
        yield WireEvent("result", value)
        if value.get("error"):
            raise ProviderError(f"{self.name}: {value['error'] if isinstance(value['error'], str) else 'the SDK request failed'}",
                                phase="response", body=str(value.get("detail") or "")[:1000] or None)
        for event in events_for_result(value, req, origin=self.name):
            yield event


def worker_dir() -> str:
    """An empty directory for the CLI's working directory. The CLI prints it in its own Environment message, so it must not be
    a project (and `setting_sources=[]` already stops it reading project files). Nexus's own system prompt names the workspace."""
    path = home() / "claude-sdk"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def worker_env() -> dict[str, str]:
    """The worker gets the OS basics only: no API keys, tokens or PYTHONPATH. The CLI finds its own login."""
    keep = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SHELL", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")
    return {k: os.environ[k] for k in keep if k in os.environ}


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)  # the SDK's CLI child shares the worker's group
            else:
                process.kill()
    with suppress(Exception):
        await process.wait()
