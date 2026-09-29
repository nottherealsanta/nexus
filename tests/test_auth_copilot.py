"""GitHub Copilot device OAuth and direct-bearer access (mocked HTTP only)."""
from __future__ import annotations

import asyncio
import gzip
import json

import httpx
import pytest

from nexus.auth.api_key import StoredKeyAuth, validate_api_key
from nexus.auth.copilot import (
    ACCESS_TOKEN_URL,
    CLIENT_ID,
    DEVICE_CODE_URL,
    VERIFY_URL,
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


class Clock:
    def __init__(self, value: float = 1_000.0):
        self.value = value

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


def response(payload, status=200):
    return httpx.Response(status, json=payload)


async def test_device_code_decodes_gzip_response_once():
    store, cancel = MemorySecrets(), asyncio.Event()

    def handler(request):
        assert request.url == httpx.URL(DEVICE_CODE_URL)
        body = json.dumps({
            "device_code": "device", "user_code": "CODE", "verification_uri": "https://github.com/login/device",
        }).encode()
        return httpx.Response(200, content=gzip.compress(body), headers={"content-encoding": "gzip"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = CopilotAuthManager(store=store, client=client)
    with pytest.raises(asyncio.CancelledError):
        await manager.device_login(cancel=cancel, on_code=lambda _url, _code: cancel.set())
    assert not store.values
    await client.aclose()


def test_domain_normalization_and_endpoints():
    assert normalize_domain("") == "github.com"
    assert normalize_domain("https://Company.GHE.com/") == "company.ghe.com"
    assert api_base_url(None) == "https://api.githubcopilot.com"
    assert api_base_url("company.ghe.com") == "https://copilot-api.company.ghe.com"
    for bad in ("localhost", "evil.com/../x y", "a..b", "http://"):
        with pytest.raises(ValueError):
            normalize_domain(bad)


def _login_handler(verify=None, seen=None):
    def handler(request):
        if request.url == httpx.URL(DEVICE_CODE_URL):
            return response({"device_code": "device", "user_code": "CODE", "verification_uri": "https://github.com/login/device"})
        if request.url == httpx.URL(ACCESS_TOKEN_URL):
            return response({"access_token": "gho_login"})
        if request.url == httpx.URL(VERIFY_URL):
            if seen is not None:
                seen.append(request)
            return verify(request) if verify else response({"data": []})
        raise AssertionError(request.url)
    return handler


async def test_device_flow_persists_v2_only_after_api_verification():
    store, clock = MemorySecrets(), Clock()
    poll_results = iter([
        {"error": "authorization_pending"},
        {"error": "slow_down"},
        {"access_token": "gho_github_secret", "expires_in": 3600},
    ])
    seen_polls = []

    def handler(request):
        if request.url == httpx.URL(DEVICE_CODE_URL):
            assert request.method == "POST"
            assert request.content == b"client_id=Ov23li8tweQw6odWQebz&scope=read%3Auser"
            assert request.headers["accept"] == "application/json"
            return response({"device_code": "device-secret", "user_code": "ABCD-EFGH", "verification_uri": "https://github.com/login/device/", "expires_in": 60, "interval": 1})
        if request.url == httpx.URL(ACCESS_TOKEN_URL):
            assert f"client_id={CLIENT_ID}".encode() in request.content
            seen_polls.append(clock.value)
            return response(next(poll_results))
        if request.url == httpx.URL(VERIFY_URL):
            assert request.headers["authorization"] == "Bearer gho_github_secret"
            assert not store.values  # verified before anything is saved
            return response({"data": []})
        raise AssertionError(request.url)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    manager = CopilotAuthManager(store=store, client=client, now=clock.now, sleep=clock.sleep)
    code_notices = []
    await manager.device_login(on_code=lambda url, code: code_notices.append((url, code)))
    assert json.loads(store.values[manager.account]) == {"v": 2, "github_token": "gho_github_secret", "domain": "github.com", "expires_at": 4_608.0}
    assert await manager.status() and await manager.domain() == "github.com"
    assert code_notices == [("https://github.com/login/device/", "ABCD-EFGH")]
    assert len(seen_polls) == 3
    # The GitHub OAuth token is the Copilot API bearer; nothing is exchanged.
    headers = await manager.headers()
    assert headers["authorization"] == "Bearer gho_github_secret"
    assert headers["copilot-integration-id"]
    assert "gho_github_secret" not in repr(manager)
    await client.aclose()


async def test_v1_refused_and_logout_removes_access():
    store = MemorySecrets()
    account = "github-copilot:default"
    store.values[account] = json.dumps({"v": 1, "token": "gho_legacy", "domain": "github.com"})
    manager = CopilotAuthManager(store=store)
    assert not await manager.status()
    assert await manager.domain() is None
    with pytest.raises(ProviderError):
        await manager.headers()
    assert "gho_legacy" not in repr(manager)

    store.values[account] = json.dumps({"v": 2, "github_token": "gho_saved", "domain": "github.com"})
    assert (await manager.headers())["authorization"] == "Bearer gho_saved"
    await manager.logout()
    with pytest.raises(ProviderError):
        await manager.headers()


async def test_changed_keychain_record_is_used_immediately():
    store = MemorySecrets()
    account = "github-copilot:default"
    store.values[account] = json.dumps({"v": 2, "github_token": "gho_old", "domain": "github.com"})
    manager = CopilotAuthManager(store=store)
    assert (await manager.headers())["authorization"] == "Bearer gho_old"
    store.values[account] = json.dumps({"v": 2, "github_token": "gho_new", "domain": "github.com"})
    assert (await manager.headers())["authorization"] == "Bearer gho_new"


async def test_github_oauth_expiry_requires_relogin():
    store = MemorySecrets()
    store.values["github-copilot:default"] = json.dumps({"v": 2, "github_token": "gho_old", "domain": "github.com", "expires_at": 999.0})
    manager = CopilotAuthManager(store=store, now=lambda: 1_000)
    with pytest.raises(ProviderError, match="expired"):
        await manager.headers()
    assert await manager.domain() is None


async def test_login_fails_closed_on_403_and_leaks_nothing():
    store = MemorySecrets()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_login_handler(
        lambda _: httpx.Response(403, text="gho_login private detail"))))
    manager = CopilotAuthManager(store=store, client=client, sleep=lambda _: asyncio.sleep(0))
    with pytest.raises(ProviderError, match="403") as error:
        await manager.device_login()
    assert "gho_login" not in str(error.value) and "private detail" not in str(error.value)
    assert not store.values
    await client.aclose()


async def test_login_rejected_token_requests_new_sign_in():
    store = MemorySecrets()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_login_handler(lambda _: httpx.Response(401))))
    manager = CopilotAuthManager(store=store, client=client, sleep=lambda _: asyncio.sleep(0))
    with pytest.raises(ProviderError, match="sign in again"):
        await manager.device_login()
    assert not store.values
    await client.aclose()


