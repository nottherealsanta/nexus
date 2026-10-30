"""Claude through the Agent SDK: request body, reply validation, the worker with a fake SDK, and CLI sign-in."""
from __future__ import annotations

import asyncio
import json
import stat
import types

import pytest

from nexus.llmhub import _claude_worker
from nexus.llmhub.auth import SIGN_INS
from nexus.llmhub.auth import claude as claude_auth
from nexus.llmhub.claude import FRAMING, ClaudeProvider, build_body, events_for_result
from nexus.llmhub.providers import PROVIDERS, make
from nexus.llmhub.types import (
    Done,
    Image,
    Media,
    Message,
    PartDone,
    ProviderError,
    Reasoning,
    Request,
    Text,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

READ = ToolSpec("read", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


def request(*messages: Message, tools=(READ,), effort=None) -> Request:
    return Request("claude-haiku-5-5", "Be brief.", messages, tuple(tools), reasoning_effort=effort)


def test_body_is_everything_the_sdk_gets():
    req = request(
        Message("user", (Text("hi"),)),
        Message("assistant", (Reasoning("thinking", origin="claude"), ToolCall("c1", "read", '{"path":"a"}'))),
        Message("tool", (ToolResult("c1", "text", is_error=False),)),
        effort="high",
    )
    body = build_body(req)
    assert body["system"] == "Be brief.\n\n" + FRAMING
    assert body["effort"] == "high" and body["model"] == "claude-haiku-5-5"
    assert body["history"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "a"}}]},  # reasoning not sent
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "text", "is_error": False}]},
    ]
    assert body["tools"][0]["input_schema"] == READ.parameters
    assert body["schema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["read"]
    assert ClaudeProvider().body(req) == body


def test_unknown_effort_is_not_sent_and_no_tools_gets_placeholder_enum():
    body = build_body(request(Message("user", (Text("x"),)), tools=(), effort="minimal"))
    assert body["effort"] is None
    assert body["schema"]["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"] == ["__none__"]


@pytest.mark.parametrize("part", [Image("data:image/png;base64,AA=="), ToolResult("c", "o", media=(Media("a", "image/png", data_url="data:x"),))])
def test_images_are_refused_not_dropped(part):
    with pytest.raises(ProviderError, match="text only"):
        build_body(request(Message("user" if isinstance(part, Image) else "tool", (part,))))


def test_reply_becomes_parts_usage_and_done():
    req = request(Message("user", (Text("go"),)))
    value = {"output": {"text": "ok", "tool_calls": [{"name": "read", "arguments": '{"path": "a"}'}]},
             "usage": {"input_tokens": 2, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 7}}
    events = events_for_result(value, req, origin="claude")
    assert isinstance(events[0], TextDelta)
    calls = [e.part for e in events if isinstance(e, PartDone) and isinstance(e.part, ToolCall)]
    assert len(calls) == 1 and calls[0].name == "read" and json.loads(calls[0].arguments) == {"path": "a"}
    assert next(e for e in events if isinstance(e, Usage)) == Usage(input_tokens=17, cached_input_tokens=10, output_tokens=7)
    assert events[-1] == Done("tool_calls")
    assert events_for_result({"output": {"text": "done", "tool_calls": []}}, req, origin="claude")[-1] == Done("end_turn")


@pytest.mark.parametrize("output", [
    {"text": "x", "tool_calls": [{"name": "bash", "arguments": "{}"}]},       # not offered
    {"text": "x", "tool_calls": [{"name": "read", "arguments": "not json"}]},  # malformed
    {"text": "x", "tool_calls": [{"name": "read", "arguments": "[1]"}]},       # not an object
    {"text": "x", "tool_calls": [{"name": "read", "arguments": "{}"}] * 33},   # too many
    {"text": 1, "tool_calls": []},
])
def test_a_bad_batch_exposes_no_calls(output):
    with pytest.raises(ProviderError):
        events_for_result({"output": output}, request(Message("user", (Text("go"),))), origin="claude")


# -- the worker, against a fake SDK -------------------------------------------


class FakeSdk(types.SimpleNamespace):
    pass


def fake_sdk(script):
    class ToolUseBlock:
        def __init__(self, name, input): self.name, self.input = name, input

    class AssistantMessage:
        def __init__(self, content, usage=None): self.content, self.usage = content, usage

    class ResultMessage:
        def __init__(self, **kw): self.is_error, self.subtype, self.errors, self.structured_output, self.usage = False, "success", [], None, {}; self.__dict__.update(kw)

    class Options:
        def __init__(self, **kw): self.__dict__.update(kw)

    class Client:
        def __init__(self, options): self.options = options
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def query(self, prompt): self.prompt = prompt
        async def receive_response(self):
            for m in script(self):
                yield m

    sdk = FakeSdk(ToolUseBlock=ToolUseBlock, AssistantMessage=AssistantMessage, ResultMessage=ResultMessage,
                  ClaudeAgentOptions=Options, ClaudeSDKClient=Client,
                  tool=lambda name, desc, schema: (lambda fn: (name, fn)),
                  create_sdk_mcp_server=lambda name, tools: {"tools": dict(tools)})
    return sdk


def payload(**kw):
    return {"model": "m", "system": "s", "history": [], "tools": [{"name": "read", "description": "d", "input_schema": {}}],
            "schema": {}, "effort": None} | kw


def test_worker_stops_at_the_first_tool_call():
    sdk = fake_sdk(lambda c: [sdk.AssistantMessage([sdk.ToolUseBlock("mcp__nexus__read", {"path": "a"})], {"output_tokens": 3}),
                              sdk.ResultMessage(structured_output={"text": "never reached", "tool_calls": []})])
    out = asyncio.run(_claude_worker.run(payload(), sdk))
    assert out == {"output": {"text": "", "tool_calls": [{"name": "read", "arguments": '{"path": "a"}'}]}, "usage": {"output_tokens": 3}}


def test_worker_rejects_a_tool_that_was_not_offered():
    sdk = fake_sdk(lambda c: [sdk.AssistantMessage([sdk.ToolUseBlock("Bash", {"command": "ls"})])])
    assert "not offered" in asyncio.run(_claude_worker.run(payload(), sdk))["error"]


def test_worker_returns_structured_output_and_reports_failures():
    ok = fake_sdk(lambda c: [ok.ResultMessage(structured_output={"text": "hi", "tool_calls": []}, usage={"input_tokens": 1})])
    assert asyncio.run(_claude_worker.run(payload(effort="low"), ok)) == {"output": {"text": "hi", "tool_calls": []}, "usage": {"input_tokens": 1}}
    bad = fake_sdk(lambda c: [bad.ResultMessage(is_error=True, subtype="error_max_turns")])
    assert "error" in asyncio.run(_claude_worker.run(payload(), bad))
    empty = fake_sdk(lambda c: [])
    assert "without a result" in asyncio.run(_claude_worker.run(payload(), empty))["error"]


# -- the provider process -------------------------------------------------------


def test_provider_reports_a_worker_error_with_wire_events(monkeypatch):
    async def run():
        async def fake_exec(*argv, **kw):
            assert argv[1:4] == ("-I", "-m", "nexus.llmhub._claude_worker")
            assert "ANTHROPIC_API_KEY" not in kw["env"]
            return await real(sys_exec, "-c", "import sys; sys.stdin.readline(); print('{\"error\": \"nope\"}')", **kw)
        import sys as _sys
        sys_exec = _sys.executable
        real = asyncio.create_subprocess_exec
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        seen = []
        with pytest.raises(ProviderError, match="nope"):
            async for event in ClaudeProvider().stream(request(Message("user", (Text("hi"),)))):
                seen.append(type(event).__name__)
        return seen

    assert asyncio.run(run()) == ["WireRequest", "WireEvent"]


# -- sign-in -------------------------------------------------------------------


def fake_cli(tmp_path, status: dict, *, login_out: str = ""):
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncase \"$2\" in\n status) echo '{json.dumps(status)}';;\n login) echo '{login_out}'; exit 0;;\n logout) exit 0;;\nesac\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_logged_in_means_a_claude_ai_subscription(tmp_path, monkeypatch):
    for status, expected in [({"loggedIn": True, "authMethod": "claude.ai"}, True), ({"loggedIn": True, "authMethod": "api_key"}, False),
                             ({"loggedIn": False}, False)]:
        monkeypatch.setenv("NEXUS10_CLAUDE_CLI", fake_cli(tmp_path, status))
        claude_auth._cache.clear()
        assert SIGN_INS["claude"]().logged_in() is expected


def test_login_hands_the_url_to_the_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_CLAUDE_CLI", fake_cli(tmp_path, {"loggedIn": True, "authMethod": "claude.ai"}, login_out="Visit https://claude.ai/oauth/x?y=1 now"))
    claude_auth._cache.clear()
    told = []
    asyncio.run(SIGN_INS["claude"]().login(notify=told.append))
    assert told == ["Open https://claude.ai/oauth/x?y=1 to sign in to Claude."]


def test_the_provider_is_in_the_table():
    assert PROVIDERS["claude"].dialect == "agent-sdk" and PROVIDERS["claude"].catalog_id == "anthropic"
    assert isinstance(make("claude", None, "claude-haiku-5-5"), ClaudeProvider)


# -- Claude Code is the user's to install ------------------------------------------


def no_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("NEXUS10_CLAUDE_CLI", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(claude_auth.Path, "home", classmethod(lambda cls: tmp_path))
    claude_auth._cache.clear()


def test_cli_is_found_on_path_or_not_at_all(tmp_path, monkeypatch):
    no_claude(tmp_path, monkeypatch)
    assert claude_auth.cli_path() is None
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    assert claude_auth.cli_path() == str(cli)


def test_without_claude_code_only_the_claude_provider_fails(tmp_path, monkeypatch):
    no_claude(tmp_path, monkeypatch)
    assert SIGN_INS["claude"]().logged_in() is False
    with pytest.raises(claude_auth.AuthError, match="not installed"):
        asyncio.run(SIGN_INS["claude"]().login(notify=print))

    async def run():
        async for _ in ClaudeProvider().stream(request(Message("user", (Text("hi"),)))):
            pass

    with pytest.raises(ProviderError, match="not installed") as caught:
        asyncio.run(run())
    assert caught.value.phase == "transport"
