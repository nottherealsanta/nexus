"""ProvidersUsage: plan limits for every connected provider, behind the host boundary."""
from __future__ import annotations

import asyncio
import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest

from nexus.host_support import provider_usage
from nexus.host_support.provider_usage import (
    parse_claude,
    parse_codex,
    parse_copilot,
    parse_opencode_go,
    providers_usage,
    window_label,
)
from nexus.model.providers import claude_agent_auth

# Shapes recorded from live endpoints on 2026-10-01 (identifiers removed).
CODEX = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": False, "limit_reached": True,
        "primary_window": {"used_percent": 100, "limit_window_seconds": 18000, "reset_after_seconds": 2564, "reset_at": 1790838719},
        "secondary_window": {"used_percent": 41, "limit_window_seconds": 604800, "reset_after_seconds": 536762, "reset_at": 1791372916},
    },
    "code_review_rate_limit": None,
    "additional_rate_limits": [{"limit_name": "GPT-5.3-Codex-Spark", "rate_limit": {
        "primary_window": {"used_percent": 3, "limit_window_seconds": 18000, "reset_at": 1790838000}}}],
    "credits": {"has_credits": True, "unlimited": False, "balance": "12.5"},
    "rate_limit_reset_credits": {"available_count": 3},
}
COPILOT = {
    "copilot_plan": "business", "token_based_billing": True,
    "quota_reset_date_utc": "2026-11-01T00:00:00.000Z",
    "quota_snapshots": {
        "chat": {"percent_remaining": 100.0, "unlimited": True, "entitlement": 0, "remaining": 0},
        "completions": {"percent_remaining": 100.0, "unlimited": True},
        "premium_interactions": {"percent_remaining": 99.8, "unlimited": False, "entitlement": 15000,
                                 "remaining": 14979, "credits_used": 20, "overage_count": 0},
    },
}
CLAUDE_TEXT = (
    "You are currently using your subscription to power your Claude Code usage\n\n"
    "Current session: 59% used · resets Oct 1 at 12:19pm (Asia/Calcutta)\n"
    "Current week (all models): 85% used · resets Oct 1 at 8:29pm (Asia/Calcutta)\n"
    "Current week (Sonnet only): 4% used\n\n"
    "What's contributing to your limits usage?\n  50% of your usage was at >150k context\n"
)


def test_window_labels():
    assert [window_label(v) for v in (18000, 604800, 86400, 259200, 900, None)] == [
        "5-hour", "Weekly", "Daily", "3-day", "15-minute", "Limit"]


def test_parse_codex_maps_windows_extra_limits_and_notes():
    result = parse_codex(CODEX)
    assert result["plan"] == "Plus"
    assert [(w["label"], w["used_percent"], w["resets_at"]) for w in result["windows"]] == [
        ("5-hour", 100.0, 1790838719), ("Weekly", 41.0, 1791372916), ("GPT-5.3-Codex-Spark 5-hour", 3.0, 1790838000)]
    assert result["notes"] == ["Limit reached: new requests wait for the next reset",
                               "Credits balance: 12.5", "Usage reset credits available: 3"]


def test_parse_copilot_reports_monthly_quota_and_unlimited_notes():
    result = parse_copilot(COPILOT)
    assert result["plan"] == "Business"
    [window] = result["windows"]
    assert window["label"] == "Premium requests (monthly)" and window["used_percent"] == pytest.approx(0.2)
    assert window["detail"] == "14,979 of 15,000 left" and window["resets_at"] == 1793491200
    assert result["notes"] == ["Chat: unlimited", "Completions: unlimited", "AI credits used this period: 20"]


def test_parse_opencode_go_uses_percent_units_and_reset_seconds():
    data = {"usage": {"rolling": {"usagePercent": 12.5, "resetInSec": 600},
                      "weekly": {"usagePercent": 0.5, "resetInSec": 86400},
                      "monthly": {"used": 30, "limit": 120, "resetAt": "2026-11-01T00:00:00Z"}}}
    result = parse_opencode_go(data, now=1000.0)
    assert [(w["label"], w["used_percent"], w["resets_at"]) for w in result["windows"]] == [
        ("5-hour", 12.5, 1600), ("Weekly", 0.5, 87400), ("Monthly", 25.0, 1793491200)]
    with pytest.raises(provider_usage.UsageError):
        parse_opencode_go({"usage": {}})


def test_parse_claude_reads_cli_lines_and_relabels_windows():
    result = parse_claude(CLAUDE_TEXT)
    assert [(w["label"], w["used_percent"], w["reset_text"]) for w in result["windows"]] == [
        ("5-hour session", 59.0, "Oct 1 at 12:19pm (Asia/Calcutta)"),
        ("Weekly (all models)", 85.0, "Oct 1 at 8:29pm (Asia/Calcutta)"),
        ("Weekly (Sonnet only)", 4.0, ""),
    ]
    with pytest.raises(provider_usage.UsageError, match="Please run /login"):
        parse_claude("Please run /login\n")


class Manager:
    def __init__(self, headers=None, error=None):
        self._headers, self._error = headers or {}, error

    async def status(self):
        return True

    async def headers(self):
        if self._error:
            raise self._error
        return self._headers


class Claude:
    async def status(self):
        return True

    async def plan(self):
        return "max"

    async def usage(self, cwd=None):
        return CLAUDE_TEXT


class Disconnected:
    async def status(self):
        return False


