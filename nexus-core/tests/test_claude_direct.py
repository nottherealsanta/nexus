import json
import time

import httpx
import pytest

from nexus.llmhub.anthropic import COMPATIBILITY_INSTRUCTION, AnthropicProvider, DirectRequest, MessagesState, build_body
from nexus.llmhub.auth.claude_direct import ClaudeDirectAuth
from nexus.llmhub.auth.errors import AuthError
from nexus.llmhub.catalog import models_for
from nexus.llmhub.providers import make
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
    WireRequest,
)


@pytest.fixture
def profile(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test",
                                                "expiresAt": (time.time() + 3600) * 1000}}))
    return path


@pytest.mark.asyncio
async def test_read_only_profile_and_env_precedence(profile, monkeypatch):
    original = profile.read_bytes()
    auth = ClaudeDirectAuth()
    assert not auth.logged_in()
    await auth.login("cli-profile")
    assert auth.logged_in()
    assert (await auth.headers())["authorization"] == "Bearer sk-ant-oat-test"
    assert profile.read_bytes() == original
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-env")
    assert (await auth.headers())["authorization"] == "Bearer sk-ant-oat-env"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "bad\nsecret")
    assert not auth.logged_in()
    with pytest.raises(AuthError):
        await auth.headers()


@pytest.mark.asyncio
async def test_expired_or_wrong_profile_does_not_fall_back(profile, monkeypatch):
    profile.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-test", "expiresAt": 1}}))
    assert not ClaudeDirectAuth().logged_in()
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", str(profile.parent / "missing"))
    with pytest.raises(AuthError):
        await ClaudeDirectAuth().headers()


def test_native_history_images_tools_and_signed_reasoning():
    image = "data:image/png;base64,AA=="
    thinking = {"type": "thinking", "thinking": "thought", "signature": "signed"}
    req = Request("haiku", "exact system", (
        Message("user", (Text("look"), Image(image))),
        Message("assistant", (Reasoning("thought", "claude-direct", thinking), ToolCall("toolu_a", "read", '{"path":"a"}'))),
        Message("tool", (ToolResult("toolu_a", "data", True, (Media("a", "image/png", data_url=image),)),)),
        Message("user", (Text("continue"),)),
    ), (ToolSpec("read", "Read", {"type": "object"}),))
    body = build_body(req, origin="claude-direct")
    assert body["system"][0]["text"] == COMPATIBILITY_INSTRUCTION
    assert body["system"][1]["text"] == "exact system"
    assert body["tools"][0]["name"] == "read"
    assert body["messages"][1]["content"][0] == thinking
    assert body["messages"][1]["content"][1]["input"] == {"path": "a"}
    result = body["messages"][2]["content"][0]
    assert result["is_error"] and result["content"][1]["type"] == "image"
    assert body["messages"][2]["content"][1]["text"] == "continue"
    assert "thought" not in json.dumps(build_body(req, origin="other"))


