"""J1–J3: API-key sign-ins, the OpenAI-compatible group and Ollama discovery."""

import asyncio

import httpx
import pytest

from nexus.llmhub import catalog
from nexus.llmhub.auth import SIGN_INS, AuthError, CredentialStore, OllamaAuth
from nexus.llmhub.chat import ChatProvider
from nexus.llmhub.providers import PROVIDERS, make
from nexus.llmhub.responses import ResponsesProvider


@pytest.mark.parametrize("pid,env", [("openai", "OPENAI_API_KEY"), ("groq", "GROQ_API_KEY"),
                                     ("nvidia", "NVIDIA_API_KEY"), ("opencode", "OPENCODE_API_KEY")])
def test_api_key_round_trip(tmp_path, monkeypatch, pid, env):
    monkeypatch.delenv(env, raising=False)
    auth = SIGN_INS[pid](CredentialStore(tmp_path / "auth.json"))
    assert not auth.logged_in()
    with pytest.raises(AuthError, match=env):
        asyncio.run(auth.headers())
    with pytest.raises(AuthError):
        auth.set_key("has space")
    auth.set_key("sk-test-123")
    assert auth.logged_in()
    assert asyncio.run(auth.headers())["authorization"] == "Bearer sk-test-123"
    auth.logout()
    assert not auth.logged_in()


def test_env_key_is_used_without_storing(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env")
    auth = SIGN_INS["groq"](CredentialStore(tmp_path / "auth.json"))
    assert auth.logged_in()
    assert asyncio.run(auth.headers())["authorization"] == "Bearer gsk-env"
    assert not (tmp_path / "auth.json").exists()


def test_dialects():
    assert isinstance(make("openai", SIGN_INS["openai"](), "gpt-6.1-terra"), ResponsesProvider)
    for pid in ("groq", "nvidia", "ollama"):
        assert isinstance(make(pid, SIGN_INS[pid](), "m"), ChatProvider)


def test_ollama_login_stores_host_and_routes_to_v1(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "box:11434")
    auth = OllamaAuth(CredentialStore(tmp_path / "auth.json"))
    assert not auth.logged_in()
    asyncio.run(auth.login(notify=lambda _m: None))
    assert auth.logged_in() and auth.base_url == "http://box:11434"
    provider = make("ollama", auth, "llama3")
    assert provider._base_url == "http://box:11434/v1"
    auth.logout()
    assert not auth.logged_in()


def test_ollama_models_from_tags():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "llama3:8b", "modified_at": "2026-10-01T00:00:00Z"},
                                                    {"name": "nomic-embed-text"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = asyncio.run(catalog.discover_ollama("http://x:1", client=client))
    assert [m.ref for m in models] == ["ollama/llama3:8b"]


def test_ollama_down_lists_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(catalog.discover_ollama("http://x:1", client=client)) == []


def test_slashed_model_ids_split_once():
    from nexus.llmhub.hub import split_ref

    assert split_ref("groq/openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")
    assert "ollama" in PROVIDERS
