"""NVIDIA (hosted catalogue) over Chat Completions.

The shared chat parser is covered by the conformance matrix (``nvidia_chat``);
these pin what the preset adds: endpoint, key sources, the attribution header,
no token counter, ``reasoning_content`` streaming, and routing through config,
runtime, Settings and setup. The stream is a synthetic OpenAI-compatible chunk
sequence; nothing here touches the real API.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from nexus.auth.api_key import StoredKeyAuth
from nexus.errors import ConfigError, ProviderError
from nexus.model.capabilities import Capabilities
from nexus.model.message import Message, Text
from nexus.model.providers.nvidia import DEFAULT_BASE_URL, NvidiaProvider
from nexus.model.registry import ADAPTER_NVIDIA, map_provider
from nexus.model.request import ModelRequest
from nexus.model.stream import MessageStart, MessageStop, TextDelta, ThinkingDelta

_STREAM = b"".join(
    b"data: " + json.dumps(frame).encode() + b"\n\n"
    for frame in (
        {"choices": [{"delta": {"role": "assistant", "reasoning_content": "Checking"}}]},
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
) + b"data: [DONE]\n\n"


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=_STREAM, headers={"content-type": "text/event-stream"})

    def json(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def _provider(recorder: Recorder, **kwargs: Any) -> NvidiaProvider:
    kwargs.setdefault("capabilities", Capabilities(thinking=True, tools=True))
    return NvidiaProvider(model="nvidia/test-model", http_transport=httpx.MockTransport(recorder), **kwargs)


def _request(**kwargs: Any) -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=[Text(text="hi")])], **kwargs)


async def _drain(provider: NvidiaProvider, req: ModelRequest | None = None) -> list[Any]:
    try:
        return [event async for event in provider.stream(req or _request())]
    finally:
        await provider.aclose()


# -- the preset ------------------------------------------------------------------


async def test_chat_request_targets_nvidia_with_the_env_key_and_attribution():
    recorder = Recorder()
    events = await _drain(_provider(recorder, environ={"NVIDIA_API_KEY": "nvapi-env-0123"}))
    request = recorder.requests[0]
    assert str(request.url) == f"{DEFAULT_BASE_URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer nvapi-env-0123"
    assert request.headers["x-billing-invoke-origin"] == "Nexus"
    assert "http-referer" not in request.headers and "x-title" not in request.headers
    body = recorder.json()
    assert body["model"] == "nvidia/test-model" and body["stream"] is True
    assert isinstance(events[0], MessageStart) and events[0].provider == "nvidia"


async def test_stream_maps_reasoning_content_then_text_and_stop():
    events = await _drain(_provider(Recorder(), api_key="nvapi-literal-0123"))
    kinds = [type(event) for event in events]
    assert kinds.index(ThinkingDelta) < kinds.index(TextDelta)
    assert next(e for e in events if isinstance(e, ThinkingDelta)).text == "Checking"
    assert next(e for e in events if isinstance(e, TextDelta)).text == "Hello"
    assert isinstance(events[-1], MessageStop) and events[-1].stop_reason == "end_turn"


async def test_a_user_header_overrides_the_attribution_header():
    recorder = Recorder()
    await _drain(_provider(recorder, api_key="nvapi-literal-0123", extra_headers={"x-billing-invoke-origin": "Mine"}))
    assert recorder.requests[0].headers["x-billing-invoke-origin"] == "Mine"


async def test_missing_key_is_a_provider_error_that_names_the_variable():
    provider = _provider(Recorder(), environ={})
    with pytest.raises(ProviderError, match="NVIDIA_API_KEY"):
        await _drain(provider)


async def test_only_the_chat_dialect_is_accepted():
    with pytest.raises(ProviderError, match="only api = 'chat'"):
        NvidiaProvider(api="responses")
    assert NvidiaProvider(api="chat").api == "chat"


async def test_repr_hides_the_key_and_count_tokens_makes_no_request():
    recorder = Recorder()
    provider = _provider(recorder, api_key="nvapi-secret-0123456789")
    try:
        assert "nvapi-secret" not in repr(provider) and repr(provider).startswith("NvidiaProvider(")
        assert provider.supports_count_tokens() is False
        assert await provider.count_tokens(_request()) is None
        assert recorder.requests == []
    finally:
        await provider.aclose()


# -- catalogue -------------------------------------------------------------------


def test_catalogue_maps_nvidia_to_its_preset_despite_the_openai_compatible_npm():
    assert map_provider("nvidia", "@ai-sdk/openai-compatible") == ADAPTER_NVIDIA
    assert map_provider("nvidia") == ADAPTER_NVIDIA


def test_offline_snapshot_lists_nvidia_with_its_key_variable():
    from nexus.model.registry import parse_catalogue

    raw = (Path(__file__).parents[1] / "nexus" / "model" / "data" / "models.min.json").read_bytes()
    providers = {provider.id: provider for provider in parse_catalogue(raw).providers}
    assert providers["nvidia"].env == ("NVIDIA_API_KEY",)


# -- routing ---------------------------------------------------------------------


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def read_secret(self, account):
        return self.values.get(account)

    async def write_secret(self, account, value):
        self.values[account] = value

    async def delete_secret(self, account):
        self.values.pop(account, None)


def _runtime(tmp_path: Path, body: str, secrets: MemorySecrets | None = None, environ: dict | None = None):
    from nexus.runtime import Runtime

    (tmp_path / "nexus.toml").write_text("config_version = 2\n\n" + body, encoding="utf-8")
    store = secrets or MemorySecrets()
    return Runtime(
        tmp_path, home=tmp_path / "home", environ=environ or {},
        api_key_auth_factory=lambda provider, **kw: StoredKeyAuth(provider, store=store, **kw),
    )


async def test_runtime_builds_nvidia_from_the_environment_key(tmp_path):
    runtime = _runtime(tmp_path, '[models]\ndefault = "nvidia/some-model"\n',
                       environ={"NVIDIA_API_KEY": "nvapi-env-0123"})
    try:
        provider = runtime.providers["nvidia"]
        assert isinstance(provider, NvidiaProvider) and provider.name == "nvidia"
        assert provider._base_url == DEFAULT_BASE_URL and provider.api == "chat"
        assert (await provider._headers())["authorization"] == "Bearer nvapi-env-0123"
    finally:
        await runtime.aclose()


async def test_runtime_builds_nvidia_on_a_pasted_key(tmp_path):
    secrets = MemorySecrets()
    secrets.values["nvidia:default"] = "nvapi-pasted-0123"
    runtime = _runtime(tmp_path, '[models]\ndefault = "nvidia/some-model"\n\n'
                                 f'[providers.nvidia]\nauth = "keychain"\nbase_url = "{DEFAULT_BASE_URL}"\napi = "chat"\n',
                       secrets, environ={"NVIDIA_API_KEY": "nvapi-env-ignored"})
    try:
        provider = runtime.providers["nvidia"]
        assert isinstance(provider, NvidiaProvider)
        assert (await provider._headers())["authorization"] == "Bearer nvapi-pasted-0123"
    finally:
        await runtime.aclose()


def test_runtime_refuses_other_sign_ins_and_the_responses_dialect(tmp_path):
    with pytest.raises(Exception, match="providers.xai|xai_oauth|only by"):
        _runtime(tmp_path, '[models]\ndefault = "nvidia/m"\n\n[providers.nvidia]\nauth = "xai_oauth"\n')
    with pytest.raises(ConfigError, match="Chat Completions"):
        _runtime(tmp_path, '[models]\ndefault = "nvidia/m"\n\n[providers.nvidia]\napi = "responses"\n')


# -- Settings and setup ----------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _host_runtime(home: Path, secrets: MemorySecrets):
    return SimpleNamespace(
        workspace=home / "workspace", _home=home, _environ={},
        _api_key_auth_factory=lambda provider, **kw: StoredKeyAuth(provider, store=secrets, **kw),
    )


async def test_pasted_key_writes_the_route_and_never_echoes_the_key(home):
    from nexus.host import protocol as p
    from nexus.host_support import provider_auth, setup

    secrets = MemorySecrets()
    runtime = _host_runtime(home, secrets)
    status = await provider_auth.dispatch_providers(p.ProvidersStatus(), runtime)
    row = next(row for row in status.providers if row["id"] == "nvidia")
    assert row["label"] == "NVIDIA" and row["methods"] == ["api_key"] and not row["connected"]
    with pytest.raises(ConfigError, match="api_key sign-in|does not support"):
        await provider_auth.provider_login(runtime, "nvidia")
    key = "nvapi-live-0123456789abcdef"
    saved = await provider_auth.dispatch_providers(p.ProviderKeySet(provider="nvidia", key=key), runtime)
    assert saved.connected and key not in repr(saved)
    assert secrets.values["nvidia:default"] == key
    route = tomllib.loads((home / ".nexus" / "config.toml").read_text(encoding="utf-8"))["providers"]["nvidia"]
    assert route == {"auth": "keychain", "base_url": DEFAULT_BASE_URL, "api": "chat"}
    assert key not in (home / ".nexus" / "config.toml").read_text(encoding="utf-8")
    assert setup._provider_config("nvidia", runtime) == (
        ("auth", "keychain"), ("base_url", DEFAULT_BASE_URL), ("api", "chat"))
    removed = await provider_auth.dispatch_providers(p.ProviderLogout(provider="nvidia"), runtime)
    assert not removed.connected and "nvidia:default" not in secrets.values


async def test_setup_offers_nvidia_only_when_a_key_is_stored(home):
    from nexus.host_support import setup

    secrets = MemorySecrets()
    runtime = _host_runtime(home, secrets)
    assert not await setup._connected(runtime, "nvidia")
    secrets.values["nvidia:default"] = "nvapi-live-0123456789abcdef"
    assert await setup._connected(runtime, "nvidia")


@pytest.mark.parametrize("model", ["nvidia/llama-3.1-nemotron-ultra-253b-v1"])
async def test_catalogue_slash_id_routes_to_nvidia_with_exact_wire_model(tmp_path, model):
    import json
    from nexus.model.registry import ModelRegistry
    from nexus.model.router import ModelRouter

    raw = (Path(__file__).parent / "fixtures/nvidia-models-dev.json").read_text()
    registry = ModelRegistry(providers={"nvidia": {}, "openai": {}}, env={},
                             offline=True, use_snapshot=False, cache_path=tmp_path / "absent")
    registry.install_raw(raw)
    reference = f"nvidia/{model}"
    info = registry.get(reference)
    assert info is not None and info.provider == "nvidia" and info.id == model
    assert info.context == json.loads(raw)["nvidia"]["models"][model]["limit"]["context"]
    captured = []

    async def handler(request):
        assert str(request.url) == DEFAULT_BASE_URL + "/chat/completions"
        captured.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                              headers={"content-type": "text/event-stream"})

    provider = NvidiaProvider(api_key="test-only", http_transport=httpx.MockTransport(handler))
    router = ModelRouter({"nvidia": provider}, registry=registry)
    resolved = router.resolve(ModelRequest(model=reference, messages=()))
    assert resolved.provider is provider and resolved.model == model
    from nexus.agents.runner import qualify_model
    assert qualify_model(resolved.provider.name, resolved.model) == reference
    assert router._resolve_legacy(reference).model == model
    try:
        [event async for event in resolved.provider.stream(ModelRequest(model=resolved.model, messages=()))]
        assert captured[0]["model"] == model
    finally:
        await provider.aclose()


def test_nvidia_snapshot_records_match_preserved_models_dev_source():
    source = json.loads((Path(__file__).parent / "fixtures/nvidia-models-dev.json").read_text())
    snapshot = json.loads((Path(__file__).parents[1] / "nexus/model/data/models.min.json").read_text())
    assert snapshot["nvidia"]["models"] == source["nvidia"]["models"]


def test_nvidia_hosted_id_does_not_merge_with_direct_vendor_listing():
    from nexus.model.registry import ModelRegistry

    raw = json.loads((Path(__file__).parent / "fixtures/nvidia-models-dev.json").read_text())
    # A direct OpenAI listing must never become an NVIDIA setup choice.
    raw["openai"] = {"id": "openai", "name": "OpenAI", "models": {
        "gpt-oss-20b": {"id": "gpt-oss-20b", "name": "Direct test listing", "tool_call": True,
                          "modalities": {"input": ["text"], "output": ["text"]}}}}
    registry = ModelRegistry(providers={"nvidia": {}, "openai": {}}, env={}, offline=True, use_snapshot=False)
    registry.install_raw(json.dumps(raw))
    assert registry.get("nvidia/openai/gpt-oss-20b") is None
    assert registry.get("openai/gpt-oss-20b").provider == "openai"


async def test_nvidia_env_setup_default(home):
    from nexus.host_support import provider_auth, setup

    runtime = _host_runtime(home, MemorySecrets())
    runtime._environ = {"NVIDIA_API_KEY": "private-env-key"}
    config = home / ".nexus" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('config_version = 2\n[providers.nvidia]\nauth = "keychain"\n')
    assert await provider_auth.connected(runtime, "nvidia")
    status = await setup.setup_status(runtime)
    row = next(p for p in status["providers"] if p["id"] == "nvidia")
    assert row["connected"] is True
    expected = "nvidia/nvidia/llama-3.1-nemotron-ultra-253b-v1"
    result = await setup.setup_save(runtime, "nvidia")
    assert result["global_model"] == expected
    text = (home / ".nexus" / "config.toml").read_text()
    assert "private-env-key" not in text + str(status) + str(result)
    saved = tomllib.loads(text)
    assert saved["providers"]["nvidia"]["api_key"] == "${env:NVIDIA_API_KEY}"
    assert "auth" not in saved["providers"]["nvidia"]


def test_nvidia_namespaced_wire_id_is_not_a_generic_provider_reference():
    from nexus.model.registry import ModelRegistry

    raw = json.loads((Path(__file__).parent / "fixtures/nvidia-models-dev.json").read_text())
    raw["nvidia"]["models"]["openai/gpt-oss-20b"] = {
        "id": "openai/gpt-oss-20b", "tool_call": True,
        "modalities": {"output": ["text"]}}
    registry = ModelRegistry(providers={"nvidia": {}, "openai": {}}, env={}, offline=True, use_snapshot=False)
    registry.install_raw(json.dumps(raw))
    assert registry.get("openai/gpt-oss-20b") is None
    assert registry.get("nvidia/openai/gpt-oss-20b").provider == "nvidia"