def chunks(stop="tool_use"):
    return [
        {"type": "message_start", "message": {"id": "msg_a", "usage": {"input_tokens": 10, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 5}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_a", "name": "read", "input": {}}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"a"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ]


def test_streaming_native_parts_usage_and_stop():
    state = MessagesState("claude-direct")
    events = [event for data in chunks() for event in state.consume(data)]
    assert TextDelta("Hello") in events
    parts = [e.part for e in events if isinstance(e, PartDone)]
    assert parts[1].name == "read" and json.loads(parts[1].arguments) == {"path": "a"}
    assert events[-2:] == [Usage(35, 20, 4), Done("tool_calls", "msg_a")]


@pytest.mark.asyncio
async def test_http_redaction_and_truncation(profile):
    await ClaudeDirectAuth().login("cli-profile")
    payload = "".join(f"event: {c['type']}\ndata: {json.dumps(c)}\n\n" for c in chunks())
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, text=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(ClaudeDirectAuth(), client=client)
        events = [e async for e in provider.stream(Request("haiku", "sys", (Message("user", (Text("go"),)),)))]
        assert isinstance(events[0], WireRequest)
        assert "sk-ant-oat" not in json.dumps(events[0].headers)
        assert seen[0].headers["authorization"] == "Bearer sk-ant-oat-test"
        assert seen[0].headers["user-agent"] == "nexus/0.10"
        assert events[-1] == Done("tool_calls", "msg_a")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload.rsplit("event: message_stop", 1)[0]))) as client:
        with pytest.raises(ProviderError, match="before message_stop"):
            _ = [e async for e in AnthropicProvider(ClaudeDirectAuth(), client=client).stream(Request("haiku", "", ()))]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_http_errors_never_echo_token(profile, status):
    await ClaudeDirectAuth().login("cli-profile")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text="sk-ant-oat-test", headers={"retry-after": "5"}))) as client:
        with pytest.raises(ProviderError) as caught:
            _ = [e async for e in AnthropicProvider(ClaudeDirectAuth(), client=client).stream(Request("haiku", "", ()))]
    assert "sk-ant-oat" not in str(caught.value)
    assert caught.value.body is None and caught.value.retry_after == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"error": {"type": "rate_limit_error", "message": "sk-ant-oat-test private prompt"}},
    {"error": {"type": "sk-ant-oat-test", "message": "private prompt"}},
    {"error": {"type": ["rate_limit_error"]}},
    {"error": "sk-ant-oat-test"},
    ["private prompt"],
])
async def test_rate_limit_diagnostic_is_safe_and_does_not_claim_unsupported(profile, payload):
    await ClaudeDirectAuth().login("cli-profile")
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, json=payload, headers={"retry-after": "60"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as caught:
            _ = [e async for e in AnthropicProvider(ClaudeDirectAuth(), client=client).stream(Request("haiku", "", ()))]
    error = caught.value
    assert "rate limit reached" in str(error)
    assert "unsupported" not in str(error)
    assert "sk-ant-oat" not in str(error) and "private prompt" not in str(error)
    assert (error.status, error.phase, error.retry_after, error.body) == (429, "response", 60, None)
    assert len(calls) == 1
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict) and payload["error"].get("type") == "rate_limit_error":
        assert "rate_limit_error" in str(error)


@pytest.mark.asyncio
async def test_oversized_http_error_body_is_not_retained(profile):
    await ClaudeDirectAuth().login("cli-profile")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="x" * 70000))) as client:
        with pytest.raises(ProviderError, match="rate limit reached") as caught:
            _ = [e async for e in AnthropicProvider(ClaudeDirectAuth(), client=client).stream(Request("haiku", "", ()))]
    assert caught.value.body is None


def test_routing_catalog_and_effort():
    assert isinstance(make("claude-direct", ClaudeDirectAuth()), AnthropicProvider)
    catalog = {"anthropic": {"models": {"claude-haiku-4-5": {"tool_call": True, "reasoning": True,
                "reasoning_options": [{"type": "effort", "values": ["high"]}]}}}}
    [model] = models_for(catalog, "claude-direct")
    assert model.ref == "claude-direct/claude-haiku-4-5" and model.name == "claude-haiku-4-5"
    assert model.reasoning and model.efforts == ("minimal", "low", "medium", "high", "xhigh", "max")
    with pytest.raises(ProviderError, match="unsupported effort"):
        build_body(Request("haiku", "", (), reasoning_effort="high"), origin="claude-direct")


@pytest.mark.parametrize("model,effort,thinking,output", [
    ("claude-haiku-4-5", "low", {"type": "enabled", "budget_tokens": 2048}, None),
    ("claude-haiku-4-5-20251001", "off", {"type": "disabled"}, None),
    ("claude-sonnet-4-6", "high", {"type": "adaptive"}, {"effort": "high"}),
    ("claude-opus-4-6", "max", {"type": "adaptive"}, {"effort": "max"}),
    ("claude-opus-4-7", "xhigh", {"type": "adaptive", "display": "summarized"}, {"effort": "xhigh"}),
    ("claude-opus-4-5", "medium", {"type": "enabled", "budget_tokens": 4192}, {"effort": "medium"}),
    ("claude-sonnet-5-5", "off", {"type": "between_tools"}, None),
    ("claude-haiku-5-5", "off", {"type": "disabled"}, None),
])
def test_model_thinking_modes(model, effort, thinking, output):
    body = build_body(Request(model, "", (), reasoning_effort=effort), origin="claude-direct", max_tokens=8192)
    assert body.get("thinking") == thinking
    assert body.get("output_config") == output
    if effort != "off":
        assert body["context_management"]["edits"][0]["keep"] == "all"


