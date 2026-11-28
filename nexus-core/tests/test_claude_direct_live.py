"""Opt-in, bounded Haiku probe; never modifies Claude credentials."""
import base64
import os
import struct
import uuid
import zlib
from pathlib import Path

import pytest

from nexus.llmhub.anthropic import AnthropicProvider, DirectRequest
from nexus.llmhub.auth.claude_direct import ClaudeDirectAuth
from nexus.llmhub.auth.store import CredentialStore
from nexus.llmhub.types import (
    Done,
    Image,
    Media,
    Message,
    PartDone,
    Reasoning,
    Request,
    Text,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    WireResponse,
)

pytestmark = pytest.mark.skipif(os.environ.get("NEXUS_CLAUDE_DIRECT_LIVE") != "1", reason="real subscription requests require explicit opt-in")


@pytest.fixture
def auth(tmp_path, monkeypatch):
    # An explicit scratch login stays separate from each test's temporary Nexus state.
    home = os.environ.get("NEXUS_CLAUDE_DIRECT_AUTH_HOME")
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    return ClaudeDirectAuth(CredentialStore(Path(home) / "auth.json") if home else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["eager", "strict"])
async def test_haiku_native_tool_roundtrip(tmp_path, monkeypatch, auth, mode):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    assert auth.logged_in(), "No usable token in the selected Claude profile or CLAUDE_CODE_OAUTH_TOKEN"
    provider = AnthropicProvider(auth, max_tokens=128, eager_tools=("probe",) if mode == "eager" else (),
                                 strict_tools=("probe",) if mode == "strict" else ())
    model = "claude-haiku-4-5"
    tools = (ToolSpec("probe", "Returns a verification word. Call this before answering.", {"type": "object", "properties": {}, "additionalProperties": False}),)
    history = (Message("user", (Text("Call probe once, then reply with its verification word."),)),)
    events = [e async for e in provider.stream(Request(model, "Use only the offered tool. Be brief.", history, tools))]
    assert any(isinstance(e, WireResponse) and e.status == 200 for e in events)
    assert events[-1].stop == "tool_calls"
    parts = tuple(e.part for e in events if isinstance(e, PartDone))
    calls = [p for p in parts if isinstance(p, ToolCall)]
    assert len(calls) == 1 and calls[0].name == "probe"
    history += (Message("assistant", parts), Message("tool", (ToolResult(calls[0].id, "NEXUS_ROUNDTRIP_OK"),)))
    reply = [e async for e in provider.stream(Request(model, "Use only the offered tool. Be brief.", history, tools))]
    assert isinstance(reply[-1], Done) and reply[-1].stop == "end_turn"
    text = "".join(e.part.text for e in reply if isinstance(e, PartDone) and isinstance(e.part, Text))
    assert "NEXUS_ROUNDTRIP_OK" in text


@pytest.mark.asyncio
async def test_haiku_thinking_and_signed_tool_replay(tmp_path, monkeypatch, auth):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    provider = AnthropicProvider(auth, max_tokens=6144)
    model = "claude-haiku-4-5"
    tools = (ToolSpec("probe", "Return a verification word. Call once before answering.", {"type": "object", "properties": {}}),)
    history = (Message("user", (Text("Think briefly about how to verify a tool's result. Call probe, then answer with its verification word."),)),)
    events = [e async for e in provider.stream(Request(model, "Be brief.", history, tools, "minimal"))]
    parts = tuple(e.part for e in events if isinstance(e, PartDone))
    assert any(isinstance(p, Reasoning) and p.raw.get("signature") for p in parts)
    calls = [p for p in parts if isinstance(p, ToolCall)]
    assert len(calls) == 1 and calls[0].name == "probe"
    history += (Message("assistant", parts), Message("tool", (ToolResult(calls[0].id, "SIGNED_REPLAY_OK"),)))
    reply = [e async for e in provider.stream(Request(model, "Be brief.", history, tools, "minimal"))]
    assert reply[-1].stop == "end_turn"
    assert "SIGNED_REPLAY_OK" in "".join(e.part.text for e in reply if isinstance(e, PartDone) and isinstance(e.part, Text))


def solid_png(rgb):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress((b"\0" + bytes(rgb) * 64) * 64)) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


@pytest.mark.asyncio
async def test_haiku_user_and_tool_images(tmp_path, monkeypatch, auth):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    provider = AnthropicProvider(auth, max_tokens=128)
    history = (
        Message("user", (Text("Image A"), Image(solid_png((0, 255, 0))), Text("Get image B from probe, then name the dominant colors of A and B in order."))),
        Message("assistant", (ToolCall("toolu_image_probe", "probe", "{}"),)),
        Message("tool", (ToolResult("toolu_image_probe", "Image B", media=(Media("probe_image", "image/png", data_url=solid_png((0, 0, 255))),)),)),
    )
    events = [e async for e in provider.stream(Request("claude-haiku-4-5", "Name colors briefly.", history))]
    text = "".join(e.part.text for e in events if isinstance(e, PartDone) and isinstance(e.part, Text)).lower()
    assert "green" in text and "blue" in text and text.index("green") < text.index("blue")


