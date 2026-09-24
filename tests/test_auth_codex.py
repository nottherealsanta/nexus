from __future__ import annotations

import asyncio
import fcntl
import json
import os
from contextlib import asynccontextmanager

import httpx
import pytest

from nexus.auth.codex import CLIENT_ID, CodexOAuthManager, routing_metadata
from nexus.auth.store import CredentialRecord, KeyringCredentialStore
from nexus.errors import ProviderError


class MemoryStore:
    def __init__(self, record=None):
        self.record = record
        self.writes = []

    async def read(self, profile):
        return self.record

    async def write(self, profile, record):
        self.writes.append(record)
        self.record = record

    async def delete(self, profile):
        self.record = None

    @asynccontextmanager
    async def lock(self, profile):
        yield


def _jwt(claims=None):
    import base64

    body = base64.urlsafe_b64encode(json.dumps(claims or {}).encode()).decode().rstrip("=")
    return f"x.{body}.x"


def test_routing_metadata_uses_pinned_account_precedence_and_access_residency():
    nested = "https://api.openai.com/auth"
    assert routing_metadata(_jwt({nested: {"chatgpt_account_id": "nested"}}), None) == ("nested", None)
    assert routing_metadata(_jwt({"chatgpt_account_id": "top", nested: {"chatgpt_account_id": "nested"}}), None) == ("top", None)
    assert routing_metadata(_jwt({"organizations": [{"id": "organization"}]}), None) == ("organization", None)
    assert routing_metadata(_jwt({"chatgpt_account_id": "identity"}), _jwt({"chatgpt_account_id": "access"})) == ("identity", None)
    assert routing_metadata(_jwt({"chatgpt_account_id": "identity"}), _jwt({nested: {"chatgpt_compute_residency": "eu"}})) == ("identity", "eu")
    assert routing_metadata(None, _jwt({"chatgpt_compute_residency": "us"})) == (None, "us")
    assert routing_metadata(None, _jwt({nested: {"chatgpt_compute_residency": "no_constraint"}})) == (None, None)
    assert routing_metadata(None, _jwt({nested: {"chatgpt_compute_residency": ""}})) == (None, None)


async def test_login_without_account_persists_and_omits_account_header():
    store = MemoryStore()
    manager = CodexOAuthManager(store=store)
    await manager._persist_login({"refresh_token": "refresh", "access_token": "access", "expires_in": 3600})
    assert store.record.account_id is None

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"access_token": "access", "expires_in": 3600})))
    headers = await CodexOAuthManager(store=store, client=client).headers()
    assert "ChatGPT-Account-Id" not in headers


async def test_refresh_preserves_account_but_clears_stale_residency():
    store = MemoryStore(CredentialRecord("refresh", "existing", residency="eu"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"access_token": _jwt({}), "expires_in": 3600})))
    headers = await CodexOAuthManager(store=store, client=client).headers()
    assert store.record.account_id == "existing"
    assert store.record.residency is None
    assert headers["ChatGPT-Account-Id"] == "existing"
    assert "x-openai-internal-codex-residency" not in headers


def test_credential_record_serializes_string_and_null_account_ids():
    string_record = CredentialRecord.parse('{"v":1,"refresh_token":"secret-refresh-token","account_id":"account","residency":null,"generation":0}')
    null_record = CredentialRecord.parse('{"v":1,"refresh_token":"secret-refresh-token","account_id":null,"residency":null,"generation":0}')
    assert string_record.account_id == "account"
    assert null_record.account_id is None
    assert json.loads(null_record.serialize())["account_id"] is None
    assert "secret-refresh-token" not in repr(null_record)


