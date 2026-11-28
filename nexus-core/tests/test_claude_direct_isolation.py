import dataclasses

import pytest

from nexus.llmhub.anthropic import AnthropicProvider
from nexus.llmhub.auth import SIGN_INS
from nexus.llmhub.auth.claude import ClaudeAuth
from nexus.llmhub.auth.claude_direct import ClaudeDirectAuth
from nexus.llmhub.catalog import models_for
from nexus.llmhub.chat import ChatProvider
from nexus.llmhub.claude import ClaudeProvider
from nexus.llmhub.providers import make
from nexus.llmhub.responses import ResponsesProvider
from nexus.llmhub.types import Request


class Auth:
    async def headers(self):
        return {}


@pytest.mark.parametrize("provider,kind", [("claude", ClaudeProvider), ("codex", ResponsesProvider),
    ("openai", ResponsesProvider), ("grok", ResponsesProvider), ("copilot", ChatProvider),
    ("opencode", ChatProvider), ("groq", ChatProvider), ("nvidia", ChatProvider), ("ollama", ChatProvider),
    ("claude-direct", AnthropicProvider)])
def test_existing_provider_routes_are_independent(provider, kind, monkeypatch, tmp_path):
    monkeypatch.setattr("nexus.llmhub.catalog.cache_path", lambda: tmp_path / "none.json")
    assert type(make(provider, Auth(), "claude-haiku-4-5")) is kind


def test_shared_request_shape_and_sdk_auth_are_unchanged():
    assert [f.name for f in dataclasses.fields(Request)] == [
        "model", "instructions", "messages", "tools", "reasoning_effort", "conversation_id"]
    assert SIGN_INS["claude"] is ClaudeAuth and ClaudeAuth.methods == ("cli",)
    assert not issubclass(ClaudeDirectAuth, ClaudeAuth)


def test_sdk_and_other_catalog_entries_do_not_inherit_direct_policy():
    catalog = {"anthropic": {"models": {"claude-haiku-4-5": {
        "name": "Haiku", "tool_call": True, "reasoning": True, "reasoning_options": [{"type": "effort", "values": ["low", "high"]}]}}}}
    [sdk] = models_for(catalog, "claude")
    [direct] = models_for(catalog, "claude-direct")
    assert sdk.name == "Haiku" and sdk.efforts == ("low", "high")
    assert direct.name == "Direct · Haiku" and direct.efforts != sdk.efforts


def test_direct_discovery_fails_closed_for_damaged_credentials(tmp_path, monkeypatch):
    from nexus.llmhub.auth.store import CredentialStore

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    store = CredentialStore(tmp_path / "auth.json")
    store.write("claude-direct", {"access_token": "sk-ant-oat-test", "expires_at": "invalid"})
    assert not ClaudeDirectAuth(store).logged_in()
    store.path.write_text("not JSON")
    assert not ClaudeDirectAuth(store).logged_in()


def test_direct_default_store_is_separate_and_no_env_autoconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-env")
    auth = ClaudeDirectAuth()
    assert auth.store.path == tmp_path / "claude-direct" / "auth.json"
    assert not auth.logged_in()
    assert auth._token() == "sk-ant-oat-env"  # explicit direct inference still accepts the environment token


@pytest.mark.asyncio
async def test_direct_lock_does_not_wait_for_shared_provider_lock(tmp_path):
    import asyncio

    from nexus.llmhub.auth.store import CredentialStore

    other = CredentialStore(tmp_path / "auth.json")
    direct = ClaudeDirectAuth(CredentialStore(tmp_path / "direct" / "auth.json"))
    async with other.alocked():
        async with asyncio.timeout(1):
            async with direct.store.alocked():
                direct.store.write("claude-direct", {"source": "test"})
