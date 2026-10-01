"""Settings → Providers behind the host boundary: sign-in flows, keys, routes."""
from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from nexus.auth.api_key import StoredKeyAuth
from nexus.errors import ProviderError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host_support import provider_auth
from nexus.runtime import Runtime


class MemorySecrets:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def read_secret(self, account):
        return self.values.get(account)

    async def write_secret(self, account, value):
        self.values[account] = value

    async def delete_secret(self, account):
        self.values.pop(account, None)


class FakeDevice:
    """A device-code manager whose approval the test releases."""

    def __init__(self, fail: str = ""):
        self.approved = asyncio.Event()
        self.fail = fail
        self.signed_in = False
        self.domain_seen = None

    async def status(self):
        return self.signed_in

    async def domain(self):
        return "github.com" if self.signed_in else None

    async def logout(self):
        self.signed_in = False

    async def device_login(self, *, on_code, cancel=None, domain=None):
        self.domain_seen = domain
        on_code("https://github.com/login/device", "WXYZ-0000")
        await self.approved.wait()
        if self.fail:
            raise ProviderError(self.fail)
        self.signed_in = True


class FakeClaude:
    """The ClaudeCliAuth shape; the test pastes the code and decides the outcome."""

    def __init__(self, connected=False, fail=False):
        self.connected, self.fail, self.code = connected, fail, None

    async def status(self):
        return self.connected

    async def plan(self):
        return "max"

    async def browser_login(self, *, on_url, code, cancel):
        on_url("https://claude.com/cai/oauth/authorize?code=true&state=x")
        self.code = await code()
        if self.fail:
            raise RuntimeError("Claude sign-in did not complete; check the code and try again")
        self.connected = True

    async def logout(self):
        raise RuntimeError("never called")


class FakeBrowser(FakeDevice):
    async def browser_login(self, *, notify, browser_open):
        browser_open("https://auth.openai.com/oauth/authorize?state=x")
        await self.approved.wait()
        self.signed_in = True


def _runtime(home: Path, copilot=None, codex=None, secrets=None, claude=None):
    secrets = secrets or MemorySecrets()
    claude = claude or FakeClaude()
    return SimpleNamespace(
        _claude_auth_factory=lambda **_: claude,
        workspace=home / "workspace", _home=home, _environ={},
        _codex_auth_factory=lambda **_: codex or FakeBrowser(),
        _copilot_auth_factory=lambda **_: copilot or FakeDevice(),
        _api_key_auth_factory=lambda provider, **kw: StoredKeyAuth(provider, store=secrets, **kw),
    ), secrets


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def _config(home: Path) -> dict:
    return tomllib.loads((home / ".nexus" / "config.toml").read_text(encoding="utf-8"))


async def test_status_lists_the_four_providers_without_credentials(home):
    runtime, _ = _runtime(home)
    rows = (await provider_auth.providers_status(runtime))["providers"]
    assert [row["id"] for row in rows] == ["codex", "github-copilot", "opencode-go", "claude-agent"]
    assert [row["methods"] for row in rows] == [["browser", "device"], ["device"], ["api_key"], ["browser"]]
    assert [row["can_logout"] for row in rows] == [True, True, True, False]
    assert not any(row["connected"] for row in rows)


async def test_claude_sign_in_takes_a_pasted_code_and_saves_the_route(home):
    claude = FakeClaude()
    runtime, _ = _runtime(home, claude=claude)
    started = await provider_auth.provider_login(runtime, "claude-agent")
    assert started["status"] == "pending" and started["code_entry"] is True
    assert started["url"].startswith("https://claude.com/cai/oauth/authorize")
    with pytest.raises(Exception, match="whole code"):
        await provider_auth.provider_login_code(runtime, started["login_id"], "a b")
    sent = await provider_auth.provider_login_code(runtime, started["login_id"], "  abc123#state  ")
    assert sent["message"].startswith("Code sent") and "abc123" not in str(sent)
    await runtime._provider_logins[started["login_id"]].task
    assert claude.code == "abc123#state"
    done = await provider_auth.provider_login_poll(runtime, started["login_id"])
    assert done["status"] == "connected"
    assert _config(home)["providers"]["claude-agent"] == {"kind": "claude-agent"}
    rows = {row["id"]: row for row in (await provider_auth.providers_status(runtime))["providers"]}
    assert rows["claude-agent"]["connected"] and rows["claude-agent"]["detail"] == "Max"


async def test_claude_sign_in_failure_and_logout_are_reported(home):
    runtime, _ = _runtime(home, claude=FakeClaude(fail=True))
    started = await provider_auth.provider_login(runtime, "claude-agent")
    await provider_auth.provider_login_code(runtime, started["login_id"], "wrong-code")
    await runtime._provider_logins[started["login_id"]].task
    failed = await provider_auth.provider_login_poll(runtime, started["login_id"])
    assert failed["status"] == "failed" and "did not complete" in failed["message"]
    with pytest.raises(Exception, match="not waiting for a code"):
        await provider_auth.provider_login_code(runtime, started["login_id"], "another")
    with pytest.raises(Exception, match="claude auth logout"):
        await provider_auth.provider_logout(runtime, "claude-agent")