async def test_device_login_rejects_enterprise_and_untrusted_verification_uri():
    store = MemorySecrets()
    manager = CopilotAuthManager(store=store)
    with pytest.raises(ProviderError, match="only on GitHub.com"):
        await manager.device_login(domain="company.ghe.com")

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response({
        "device_code": "device", "user_code": "CODE", "verification_uri": "https://github.com.evil.test/login/device",
    })))
    manager._client = client
    with pytest.raises(ProviderError, match="unexpected device verification URL"):
        await manager.device_login()
    assert not store.values
    await client.aclose()


async def test_device_login_cancellation_does_not_persist():
    store, clock, cancel = MemorySecrets(), Clock(), asyncio.Event()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response({
        "device_code": "device", "user_code": "CODE", "verification_uri": "https://github.com/login/device", "interval": 1,
    })))

    async def cancel_sleep(_):
        cancel.set()

    manager = CopilotAuthManager(store=store, client=client, now=clock.now, sleep=cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await manager.device_login(cancel=cancel)
    assert not store.values
    await client.aclose()


async def test_cancel_during_verification_does_not_persist_login():
    store, cancel = MemorySecrets(), asyncio.Event()

    def verify(_):
        cancel.set()
        return response({"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(_login_handler(verify)))
    manager = CopilotAuthManager(store=store, client=client, sleep=lambda _: asyncio.sleep(0))
    with pytest.raises(asyncio.CancelledError):
        await manager.device_login(cancel=cancel)
    assert not store.values
    await client.aclose()


async def test_task_cancel_during_keychain_write_cleans_up_record():
    class PausedSecrets(MemorySecrets):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def write_secret(self, account, value):
            await super().write_secret(account, value)
            self.entered.set()
            await self.release.wait()

    store = PausedSecrets()
    client = httpx.AsyncClient(transport=httpx.MockTransport(_login_handler()))
    manager = CopilotAuthManager(store=store, client=client, sleep=lambda _: asyncio.sleep(0))
    login = asyncio.create_task(manager.device_login())
    await store.entered.wait()
    login.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await login
    assert not store.values
    await client.aclose()


async def test_logout_during_verification_prevents_login_persistence():
    store = MemorySecrets()
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedManager(CopilotAuthManager):
        async def _verify(self, github_token):
            entered.set()
            await release.wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(_login_handler()))
    manager = PausedManager(store=store, client=client, sleep=lambda _: asyncio.sleep(0))
    login = asyncio.create_task(manager.device_login())
    await entered.wait()
    await manager.logout()
    release.set()
    with pytest.raises(ProviderError, match="superseded"):
        await login
    assert not store.values
    await client.aclose()


async def test_verification_never_follows_redirects():
    store, seen = MemorySecrets(), []
    client = httpx.AsyncClient(transport=httpx.MockTransport(_login_handler(
        lambda _: httpx.Response(302, headers={"location": "https://attacker.example/steal"}), seen)),
        follow_redirects=True)
    manager = CopilotAuthManager(store=store, client=client, sleep=lambda _: asyncio.sleep(0))
    with pytest.raises(ProviderError, match="redirect"):
        await manager.device_login()
    assert [r.url for r in seen] == [httpx.URL(VERIFY_URL)]
    assert not store.values
    await client.aclose()


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
