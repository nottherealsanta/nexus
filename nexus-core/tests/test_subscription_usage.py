import json
import sys

import httpx
import pytest

from nexus import provider_usage as usage
from nexus.claude_usage import interactive_usage, oauth_credentials


@pytest.mark.parametrize("plan", ["free", "plus", "pro", "team", "business", "enterprise", "edu", "future"])
@pytest.mark.parametrize("parent", ["root", "rate_limit", "spend_control"])
def test_codex_all_plans_and_limit_locations(plan, parent):
    limit = {"individualLimit": {"limit": "1000", "used": "36.8", "remainingPercent": 96, "reset_at": 1793491200}}
    data = {"plan_type": plan, **(limit if parent == "root" else {parent: limit})}
    result = usage.parse_codex(data)
    assert result["plan"] == plan.title()
    window = result["windows"][0]
    assert window["used_percent"] == 4
    assert window["kind"] == "monthly"
    assert window["resets_at"] == 1793491200


def test_missing_data_is_not_zero_or_unlimited():
    assert usage.parse_codex({})["windows"] == []
    assert usage.parse_codex({"individual_limit": {"limit": 500}})["windows"][0]["used_percent"] is None
    assert usage.parse_claude_oauth({})["notes"]
    assert usage.parse_codex_monthly({"effective_monthly_limit": {"limit": 500, "enforcement_mode": "NONE"}}) is None
    with pytest.raises(usage.UsageError):
        usage.parse_codex_monthly({"effective_monthly_limit": {"limit": "bad"}})
    with pytest.raises(usage.UsageError):
        usage.parse_claude("Context: 99% used")


def test_monthly_limit_survives_many_side_windows():
    side = {"primary_window": {"used_percent": 10}, "secondary_window": {"used_percent": 20}}
    result = usage.parse_codex({"rate_limit": side, "individual_limit": {"limit": 1000, "used": 100},
        "additional_rate_limits": [{"rate_limit": side}] * 8})
    assert len(result["windows"]) == usage.MAX_WINDOWS
    assert any(w["label"] == "Monthly credits" for w in result["windows"])


@pytest.mark.parametrize("plan", ["pro", "max", "team", "enterprise", ""])
def test_claude_spend_only_is_valid_and_minor_units(plan):
    result = usage.parse_claude_oauth({"five_hour": None, "seven_day": None, "extra_usage": {
        "is_enabled": True, "monthly_limit": 2000, "used_credits": 763, "currency": "EUR"}}, plan)
    window = result["windows"][0]
    assert window["label"] == "Spend limit"
    assert window["used_percent"] == 38.15
    assert window["detail"] == "EUR 7.63 of 20.00"
    assert window["resets_at"] is None
    assert window["period_seconds"] is None


def test_claude_weekly_only_and_disabled_extra_usage():
    result = usage.parse_claude_oauth({"seven_day": {"utilization": 10},
        "seven_day_sonnet": {"utilization": 1}, "extra_usage": {"is_enabled": False}})
    assert [w["kind"] for w in result["windows"]] == ["weekly", "other"]
    assert [w["used_percent"] for w in result["windows"]] == [10, 1]


@pytest.mark.parametrize("suffix", ["", " · Resets Dec 1 (Asia/Calcutta)"])
def test_claude_multiline_and_spend_cli(suffix):
    result = usage.parse_claude("Current week (all models)\n████ 10% used\n"
                                "Resets Nov 9 at 9am (UTC)\nCurrent week (Sonnet only)\n1% used\n"
                                "Usage insights\n99% used\nUsage credits\n$42.00 / $1,000.00 spent" + suffix)
    assert [w["used_percent"] for w in result["windows"]] == [10, 1, 4.2]
    assert usage.parse_claude("Usage credits\n$42.00 / $1,000.00 spent")["windows"][0]["kind"] == "monthly"
    with pytest.raises(usage.UsageError):
        usage.parse_claude("Usage insights\n99% used\nsecret startup text")


def test_credentials_are_profile_scoped_and_stale_tokens_are_not_used(tmp_path):
    default = tmp_path / ".claude"
    default.mkdir()
    path = default / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret", "subscriptionType": "enterprise"}}))
    env = {"HOME": str(tmp_path)}
    assert oauth_credentials(env) == ("secret", "enterprise")
    assert oauth_credentials({**env, "CLAUDE_CONFIG_DIR": str(tmp_path / "different")}) is None
    assert oauth_credentials({**env, "CLAUDE_CONFIG_DIR": "different", "CLAUDE_SECURESTORAGE_CONFIG_DIR": ""}) == ("secret", "enterprise")
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret", "expiresAt": 1}}))
    assert oauth_credentials(env) is None


