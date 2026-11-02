import json

import httpx

from nexus import provider_usage
from nexus.provider_usage import (
    NO_LIMITS_NOTE,
    parse_claude,
    parse_codex,
    parse_copilot,
    parse_opencode,
    parse_xai,
    providers_usage,
)


class FakeAuth:
    def __init__(self, headers=None):
        self._headers = headers or {}

    async def headers(self):
        return self._headers


def test_plan_windows_parse_from_each_provider():
    codex = parse_codex({
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": 42, "reset_at": 1_700_000_000, "limit_window_seconds": 18_000},
            "secondary_window": {"used_percent": 80, "reset_at": 1_700_100_000, "limit_window_seconds": 604_800},
        },
    })
    assert codex["plan"] == "Plus"
    assert [w["label"] for w in codex["windows"]] == ["5-hour", "Weekly"]
    assert codex["windows"][0]["used_percent"] == 42

    copilot = parse_copilot({
        "copilot_plan": "pro",
        "quota_reset_date_utc": "2026-12-01T00:00:00Z",
        "quota_snapshots": {"premium_interactions": {
            "percent_remaining": 75, "entitlement": 300, "remaining": 225}},
    })
    assert copilot["plan"] == "Pro"
    assert copilot["windows"][0]["used_percent"] == 25
    assert copilot["windows"][0]["detail"] == "225 of 300 left"

    opencode = parse_opencode({"usage": {"rolling": {"usagePercent": 7, "resetInSec": 600}}}, now=1000)
    assert opencode["windows"][0]["label"] == "5-hour"
    assert opencode["windows"][0]["used_percent"] == 7
    assert opencode["windows"][0]["resets_at"] == 1600

    grok = parse_xai({"config": {
        "creditUsagePercent": 12.5,
        "currentPeriod": {"start": 1_700_000_000, "end": 1_700_604_800},
        "prepaidBalance": {"val": 250},
    }}, plan="SuperGrok", now=1_700_100_000)
    assert grok["plan"] == "SuperGrok"
    assert grok["windows"][0]["label"] == "Weekly"
    assert grok["notes"] == ["Prepaid credits: $2.50"]

    claude = parse_claude(
        "Current session: 59% used · resets Oct 15 at 8:30pm (UTC)\n"
        "Current week (Sonnet only): 31% used · resets Oct 20 at 12pm (UTC)\n",
        now=1_700_000_000,
    )
    assert [w["label"] for w in claude["windows"]] == ["5-hour session", "Weekly (Sonnet only)"]
    assert claude["windows"][0]["used_percent"] == 59
    assert claude["windows"][1]["kind"] == "other"


async def test_every_connected_provider_gets_a_row(monkeypatch):
    monkeypatch.setattr(provider_usage, "connected_providers",
                        lambda: ["codex", "openai", "groq", "nvidia", "ollama"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer codex-token"
        assert request.url.host == "chatgpt.com"
        return httpx.Response(200, json={"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 42, "reset_at": 1_700_000_000, "limit_window_seconds": 18_000}}})

    monkeypatch.setitem(provider_usage.SIGN_INS, "codex", lambda: FakeAuth({"authorization": "Bearer codex-token"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await providers_usage(client)

    rows = {row["id"]: row for row in result["providers"]}
    assert list(rows) == ["codex", "openai", "groq", "nvidia", "ollama"]
    assert rows["codex"]["windows"][0]["used_percent"] == 42
    for provider in ("openai", "groq", "nvidia", "ollama"):
        assert rows[provider]["windows"] == []
        assert rows[provider]["notes"] == [NO_LIMITS_NOTE]
        assert rows[provider]["error"] is None
    assert "codex-token" not in json.dumps(result)


async def test_one_failing_provider_does_not_hide_the_others(monkeypatch):
    monkeypatch.setattr(provider_usage, "connected_providers", lambda: ["codex", "copilot"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "chatgpt.com":
            return httpx.Response(200, json={"rate_limit": {}})
        return httpx.Response(500, json={"error": "down"})

    monkeypatch.setitem(provider_usage.SIGN_INS, "codex", lambda: FakeAuth({"authorization": "Bearer codex"}))
    monkeypatch.setitem(provider_usage.SIGN_INS, "copilot", lambda: FakeAuth({"authorization": "Bearer gho"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await providers_usage(client)

    rows = {row["id"]: row for row in result["providers"]}
    assert rows["codex"]["error"] is None
    assert rows["copilot"]["error"] == "HTTP 500 from the usage endpoint"


async def test_opencode_and_grok_use_their_own_usage_endpoints(monkeypatch):
    monkeypatch.setattr(provider_usage, "connected_providers", lambda: ["opencode", "grok"])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path))
        if request.url.host == "opencode.ai":
            assert request.headers["authorization"] == "Bearer go-key"
            return httpx.Response(200, json={"usage": {"rolling": {"usagePercent": 7, "resetInSec": 60}}})
        if request.url.path == "/v1/billing":
            assert request.headers["authorization"] == "Bearer xai-token"
            return httpx.Response(200, json={"config": {"creditUsagePercent": 12}})
        return httpx.Response(200, json={"subscription_tier_display": "SuperGrok"})

    monkeypatch.setitem(provider_usage.SIGN_INS, "opencode",
                        lambda: FakeAuth({"authorization": "Bearer go-key"}))
    monkeypatch.setitem(provider_usage.SIGN_INS, "grok",
                        lambda: FakeAuth({"authorization": "Bearer xai-token"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await providers_usage(client)

    rows = {row["id"]: row for row in result["providers"]}
    assert rows["opencode"]["windows"][0]["used_percent"] == 7
    assert rows["grok"]["windows"][0]["used_percent"] == 12
    assert rows["grok"]["plan"] == "SuperGrok"
    assert ("opencode.ai", "/zen/go/v1/usage") in seen
    assert ("cli-chat-proxy.grok.com", "/v1/billing") in seen