def test_output_limit_and_budget_clamp(tmp_path, monkeypatch):
    from nexus.llmhub import catalog

    path = tmp_path / "models.json"
    path.write_text(json.dumps({"anthropic": {"models": {"claude-haiku-4-5": {"limit": {"output": 10000}}}}}))
    monkeypatch.setattr(catalog, "cache_path", lambda: path)
    req = Request("claude-haiku-4-5", "", (), reasoning_effort="high")
    body = build_body(req, origin="claude-direct")
    assert body["max_tokens"] == 10000 and body["thinking"]["budget_tokens"] == 6000
    assert build_body(req, origin="claude-direct", max_tokens=12000)["max_tokens"] == 10000
    with pytest.raises(ProviderError, match="at least 5024"):
        build_body(req, origin="claude-direct", max_tokens=2048)
    with pytest.raises(ProviderError, match="positive"):
        build_body(req, origin="claude-direct", max_tokens=0)


def test_caching_four_breakpoints_and_no_mutation():
    thinking = {"type": "thinking", "thinking": "x", "signature": "s"}
    history = tuple(m for i in range(31) for m in (Message("user", (Text(f"turn {i}"),)),
                    Message("assistant", (Reasoning("x", "claude-direct", thinking), Text("reply")))))
    req = Request("claude-haiku-4-5", "frozen", history, (ToolSpec("probe", "probe", {"type": "object"}),))
    body = build_body(req, origin="claude-direct")
    decorated = [b for group in [body["system"], body["tools"], *[m["content"] for m in body["messages"]]]
                 for b in group if "cache_control" in b]
    assert len(decorated) == 4
    assert all(b.get("type") != "thinking" for b in decorated)
    assert "cache_control" not in thinking
    assert "cache_control" not in json.dumps(build_body(req, origin="claude-direct", cache=False))
    assert "cache_control" in body["messages"][58]["content"][0]  # 30th user turn


