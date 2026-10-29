"""Copilot serves its GPT models on /responses and everything else on /chat/completions."""

from nexus.llmhub.chat import ChatProvider
from nexus.llmhub.providers import copilot_uses_responses, make
from nexus.llmhub.responses import ResponsesProvider


class _Auth:
    async def headers(self):
        return {}


def test_copilot_gpt_models_use_responses(monkeypatch, tmp_path):
    monkeypatch.setattr("nexus.llmhub.catalog.cache_path", lambda: tmp_path / "none.json")
    assert copilot_uses_responses("gpt-6-luna")
    assert not copilot_uses_responses("claude-sonnet-5")
    assert isinstance(make("copilot", _Auth(), "gpt-6-luna"), ResponsesProvider)
    assert isinstance(make("copilot", _Auth(), "claude-sonnet-5"), ChatProvider)


def test_enabled_only_override_keeps_default_command():
    from nexus.mcp.config import with_defaults

    merged = with_defaults({"browser-use": {"enabled": True}})
    assert merged["browser-use"]["enabled"] is True
    assert merged["browser-use"]["command"] == "uvx"
    assert with_defaults({"x": {"url": "http://a"}})["x"] == {"url": "http://a"}