@pytest.mark.asyncio
async def test_haiku_prompt_cache_hit(tmp_path, monkeypatch, auth):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    provider = AnthropicProvider(auth, max_tokens=16)
    # Fresh prefix prevents a previous run from satisfying the cold-write check.
    system = f"Cache probe {uuid.uuid4()}. Reply only OK.\n" + "This is neutral reference text for a bounded prompt cache verification.\n" * 500
    req = Request("claude-haiku-4-5", system, (Message("user", (Text("OK?"),)),))
    first = [e async for e in provider.stream(req)]
    second = [e async for e in provider.stream(req)]
    usage = next(e for e in second if isinstance(e, Usage))
    assert first[-1].stop == second[-1].stop == "end_turn"
    assert usage.cached_input_tokens > 0


@pytest.mark.asyncio
async def test_haiku_explicit_prewarm_then_cache_hit(auth):
    provider = AnthropicProvider(auth, max_tokens=16)
    system = f"Prewarm probe {uuid.uuid4()}. Reply only OK.\n" + "Neutral bounded cache verification reference text.\n" * 600
    messages = (Message("user", (Text("OK?"),)),)
    warm = [e async for e in provider.stream(DirectRequest("claude-haiku-4-5", system, messages, prewarm=True))]
    assert warm[-1].stop == "max_tokens" and not any(isinstance(e, PartDone) for e in warm)
    warm_usage = next(e for e in warm if isinstance(e, Usage))
    assert warm_usage.input_tokens > 0 and warm_usage.output_tokens == 0
    reply = [e async for e in provider.stream(Request("claude-haiku-4-5", system, messages))]
    assert reply[-1].stop == "end_turn"
    assert next(e for e in reply if isinstance(e, Usage)).cached_input_tokens > 0


@pytest.mark.asyncio
async def test_haiku_new_api_version(auth):
    provider = AnthropicProvider(auth, max_tokens=16, api_version="2026-10-21")
    events = [e async for e in provider.stream(Request("claude-haiku-4-5", "Reply only OK.",
                                                      (Message("user", (Text("OK?"),)),)))]
    assert events[-1].stop == "end_turn"
    assert "OK" in "".join(e.part.text for e in events if isinstance(e, PartDone) and isinstance(e.part, Text))


@pytest.mark.asyncio
async def test_haiku_gzip_transport(auth):
    provider = AnthropicProvider(auth, max_tokens=16, request_encoding="gzip")
    req = DirectRequest("claude-haiku-4-5", "Reply only OK.", (Message("user", (Text("OK?"),)),))
    events = [e async for e in provider.stream(req)]
    assert events[-1].stop == "end_turn"
    assert "OK" in "".join(e.part.text for e in events if isinstance(e, PartDone) and isinstance(e.part, Text))


@pytest.mark.asyncio
async def test_haiku_accepts_nexus_tool_schemas_and_executes_in_nexus(tmp_path, monkeypatch, auth):
    from nexus.loop import Agent, ToolEnd, TurnEnd

    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "verification.txt").write_text("NEXUS_LIVE_LOOP_OK")
    agent = Agent(AnthropicProvider(auth, max_tokens=256), model="claude-haiku-4-5", workspace=workspace,
                  instructions="Read verification.txt with read, then reply with its content. Do nothing else.", max_steps=3)
    events = [e async for e in agent.turn("Verify this workspace.")]
    assert any(isinstance(e, ToolEnd) and e.call.name == "read" and not e.result.is_error for e in events)
    assert isinstance(events[-1], TurnEnd) and events[-1].stop == "end_turn"
    assert "NEXUS_LIVE_LOOP_OK" in "".join(p.text for m in agent.messages for p in m.parts if isinstance(p, Text) and m.role == "assistant")


@pytest.mark.asyncio
async def test_owned_browser_refresh_roundtrip(auth):
    from nexus.llmhub.auth.claude_direct import ENTRY, SCOPES

    if not os.environ.get("NEXUS_CLAUDE_DIRECT_AUTH_HOME"):
        pytest.skip("requires an explicit Nexus-owned scratch browser login")
    async with auth.store.alocked():
        previous = auth.store.read(ENTRY)
        assert previous and previous.get("refresh_token"), "Scratch home has no owned refresh grant"
        data = await auth._exchange({"grant_type": "refresh_token", "refresh_token": previous["refresh_token"],
                                     "scope": SCOPES}, refresh=True)
        refreshed = auth._entry(data, previous)
        auth.store.write(ENTRY, refreshed)
    assert refreshed["authorized_at"] == previous["authorized_at"]
    assert refreshed.get("organization") == previous.get("organization")
    assert (await auth.headers())["authorization"] == f"Bearer {refreshed['access_token']}"