async def test_device_flow_uses_pinned_request_bodies_and_returned_verifier(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/usercode"):
            return httpx.Response(200, json={"device_auth_id": "device", "user_code": "USER", "interval": 0.001})
        if request.url.path.endswith("/deviceauth/token"):
            return httpx.Response(200, json={"authorization_code": "auth", "code_verifier": "returned-verifier"})
        return httpx.Response(200, json={"refresh_token": "refresh", "access_token": "access", "id_token": _jwt(), "expires_in": 3600})

    original_sleep = asyncio.sleep

    async def immediate_sleep(_delay):
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", immediate_sleep)
    store = MemoryStore()
    manager = CodexOAuthManager(store=store, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await manager.device_login(notify=lambda _message: None)
    assert json.loads(calls[0].content) == {"client_id": CLIENT_ID}
    assert json.loads(calls[1].content) == {"device_auth_id": "device", "user_code": "USER"}
    token = dict(item.split("=", 1) for item in calls[2].content.decode().split("&"))
    assert token["code_verifier"] == "returned-verifier"
    assert token["redirect_uri"] == "https%3A%2F%2Fauth.openai.com%2Fdeviceauth%2Fcallback"


async def test_invalid_refresh_response_does_not_rotate_stored_credential():
    old = CredentialRecord("old-refresh", "acct", generation=4)
    store = MemoryStore(old)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"refresh_token": "new-refresh", "expires_in": 3600})))
    with pytest.raises(ProviderError, match="access token"):
        await CodexOAuthManager(store=store, client=client).headers()
    assert store.record == old
    assert store.writes == []


@pytest.mark.parametrize("payload", [{"expires_in": "nan"}, {"expires_in": 0}, {"expires_in": "secret-body"}])
async def test_invalid_oauth_expiry_is_redacted(payload):
    old = CredentialRecord("old-refresh", "acct")
    payload.update({"access_token": "access-secret", "refresh_token": "new-refresh"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError) as error:
        await CodexOAuthManager(store=MemoryStore(old), client=client).headers()
    assert "access-secret" not in str(error.value)
    assert "secret-body" not in str(error.value)


@pytest.mark.skipif(not hasattr(__import__("os"), "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
async def test_oauth_lock_refuses_symlink_and_hides_profile(tmp_path):
    store = KeyringCredentialStore(lock_dir=tmp_path)
    import hashlib

    path = tmp_path / f"codex-{hashlib.sha256(b'default').hexdigest()}.lock"
    path.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        async with store.lock("default"):
            pass
    assert "default" not in path.name


async def test_oauth_lock_times_out_and_cancellation_releases_descriptor(tmp_path):
    store = KeyringCredentialStore(lock_dir=tmp_path, lock_timeout=0.01)
    import hashlib

    path = tmp_path / f"codex-{hashlib.sha256(b'default').hexdigest()}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(ProviderError, match="busy"):
            async with store.lock("default"):
                pass
        task = asyncio.create_task(store.lock("default").__aenter__())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    async with store.lock("default"):
        pass


async def test_browser_denial_resolves_promptly_without_reflecting_provider_values(monkeypatch):
    callback = None

    class Server:
        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    async def start_server(handler, **_kwargs):
        nonlocal callback
        callback = handler
        return Server()

    class Writer:
        def __init__(self):
            self.data = b""

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    monkeypatch.setattr(asyncio, "start_server", start_server)

    def browser_open(_url):
        async def deny():
            reader = asyncio.StreamReader()
            reader.feed_data(b"GET /auth/callback?error=access_denied&error_description=secret-description HTTP/1.1\r\n")
            reader.feed_eof()
            writer = Writer()
            await callback(reader, writer)
            assert b"secret-description" not in writer.data
        asyncio.create_task(deny())
        return True

    manager = CodexOAuthManager(store=MemoryStore())
    with pytest.raises(ProviderError, match="denied"):
        await manager.browser_login(browser_open=browser_open)


def test_keyring_chain_requires_only_secure_native_backends():
    Mac = type("Keyring", (), {"priority": 1})
    Mac.__module__ = "keyring.backends.macos"
    File = type("Keyring", (), {"priority": 1})
    File.__module__ = "keyring.backends.file"
    Chain = type("ChainerBackend", (), {"priority": 1})
    Chain.__module__ = "keyring.backends.chainer"
    assert KeyringCredentialStore._secure_backend(Chain()) is False
    secure_chain = Chain()
    secure_chain.backends = [Mac()]
    assert KeyringCredentialStore._secure_backend(secure_chain) is True
    mixed_chain = Chain()
    mixed_chain.backends = [Mac(), File()]
    assert KeyringCredentialStore._secure_backend(mixed_chain) is False
