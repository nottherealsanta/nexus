"""Codex device sign-in: the browser opens the page that accepts the code."""
import asyncio
import json

import httpx

from nexus.llmhub.auth import codex


class Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        if url.endswith("/api/accounts/deviceauth/usercode"):
            return httpx.Response(200, json={"user_code": "AB-12", "device_auth_id": "dev-1", "interval": "1"})
        if url.endswith("/api/accounts/deviceauth/token"):
            return httpx.Response(200, json={"authorization_code": "code-1", "code_verifier": "verifier-1"})
        if url.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "access", "refresh_token": "refresh"})
        raise AssertionError(f"unexpected URL {url}")


def test_device_login_opens_the_code_entry_page(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    told: list[str] = []

    async def skip(_seconds):
        return None

    monkeypatch.setattr(codex.asyncio, "sleep", skip)
    asyncio.run(codex.CodexAuth().device_login(told.append))

    assert told == ["Open https://auth.openai.com/codex/device and enter code AB-12"]
    stored = json.loads((tmp_path / "home" / "auth.json").read_text())
    assert stored["codex"]["refresh_token"] == "refresh"
