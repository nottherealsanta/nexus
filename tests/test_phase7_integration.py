"""Phase 7 P7I: provider breadth and the config-driven provider set.

This is the end-to-end packet that wires the Phase 7 adapters into the runtime,
the router, the loop, and configuration. Everything here is offline: HTTP
adapters dial an ``httpx.MockTransport``, and the streamed bytes are the same
normalized encoders the conformance suite uses.

Covered:

* provider construction from ``[providers.*]`` -- OpenAI (Responses and Chat),
  an arbitrary OpenAI-compatible vendor with only a config block, Gemini under
  ``google``/``gemini``, and Ollama/llama.cpp;
* one shared HTTP client owned by the runtime, closed exactly once;
* request-time secrets (``${env:VAR}`` never resolved at construction);
* Codex model ids routing to the OpenAI Responses dialect;
* the ``model.fallback`` chain, tried only on a provider-level failure before
  any output, with visible ``model.retrying``/``context.degraded`` -- never on
  a refusal or after partial output;
* a visible mid-session provider switch;
* quarantined file-loaded providers from ``.nexus/providers/``;
* every shipped streaming adapter present in the conformance aggregate.

The legacy ``Agent``/``CLI`` remain importable; they are retired by a later
plan step, not this one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from provider_conformance import ADAPTER_SPECS, run_all
from provider_conformance.encoding import encode_openai_chat_sse

from nexus.config import Config
from nexus.config.schema import (
    ConfigV2,
    ModelSection,
    ModelsSection,
    PermissionsSection,
    ProviderSection,
)
from nexus.errors import ConfigError, ProviderError
from nexus.model.providers.anthropic import AnthropicProvider
from nexus.model.providers.gemini import GeminiProvider
from nexus.model.providers.ollama import OllamaProvider
from nexus.model.providers.openai import API_CHAT, API_RESPONSES, OpenAIProvider
from nexus.model.providers.opencode import OpenCodeProvider
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.model.registry import ADAPTER_OPENAI, ModelRegistry, map_provider
from nexus.model.message import Message, Text
from nexus.model.request import ModelRequest, ToolSchema
from nexus.model.stream import MessageStart, MessageStop, TextDelta, ToolCallEnd
from nexus.runtime import Runtime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(
    *,
    default: str | None = None,
    fallback: list[str] | None = None,
    providers: dict[str, ProviderSection] | None = None,
) -> Config:
    default = default or "acme/llama-3"
    # Fallback lives on ``[model]`` (the compatibility section) so ``[models]``
    # is left at its defaults and no live registry is built; these tests are
    # offline and must never fetch a catalogue.
    v2 = ConfigV2(
        model=ModelSection(default=default, fallback=list(fallback or [])),
        providers=dict(providers or {}),
        permissions=PermissionsSection(mode="allow", on_unattended="allow"),
    )
    return Config(model=default, version=2, v2=v2)


def mock_stream(events, encoder=encode_openai_chat_sse) -> httpx.MockTransport:
    content = encoder(events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    return httpx.MockTransport(handler)


async def drain(session):
    return [event async for event in session.send("go")]


def event_types(events) -> list[str]:
    return [event.type for event in events]


def events_of(events, kind: str) -> list:
    return [event for event in events if event.type == kind]


def assistant_messages(session):
    return [message for message in session.messages if message.role == "assistant"]


# ---------------------------------------------------------------------------
# Provider construction from config
# ---------------------------------------------------------------------------


async def test_config_constructs_each_adapter_by_name_and_kind(tmp_path):
    config = make_config(
        default="openai/gpt-5.6",
        providers={
            "openai": ProviderSection(api_key="k"),
            "gemini": ProviderSection(api_key="g"),
            "ollama": ProviderSection(base_url="http://localhost:11434"),
            "anthropic": ProviderSection(api_key="a"),
            "acme": ProviderSection(
                kind="openai_compatible",
                base_url="https://api.acme.test/v1",
                api_key="v",
            ),
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        assert isinstance(runtime.providers["openai"], OpenAIProvider)
        assert runtime.providers["openai"].api == API_RESPONSES
        assert isinstance(runtime.providers["anthropic"], AnthropicProvider)
        assert isinstance(runtime.providers["ollama"], OllamaProvider)
        assert runtime.providers["ollama"].api == "ollama"
        # Gemini answers to the catalogue id ``google`` and the bare name.
        assert runtime.providers["google"] is runtime.providers["gemini"]
        assert isinstance(runtime.providers["google"], GeminiProvider)
        # An arbitrary OpenAI-compatible vendor needs only the config block.
        acme = runtime.providers["acme"]
        assert isinstance(acme, OpenAIProvider)
        assert acme.api == API_CHAT
        assert acme._base_url == "https://api.acme.test/v1"
    finally:
        await runtime.aclose()


async def test_shared_client_is_owned_once_by_the_runtime(tmp_path):
    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible", base_url="https://api.acme.test/v1"
            ),
            "ollama": ProviderSection(base_url="http://localhost:11434"),
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    first = runtime.providers["acme"].transport.client
    second = runtime.providers["ollama"].transport.client
    assert first is second
    assert first.is_closed is False

    await runtime.aclose()
    assert first.is_closed is True
    await runtime.aclose()  # idempotent
    assert first.is_closed is True


async def test_request_time_secret_is_not_read_at_construction(tmp_path):
    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible",
                base_url="https://api.acme.test/v1",
                api_key="${env:ACME_KEY}",
            )
        },
    )
    # Constructing with the variable absent must not raise; credentials are
    # references resolved at request time.
    runtime = Runtime(
        tmp_path,
        config=config,
        environ={},
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    try:
        provider = runtime.providers["acme"]
        assert "ACME_KEY" not in repr(provider)
        with pytest.raises(ProviderError) as excinfo:
            [event async for event in provider.stream(ModelRequest(messages=[]))]
        assert "ACME_KEY" in str(excinfo.value)
        # The reference itself is never the configured value.
        assert "${env:ACME_KEY}" not in str(excinfo.value)
    finally:
        await runtime.aclose()


async def test_config_only_vendor_completes_a_turn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n"
        "[models]\n"
        'default = "acme/llama-3"\n'
        "offline = true\n"
        "\n"
        "[providers.acme]\n"
        'kind = "openai_compatible"\n'
        'base_url = "https://api.acme.test/v1"\n'
        'api_key = "${env:ACME_KEY}"\n',
        encoding="utf-8",
    )
    config = Config.load(tmp_path, home=home, environ={"ACME_KEY": "sekret"})
    events = [
        MessageStart(model="llama-3"),
        TextDelta(text="hi"),
        MessageStop(stop_reason="end_turn"),
    ]
    runtime = Runtime(
        tmp_path,
        home=home,
        config=config,
        environ={"ACME_KEY": "sekret"},
        http_transport=mock_stream(events),
    )
    try:
        session = runtime.session("s")
        await drain(session)
        assistants = assistant_messages(session)
        assert assistants and assistants[-1].content[0].text == "hi"
        assert assistants[-1].meta.provider == "acme"
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Codex ids route OpenAI Responses
# ---------------------------------------------------------------------------


def test_codex_provider_id_maps_to_openai_adapter():
    assert map_provider("codex", None) == ADAPTER_OPENAI


async def test_codex_model_reference_uses_responses_system_tools_and_normalized_calls(
    tmp_path,
):
    captured: dict[str, object] = {}
    body = b"".join(
        (
            b"event: response.created\n",
            b'data: {"type":"response.created","response":{"id":"r1","model":"gpt-5.6-luna","status":"in_progress"}}\n\n',
            b"event: response.output_item.added\n",
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc1","call_id":"call1","name":"Read","arguments":"{\\\"path\\\":\\\"a.txt\\\"}"}}\n\n',
            b"event: response.output_item.done\n",
            b'data: {"type":"response.output_item.done","item":{"type":"function_call","id":"fc1","call_id":"call1","name":"Read","arguments":"{\\\"path\\\":\\\"a.txt\\\"}"}}\n\n',
            b"event: response.completed\n",
            b'data: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{}}}\n\n',
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    config = make_config(
        default="codex/gpt-5.6-luna",
        providers={
            "codex": ProviderSection(api_key="${env:OPENAI_API_KEY}", api="responses")
        },
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        environ={"OPENAI_API_KEY": "test-key"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        provider = runtime.providers["codex"]
        assert isinstance(provider, OpenAIProvider)
        assert provider.api == API_RESPONSES
        events = [
            event
            async for event in provider.stream(
                ModelRequest(
                    messages=[Message("user", [Text("inspect")])],
                    system="Nexus system prompt",
                    tools=[ToolSchema("Read", "Read a file", {"type": "object"})],
                )
            )
        ]
        assert captured["url"] == "https://api.openai.com/v1/responses"
        assert captured["body"] == {
            "model": "gpt-5.6-luna",
            "instructions": "Nexus system prompt",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "Read",
                    "description": "Read a file",
                    "parameters": {"type": "object"},
                }
            ],
            "stream": True,
            "max_output_tokens": 4096,
        }
        calls = [event for event in events if isinstance(event, ToolCallEnd)]
        assert calls == [ToolCallEnd(id="call1", input={"path": "a.txt"})]
    finally:
        await runtime.aclose()


async def test_codex_oauth_runtime_uses_private_endpoint_and_disables_response_storage(tmp_path):
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\",\"usage\":{}}}\n\n")

    class OAuthManager:
        def __init__(self, *, profile):
            assert profile == "default"

        async def headers(self):
            return {"authorization": "Bearer test-token"}

    config = make_config(
        default="codex/gpt-5.6-luna",
        providers={"codex": ProviderSection(auth="chatgpt_oauth", profile="default", api="responses")},
    )
    runtime = Runtime(
        tmp_path,
        config=config,
        codex_auth_factory=OAuthManager,
        http_transport=httpx.MockTransport(handler),
    )
    try:
        provider = runtime.providers["codex"]
        assert isinstance(provider, OpenAIProvider)
        [event async for event in provider.stream(ModelRequest(messages=[Message("user", [Text("ping")])]))]
    finally:
        await runtime.aclose()

    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert captured["body"]["store"] is False
    assert "max_output_tokens" not in captured["body"]


async def test_codex_registry_projection_routes_first_turn_with_default_output_limit(tmp_path):
    captured: dict[str, object] = {}
    body = b"".join((
        b"event: response.created\n",
        b'data: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n\n',
        b"event: response.output_text.delta\n",
        b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
        b"event: response.completed\n",
        b'data: {"type":"response.completed","response":{"id":"r1","status":"completed","usage":{}}}\n\n',
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    section = ProviderSection(api_key="${env:OPENAI_API_KEY}", api="responses")
    registry = ModelRegistry(
        providers={"codex": section}, provider_aliases={"codex": "openai"},
        env={}, snapshot_path=tmp_path / "missing.json",
    )
    registry.install_raw(json.dumps({"openai": {"models": {"gpt-5.6-luna": {
        "modalities": {"output": ["text"]},
        "limit": {"context": 1_050_000, "output": 128_000},
        "tool_call": True,
    }}}}).encode())
    config = make_config(
        default="codex/gpt-5.6-luna", providers={"codex": section}
    )
    runtime = Runtime(
        tmp_path, config=config, registry=registry,
        environ={"OPENAI_API_KEY": "test-key"},
        http_transport=httpx.MockTransport(handler),
    )
    try:
        assert [model.ref for model in registry.list()] == ["codex/gpt-5.6-luna"]
        assert runtime.router.resolve(ModelRequest(messages=[])).provider.name == "codex"
        await drain(runtime.session("codex-projection"))
        sent = captured["body"]
        assert sent["model"] == "gpt-5.6-luna"
        assert sent["max_output_tokens"] == 4096
        assert sent["instructions"]
        assert sent["tools"]
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Fallback: only on a provider-level, pre-output failure
# ---------------------------------------------------------------------------


def fallback_config(default: str = "primary/m1", fallback=("backup/m2",)):
    return make_config(
        default=default,
        fallback=list(fallback),
        providers={
            "primary": ProviderSection(
                kind="openai_compatible", base_url="https://primary.test/v1"
            ),
            "backup": ProviderSection(
                kind="openai_compatible", base_url="https://backup.test/v1"
            ),
        },
    )


async def test_provider_failure_falls_back_with_visible_events(tmp_path):
    primary = ScriptedProvider([ProviderError("boom")], name="primary")
    backup = ScriptedProvider(text_response("backup ok"), name="backup")
    runtime = Runtime(
        tmp_path,
        config=fallback_config(),
        providers={"primary": primary, "backup": backup},
    )
    try:
        session = runtime.session("s")
        events = await drain(session)

        assert "model.retrying" in event_types(events)
        retrying = events_of(events, "model.retrying")[0]
        assert retrying.data["from_provider"] == "primary"
        assert retrying.data["provider"] == "backup"
        degraded = events_of(events, "context.degraded")
        assert any(item.data["reason"] == "provider_failed" for item in degraded)
        # The fallback's own model id is used, not the failed primary's.
        assert retrying.data["model"] == "m2"
        assistants = assistant_messages(session)
        assert assistants[-1].meta.provider == "backup"
        assert assistants[-1].meta.model == "m2"
        assert assistants[-1].content[0].text == "backup ok"
    finally:
        await runtime.aclose()


async def test_refusal_does_not_fall_back(tmp_path):
    primary = ScriptedProvider(
        text_response("i cannot", stop_reason="refusal"), name="primary"
    )
    backup = ScriptedProvider(text_response("backup ok"), name="backup")
    runtime = Runtime(
        tmp_path,
        config=fallback_config(),
        providers={"primary": primary, "backup": backup},
    )
    try:
        session = runtime.session("s")
        events = await drain(session)
        assert "model.retrying" not in event_types(events)
        assert backup.requests == []
        assistants = assistant_messages(session)
        assert assistants[-1].meta.provider == "primary"
    finally:
        await runtime.aclose()


async def test_partial_output_does_not_fall_back(tmp_path):
    primary = ScriptedProvider(
        [MessageStart(model="p"), TextDelta(text="partial"), ProviderError("boom")],
        name="primary",
    )
    backup = ScriptedProvider(text_response("backup ok"), name="backup")
    runtime = Runtime(
        tmp_path,
        config=fallback_config(),
        providers={"primary": primary, "backup": backup},
    )
    try:
        session = runtime.session("s")
        events = await drain(session)
        assert "text.delta" in event_types(events)
        assert "model.retrying" not in event_types(events)
        assert backup.requests == []
        assert events[-1].type == "turn.failed"
    finally:
        await runtime.aclose()


async def test_no_fallback_configured_has_no_retry(tmp_path):
    primary = ScriptedProvider([ProviderError("boom")], name="primary")
    runtime = Runtime(
        tmp_path,
        config=make_config(default="primary/m1"),
        providers={"primary": primary},
    )
    try:
        session = runtime.session("s")
        events = await drain(session)
        assert "model.retrying" not in event_types(events)
        assert events[-1].type == "turn.failed"
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Mid-session provider switch degradation
# ---------------------------------------------------------------------------


async def test_mid_session_provider_switch_is_visible(tmp_path):
    # Turn 1: primary fails, fallback "backup" answers. Turn 2: primary is up,
    # so the session switches back and the reinterpretation is surfaced.
    primary = ScriptedProvider(
        [ProviderError("boom")], text_response("primary turn two"), name="primary"
    )
    backup = ScriptedProvider(text_response("backup turn one"), name="backup")
    runtime = Runtime(
        tmp_path,
        config=fallback_config(),
        providers={"primary": primary, "backup": backup},
    )
    try:
        session = runtime.session("s")
        first = await drain(session)
        assert assistant_messages(session)[-1].meta.provider == "backup"

        second = await drain(session)
        switches = [
            event
            for event in events_of(first + second, "context.degraded")
            if event.data.get("reason") == "provider_switch"
        ]
        assert switches
        assert {item.data["from"] for item in switches} == {"backup"}
        assert {item.data["to"] for item in switches} == {"primary"}
        assert assistant_messages(session)[-1].meta.provider == "primary"
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# File-loaded (hot) providers with quarantine
# ---------------------------------------------------------------------------


def write_provider(tmp_path: Path, name: str, body: str, *, root: str = ".nexus"):
    directory = tmp_path / root / "providers"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(body, encoding="utf-8")


HOT_PROVIDER = (
    "from nexus.model.providers.scripted import ScriptedProvider, text_response\n"
    "PROVIDER = ScriptedProvider("
    "text_response('from hot provider', model='hot-1'), name='hot', model='hot-1')\n"
)


async def test_file_loaded_provider_is_registered_and_used(tmp_path):
    write_provider(tmp_path, "hot", HOT_PROVIDER)
    config = make_config(
        default="hot/hot-1",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible", base_url="https://api.acme.test/v1"
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        assert "hot" in runtime.providers
        session = runtime.session("s")
        await drain(session)
        assistants = assistant_messages(session)
        assert assistants[-1].meta.provider == "hot"
        assert assistants[-1].content[0].text == "from hot provider"
    finally:
        await runtime.aclose()


async def test_broken_provider_files_are_quarantined_not_fatal(tmp_path):
    write_provider(tmp_path, "hot", HOT_PROVIDER)
    write_provider(tmp_path, "syntax", "def (:\n")
    write_provider(
        tmp_path,
        "side_effect",
        "import os\nos.system('echo pwned')\nPROVIDER = None\n",
    )
    runtime = Runtime(
        tmp_path, config=make_config(default="hot/hot-1"), environ={}
    )
    try:
        assert "hot" in runtime.providers
        diagnostics = runtime._provider_file_diagnostics
        codes = {item["code"] for item in diagnostics}
        assert codes == {"syntax", "side_effect"}
        assert all(item["path"] for item in diagnostics)
    finally:
        await runtime.aclose()


async def test_configured_provider_shadows_a_file_of_the_same_name(tmp_path):
    write_provider(tmp_path, "acme", HOT_PROVIDER)
    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible", base_url="https://api.acme.test/v1"
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        assert isinstance(runtime.providers["acme"], OpenAIProvider)
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Fallback config reconciliation
# ---------------------------------------------------------------------------


def test_conflicting_fallback_lists_are_rejected():
    with pytest.raises(ValueError, match="fallback"):
        ConfigV2(
            model=ModelSection(default="a/m", fallback=["x/1"]),
            models=ModelsSection(default="a/m", fallback=["y/2"]),
        )


# ---------------------------------------------------------------------------
# Registry capabilities flow through the router
# ---------------------------------------------------------------------------


async def test_registry_capabilities_override_the_adapter_fallback(tmp_path):
    from nexus.model.registry import ModelInfo

    info = ModelInfo(
        provider="acme",
        id="llama-3",
        tool_call=False,
        reasoning=False,
        context=8192,
        max_output=2048,
    )

    class FakeRegistry:
        def get(self, ref):
            return info if ref == "acme/llama-3" else None

        def list(self, tier=None, selectable_only=False):
            return []

    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible", base_url="https://api.acme.test/v1"
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={}, registry=FakeRegistry())
    try:
        resolved = runtime.router.resolve(
            ModelRequest(messages=[], model="acme/llama-3")
        )
        # The registry is authoritative for the fields it describes ...
        assert resolved.capabilities.tools is False
        assert resolved.capabilities.max_context_tokens == 8192
        # ... while the transport-only fields stay the adapter's.
        assert resolved.capabilities.streaming is True
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Conformance aggregate covers every shipped streaming adapter
# ---------------------------------------------------------------------------


def test_conformance_aggregate_contains_every_shipped_adapter():
    names = {spec.name for spec in ADAPTER_SPECS}
    assert {
        "anthropic",
        "gemini",
        "openai_responses",
        "openai_chat",
        "ollama",
        "ollama-openai",
        "opencode_acp",
        "scripted",
    } <= names


async def test_conformance_aggregate_is_green():
    report = await run_all()
    assert report.failed == 0, report.format()
    assert report.errors == 0, report.format()


# ---------------------------------------------------------------------------
# The canonical CLI is a pure daemon client (the legacy agent path is gone)
# ---------------------------------------------------------------------------


def test_canonical_cli_exposes_parser_and_no_legacy_agent():
    from nexus import cli

    parser = cli.build_parser()
    actions = set(parser._subparsers._group_actions[0].choices)
    assert {
        "init",
        "doctor",
        "run",
        "chat",
        "replay",
        "daemon",
        "sessions",
        "ext",
        "models",
        "agents",
    } <= actions

    import nexus

    for legacy in ("Agent", "CodexProvider", "Provider"):
        assert legacy not in nexus.__all__


# ---------------------------------------------------------------------------
# OpenAI-compatible vendors must name their endpoint
# ---------------------------------------------------------------------------


def test_openai_compatible_kind_requires_a_base_url(tmp_path):
    # Never default a third-party vendor to api.openai.com: that would send the
    # vendor's model id (and key) to the wrong service.
    config = make_config(
        default="acme/llama-3",
        providers={"acme": ProviderSection(kind="openai_compatible")},
    )
    with pytest.raises(ConfigError, match="base_url"):
        Runtime(tmp_path, config=config, environ={})


async def test_unknown_vendor_with_base_url_is_openai_compatible(tmp_path):
    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(base_url="https://api.acme.test/v1")
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        provider = runtime.providers["acme"]
        assert isinstance(provider, OpenAIProvider)
        assert provider.api == API_CHAT
    finally:
        await runtime.aclose()


async def test_registry_capability_source_reaches_the_adapter(tmp_path):
    from nexus.model.registry import ModelInfo

    info = ModelInfo(
        provider="acme",
        id="llama-3",
        tool_call=False,
        reasoning=False,
        context=8192,
        max_output=2048,
    )

    class FakeRegistry:
        def get(self, ref):
            return info if ref in ("acme/llama-3", "llama-3") else None

        def list(self, tier=None, selectable_only=False):
            return []

    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible", base_url="https://api.acme.test/v1"
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={}, registry=FakeRegistry())
    try:
        provider = runtime.providers["acme"]
        # The adapter's own view must match the router's, or it would build a
        # body with tools the loop already dropped.
        adapter_caps = provider.capabilities("llama-3")
        assert adapter_caps.tools is False
        resolved = runtime.router.resolve(
            ModelRequest(messages=[], model="acme/llama-3")
        )
        assert resolved.capabilities.tools is False
        assert adapter_caps.tools == resolved.capabilities.tools
        # Registry-owned fields are adopted while transport-only fields and the
        # degradation policy survive the merge.
        assert adapter_caps.max_context_tokens == 8192
        assert adapter_caps.streaming is True
        assert adapter_caps.degradation.get("thinking") in {
            "drop",
            "to_text",
            "error",
        }
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Injected client ownership
# ---------------------------------------------------------------------------


async def test_injected_client_is_never_closed_by_the_runtime(tmp_path):
    injected = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    config = make_config(
        default="acme/llama-3",
        providers={
            "acme": ProviderSection(
                kind="openai_compatible", base_url="https://api.acme.test/v1"
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={}, client=injected)
    try:
        assert runtime.providers["acme"].transport.client is injected
        await runtime.aclose()
        assert injected.is_closed is False
    finally:
        await injected.aclose()


# ---------------------------------------------------------------------------
# Fallback validation
# ---------------------------------------------------------------------------


def test_fallback_entries_must_be_nonempty_strings():
    with pytest.raises(ValueError, match="fallback"):
        ConfigV2(model=ModelSection(default="a/m", fallback=[""]))


def test_router_rejects_malformed_fallback_entries():
    from nexus.model.router import ModelRouter

    with pytest.raises(ConfigError):
        ModelRouter({}, fallback=["ok/1", 123])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# The user provider directory works without an explicit home
# ---------------------------------------------------------------------------


async def test_user_provider_directory_is_discovered_with_default_home(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    directory = home / ".nexus" / "providers"
    directory.mkdir(parents=True)
    (directory / "hot.py").write_text(HOT_PROVIDER, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    runtime = Runtime(tmp_path, config=make_config(default="hot/hot-1"), environ={})
    try:
        assert "hot" in runtime.providers
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# File-loaded provider quarantine semantics
# ---------------------------------------------------------------------------


async def test_top_level_side_effect_is_quarantined_but_a_function_body_is_not(tmp_path):
    write_provider(tmp_path, "innocuous", "import os\n"
                   "def helper():\n"
                   "    os.system('echo used-only-if-called')\n"
                   "from nexus.model.providers.scripted import ScriptedProvider, text_response\n"
                   "PROVIDER = ScriptedProvider(text_response('ok'), name='innocuous')\n")
    write_provider(tmp_path, "eager", "import os\nos.system('echo pwned')\nPROVIDER = None\n")
    runtime = Runtime(
        tmp_path, config=make_config(default="hot/hot-1"), environ={}
    )
    try:
        # The dangerous call nested in a function body is deferred, not flagged.
        assert "innocuous" in runtime.providers
        codes = {item["code"] for item in runtime._provider_file_diagnostics}
        assert codes == {"side_effect"}
    finally:
        await runtime.aclose()


async def test_provider_file_build_baseexception_is_quarantined(tmp_path):
    write_provider(
        tmp_path,
        "explode",
        "def build(context):\n    raise SystemExit('boom')\n",
    )
    runtime = Runtime(tmp_path, config=make_config(default="hot/hot-1"), environ={})
    try:
        assert "explode" not in runtime.providers
        codes = {item["code"] for item in runtime._provider_file_diagnostics}
        assert codes == {"build_failed"}
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# OpenCode ACP integration
# ---------------------------------------------------------------------------

OPENCODE_FAKE = (
    Path(__file__).parent / "fixtures" / "opencode" / "fake_acp_agent.py"
)


async def test_opencode_provider_is_agent_surface_from_config(tmp_path):
    config = make_config(
        default="opencode/opaque-agent-model",
        providers={
            "opencode": ProviderSection(
                kind="opencode_agent",
                command=[sys.executable, str(OPENCODE_FAKE), "--scenario", "text"],
                permission_policy="deny",
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        provider = runtime.providers["opencode"]
        assert isinstance(provider, OpenCodeProvider)
        # It is an agent surface: model id is opaque, tools are the agent's own.
        caps = provider.capabilities("opaque-agent-model")
        assert caps.tools is False
        assert caps.streaming is True
    finally:
        await runtime.aclose()


async def test_opencode_agent_completes_a_turn_and_is_opaque(tmp_path):
    config = make_config(
        default="opencode/whatever",
        providers={
            "opencode": ProviderSection(
                kind="opencode_agent",
                command=[sys.executable, str(OPENCODE_FAKE), "--scenario", "text"],
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        session = runtime.session("s")
        events = await drain(session)
        assistants = assistant_messages(session)
        assert assistants and assistants[-1].content[0].text == "hello world"
        assert "tool.requested" not in event_types(events)
    finally:
        await runtime.aclose()


def test_map_provider_covers_opencode():
    from nexus.model.registry import ADAPTER_OPENCODE

    assert map_provider("opencode", None) == ADAPTER_OPENCODE
    assert map_provider("opencode", "@ai-sdk/no-such-thing") == ADAPTER_OPENCODE


# ---------------------------------------------------------------------------
# ``google`` is a Gemini alias, consistently in runtime and registry
# ---------------------------------------------------------------------------


async def test_kind_google_selects_gemini_for_a_custom_provider_name(tmp_path):
    config = make_config(
        default="vertexish/m",
        providers={
            "vertexish": ProviderSection(kind="google", api_key="g")
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        assert isinstance(runtime.providers["vertexish"], GeminiProvider)
    finally:
        await runtime.aclose()


def test_registry_normalizes_google_kind_to_gemini():
    from nexus.model.registry import ADAPTER_GEMINI, normalize_adapter_kind

    assert normalize_adapter_kind("google") == ADAPTER_GEMINI
    assert normalize_adapter_kind("gemini") == ADAPTER_GEMINI
    assert normalize_adapter_kind("GOOGLE") == ADAPTER_GEMINI
    assert normalize_adapter_kind("not-a-kind") is None


def test_raw_copilot_adapter_is_deliberately_absent():
    # Architecture decision (plan assumption #1): the Copilot token-exchange
    # endpoint and non-editor licence terms are unresolved, so no raw Copilot
    # adapter ships. OpenCode is integrated over ACP only.
    import nexus.model.providers as providers_pkg

    names = {name.lower() for name in providers_pkg.__all__}
    assert not any("copilot" in name for name in names)
    assert (Path(providers_pkg.__file__).parent / "copilot.py").exists() is False
    assert "Copilot" not in {name for name in providers_pkg.__all__}


# ---------------------------------------------------------------------------
# Adapter reprs never leak endpoint userinfo or argv credentials
# ---------------------------------------------------------------------------


def test_adapter_reprs_redact_base_url_userinfo():
    from nexus.model.providers.gemini import GeminiProvider
    from nexus.model.providers.ollama import OllamaProvider

    url = "https://user:hunter2@gateway.test/v1"
    providers = [
        AnthropicProvider(api_key="k", base_url=url),
        OpenAIProvider(api_key="k", base_url=url, api="chat"),
        GeminiProvider(api_key="k", base_url=url),
        OllamaProvider(base_url=url),
    ]
    for provider in providers:
        text = repr(provider)
        assert "hunter2" not in text, text
        assert "gateway.test" in text, text


def test_opencode_repr_redacts_argv_credentials(tmp_path):
    secret = "sk-live-OPENCODEARG0123456789"
    provider = OpenCodeProvider(
        command=["/opt/opencode", "acp", "--token", secret],
        workspace=tmp_path,
    )
    text = repr(provider)
    assert secret not in text
    assert "/opt/opencode" in text
    # The raw argv accessor still returns the real value for execution.
    assert secret in provider.server_argv()


# ---------------------------------------------------------------------------
# Gemini is built once per configured section, never memo-collapsed
# ---------------------------------------------------------------------------


async def test_two_google_kind_sections_build_distinct_instances(tmp_path):
    config = make_config(
        default="g1/m",
        providers={
            "g1": ProviderSection(
                kind="google",
                api_key="key-one",
                base_url="https://one.example.com",
            ),
            "g2": ProviderSection(
                kind="google",
                api_key="key-two",
                base_url="https://two.example.com",
            ),
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        first = runtime.providers["g1"]
        second = runtime.providers["g2"]
        assert isinstance(first, GeminiProvider)
        assert isinstance(second, GeminiProvider)
        assert first is not second
        assert first._base_url == "https://one.example.com"
        assert second._base_url == "https://two.example.com"
        # A custom Google-kind name never claims the catalogue keys.
        assert "google" not in runtime.providers
        assert "gemini" not in runtime.providers
    finally:
        await runtime.aclose()


async def test_configured_gemini_section_owns_both_catalogue_names(tmp_path):
    config = make_config(
        default="google/m",
        providers={
            "gemini": ProviderSection(
                kind="gemini",
                api_key="key",
                base_url="https://configured.example.com",
            )
        },
    )
    runtime = Runtime(tmp_path, config=config, environ={})
    try:
        # The configured section is not replaced by a bare ``google/`` default.
        assert runtime.providers["google"] is runtime.providers["gemini"]
        assert runtime.providers["google"]._base_url == "https://configured.example.com"
    finally:
        await runtime.aclose()


# ---------------------------------------------------------------------------
# Hot provider modules do not accumulate in sys.modules
# ---------------------------------------------------------------------------


async def test_hot_provider_modules_are_not_retained(tmp_path):
    import sys

    write_provider(tmp_path, "hot", HOT_PROVIDER)
    before = {name for name in sys.modules if name.startswith("nexus_hot_provider_")}
    for _ in range(3):
        runtime = Runtime(tmp_path, config=make_config(default="hot/hot-1"), environ={})
        try:
            assert "hot" in runtime.providers
        finally:
            await runtime.aclose()
    after = {name for name in sys.modules if name.startswith("nexus_hot_provider_")}
    assert after == before
