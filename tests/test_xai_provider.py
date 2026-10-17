"""xAI (Grok) over the SuperGrok subscription.

The shared Responses parser is covered by the conformance matrix
(``xai_responses``); these pin what xAI adds:

* the preset: endpoint, ``store: false``, the conversation cache header, the
  image-type filter, no token counter;
* the SuperGrok device sign-in (``nexus/auth/xai.py``) against a mock
  ``auth.x.ai``: polling rules, stored refresh token only, refresh and rotation,
  logout, and refusing a foreign verification URL;
* subscription-only routing through config, runtime, Settings and setup.

The stream fixture is synthetic, shaped after the reasoning-summary events in
OpenCode's ``packages/ai/test/provider/xai-responses.test.ts``; the OAuth
responses follow OpenCode's ``packages/core/src/plugin/provider/xai.ts``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from nexus.auth.xai import CLIENT_ID, XAIHeaders, XAIOAuthManager
from nexus.errors import ConfigError, ProviderError
from nexus.model.capabilities import Capabilities
from nexus.model.message import Image, Message, Text
from nexus.model.providers.xai import VERSION_ENV, XAIProvider, installed_grok_version
from nexus.model.registry import ADAPTER_XAI, map_provider
from nexus.model.request import ModelRequest, SamplingParams
from nexus.model.stream import MessageStart, MessageStop, TextDelta, ThinkingDelta, ThinkingEnd, Usage

_STREAM = b"".join(
    b"data: " + json.dumps(frame).encode() + b"\n\n"
    for frame in (
        {"type": "response.created", "response": {"id": "r1", "model": "grok-4.7", "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.reasoning_summary_text.delta", "item_id": "rs_1", "summary_index": 0,
         "delta": "**Checking the question**"},
        {"type": "response.output_item.done", "output_index": 0,
         "item": {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}},
        {"type": "response.output_text.delta", "item_id": "msg_1", "delta": "Hello"},
        {"type": "response.completed", "response": {
            "id": "r1", "status": "completed",
            "usage": {"input_tokens": 12, "output_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 4},
                      "output_tokens_details": {"reasoning_tokens": 3}}}},
    )
)


class Recorder:
    def __init__(self, body: bytes = _STREAM) -> None:
        self.requests: list[httpx.Request] = []
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=self.body, headers={"content-type": "text/event-stream"})

    def json(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


class StaticHeaders:
    """The ``auth_headers`` shape ``XAIHeaders`` provides, with a fixed token."""

    async def headers(self) -> dict[str, str]:
        return {"authorization": "Bearer sgk-access-token"}


def _provider(recorder: Recorder, **kwargs: Any) -> XAIProvider:
    kwargs.setdefault("auth_headers", StaticHeaders())
    kwargs.setdefault("capabilities", Capabilities(thinking=True, tools=True, vision=True))
    kwargs.setdefault("environ", {VERSION_ENV: "1.0.13"})
    return XAIProvider(model="grok-4.7", http_transport=httpx.MockTransport(recorder), **kwargs)


def _request(*blocks: Any, **kwargs: Any) -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=list(blocks) or [Text(text="hi")])], **kwargs)


async def _drain(provider: XAIProvider, req: ModelRequest) -> list[Any]:
    try:
        return [event async for event in provider.stream(req)]
    finally:
        await provider.aclose()


# -- the preset ----------------------------------------------------------------


async def test_responses_request_targets_xai_with_the_subscription_token_and_no_storage():
    recorder = Recorder()
    events = await _drain(_provider(recorder), _request(metadata={"session_id": "sess-1"}))
    request = recorder.requests[0]
    assert str(request.url) == "https://cli-chat-proxy.grok.com/v1/responses"
    assert request.headers["authorization"] == "Bearer sgk-access-token"
    assert request.headers["x-xai-token-auth"] == "xai-grok-cli"
    assert request.headers["x-grok-conv-id"] == "sess-1"
    assert request.headers["x-grok-client-version"] == "1.0.13"
    assert request.headers["x-grok-client-identifier"] == "grok-shell"
    assert request.headers["user-agent"] == "xai-grok-cli"
    body = recorder.json()
    assert body["store"] is False and body["stream"] is True
    assert body["reasoning"] == {"summary": "auto"}
    assert "include" not in body  # encrypted reasoning is never replayed, so not requested
    assert isinstance(events[0], MessageStart) and events[0].provider == "xai" and events[0].model == "grok-4.7"


async def test_stream_normalizes_reasoning_text_usage_and_stop():
    events = await _drain(_provider(Recorder()), _request())
    kinds = [type(event) for event in events]
    assert kinds.index(ThinkingDelta) < kinds.index(ThinkingEnd) < kinds.index(TextDelta)
    thinking = next(event for event in events if isinstance(event, ThinkingDelta))
    assert thinking.text == "**Checking the question**"
    usage = next(event for event in events if isinstance(event, Usage))
    assert (usage.input, usage.output, usage.reasoning) == (12, 5, 3)
    assert isinstance(events[-1], MessageStop) and events[-1].stop_reason == "end_turn"


async def test_explicit_effort_is_sent_with_the_summary():
    recorder = Recorder()
    await _drain(_provider(recorder), _request(params=SamplingParams(reasoning_effort="high")))
    assert recorder.json()["reasoning"] == {"effort": "high", "summary": "auto"}


async def test_chat_dialect_keeps_cache_header_but_sends_no_store():
    body = b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    recorder = Recorder(body)
    await _drain(_provider(recorder, api="chat"), _request(metadata={"session_id": "sess-2"}))
    assert str(recorder.requests[0].url) == "https://cli-chat-proxy.grok.com/v1/chat/completions"
    assert recorder.requests[0].headers["x-xai-token-auth"] == "xai-grok-cli"
    assert recorder.requests[0].headers["x-grok-conv-id"] == "sess-2"
    assert "store" not in recorder.json()


async def test_unsupported_image_types_become_a_note_and_supported_ones_pass():
    recorder = Recorder()
    gif = Image(media_type="image/gif", data=b"GIF89a")
    png = Image(media_type="image/png", data=b"\x89PNG")
    req = _request(Text(text="look"), gif, png)
    await _drain(_provider(recorder), req)
    parts = recorder.json()["input"][0]["content"]
    assert [part["type"] for part in parts] == ["input_text", "input_text", "input_image"]
    assert "image/gif omitted" in parts[1]["text"]
    assert parts[2]["image_url"].startswith("data:image/png;base64,")
    assert req.messages[0].content[1] is gif  # the durable request is not rewritten


async def test_no_token_counter_and_no_network_for_counting():
    recorder = Recorder()
    provider = _provider(recorder)
    try:
        assert provider.supports_count_tokens() is False
        assert await provider.count_tokens(_request()) is None
        assert recorder.requests == []
    finally:
        await provider.aclose()


async def test_no_environment_key_is_ever_read():
    provider = XAIProvider(model="grok-4.7", environ={"XAI_API_KEY": "xai-env-key-123"},
                           http_transport=httpx.MockTransport(Recorder()))
    with pytest.raises(ProviderError, match="no API key"):
        await _drain(provider, _request())


def _version_file(tmp_path: Path, content: str | bytes) -> Path:
    home = tmp_path / ".grok"
    home.mkdir()
    (home / "version.json").write_bytes(content if isinstance(content, bytes) else content.encode())
    return home


def test_installed_version_prefers_version_then_stable_version(tmp_path):
    assert installed_grok_version(_version_file(tmp_path, '{"version": "1.0.14", "stable_version": "1.0.13"}')) == "1.0.14"
    (tmp_path / ".grok" / "version.json").write_text('{"stable_version": "1.0.13"}')
    assert installed_grok_version(tmp_path / ".grok") == "1.0.13"


@pytest.mark.parametrize("content", [
    "", "not json", "[]", '{"version": 1}', '{"version": "1.0"}', '{"version": "latest"}',
    '{"version": "1.0.13\\r\\nx-evil: 1"}', '{"version": "1.0.13 "}', '{"version": "' + "1" * 5000 + '"}',
    '{"version": "1.0.13"}' + " " * 5000,
])
def test_installed_version_rejects_corrupt_oversized_or_unsafe_files(tmp_path, content):
    assert installed_grok_version(_version_file(tmp_path, content)) is None


def test_installed_version_missing_or_not_a_file(tmp_path):
    assert installed_grok_version(tmp_path / "absent") is None
    (tmp_path / "dir" / "version.json").mkdir(parents=True)
    assert installed_grok_version(tmp_path / "dir") is None


async def test_missing_version_is_an_actionable_error_and_sends_nothing(tmp_path):
    recorder = Recorder()
    provider = _provider(recorder, environ={}, grok_home=tmp_path / "absent")
    with pytest.raises(ProviderError, match=r"grok update.*NEXUS_XAI_CLIENT_VERSION"):
        await _drain(provider, _request())
    assert recorder.requests == []


async def test_version_comes_from_the_installed_cli_and_a_bad_override_is_refused(tmp_path):
    recorder = Recorder()
    home = _version_file(tmp_path, '{"version": "1.0.20"}')
    await _drain(_provider(recorder, environ={}, grok_home=home), _request())
    assert recorder.requests[0].headers["x-grok-client-version"] == "1.0.20"
    with pytest.raises(ProviderError, match="not a plain version"):
        await _drain(_provider(Recorder(), environ={VERSION_ENV: "1.0.13\r\nx: y"}), _request())


async def test_http_426_explains_how_to_update():
    def outdated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(426, text="Your Grok CLI version (1.0.1) is outdated. Please update to 1.0.13")

    provider = XAIProvider(model="grok-4.7", environ={VERSION_ENV: "1.0.1"}, auth_headers=StaticHeaders(),
                           capabilities=Capabilities(thinking=True, tools=True, vision=True),
                           http_transport=httpx.MockTransport(outdated))
    with pytest.raises(ProviderError, match=r"rejected client version 1\.0\.1.*grok update.*1\.0\.13") as caught:
        await _drain(provider, _request())
    assert "HTTP 426" in str(caught.value)


def test_catalogue_maps_xai_to_its_adapter():
    assert map_provider("xai", "@ai-sdk/xai") == ADAPTER_XAI
    assert map_provider("xai") == ADAPTER_XAI


def test_offline_snapshot_lists_grok_tool_models():
    from nexus.model.registry import parse_catalogue

    raw = (Path(__file__).parents[1] / "nexus" / "model" / "data" / "models.min.json").read_bytes()
    providers = {provider.id: provider for provider in parse_catalogue(raw).providers}
    assert {"grok-4.7", "grok-4.6", "grok-build-0.1"} <= {model.id for model in providers["xai"].models}


# -- SuperGrok sign-in -----------------------------------------------------------


class MemorySecrets:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def read_secret(self, account):
        return self.values.get(account)

    async def write_secret(self, account, value):
        self.values[account] = value

    async def delete_secret(self, account):
        self.values.pop(account, None)


def _jwt(exp: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{claims}.sig"


class FakeIssuer:
    """``auth.x.ai/oauth2`` answering from a queue of ``/token`` replies."""

    def __init__(self, token_replies: list[tuple[int, dict]], device: dict | None = None) -> None:
        self.device = device or {
            "device_code": "dev-123", "user_code": "ABCD-EFGH", "verification_uri": "https://accounts.x.ai/device",
            "verification_uri_complete": "https://accounts.x.ai/device?code=ABCD-EFGH", "expires_in": 600, "interval": 5,
        }
        self.token_replies = list(token_replies)
        self.forms: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.forms.append((request.url.path, form))
        if request.url.path == "/oauth2/device/code":
            return httpx.Response(200, json=self.device)
        status, payload = self.token_replies.pop(0)
        return httpx.Response(status, json=payload)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _manager(issuer: FakeIssuer, secrets: MemorySecrets, clock: Clock) -> tuple[XAIOAuthManager, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(issuer))
    return XAIOAuthManager(store=secrets, client=client, now=clock, sleep=clock.sleep), client


async def test_device_sign_in_polls_politely_and_stores_only_the_refresh_token():
    clock, secrets = Clock(), MemorySecrets()
    issuer = FakeIssuer([
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": "sgk-access-1", "refresh_token": "sgk-refresh-1", "expires_in": 3600}),
    ])
    manager, client = _manager(issuer, secrets, clock)
    shown: list[tuple[str, str]] = []
    try:
        await manager.device_login(on_code=lambda url, code: shown.append((url, code)))
        assert shown == [("https://accounts.x.ai/device?code=ABCD-EFGH", "ABCD-EFGH")]
        path, form = issuer.forms[0]
        assert path == "/oauth2/device/code" and form["client_id"] == CLIENT_ID
        assert "grok-cli:access" in form["scope"] and "offline_access" in form["scope"]
        assert [form["grant_type"] for _, form in issuer.forms[1:]] == [
            "urn:ietf:params:oauth:grant-type:device_code"] * 3
        assert clock.sleeps == [8.0, 8.0, 13.0]  # interval + 3 s margin; slow_down adds 5 s
        assert json.loads(secrets.values["xai:default"]) == {"v": 1, "refresh_token": "sgk-refresh-1"}
        assert "sgk-access-1" not in secrets.values["xai:default"]
        assert await manager.status()
        assert (await manager.headers())["authorization"] == "Bearer sgk-access-1"
        assert len(issuer.forms) == 4  # the fresh access token is reused, not refreshed
    finally:
        await client.aclose()


@pytest.mark.parametrize("error,message", [
    ("access_denied", "denied"), ("expired_token", "expired"), ("server_error", "failed"),
])
async def test_device_sign_in_failures_are_named_and_store_nothing(error, message):
    clock, secrets = Clock(), MemorySecrets()
    manager, client = _manager(FakeIssuer([(400, {"error": error})]), secrets, clock)
    try:
        with pytest.raises(ProviderError, match=message):
            await manager.device_login()
        assert secrets.values == {}
    finally:
        await client.aclose()


async def test_device_sign_in_refuses_a_verification_url_off_x_ai():
    clock, secrets = Clock(), MemorySecrets()
    issuer = FakeIssuer([], device={"device_code": "d", "user_code": "C", "verification_uri": "https://evil.example/x"})
    manager, client = _manager(issuer, secrets, clock)
    try:
        with pytest.raises(ProviderError, match="invalid response"):
            await manager.device_login(on_code=lambda *_: pytest.fail("a foreign URL must never be shown"))
    finally:
        await client.aclose()


async def test_device_sign_in_stops_when_cancelled():
    clock, secrets = Clock(), MemorySecrets()
    manager, client = _manager(FakeIssuer([]), secrets, clock)
    cancel = asyncio.Event()
    cancel.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.device_login(cancel=cancel)
        assert secrets.values == {}
    finally:
        await client.aclose()


async def test_headers_refresh_near_expiry_and_keep_a_rotated_refresh_token():
    clock, secrets = Clock(), MemorySecrets()
    await secrets.write_secret("xai:default", json.dumps({"v": 1, "refresh_token": "sgk-refresh-1"}))
    issuer = FakeIssuer([
        # No expires_in: the JWT ``exp`` claim is used.
        (200, {"access_token": _jwt(clock.now + 3600), "refresh_token": "sgk-refresh-2"}),
        (200, {"access_token": "sgk-access-3", "expires_in": 3600}),
    ])
    manager, client = _manager(issuer, secrets, clock)
    try:
        first = await manager.headers()
        assert first["authorization"].startswith("Bearer eyJ")
        assert issuer.forms[0][1] == {"grant_type": "refresh_token", "refresh_token": "sgk-refresh-1", "client_id": CLIENT_ID}
        assert json.loads(secrets.values["xai:default"])["refresh_token"] == "sgk-refresh-2"
        assert await manager.headers() == first  # cached while comfortably valid
        clock.now += 3600 - 200  # inside the five-minute margin
        assert (await manager.headers())["authorization"] == "Bearer sgk-access-3"
        assert issuer.forms[-1][1]["refresh_token"] == "sgk-refresh-2"
        assert json.loads(secrets.values["xai:default"])["refresh_token"] == "sgk-refresh-2"  # not rotated: kept
    finally:
        await client.aclose()


async def test_rejected_refresh_asks_to_sign_in_again_and_logout_removes_the_token():
    clock, secrets = Clock(), MemorySecrets()
    await secrets.write_secret("xai:default", json.dumps({"v": 1, "refresh_token": "sgk-refresh-1"}))
    manager, client = _manager(FakeIssuer([(401, {"error": "invalid_grant"})]), secrets, clock)
    try:
        with pytest.raises(ProviderError, match="sign in again") as raised:
            await manager.headers()
        assert "sgk-refresh-1" not in str(raised.value)
        await manager.logout()
        assert not await manager.status()
        with pytest.raises(ProviderError, match="Settings"):
            await manager.headers()
    finally:
        await client.aclose()


# -- routing: subscription only ------------------------------------------------------


def _runtime(tmp_path: Path, body: str, secrets: MemorySecrets | None = None):
    from nexus.runtime import Runtime

    (tmp_path / "nexus.toml").write_text("config_version = 2\n\n" + body, encoding="utf-8")
    store = secrets or MemorySecrets()
    return Runtime(
        tmp_path, home=tmp_path / "home", environ={"XAI_API_KEY": "xai-env-key-123"},
        xai_auth_factory=lambda **kw: XAIOAuthManager(store=store, **kw),
    )


async def test_runtime_builds_xai_on_the_subscription_sign_in(tmp_path):
    secrets = MemorySecrets()
    runtime = _runtime(tmp_path, '[models]\ndefault = "xai/grok-4.7"\n\n'
                                 '[providers.xai]\nauth = "xai_oauth"\napi = "responses"\n', secrets)
    try:
        provider = runtime.providers["xai"]
        assert isinstance(provider, XAIProvider) and isinstance(provider._auth_headers, XAIHeaders)
        assert provider._base_url == "https://cli-chat-proxy.grok.com/v1" and provider.api == "responses"
        with pytest.raises(ProviderError, match="SuperGrok sign-in required"):
            await provider._headers()  # the env key is never used
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("section", [
    '[providers.xai]\n',
    '[providers.xai]\napi_key = "${env:XAI_API_KEY}"\n',
    '[providers.grok-work]\nkind = "xai"\napi_key = "${env:XAI_API_KEY}"\n',
])
def test_runtime_refuses_api_key_routes_for_xai(tmp_path, section):
    with pytest.raises(ConfigError, match="SuperGrok"):
        _runtime(tmp_path, '[models]\ndefault = "xai/grok-4.7"\n\n' + section)


@pytest.mark.parametrize("section,message", [
    ('[providers.xai]\nauth = "xai_oauth"\napi_key = "sk-inline"\n', "xai_oauth"),
    ('[providers.xai]\nauth = "xai_oauth"\nbase_url = "https://attacker.example/v1"\n', "xai_oauth"),
    ('[providers.xai]\nauth = "xai_oauth"\nbase_url = "https://api.x.ai/v1"\n', "xai_oauth"),
    ('[providers.other]\nauth = "xai_oauth"\n', "only by providers.xai"),
    ('[providers.xai]\nauth = "keychain"\nbase_url = "https://api.x.ai/v1"\nkind = "xai"\n', "OpenAI-compatible"),
])
def test_config_keeps_the_subscription_token_on_xai(tmp_path, section, message):
    from nexus.config import Config

    (tmp_path / "nexus.toml").write_text("config_version = 2\n\n" + section, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        Config.load(tmp_path, home=tmp_path / "home", environ={})


# -- Settings → Providers and first-run setup ------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


class FakeXAI:
    """The ``XAIOAuthManager`` shape; the test approves the device code."""

    def __init__(self) -> None:
        self.approved = asyncio.Event()
        self.signed_in = False

    async def status(self):
        return self.signed_in

    async def logout(self):
        self.signed_in = False

    async def device_login(self, *, on_code, cancel=None):
        on_code("https://accounts.x.ai/device?code=ABCD-EFGH", "ABCD-EFGH")
        await self.approved.wait()
        self.signed_in = True


def _host_runtime(home: Path, xai: FakeXAI, environ=None):
    return SimpleNamespace(workspace=home / "workspace", _home=home, _environ=environ or {},
                           _xai_auth_factory=lambda **_: xai)


async def test_supergrok_device_sign_in_saves_the_route(home):
    from nexus.host import protocol as p
    from nexus.host_support import provider_auth

    xai = FakeXAI()
    runtime = _host_runtime(home, xai)
    status = await provider_auth.dispatch_providers(p.ProvidersStatus(), runtime)
    row = next(row for row in status.providers if row["id"] == "xai")
    assert row["label"] == "xAI (SuperGrok)" and row["methods"] == ["device"] and not row["connected"]
    with pytest.raises(ConfigError, match="API key"):
        await provider_auth.provider_key_set(runtime, "xai", "xai-live-0123456789abcdef")
    started = await provider_auth.provider_login(runtime, "xai")
    assert started["status"] == "pending" and started["user_code"] == "ABCD-EFGH"
    assert started["url"].startswith("https://accounts.x.ai/device")
    xai.approved.set()
    await runtime._provider_logins[started["login_id"]].task
    done = await provider_auth.provider_login_poll(runtime, started["login_id"])
    assert done["status"] == "connected"
    config = tomllib.loads((home / ".nexus" / "config.toml").read_text(encoding="utf-8"))
    assert config["providers"]["xai"] == {"auth": "xai_oauth", "api": "responses"}
    removed = await provider_auth.dispatch_providers(p.ProviderLogout(provider="xai"), runtime)
    assert not removed.connected and not xai.signed_in


async def test_setup_offers_xai_only_after_the_subscription_sign_in(home):
    from nexus.host_support import setup

    xai = FakeXAI()
    runtime = _host_runtime(home, xai, environ={"XAI_API_KEY": "xai-env-key-123"})
    assert not await setup._connected(runtime, "xai")  # an env key does not count
    xai.signed_in = True
    assert await setup._connected(runtime, "xai")
    assert setup._provider_config("xai", runtime) == (("auth", "xai_oauth"), ("api", "responses"))