def test_thinking_stream_provenance_usage_replacement_and_context_limit():
    state = MessagesState("claude-direct", "claude-haiku-4-5")
    events = []
    for data in [
        {"type": "message_start", "message": {"id": "a", "usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "thought"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 4}},
        {"type": "message_delta", "delta": {"stop_reason": "model_context_window_exceeded"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]:
        events.extend(state.consume(data))
    part = next(e.part for e in events if isinstance(e, PartDone))
    assert part.raw["signature"] == "signed" and part.raw["_model"] == "claude-haiku-4-5"
    assert events[-2] == Usage(10, 0, 7) and events[-1].stop == "max_tokens"
    req = Request("claude-haiku-4-5", "", (Message("assistant", (part, Text("answer"))),))
    assert "_model" not in json.dumps(build_body(req, origin="claude-direct"))
    switched = Request("claude-sonnet-4-6", "", req.messages)
    assert "signed" not in json.dumps(build_body(switched, origin="claude-direct"))


@pytest.mark.asyncio
async def test_direct_usage_matches_inference_credentials(profile, monkeypatch):
    from nexus import provider_usage

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-env")
    def handler(req):
        assert req.headers["authorization"] == "Bearer sk-ant-oat-env"
        assert req.url.path == "/api/oauth/usage"
        return httpx.Response(200, json={"five_hour": {"utilization": 42}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider_usage._claude_direct(client)
    assert result["windows"][0]["used_percent"] == 42
    assert result["plan"] == ""  # do not label an env token with a different profile's plan


def test_object_tool_schema_validation():
    req = Request("haiku", "", (), (ToolSpec("bad", "bad", {"type": "array"}),))
    with pytest.raises(ProviderError, match="object root"):
        build_body(req, origin="claude-direct")


@pytest.mark.asyncio
async def test_owned_oauth_refresh_is_locked_and_does_not_touch_cli(profile):
    import asyncio

    from nexus.llmhub.auth.store import CredentialStore

    store = CredentialStore(profile.parent / "auth.json")
    store.write("claude-direct", {"access_token": "sk-ant-oat-old", "refresh_token": "rotate-me", "expires_at": 1})
    requests = []
    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json={"access_token": "sk-ant-oat-new", "refresh_token": "rotated", "expires_in": 3600})
    original = profile.read_bytes()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        a, b = ClaudeDirectAuth(store, client=client), ClaudeDirectAuth(store, client=client)
        headers = await asyncio.gather(a.headers(), b.headers())
    assert len(requests) == 1 and requests[0]["refresh_token"] == "rotate-me"
    assert all(h["authorization"] == "Bearer sk-ant-oat-new" for h in headers)
    assert store.read("claude-direct")["refresh_token"] == "rotated"
    assert profile.read_bytes() == original
    a.logout()
    assert store.read("claude-direct") is None and profile.read_bytes() == original


@pytest.mark.asyncio
async def test_owned_browser_pkce_callback_and_persistence(profile):
    import asyncio
    import base64
    import hashlib
    from urllib.parse import parse_qs, urlsplit

    from nexus.llmhub.auth.store import CredentialStore

    query = {}
    tasks = []
    async def callback(url):
        nonlocal query
        query = parse_qs(urlsplit(url).query)
        redirect = query["redirect_uri"][0]
        async with httpx.AsyncClient() as client:
            await client.get(redirect, params={"code": "authorization-code", "state": "wrong"})
            await client.get(redirect, params={"code": "authorization-code", "state": query["state"][0]})
    def open_browser(url):
        tasks.append(asyncio.create_task(callback(url)))
        return True
    def handler(req):
        data = json.loads(req.content)
        assert data["code"] == "authorization-code"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest()).rstrip(b"=").decode()
        assert query["code_challenge"] == [challenge]
        assert data["state"] == query["state"][0]
        assert "org:create_api_key" not in query["scope"][0]
        return httpx.Response(200, json={"access_token": "sk-ant-oat-owned", "refresh_token": "owned-refresh", "expires_in": 3600})
    store = CredentialStore(profile.parent / "auth.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = ClaudeDirectAuth(store, client=client, callback_port=0)
        await auth.login(browser_open=open_browser)
        await asyncio.gather(*tasks)
        assert (await auth.headers())["authorization"] == "Bearer sk-ant-oat-owned"
    assert store.read("claude-direct")["refresh_token"] == "owned-refresh"


@pytest.mark.asyncio
async def test_owned_auth_failure_keeps_token_and_never_falls_back(profile):
    from nexus.llmhub.auth.store import CredentialStore

    store = CredentialStore(profile.parent / "auth.json")
    entry = {"access_token": "sk-ant-oat-old", "refresh_token": "secret-refresh", "expires_at": 1}
    store.write("claude-direct", entry)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400, text="secret-refresh"))) as client:
        with pytest.raises(AuthError) as error:
            await ClaudeDirectAuth(store, client=client).headers()
    assert "secret-refresh" not in str(error.value)
    assert store.read("claude-direct") == entry


@pytest.mark.asyncio
async def test_native_provider_runs_real_nexus_tool_loop(profile, tmp_path):
    await ClaudeDirectAuth().login("cli-profile")
    from nexus.loop import Agent, ToolEnd, TurnEnd

    bodies = []
    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            data = [
                {"type": "message_start", "message": {"id": "a", "usage": {}}},
                {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": "toolu_write", "name": "write", "input": {"path": "proof.txt", "content": "nexus owns tools"}}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ]
        else:
            assert body["messages"][-1]["content"][0]["type"] == "tool_result"
            assert "created proof.txt" in body["messages"][-1]["content"][0]["content"]
            data = [
                {"type": "message_start", "message": {"id": "b", "usage": {}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "done"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
        return httpx.Response(200, text="".join(f"event: {c['type']}\ndata: {json.dumps(c)}\n\n" for c in data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        agent = Agent(AnthropicProvider(ClaudeDirectAuth(), client=client), model="claude-haiku-4-5", workspace=tmp_path)
        events = [e async for e in agent.turn("write proof.txt")]
    assert (tmp_path / "proof.txt").read_text() == "nexus owns tools"
    assert any(isinstance(e, ToolEnd) and not e.result.is_error for e in events)
    assert events[-1] == TurnEnd("end_turn", 2)


def test_unknown_models_do_not_guess_thinking():
    from nexus.llmhub.anthropic_policy import thinking_policy

    assert thinking_policy("claude-opus-6").mode == "none"
    assert "thinking" not in build_body(Request("unknown", "", (), reasoning_effort="off"), origin="claude-direct")


def test_env_disconnect_does_not_touch_cli(profile, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-env")
    with pytest.raises(AuthError, match="unset"):
        ClaudeDirectAuth().logout()


@pytest.mark.parametrize("kind", ["server_tool_use", "compaction", "unknown"])
def test_unimplemented_context_blocks_fail_explicitly(kind):
    state = MessagesState("claude-direct")
    with pytest.raises(ProviderError, match="unsupported content block"):
        state.consume({"type": "content_block_start", "index": 0, "content_block": {"type": kind}})


def test_cache_retention_modes():
    req = Request("claude-haiku-4-5", "system", (Message("user", (Text("hi"),)),))
    long = build_body(req, origin="claude-direct")
    short = build_body(req, origin="claude-direct", cache_retention="short")
    assert long["system"][1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert short["system"][1]["cache_control"] == {"type": "ephemeral"}
    with pytest.raises(ProviderError, match="retention"):
        build_body(req, origin="claude-direct", cache_retention="invalid")


def test_builtin_root_combinators_move_to_description_without_mutation():
    from nexus.tools import DEFAULT_TOOLS

    tools = tuple(t.spec for t in DEFAULT_TOOLS)
    body = build_body(Request("claude-haiku-4-5", "", (), tools), origin="claude-direct")
    assert all(not any(k in t["input_schema"] for k in ("oneOf", "anyOf", "allOf")) for t in body["tools"])
    edit = next(t for t in body["tools"] if t["name"] == "edit")
    assert '"anyOf"' in edit["input_schema"]["description"]
    assert "anyOf" in next(t.parameters for t in tools if t.name == "edit")


@pytest.mark.parametrize("model,supported", [("claude-haiku-4-5", False), ("claude-opus-4-7", False),
    ("claude-haiku-5-5", True), ("claude-sonnet-5-5", True), ("claude-fable-5-0", False),
    ("claude-fable-5-1", True)])
def test_prefix_binding_fails_closed(model, supported):
    body = build_body(Request(model, "", (), reasoning_effort="low"), origin="claude-direct")
    assert body["thinking"].get("block_binding") == ({"prefix_mismatch_behavior": "error"} if supported else None)


@pytest.mark.asyncio
async def test_eager_tools_and_binding_headers_are_selective(profile):
    await ClaudeDirectAuth().login("cli-profile")
    payload = "".join(f"event: {c['type']}\ndata: {json.dumps(c)}\n\n" for c in chunks())
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, text=payload)
    tools = tuple(ToolSpec(name, name, {"type": "object"}) for name in ("read", "write"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(ClaudeDirectAuth(), client=client, eager_tools=("read",))
        req = Request("claude-sonnet-5-5", "", (Message("user", (Text("go"),)),), tools, "low")
        events = [e async for e in provider.stream(req)]
    assert events[-1].stop == "tool_calls"
    body = json.loads(seen[0].content)
    assert body["tools"][0]["eager_input_streaming"] is True
    assert "eager_input_streaming" not in body["tools"][1]
    assert "thinking-binding-controls-2026-08-01" in seen[0].headers["anthropic-beta"]


def test_owned_identity_is_bounded_and_stable_on_refresh(profile):
    auth = ClaudeDirectAuth()
    data = {"access_token": "sk-ant-oat-a", "refresh_token": "refresh", "expires_in": 3600,
            "account": {"uuid": "account-a", "email_address": "a@example.com", "secret": "omit"},
            "organization": {"uuid": "org-a", "name": "A"}, "scope": "user:inference"}
    entry = auth._entry(data)
    assert entry["account"] == {"uuid": "account-a", "email_address": "a@example.com"}
    refreshed = auth._entry({**data, "organization": {"uuid": "org-a", "name": "New name"}}, entry)
    assert refreshed["organization"] == entry["organization"]
    assert refreshed["authorized_at"] == entry["authorized_at"]


@pytest.mark.parametrize("kind", ["account", "organization"])
def test_refresh_cannot_silently_switch_identity(profile, kind):
    auth = ClaudeDirectAuth()
    data = {"access_token": "sk-ant-oat-a", "refresh_token": "refresh", "expires_in": 3600,
            kind: {"uuid": "original"}}
    previous = auth._entry(data)
    with pytest.raises(AuthError, match="refresh identity changed"):
        auth._entry({**data, kind: {"uuid": "different"}}, previous)
    assert previous[kind]["uuid"] == "original"


def test_unknown_stream_content_never_disappears():
    state = MessagesState("claude-direct")
    state.consume({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    with pytest.raises(ProviderError, match="unsupported content delta"):
        state.consume({"type": "content_block_delta", "index": 0, "delta": {"type": "future_delta"}})
    with pytest.raises(ProviderError, match="unsupported Messages event"):
        state.consume({"type": "future_event"})


@pytest.mark.asyncio
@pytest.mark.parametrize("retention,enabled,long", [("none", False, False), ("short", True, False), ("long", True, True)])
async def test_request_cache_override_and_headers(profile, retention, enabled, long):
    await ClaudeDirectAuth().login("cli-profile")
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, text="".join(f"data: {json.dumps(c)}\n\n" for c in chunks()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AnthropicProvider(ClaudeDirectAuth(), client=client, cache=False)
        req = DirectRequest("claude-haiku-4-5", "sys", (Message("user", (Text("hi"),)),), cache_retention=retention)
        _ = [e async for e in provider.stream(req)]
    assert ("cache_control" in seen[0].content.decode()) == enabled
    assert ("extended-cache-ttl-2025-04-11" in seen[0].headers["anthropic-beta"]) == long


def test_mismatched_content_delta_is_rejected():
    state = MessagesState("claude-direct")
    state.consume({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
    with pytest.raises(ProviderError, match="does not match"):
        state.consume({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}})


@pytest.mark.asyncio
async def test_direct_cli_mode_never_starts_or_logs_out_claude_code(profile, monkeypatch):
    from nexus.llmhub.auth.claude import ClaudeAuth

    def forbidden(*args, **kwargs):
        raise AssertionError("direct mode must not mutate Claude Code login")
    monkeypatch.setattr(ClaudeAuth, "login", forbidden)
    monkeypatch.setattr(ClaudeAuth, "logout", forbidden)
    original = profile.read_bytes()
    auth = ClaudeDirectAuth()
    await auth.login("cli-profile")
    auth.logout()
    assert not auth.logged_in()
    assert profile.read_bytes() == original


@pytest.mark.asyncio
async def test_long_lived_browser_grant_is_narrow_and_never_refreshed(profile):
    import asyncio
    from urllib.parse import parse_qs, urlsplit

    from nexus.llmhub.auth.store import CredentialStore

    tasks = []
    async def callback(url):
        query = parse_qs(urlsplit(url).query)
        assert set(query["scope"][0].split()) == {"user:profile", "user:inference"}
        async with httpx.AsyncClient() as client:
            await client.get(query["redirect_uri"][0], params={"code": "long-code", "state": query["state"][0]})
    def open_browser(url):
        tasks.append(asyncio.create_task(callback(url)))
        return True
    calls = []
    def handler(req):
        calls.append(json.loads(req.content))
        assert calls[-1]["expires_in"] == 31536000
        return httpx.Response(200, json={"access_token": "sk-ant-oat-long", "refresh_token": "never-store",
                                        "expires_in": 31536000})
    store = CredentialStore(profile.parent / "auth.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        auth = ClaudeDirectAuth(store, client=client, callback_port=0)
        await auth.login("browser-long", browser_open=open_browser)
        await asyncio.gather(*tasks)
        entry = store.read("claude-direct")
        assert entry["grant"] == "long-lived" and entry["refresh_token"] == ""
        store.write("claude-direct", {**entry, "expires_at": time.time() + 120})
        assert (await auth.headers())["authorization"] == "Bearer sk-ant-oat-long"
        store.write("claude-direct", {**entry, "expires_at": 1})
        assert not auth.logged_in()
        with pytest.raises(AuthError):
            await auth.headers()
    assert len(calls) == 1
