import asyncio
import json
import os
import sys
from pathlib import Path

from nexus.config import Config
from nexus.errors import ProviderError
from nexus.model.message import (
    Document,
    Image,
    Message,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
)
from nexus.model.provider import Provider
from nexus.model.providers import LegacyCodexCLIProvider, request_to_prompt
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart, MessageStop, Raw, TextDelta, Usage


def _fake_codex(root: Path, body: str) -> Path:
    executable = root / "codex"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o755)
    return executable


async def test_adapter_normalizes_legacy_transport(tmp_path):
    executable = _fake_codex(
        tmp_path,
        """
import json, sys
assert sys.stdin.read() == "hello"
print(json.dumps({"type": "thread.started", "thread_id": "t"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 4}}))
""",
    )
    config = Config(executable=str(executable), timeout_seconds=5)
    provider = LegacyCodexCLIProvider(workspace=tmp_path, config=config)
    assert isinstance(provider, Provider)

    request = ModelRequest(
        messages=[Message("user", [Text("hi")])],
        metadata={"prompt": "hello"},
        model="test-model",
    )
    events = [event async for event in provider.stream(request)]

    assert isinstance(events[0], MessageStart)
    assert events[0].provider == "legacy-codex-cli"
    assert events[0].model == "test-model"
    assert any(isinstance(e, TextDelta) and e.text == "ok" for e in events)
    assert any(isinstance(e, Usage) and e.input == 3 and e.output == 4 for e in events)
    assert isinstance(events[-1], MessageStop)
    assert events[-1].stop_reason == "end_turn"


async def test_adapter_capabilities_are_conservative(tmp_path):
    provider = LegacyCodexCLIProvider(workspace=tmp_path)
    caps = provider.capabilities("anything")
    assert caps.tools is False
    assert caps.parallel_tool_calls is False
    assert caps.streaming is True
    assert await provider.count_tokens(ModelRequest(messages=[])) is None


async def test_adapter_close_is_idempotent_and_blocks_stream(tmp_path):
    provider = LegacyCodexCLIProvider(workspace=tmp_path)
    await provider.aclose()
    await provider.aclose()
    try:
        [event async for event in provider.stream(ModelRequest(messages=[]))]
        assert False
    except ProviderError:
        pass


def test_request_to_prompt_does_not_duplicate_final_user_message():
    request = ModelRequest(
        messages=[
            Message("user", [Text("first")]),
            Message("assistant", [Text("reply")]),
            Message("user", [Text("latest")]),
        ],
        system="sys",
    )
    rendered = json.loads(request_to_prompt(request))
    assert rendered["instructions"] == "sys"
    assert rendered["user"] == "latest"
    assert rendered["history"] == [
        {"role": "user", "text": "first"},
        {"role": "assistant", "text": "reply"},
    ]


def test_request_to_prompt_single_user_message_has_empty_history():
    request = ModelRequest(messages=[Message("user", [Text("hello")])])
    rendered = json.loads(request_to_prompt(request))
    assert rendered["user"] == "hello"
    assert rendered["history"] == []


def test_request_to_prompt_trailing_assistant_is_all_history():
    request = ModelRequest(
        messages=[Message("user", [Text("q")]), Message("assistant", [Text("a")])]
    )
    rendered = json.loads(request_to_prompt(request))
    assert rendered["user"] == ""
    assert rendered["history"] == [
        {"role": "user", "text": "q"},
        {"role": "assistant", "text": "a"},
    ]


def test_request_to_prompt_degrades_non_text_blocks_deterministically():
    request = ModelRequest(
        messages=[
            Message(
                "assistant",
                [
                    Thinking("secret reasoning", signature="sig"),
                    ToolUse("c1", "Read", {"path": "a.txt", "z": 1}),
                    ToolResult("c1", [Text("contents")], is_error=True),
                    Image("image/png", data=b"xx"),
                    Document("application/pdf", b"%PDF", title="notes"),
                ],
            ),
            Message("user", [Text("go")]),
        ]
    )
    rendered = json.loads(request_to_prompt(request))
    history_text = rendered["history"][0]["text"]
    assert "secret reasoning" not in history_text  # thinking is dropped by policy
    assert '[tool_use Read {"path": "a.txt", "z": 1}]' in history_text
    assert "[tool_result_error contents]" in history_text
    assert "[image data:image/png]" in history_text
    assert "[document application/pdf notes]" in history_text


def test_request_to_prompt_metadata_override():
    request = ModelRequest(messages=[], metadata={"prompt": "raw prompt"})
    assert request_to_prompt(request) == "raw prompt"


async def test_adapter_tolerates_malformed_usage(tmp_path):
    executable = _fake_codex(
        tmp_path,
        """
import json
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": "not-a-number"}}))
""",
    )
    config = Config(executable=str(executable), timeout_seconds=5)
    provider = LegacyCodexCLIProvider(workspace=tmp_path, config=config)
    request = ModelRequest(messages=[], metadata={"prompt": "hello"})
    events = [event async for event in provider.stream(request)]
    assert not any(isinstance(event, Usage) for event in events)
    assert any(isinstance(event, Raw) for event in events)
    assert isinstance(events[-1], MessageStop)


async def test_early_close_terminates_inner_process(tmp_path):
    executable = _fake_codex(
        tmp_path,
        """
import json, os, time
print(json.dumps({"type": "pid", "pid": os.getpid()}), flush=True)
time.sleep(30)
""",
    )
    config = Config(executable=str(executable), timeout_seconds=5)
    provider = LegacyCodexCLIProvider(workspace=tmp_path, config=config)
    request = ModelRequest(messages=[], metadata={"prompt": "hello"})
    stream = provider.stream(request)
    first = await anext(stream)
    assert isinstance(first, MessageStart)
    pid_event = await anext(stream)
    assert isinstance(pid_event, Raw)
    pid = pid_event.data["pid"]
    await asyncio.wait_for(stream.aclose(), timeout=5)
    # The inner transport's finally block reaps the process group on aclose.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("inner Codex process was not terminated on aclose")
