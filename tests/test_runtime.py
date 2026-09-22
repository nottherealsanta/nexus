"""Phase 1 Runtime tests: composition, lifecycle, and provider ownership."""
from pathlib import Path

import httpx

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, ProviderSection
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.tools.manager import ToolManager

FIXTURES = Path(__file__).parent / "fixtures" / "anthropic"


def scripted_config(model="scripted/sm"):
    return Config(model=model, version=2, v2=ConfigV2(model=ModelSection(default=model)))


def anthropic_config(model="anthropic/claude-test"):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            providers={"anthropic": ProviderSection(api_key="test-key")},
        ),
    )


# ---------------------------------------------------------------------------
# Multi-turn conversation
# ---------------------------------------------------------------------------


async def test_two_turn_scripted_conversation_sees_exact_ir(tmp_path):
    provider = ScriptedProvider(text_response("first"), text_response("second"))
    runtime = Runtime(tmp_path, config=scripted_config(), providers={"scripted": provider})
    session = runtime.session("conv")

    first_events = [event async for event in session.send("one")]
    second_events = [event async for event in session.send("two")]

    assert provider.calls == 2
    first, second = provider.requests

    assert [(m.role, m.content[0].text) for m in first.messages] == [("user", "one")]
    assert [(m.role, m.content[0].text) for m in second.messages] == [
        ("user", "one"),
        ("assistant", "first"),
        ("user", "two"),
    ]
    assert first.model == "sm"
    assert first.provider == "scripted"
    assert second.model == "sm"

    assert first_events[0].type == "turn.started"
    assert first_events[-1].type == "turn.completed"
    assert second_events[-1].type == "turn.completed"


async def test_runtime_open_and_default_session_directory(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime.open(tmp_path, config=scripted_config(), providers={"scripted": provider})
    session = runtime.session("main")

    await _drain(session.send("hi"))

    assert session.path == tmp_path / ".nexus" / "sessions" / "main.jsonl"
    assert session.path.exists()


# ---------------------------------------------------------------------------
# Provider lifecycle / ownership
# ---------------------------------------------------------------------------


async def test_injected_providers_are_not_closed_by_runtime(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    runtime = Runtime(tmp_path, config=scripted_config(), providers={"scripted": provider})

    await runtime.aclose()
    await runtime.aclose()  # idempotent

    assert provider.closed is False
    assert runtime.closed is True


async def test_owned_anthropic_provider_is_closed(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=anthropic_config(),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")),
    )
    provider = runtime.providers["anthropic"]
    client = provider.transport.client
    assert client.is_closed is False

    await runtime.aclose()

    assert client.is_closed is True
    assert runtime.closed is True


async def test_context_manager_closes_owned_providers(tmp_path):
    async with Runtime(
        tmp_path,
        config=anthropic_config(),
        http_transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"")),
    ) as runtime:
        client = runtime.providers["anthropic"].transport.client
        assert client.is_closed is False
    assert client.is_closed is True


async def test_runtime_snapshots_config_once_at_construction(tmp_path):
    calls = {"n": 0}
    config = anthropic_config()

    def loader():
        calls["n"] += 1
        return config

    runtime = Runtime(
        tmp_path,
        config_loader=loader,
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )

    # Providers and router share one construction-time snapshot.
    assert calls["n"] == 1
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Normal Anthropic construction without network
# ---------------------------------------------------------------------------


async def test_anthropic_runtime_with_mock_transport(tmp_path):
    fixture = (FIXTURES / "text_stream.sse").read_bytes()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=fixture)

    runtime = Runtime(
        tmp_path,
        config=anthropic_config(),
        http_transport=httpx.MockTransport(handler),
    )
    session = runtime.session("live")

    events = [event async for event in session.send("hello")]

    assert requests[0].headers["x-api-key"] == "test-key"
    assert any(event.type == "text" and event.data["text"] == "Hello" for event in events)
    assert events[-1].type == "turn.completed"

    await runtime.aclose()


# ---------------------------------------------------------------------------
# Native tool infrastructure
# ---------------------------------------------------------------------------


async def test_runtime_builds_native_tools_and_closes_owned_registry(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    config = scripted_config()
    runtime = Runtime(tmp_path, config=config, providers={"scripted": provider})

    assert runtime.job_registry is not None
    assert runtime.job_registry.closed is False

    session = runtime.session("t")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="turn-1", attended=False
    )
    assert turn is not None
    assert len(turn.schemas) == 15
    assert turn.gate is not None

    await runtime.aclose()
    assert runtime.job_registry.closed is True


async def test_runtime_injected_tool_manager_is_used(tmp_path):
    provider = ScriptedProvider(text_response("ok"))
    config = scripted_config()
    manager = ToolManager(config, workspace=tmp_path, profile="research")
    runtime = Runtime(
        tmp_path, config=config, providers={"scripted": provider}, tools=manager
    )
    session = runtime.session("t")
    turn = runtime._make_tool_turn(
        config=config, session=session, turn_id="turn-1", attended=False
    )
    assert turn.manager is manager
    assert [schema.name for schema in turn.schemas] == ["Read", "Glob", "Grep", "LS"]
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Gemini registration (registry catalogue id ``google``)
# ---------------------------------------------------------------------------


def gemini_config(*, section="google", model="google/gemini-3-pro"):
    return Config(
        model=model,
        version=2,
        v2=ConfigV2(
            model=ModelSection(default=model),
            providers={section: ProviderSection(api_key="test-key")},
        ),
    )


async def test_gemini_is_registered_under_google_and_gemini(tmp_path):
    from nexus.model.request import ModelRequest

    runtime = Runtime(
        tmp_path,
        config=gemini_config(),
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )
    try:
        google = runtime.providers["google"]
        assert google is runtime.providers["gemini"]
        # The adapter identity stays ``gemini`` even though the registry key is
        # the catalogue id ``google``.
        assert google.name == "gemini"
        resolved = runtime.router.resolve(
            ModelRequest(messages=[], model="google/gemini-3-pro")
        )
        assert resolved.provider is google
        assert resolved.model == "gemini-3-pro"
        resolved_alias = runtime.router.resolve(
            ModelRequest(messages=[], model="gemini/gemini-3-pro")
        )
        assert resolved_alias.provider is google
    finally:
        await runtime.aclose()


async def test_gemini_config_section_may_be_named_gemini(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=gemini_config(section="gemini", model="gemini/gemini-3-pro"),
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )
    try:
        assert runtime.providers["google"].name == "gemini"
    finally:
        await runtime.aclose()


async def test_owned_gemini_provider_closes_once(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=gemini_config(),
        http_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"")
        ),
    )
    client = runtime.providers["google"].transport.client
    assert client.is_closed is False
    await runtime.aclose()
    assert client.is_closed is True
    # Registered under two keys but owned once; a second close must be a no-op.
    await runtime.aclose()
    assert client.is_closed is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain(iterator):
    return [event async for event in iterator]
