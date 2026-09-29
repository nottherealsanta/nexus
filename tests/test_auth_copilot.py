"""GitHub Copilot stored tokens and refused sign-in (no network, no keychain)."""
from __future__ import annotations

import json

import pytest

from nexus.auth.api_key import StoredKeyAuth, validate_api_key
from nexus.auth.copilot import (
    CopilotAuthManager,
    CopilotHeaders,
    api_base_url,
    normalize_domain,
)
from nexus.auth.store import validate_account
from nexus.errors import ProviderError


class MemorySecrets:
    def __init__(self):
        self.values: dict[str, str] = {}

    async def read_secret(self, account):
        validate_account(account)
        return self.values.get(account)

    async def write_secret(self, account, value):
        validate_account(account)
        self.values[account] = value

    async def delete_secret(self, account):
        self.values.pop(account, None)


def test_domain_normalization_and_endpoints():
    assert normalize_domain("") == "github.com"
    assert normalize_domain("https://Company.GHE.com/") == "company.ghe.com"
    assert api_base_url(None) == "https://api.githubcopilot.com"
    assert api_base_url("company.ghe.com") == "https://copilot-api.company.ghe.com"
    for bad in ("localhost", "evil.com/../x y", "a..b", "http://"):
        with pytest.raises(ValueError):
            normalize_domain(bad)


async def test_stored_token_is_sent_as_bearer_and_sign_in_is_refused():
    store = MemorySecrets()
    store.values["github-copilot:default"] = json.dumps({"v": 1, "token": "gho_token", "domain": "github.com"})
    manager = CopilotAuthManager(store=store)

    assert await manager.status() and await manager.domain() == "github.com"
    headers = await manager.headers()
    assert headers["authorization"] == "Bearer gho_token"
    assert "gho_token" not in repr(manager)
    with pytest.raises(ProviderError, match="OpenCode"):
        await manager.device_login(on_code=lambda *_: None)
    assert store.values["github-copilot:default"]
    await manager.logout()
    assert not await manager.status()
    with pytest.raises(ProviderError, match="OpenCode"):
        await manager.headers()


def test_copilot_request_headers_mark_follow_ups_as_agent_initiated():
    headers = CopilotHeaders(CopilotAuthManager(store=MemorySecrets()))
    assert headers.request_headers({"messages": [{"role": "user", "content": "hi"}]}) == {"x-initiator": "user"}
    follow_up = {"messages": [{"role": "user", "content": "hi"}, {"role": "tool", "content": "ok"}]}
    assert headers.request_headers(follow_up) == {"x-initiator": "agent"}
    image = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]}
    assert headers.request_headers(image) == {"x-initiator": "user", "copilot-vision-request": "true"}


async def test_stored_api_key_round_trip_never_echoes_value():
    store = MemorySecrets()
    auth = StoredKeyAuth("opencode-go", store=store)
    assert not await auth.status()
    with pytest.raises(ProviderError, match="Settings"):
        await auth.headers()
    await auth.save("  sk-go-123456  ")
    assert store.values == {"opencode-go:default": "sk-go-123456"}
    assert await auth.headers() == {"authorization": "Bearer sk-go-123456"}
    for bad in ("short", "has space inside", "x" * 600, None):
        with pytest.raises(ValueError) as caught:
            validate_api_key(bad)
        assert "has space" not in str(caught.value)
    await auth.logout()
    assert not await auth.status()