@pytest.mark.parametrize("failure", [False, True])
async def test_codex_optional_monthly_enrichment_preserves_main_data(monkeypatch, failure):
    class Auth:
        async def headers(self):
            return {"authorization": "Bearer secret", "chatgpt-account-id": "workspace"}
    monkeypatch.setitem(usage.SIGN_INS, "codex", Auth)
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"plan_type": "business", "spend_control": {}, "rate_limit": {
                "primary_window": {"used_percent": 20, "limit_window_seconds": 18000}}})
        return httpx.Response(404 if failure else 200, json={"current_month_usage": "100",
            "effective_monthly_limit": {"limit": "1000", "enforcement_mode": "HARD_CAP"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await usage._codex(client)
    assert any(w["used_percent"] == 20 for w in result["windows"])
    assert len(result["windows"]) == (1 if failure else 2)
    assert len(seen) == 2
    assert "secret" not in json.dumps(result)


async def test_claude_oauth_no_cli_prompt(monkeypatch):
    monkeypatch.setattr(usage, "cli_path", lambda: "claude")
    monkeypatch.setattr(usage, "claude_status", lambda: {"subscriptionType": "enterprise"})
    monkeypatch.setattr(usage, "oauth_credentials", lambda env: ("secret", "enterprise"))
    def handler(request):
        assert request.url == usage.CLAUDE_URL
        assert request.headers["anthropic-beta"] == "oauth-2025-04-20"
        return httpx.Response(200, json={"extra_usage": {"is_enabled": True, "monthly_limit": 10000, "used_credits": 1000}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await usage._claude(client)
    assert result["windows"][0]["used_percent"] == 10
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_claude_auth_fallback_but_not_rate_limit(monkeypatch, status):
    monkeypatch.setattr(usage, "cli_path", lambda: "claude")
    monkeypatch.setattr(usage, "claude_status", lambda: {"subscriptionType": "enterprise"})
    monkeypatch.setattr(usage, "oauth_credentials", lambda env: ("secret", "enterprise"))
    calls = []
    async def fallback(*args):
        calls.append(args)
        return "Usage credits\n$42.00 / $1,000.00 spent"
    monkeypatch.setattr(usage, "interactive_usage", fallback)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        if status == 429:
            with pytest.raises(usage.UsageError, match="HTTP 429"):
                await usage._claude(client)
            assert not calls
        else:
            result = await usage._claude(client)
            assert result["plan"] == "Enterprise"
            assert len(calls) == 1


@pytest.mark.parametrize("response_account,code", [("other", 200), ("workspace", 200), ("workspace", 403)])
async def test_workspace_balance_respects_account_and_optional_permissions(monkeypatch, response_account, code):
    class Auth:
        async def headers(self):
            return {"authorization": "Bearer secret", "chatgpt-account-id": "workspace"}
    monkeypatch.setitem(usage.SIGN_INS, "codex", Auth)
    seen = []
    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"account_id": response_account,
                "credits": {"has_credits": True, "balance": None}})
        return httpx.Response(code, json={"balance": "1234"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await usage._codex(client)
    assert len(seen) == (1 if response_account == "other" else 2)
    if response_account == "workspace":
        assert result["notes"] == (["Workspace credits balance: 1234"] if code == 200 else
                                   ["Workspace credits balance is unavailable to this account."])


@pytest.mark.skipif(sys.platform == "win32", reason="PTY requires POSIX")
@pytest.mark.parametrize("caption", ["Show plan", "Show session cost, plan usage, and activity stats"])
async def test_pty_verifies_command_before_enter(tmp_path, caption):
    cli = tmp_path / "claude"
    cli.write_text(f'#!{sys.executable}\n' + '''import os, tty
tty.setraw(0)
os.write(1, "❯\\r\\n".encode())
command = os.read(0, 6)
if command != b"/usage":
    raise SystemExit(1)
os.write(1, "❯ /usage CAPTION".encode())
assert os.read(0, 1) == b"\\r"
os.write(1, b"\\x1b[2J\\x1b[HUsage credits\\r\\n$42.00 / $1,000.00 spent")
import time
time.sleep(20)
'''.replace("CAPTION", caption))
    cli.chmod(0o700)
    text = await interactive_usage(str(cli), {"HOME": str(tmp_path)}, 8)
    assert text is not None
    assert usage.parse_claude(text)["windows"][0]["used_percent"] == 4.2


@pytest.mark.skipif(sys.platform == "win32", reason="PTY requires POSIX")
@pytest.mark.parametrize("palette", ["Show plan", "❯ /other Show plan", " /usage Show plan"])
async def test_pty_never_submits_unverified_palette(tmp_path, palette):
    cli = tmp_path / "claude"
    marker = tmp_path / "submitted"
    cli.write_text(f'#!{sys.executable}\n' + f'''import os, tty
tty.setraw(0)
os.write(1, "❯\\r\\n".encode())
assert os.read(0, 6) == b"/usage"
os.write(1, {palette!r}.encode())
if os.read(0, 1) == b"\\r":
    open({str(marker)!r}, "w").close()
''')
    cli.chmod(0o700)
    assert await interactive_usage(str(cli), {"HOME": str(tmp_path)}, 3) is None
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="PTY requires POSIX")
async def test_pty_resolves_relative_profile_before_changing_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli = tmp_path / "claude"
    marker = tmp_path / "profile"
    cli.write_text(f'#!{sys.executable}\n' + f'''import os
open({str(marker)!r}, "w").write(os.environ["CLAUDE_CONFIG_DIR"])
''')
    cli.chmod(0o700)
    assert await interactive_usage(str(cli), {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "profile-dir"}, 2) is None
    assert marker.read_text() == str(tmp_path / "profile-dir")
