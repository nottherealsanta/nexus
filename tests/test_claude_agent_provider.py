"""Offline SDK bridge, subscription gating, and provider route contracts."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, ProviderSection
from nexus.errors import ConfigError, ProviderError
from nexus.model.message import Image, Message, Text, ToolResult, ToolUse
from nexus.model.providers import _claude_agent_worker as worker
from nexus.model.providers.claude_agent import ClaudeAgentProvider, request_payload, response_events
from nexus.model.providers import claude_agent_auth as auth
from nexus.model.registry import ModelRegistry
from nexus.model.request import ModelRequest, ToolSchema
from nexus.model.stream import MessageStop, TextDelta, ToolCallEnd, Usage
from nexus.runtime import Runtime


def request():
    return ModelRequest(messages=[Message(role="user", content=[Text("Read the file")])],
                        tools=[ToolSchema("Read", "Read a file", {"type": "object"})], system="Nexus system")


def result(calls=None):
    return {"output": {"text": "hello", "tool_calls": calls or []},
            "usage": {"input_tokens": 12, "output_tokens": 4, "cache_read_input_tokens": 7}}


async def test_worker_uses_official_options_without_executing_tools():
    seen = {}
    class Options:
        def __init__(self, **kwargs):
            seen.update(kwargs)
    class Result:
        is_error, subtype, structured_output, usage = False, "success", result()["output"], result()["usage"]
    async def query(**kwargs):
        seen["prompt"] = kwargs["prompt"]
        yield SimpleNamespace(content="intermediate text must not leak")
        yield Result()
        seen["exhausted"] = True
    sdk = SimpleNamespace(ClaudeAgentOptions=Options, ResultMessage=Result, query=query)
    payload = request_payload(request(), "sonnet", None)
    assert await worker.run(payload, sdk) == result()
    assert seen["exhausted"] is True
    assert seen["tools"] == [] and seen["mcp_servers"] == {} and seen["setting_sources"] == []
    assert seen["permission_mode"] == "dontAsk"
    assert json.loads(seen["settings"])["disableAllHooks"] is True
    assert "no-session-persistence" in seen["extra_args"]
    assert "strict-mcp-config" in seen["extra_args"]
    assert "Read" in seen["prompt"] and "Nexus system" in seen["system_prompt"]


async def test_worker_refuses_error_and_missing_result():
    class Result:
        is_error, subtype = True, "error_max_turns"
    async def query(**kwargs):
        yield Result()
    sdk = SimpleNamespace(ClaudeAgentOptions=lambda **_: None, ResultMessage=Result, query=query)
    assert await worker.run(request_payload(request(), "sonnet", None), sdk) == {"error": True}


def test_history_retains_tool_results_without_harness_metadata():
    req = request()
    req = ModelRequest(messages=[Message(role="user", content=[ToolResult("call", [Text("file body")],
                        display="private display", diff={"secret": "private"}, metrics={"private": 1})])])
    payload = request_payload(req, "sonnet", None)
    assert "file body" in json.dumps(payload)
    assert "private" not in json.dumps(payload)
    with pytest.raises(ProviderError, match="text only"):
        request_payload(ModelRequest(messages=[Message("user", [Image("image/png", b"x")])]), "sonnet", None)


def test_validates_batch_and_emits_native_calls_and_usage():
    events = list(response_events(result([{"name": "Read", "arguments": '{"path":"a.txt"}'}]), request()))
    assert isinstance(events[0], TextDelta)
    call = next(event for event in events if isinstance(event, ToolCallEnd))
    assert call.input == {"path": "a.txt"}
    assert next(event for event in events if isinstance(event, Usage)) == Usage(input=12, output=4, cache_read=7)
    assert events[-1] == MessageStop("tool_use")
    bad = result([{"name": "Read", "arguments": '{}'}, {"name": "Bash", "arguments": '{}'}])
    with pytest.raises(ProviderError, match="undeclared"):
        next(response_events(bad, request()))


@pytest.mark.parametrize("arguments", ["[]", "invalid", None])
def test_rejects_invalid_arguments(arguments):
    with pytest.raises(ProviderError):
        list(response_events(result([{"name": "Read", "arguments": arguments}]), request()))


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    script = tmp_path / "worker.py"
    script.write_text('''import json, os, sys, time
payload = json.loads(sys.stdin.readline())
if any('bridge-read' in str(m) for m in payload['history']):
    history = json.dumps(payload['history'])
    if 'tool_result' in history:
        assert 'contents from Nexus' in history
        print(json.dumps({'output': {'text': 'read complete', 'tool_calls': []}, 'usage': {}}), flush=True)
    else:
        name = next(t['name'] for t in payload['tools'] if t['name'].lower() == 'read')
        print(json.dumps({'output': {'text': '', 'tool_calls': [{'name': name, 'arguments': '{"path":"a.txt"}'}]}, 'usage': {}}), flush=True)
elif payload['system'] == 'hang':
    time.sleep(30)
else:
    assert 'ANTHROPIC_API_KEY' not in os.environ
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ
    print(json.dumps({'output': {'text': 'hello', 'tool_calls': []}, 'usage': {}}), flush=True)
''')
    original = asyncio.create_subprocess_exec
    processes = []
    async def spawn(*args, **kwargs):
        assert args[1] == "-I"
        process = await original(sys.executable, "-I", str(script), **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes


async def test_worker_process_environment_and_cleanup(tmp_path, fake_worker):
    provider = ClaudeAgentProvider(workspace=tmp_path, environ={"PATH": os.environ["PATH"],
                     "ANTHROPIC_API_KEY": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret"})
    events = [event async for event in provider.stream(request())]
    assert TextDelta("hello") in events and events[-1] == MessageStop("end_turn")
    assert not provider._active
    await provider.aclose()
    with pytest.raises(ProviderError, match="closed"):
        _ = [event async for event in provider.stream(request())]


async def test_timeout_and_cancel_reap_worker(tmp_path, fake_worker):
    provider = ClaudeAgentProvider(workspace=tmp_path, timeout_seconds=0.15)
    req = ModelRequest(messages=[], system="hang")
    with pytest.raises(ProviderError, match="timed out"):
        _ = [event async for event in provider.stream(req)]
    assert fake_worker[-1].returncode is not None and not provider._active
    provider = ClaudeAgentProvider(workspace=tmp_path)
    async def consume():
        return [event async for event in provider.stream(req)]
    task = asyncio.create_task(consume())
    while not provider._active:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_worker[-1].returncode is not None and not provider._active


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), 3601])
def test_timeout_bounded(tmp_path, value):
    with pytest.raises(ConfigError):
        ClaudeAgentProvider(workspace=tmp_path, timeout_seconds=value)


async def test_runtime_route_catalogue_and_transport_capabilities(tmp_path):
    reference = "claude-agent/claude-test"
    config = Config(model=reference, version=2, v2=ConfigV2(model=ModelSection(default=reference),
                    providers={"claude-agent": ProviderSection(kind="claude-agent")}))
    runtime = Runtime(tmp_path, config=config, environ={})
    provider = runtime._construct_provider("claude-agent", "claude-agent", config.v2.providers["claude-agent"], reference)
    assert isinstance(provider, ClaudeAgentProvider) and provider.name == "claude-agent"
    with pytest.raises(ConfigError, match="subscription"):
        runtime._construct_provider("claude-agent", "claude-agent", ProviderSection(api_key="key"), reference)
    registry = ModelRegistry(providers={"claude-agent": {}}, env={"ANTHROPIC_API_KEY": "unrelated"}, offline=True,
                    use_snapshot=False, provider_aliases={"claude-agent": "anthropic"})
    registry.install_raw(json.dumps({"anthropic": {"npm": "@ai-sdk/anthropic", "models": {
        "claude-test": {"tool_call": True, "reasoning": True, "structured_output": True,
                       "modalities": {"input": ["text", "image"], "output": ["text"]}}}}}))
    info = registry.resolve(reference)
    status = next(status for status in registry.providers() if status.id == "claude-agent")
    assert status.adapter == "claude-agent" and not status.reachable
    assert info.provider == "claude-agent"
    assert info.input_modalities == ("text",) and not info.reasoning and info.cost is None
    assert provider.capabilities(info.id).streaming is False
    await provider.aclose()
    await runtime.aclose()


@pytest.mark.parametrize("logged_in,method,expected", [(True, "claude.ai", True), (True, "api_key", False), (False, "claude.ai", False)])
async def test_auth_status_requires_subscription(tmp_path, monkeypatch, logged_in, method, expected):
    script = tmp_path / "claude"
    script.write_text(f'#!/bin/sh\nprintf \'%s\\n\' \'{json.dumps({"loggedIn": logged_in, "authMethod": method})}\'\n')
    script.chmod(0o755)
    monkeypatch.setattr(auth, "cli_path", lambda executable: str(script))
    assert await auth.subscription_connected(environ={}) is expected


async def test_sdk_intentions_execute_and_persist_through_nexus(tmp_path, fake_worker):
    (tmp_path / "a.txt").write_text("contents from Nexus")
    provider = ClaudeAgentProvider(workspace=tmp_path)
    reference = "claude-agent/sonnet"
    config = Config(model=reference, version=2, v2=ConfigV2(model=ModelSection(default=reference)))
    runtime = Runtime(tmp_path, home=tmp_path / "home", config=config, providers={"claude-agent": provider})
    try:
        session = runtime.session("sdk-tools")
        events = [event async for event in session.send("bridge-read a.txt")]
        assert events[-1].type == "turn.completed", [(event.type, event.data) for event in events]
        assert len(fake_worker) == 2
        messages = session.messages
        assert any(isinstance(block, ToolUse) for message in messages for block in message.content)
        assert any(isinstance(block, ToolResult) for message in messages for block in message.content)
        assert any(isinstance(block, Text) and block.text == "read complete" for message in messages for block in message.content)
    finally:
        await runtime.aclose()
        await provider.aclose()


async def test_pinned_sdk_accepts_worker_options():
    sdk = pytest.importorskip("claude_agent_sdk")
    seen = []
    async def query(**kwargs):
        seen.append(kwargs["options"])
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id="isolated", structured_output=result()["output"], usage={})
    proxy = SimpleNamespace(ClaudeAgentOptions=sdk.ClaudeAgentOptions, ResultMessage=sdk.ResultMessage, query=query)
    await worker.run(request_payload(request(), "sonnet", None), proxy)
    assert seen[0].tools == []