def _runtime(**overrides):
    values = dict(
        workspace=None, _environ={},
        _codex_auth_factory=lambda **_: Manager({"authorization": "Bearer codex-token", "ChatGPT-Account-Id": "acct"}),
        _copilot_auth_factory=lambda **_: Manager({"authorization": "Bearer gho_token", "copilot-integration-id": "x"}),
        _api_key_auth_factory=lambda provider, **_: Disconnected(),
        _claude_auth_factory=lambda **_: Claude(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_providers_usage_fetches_connected_providers_concurrently():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.headers.get("authorization")))
        if request.url.host == "chatgpt.com":
            assert request.headers["chatgpt-account-id"] == "acct"
            return httpx.Response(200, json=CODEX)
        assert "copilot-integration-id" not in request.headers
        return httpx.Response(200, json=COPILOT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await providers_usage(_runtime(), client)
    rows = {row["id"]: row for row in result["providers"]}
    assert list(rows) == ["codex", "claude-agent", "github-copilot"]
    assert result["not_connected"] == ["OpenCode Go"]
    assert rows["claude-agent"]["plan"] == "Max" and rows["claude-agent"]["source"] == "claude CLI · /usage"
    assert rows["github-copilot"]["windows"][0]["detail"] == "14,979 of 15,000 left"
    assert sorted(seen) == [("api.github.com", "/copilot_internal/user", "Bearer gho_token"),
                            ("chatgpt.com", "/backend-api/wham/usage", "Bearer codex-token")]
    assert "token" not in json.dumps(result).replace("tokens", "")


async def test_one_failing_provider_is_a_redacted_row_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"}) if request.url.host == "chatgpt.com" else httpx.Response(500)

    runtime = _runtime(_claude_auth_factory=lambda **_: Disconnected(),
                       _copilot_auth_factory=lambda **_: Manager(error=RuntimeError("keychain said sk-live-supersecret123456")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await providers_usage(runtime, client)
    rows = {row["id"]: row for row in result["providers"]}
    assert rows["codex"]["error"] == "HTTP 401: sign in again in Settings → Providers"
    assert "supersecret" not in rows["github-copilot"]["error"]
    assert result["not_connected"] == ["Claude", "OpenCode Go"]


async def test_opencode_go_reads_with_the_stored_key():
    class Key(Manager):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(provider_usage.OPENCODE_GO_USAGE_URL)
        assert request.headers["authorization"] == "Bearer go-key"
        return httpx.Response(200, json={"usage": {"rolling": {"usagePercent": 7, "resetInSec": 60}}})

    runtime = _runtime(_codex_auth_factory=lambda **_: Disconnected(), _copilot_auth_factory=lambda **_: Disconnected(),
                       _claude_auth_factory=lambda **_: Disconnected(),
                       _api_key_auth_factory=lambda provider, **_: Key({"authorization": "Bearer go-key"}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await providers_usage(runtime, client)
    [row] = result["providers"]
    assert row["id"] == "opencode-go" and row["windows"][0]["used_percent"] == 7.0


# --- the official Claude CLI helpers ---------------------------------------------------------


def _fake_cli(tmp_path, body: str) -> str:
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.skipif(os.name != "posix", reason="shell script stands in for the CLI")
async def test_cli_usage_runs_slash_usage_and_returns_the_text(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_agent_auth.importlib.util, "find_spec", lambda _: SimpleNamespace(origin=str(tmp_path / "x.py")))
    payload = json.dumps({"type": "result", "is_error": False, "result": CLAUDE_TEXT})
    cli = _fake_cli(tmp_path, f'[ "$2" = "/usage" ] || exit 3\ncat <<\'EOF\'\n{payload}\nEOF\n')
    text = await claude_agent_auth.usage(cli, {"PATH": os.environ.get("PATH", "")})
    assert "Current session: 59% used" in text
    failing = _fake_cli(tmp_path, 'echo "{}"; exit 1\n')
    with pytest.raises(RuntimeError, match="claude auth login"):
        await claude_agent_auth.usage(failing, {})


def test_login_url_strips_terminal_hyperlinks():
    url = "https://claude.com/cai/oauth/authorize?code=true&state=abc"
    text = f"Opening browser to sign in…\nIf the browser didn't open, visit: \x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\\nPaste code here if prompted > "
    assert claude_agent_auth.login_url(text) == url


@pytest.mark.skipif(os.name != "posix", reason="shell script stands in for the CLI")
async def test_cli_login_prints_url_reads_the_pasted_code_and_never_opens_a_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_agent_auth.importlib.util, "find_spec", lambda _: SimpleNamespace(origin=str(tmp_path / "x.py")))
    record = tmp_path / "seen"
    cli = _fake_cli(tmp_path, (
        'echo "If the browser didn\'t open, visit: https://claude.com/cai/oauth/authorize?code=true"\n'
        f'printf "Paste code here if prompted > "\nread code\necho "$code|$BROWSER" > {record}\n'
        '[ "$code" = "good-code" ]\n'))
    urls: list[str] = []
    code: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def on_url(url: str) -> None:
        urls.append(url)
        code.set_result("good-code")

    await claude_agent_auth.login(cli, {"PATH": os.environ.get("PATH", "")}, on_url=on_url, code=lambda: code, cancel=asyncio.Event())
    assert urls == ["https://claude.com/cai/oauth/authorize?code=true"]
    assert record.read_text().strip() == "good-code|true"