def test_login_code_never_appears_in_repr():
    command = p.ProviderLoginCode(login_id="l", code="secret-code-123")
    assert "secret-code-123" not in repr(command)


async def test_copilot_sign_in_pending_then_connected_saves_pinned_github_route(home):
    device = FakeDevice()
    runtime, _ = _runtime(home, copilot=device)
    started = await provider_auth.provider_login(runtime, "github-copilot", domain="HTTPS://GitHub.com/")
    assert started["status"] == "pending"
    assert started["url"] == "https://github.com/login/device"
    assert device.domain_seen == "github.com" and not device.signed_in
    device.approved.set()
    await runtime._provider_logins[started["login_id"]].task
    connected = await provider_auth.provider_login_poll(runtime, started["login_id"])
    assert connected["status"] == "connected"
    assert "route was not saved" not in connected["message"]
    assert (home / ".nexus" / "config.toml").is_file(), connected["message"]
    assert _config(home)["providers"]["github-copilot"] == {
        "auth": "github_copilot", "base_url": "https://api.githubcopilot.com", "api": "chat",
    }


async def _connect_copilot(runtime, device, idle):
    started = await provider_auth.provider_login(runtime, "github-copilot", idle=idle)
    device.approved.set()
    await runtime._provider_logins[started["login_id"]].task
    return await provider_auth.provider_login_poll(runtime, started["login_id"])


async def test_connect_rebuilds_routes_in_place_when_no_turn_is_running(home):
    device, reloads = FakeDevice(), []
    runtime, _ = _runtime(home, copilot=device)

    async def reload():
        reloads.append(True)
        return True

    runtime.reload_model_routes = reload
    result = await _connect_copilot(runtime, device, lambda: True)
    assert result["status"] == "connected" and reloads == [True]
    assert "Restart" not in result["message"]


async def test_connect_asks_for_restart_while_a_turn_runs_or_routes_are_injected(home):
    device, reloads = FakeDevice(), []
    runtime, _ = _runtime(home, copilot=device)

    async def reload():
        reloads.append(True)
        return False  # routes were injected

    runtime.reload_model_routes = reload
    busy = await _connect_copilot(runtime, device, lambda: False)
    assert busy["status"] == "connected" and reloads == []
    assert "Restart the daemon" in busy["message"]

    device2 = FakeDevice()
    runtime2, _ = _runtime(home, copilot=device2)
    runtime2.reload_model_routes = reload
    injected = await _connect_copilot(runtime2, device2, lambda: True)
    assert reloads == [True] and "Restart the daemon" in injected["message"]


async def test_copilot_denial_and_cancel_do_not_write_config(home):
    denied = FakeDevice(fail="GitHub device authorization was denied")
    runtime, _ = _runtime(home, copilot=denied)
    started = await provider_auth.provider_login(runtime, "github-copilot")
    denied.approved.set()
    for _ in range(20):
        result = await provider_auth.provider_login_poll(runtime, started["login_id"])
        if result["status"] == "failed":
            break
        await asyncio.sleep(0)
    assert result["status"] == "failed" and "denied" in result["message"]
    assert not (home / ".nexus" / "config.toml").exists()

    cancelled_device = FakeDevice()
    runtime, _ = _runtime(home, copilot=cancelled_device)
    started = await provider_auth.provider_login(runtime, "github-copilot")
    result = await provider_auth.provider_login_cancel(runtime, started["login_id"])
    assert result["status"] == "cancelled" and not cancelled_device.signed_in
    assert not (home / ".nexus" / "config.toml").exists()


async def test_codex_browser_sign_in_returns_url_without_opening_a_browser(home):
    browser = FakeBrowser()
    runtime, _ = _runtime(home, codex=browser)
    started = await provider_auth.provider_login(runtime, "codex")
    assert started["method"] == "browser" and started["url"].startswith("https://auth.openai.com/")
    cancelled = await provider_auth.provider_login_cancel(runtime, started["login_id"])
    assert cancelled["status"] == "cancelled" and not browser.signed_in
    assert not (home / ".nexus" / "config.toml").exists()


