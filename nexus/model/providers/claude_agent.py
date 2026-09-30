"""Claude subscription provider through the official Agent SDK (plan section 8).

Each request starts an isolated SDK worker with no built-in tools, hooks, or
project settings. Structured tool intentions become normal Nexus tool calls;
only the harness approves and executes them. SQLite history is authoritative.
The text-only bridge buffers the SDK's final structured response.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import msgspec

from ...errors import ConfigError, ProviderError
from ..capabilities import Capabilities
from ..message import Document, Image, Thinking
from ..request import ModelRequest
from ..stream import MessageStart, MessageStop, StreamEvent, TextDelta, ToolCallEnd, ToolCallStart, Usage
from .opencode import build_child_env

MAX_BYTES = 8 * 1024 * 1024


def request_payload(req: ModelRequest, model: str, executable: str | None) -> dict:
    """Serialize only model-visible history, never harness message metadata."""
    history = []
    for message in req.messages:
        blocks = []
        for block in message.content:
            if isinstance(block, Thinking):
                continue
            if isinstance(block, (Image, Document)) or (hasattr(block, "content") and any(isinstance(item, (Image, Document)) for item in block.content)):
                raise ProviderError("claude-agent supports text only; remove image/document inputs")
            value = msgspec.to_builtins(block)
            if value.get("type") == "tool_result":
                value = {key: value[key] for key in ("type", "tool_use_id", "content", "is_error")}
            blocks.append(value)
        history.append({"role": message.role, "content": blocks})
    names = [tool.name for tool in req.tools]
    schema = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "tool_calls": {"type": "array", "maxItems": 32, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"name": {"type": "string", "enum": names or ["__none__"]},
                               "arguments": {"type": "string"}},
                "required": ["name", "arguments"],
            }},
        }, "required": ["text", "tool_calls"],
    }
    return {"model": req.model or model, "executable": executable,
            "system": req.system or "", "history": history,
            "tools": msgspec.to_builtins(req.tools), "schema": schema,
            "effort": req.params.reasoning_effort}


def response_events(value: dict, req: ModelRequest):
    """Validate the entire tool batch before exposing any executable call."""
    output = value.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("text"), str):
        raise ProviderError("claude-agent returned no valid structured response")
    calls = output.get("tool_calls")
    if not isinstance(calls, list) or len(calls) > 32:
        raise ProviderError("claude-agent returned an invalid tool batch")
    declared = {tool.name for tool in req.tools}
    parsed = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("name"), str) or call["name"] not in declared:
            raise ProviderError("claude-agent requested an undeclared tool")
        try:
            arguments = json.loads(call["arguments"])
        except (KeyError, TypeError, ValueError):
            raise ProviderError("claude-agent returned malformed tool arguments") from None
        if not isinstance(arguments, dict):
            raise ProviderError("claude-agent tool arguments must be a JSON object")
        parsed.append((uuid4().hex, call["name"], arguments))
    usage = value.get("usage") or {}
    if not isinstance(usage, dict):
        raise ProviderError("claude-agent returned malformed usage")
    if output["text"]:
        yield TextDelta(output["text"])
    for call_id, name, arguments in parsed:
        yield ToolCallStart(call_id, name)
        yield ToolCallEnd(call_id, arguments)
    def count(key):
        value = usage.get(key, 0)
        return value if isinstance(value, int) and value >= 0 else 0
    yield Usage(input=count("input_tokens"), output=count("output_tokens"),
                cache_read=count("cache_read_input_tokens"), cache_write=count("cache_creation_input_tokens"))
    yield MessageStop("tool_use" if parsed else "end_turn")


class ClaudeAgentProvider:
    name = "claude-agent"
    usage_input_excludes_cache = True

    def __init__(self, *, workspace: Path, model: str = "sonnet", executable: str | None = None,
                 timeout_seconds: float | None = None, environ: Mapping[str, str] | None = None):
        self._workspace, self._model, self._executable = workspace, model, executable
        self._timeout = timeout_seconds if timeout_seconds is not None else 600.0
        if not math.isfinite(self._timeout) or self._timeout <= 0 or self._timeout > 3600:
            raise ConfigError("claude-agent timeout_seconds must be between 0 and 3600")
        self._environ = environ
        self._active: set[asyncio.subprocess.Process] = set()
        self._closed = False

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(tools=True, parallel_tool_calls=True, streaming=False,
                            max_context_tokens=200_000, max_output_tokens=16_384,
                            degradation={"vision": "error", "documents": "error", "thinking": "drop"})

    async def count_tokens(self, req: ModelRequest) -> None:
        return None

    async def _stop(self, process):
        # The SDK CLI inherits the worker's process group, including on cancel.
        if os.name == "posix":
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
        await process.wait()

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*(self._stop(process) for process in tuple(self._active)))

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("claude-agent provider is closed")
        payload = json.dumps(request_payload(req, self._model, self._executable)).encode()
        if len(payload) > MAX_BYTES:
            raise ProviderError("claude-agent request exceeds 8 MiB")
        env = build_child_env(self._environ)
        # The official CLI owns the credential store. No API keys, OAuth tokens,
        # cloud-provider switches, PYTHONPATH, or unrelated secrets are inherited.
        package_root = str(Path(__file__).resolve().parents[3])
        bootstrap = "import sys; sys.path.insert(0, " + repr(package_root) + "); from nexus.model.providers._claude_agent_worker import main; main()"
        process = None
        try:
            async with asyncio.timeout(self._timeout):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-c", bootstrap,
                    cwd=self._workspace, env=env, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=MAX_BYTES + 1, start_new_session=os.name == "posix")
                self._active.add(process)
                yield MessageStart(model=req.model or self._model, provider=self.name)
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
                process.stdin.close()
                line = await process.stdout.readline()
                if len(line) > MAX_BYTES:
                    raise ProviderError("claude-agent response exceeds 8 MiB")
                value = json.loads(line)
                if await process.wait() != 0 or not isinstance(value, dict) or value.get("error"):
                    raise ProviderError("claude-agent SDK failed; install claude-agent-sdk and run `claude auth login` with your Pro/Max account")
                for event in response_events(value, req):
                    yield event
        except TimeoutError:
            raise ProviderError("claude-agent SDK request timed out") from None
        except (OSError, ValueError):
            raise ProviderError("claude-agent SDK worker failed or returned malformed output") from None
        finally:
            if process is not None:
                await self._stop(process)
                self._active.discard(process)
