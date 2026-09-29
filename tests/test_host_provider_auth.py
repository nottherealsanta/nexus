"""Settings → Providers behind the host boundary: sign-in flows, keys, routes."""
from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

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


class FakeBrowser(FakeDevice):
    async def browser_login(self, *, notify, browser_open):
        browser_open("https://auth.openai.com/oauth/authorize?state=x")
        await self.approved.wait()
        self.signed_in = True


def _runtime(home: Path, copilot=None, codex=None, secrets=None):
    secrets = secrets or MemorySecrets()
    return SimpleNamespace(
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


async def test_status_lists_the_three_providers_without_credentials(home):
    runtime, _ = _runtime(home)
    rows = (await provider_auth.providers_status(runtime))["providers"]
    assert [row["id"] for row in rows] == ["codex", "github-copilot", "opencode-go"]
    assert [row["methods"] for row in rows] == [["browser", "device"], [], ["api_key"]]
    assert not any(row["connected"] for row in rows)


async def test_copilot_sign_in_refuses_opencode_oauth_app(home):
    device = FakeDevice()
    runtime, _ = _runtime(home, copilot=device)
    with pytest.raises(Exception, match="OpenCode"):
        await provider_auth.provider_login(runtime, "github-copilot", domain="Company.GHE.com")
    assert device.domain_seen is None and not device.signed_in
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
    with pytest.raises(Exception, match="OpenCode"):
        await provider_auth.provider_login(runtime, "github-copilot", domain="localhost")


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


async def test_runtime_builds_copilot_and_opencode_go_routes_from_keychain(tmp_path):
    secrets = MemorySecrets()
    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n\n[models]\ndefault = "opencode-go/kimi-k3"\n\n'
        '[providers.github-copilot]\nauth = "github_copilot"\napi = "chat"\n\n'
        '[providers.opencode-go]\nauth = "keychain"\nbase_url = "https://opencode.ai/zen/go/v1"\napi = "chat"\n',
        encoding="utf-8",
    )
    from nexus.auth.copilot import CopilotAuthManager

    runtime = Runtime(
        tmp_path, home=tmp_path / "home", environ={},
        copilot_auth_factory=lambda **kw: CopilotAuthManager(store=secrets, **kw),
        api_key_auth_factory=lambda provider, **kw: StoredKeyAuth(provider, store=secrets, **kw),
    )
    try:
        copilot, go = runtime.providers["github-copilot"], runtime.providers["opencode-go"]
        assert copilot._base_url == "https://api.githubcopilot.com"
        assert go._base_url == "https://opencode.ai/zen/go/v1"
        await secrets.write_secret("opencode-go:default", "sk-go-123456")
        headers = await go._headers({"messages": []})
        assert headers["authorization"] == "Bearer sk-go-123456"
        await secrets.write_secret("github-copilot:default", '{"v":1,"token":"gho_x","domain":"github.com"}')
        headers = await copilot._headers({"messages": [{"role": "tool", "content": "done"}]})
        assert headers["authorization"] == "Bearer gho_x" and headers["x-initiator"] == "agent"
    finally:
        await runtime.aclose()


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