async def test_failed_sign_in_is_reported_redacted(home):
    browser = FakeBrowser()
    runtime, _ = _runtime(home, codex=browser)
    started = await provider_auth.provider_login(runtime, "codex", method="device")
    browser.fail = "codex: sign-in was denied token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    browser.approved.set()
    await asyncio.sleep(0.05)
    polled = await provider_auth.provider_login_poll(runtime, started["login_id"])
    assert polled["status"] == "failed" and "denied" in polled["message"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in polled["message"]


async def test_login_rejects_unknown_provider_method_and_domain(home):
    runtime, _ = _runtime(home)
    with pytest.raises(Exception, match="unknown provider"):
        await provider_auth.provider_login(runtime, "../codex")
    with pytest.raises(Exception, match="does not support"):
        await provider_auth.provider_login(runtime, "opencode-go")
    device = FakeDevice()
    runtime, _ = _runtime(home, copilot=device)
    with pytest.raises(Exception, match="only on GitHub.com"):
        await provider_auth.provider_login(runtime, "github-copilot", domain="localhost")
    assert device.domain_seen is None
    assert not (home / ".nexus" / "config.toml").exists()


async def test_facade_stores_opencode_go_key_and_never_returns_it(home):
    runtime, secrets = _runtime(home)
    facade = HostFacade.__new__(HostFacade)
    facade.runtime = runtime
    key = "sk-go-live-0123456789abcdef"
    saved = await provider_auth.dispatch_providers(p.ProviderKeySet(provider="opencode-go", key=key), runtime)
    assert isinstance(saved, p.ProviderAuthResult) and saved.connected
    assert secrets.values["opencode-go:default"] == key
    assert key not in repr(saved) and key not in p.encode_result(saved).decode()
    route = _config(home)["providers"]["opencode-go"]
    assert route == {"auth": "keychain", "base_url": "https://opencode.ai/zen/go/v1", "api": "chat"}
    assert key not in (home / ".nexus" / "config.toml").read_text(encoding="utf-8")
    status = await provider_auth.dispatch_providers(p.ProvidersStatus(), runtime)
    assert next(row for row in status.providers if row["id"] == "opencode-go")["connected"]
    assert key not in p.encode_result(status).decode()
    bad = await HostFacade.handle(facade, p.ProviderKeySet(provider="opencode-go", key="bad key value"))
    assert isinstance(bad, p.ErrorResult) and "bad key" not in bad.message
    removed = await provider_auth.dispatch_providers(p.ProviderLogout(provider="opencode-go"), runtime)
    assert not removed.connected and "opencode-go:default" not in secrets.values


async def test_runtime_uses_direct_copilot_bearer_and_opencode_go_routes_from_keychain(tmp_path):
    secrets = MemorySecrets()
    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n\n[models]\ndefault = "opencode-go/kimi-k3"\n\n'
        '[providers.github-copilot]\nauth = "github_copilot"\napi = "chat"\n\n'
        '[providers.opencode-go]\nauth = "keychain"\nbase_url = "https://opencode.ai/zen/go/v1"\napi = "chat"\n',
        encoding="utf-8",
    )
    from nexus.auth.copilot import CopilotAuthManager

    def no_exchange(request):
        raise AssertionError(f"unexpected request {request.url}")

    copilot_http = httpx.AsyncClient(transport=httpx.MockTransport(no_exchange))

    runtime = Runtime(
        tmp_path, home=tmp_path / "home", environ={},
        copilot_auth_factory=lambda **kw: CopilotAuthManager(store=secrets, client=copilot_http, **kw),
        api_key_auth_factory=lambda provider, **kw: StoredKeyAuth(provider, store=secrets, **kw),
    )
    try:
        copilot, go = runtime.providers["github-copilot"], runtime.providers["opencode-go"]
        assert copilot._base_url == "https://api.githubcopilot.com"
        assert go._base_url == "https://opencode.ai/zen/go/v1"
        await secrets.write_secret("opencode-go:default", "sk-go-123456")
        headers = await go._headers({"messages": []})
        assert headers["authorization"] == "Bearer sk-go-123456"
        await secrets.write_secret("github-copilot:default", '{"v":2,"github_token":"gho_x","domain":"github.com"}')
        headers = await copilot._headers({"messages": [{"role": "tool", "content": "done"}]})
        assert headers["authorization"] == "Bearer gho_x" and headers["x-initiator"] == "agent"
    finally:
        await runtime.aclose()
        await copilot_http.aclose()


def test_config_rejects_api_key_on_keychain_providers(tmp_path):
    from nexus.config import Config
    from nexus.errors import ConfigError

    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n\n[providers.opencode-go]\nauth = "keychain"\n'
        'base_url = "https://opencode.ai/zen/go/v1"\napi_key = "sk-inline"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="keychain"):
        Config.load(tmp_path, home=tmp_path / "home", environ={})


@pytest.mark.parametrize("provider,api,base_url", [
    ("github-copilot", "chat", "https://attacker.example"),
    ("github-copilot", "responses", "https://api.githubcopilot.com"),
    ("other", "chat", "https://api.githubcopilot.com"),
])
def test_copilot_route_cannot_send_credentials_to_another_endpoint(tmp_path, provider, api, base_url):
    from nexus.config import ConfigError

    (tmp_path / "nexus.toml").write_text(
        f'config_version = 2\n[models]\ndefault = "{provider}/example"\n'
        f'[providers.{provider}]\nauth = "github_copilot"\napi = "{api}"\nbase_url = "{base_url}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="official chat endpoint"):
        Runtime(tmp_path, home=tmp_path / "home", environ={})


def test_copilot_auth_rejects_non_openai_provider_kind(tmp_path):
    from nexus.config import ConfigError

    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "github-copilot/example"\n'
        '[providers.github-copilot]\nauth = "github_copilot"\nkind = "ollama"\napi = "chat"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        Runtime(tmp_path, home=tmp_path / "home", environ={})
