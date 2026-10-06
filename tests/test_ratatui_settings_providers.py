"""Settings → Providers is one page with a collapsible section per provider."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import protocol as p
from nexus.ui.ratatui.actions import ShellActions


@pytest.fixture
def shell(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    shell = ShellActions(SimpleNamespace(session="s", client=SimpleNamespace()))
    shell.client.providers_status = AsyncMock(return_value=p.ProvidersStatusResult(providers=[
        {"id": "google", "label": "Google", "connected": False, "methods": ["api_key"], "instruction": "Set a key."},
        {"id": "anthropic", "label": "Anthropic", "connected": True, "methods": ["browser", "api_key"], "can_logout": True},
        {"id": "ollama", "label": "Ollama", "connected": False, "methods": [], "detail": "Not running on localhost:11434"},
        {"id": "openai", "label": "OpenAI", "connected": True, "methods": ["api_key"]},
    ]))
    return shell


def _sections(shell):
    return [b for b in shell.workflows.settings_page["blocks"] if b.get("t") == "section"]


def _walk(blocks):
    for block in blocks:
        yield block
        if block.get("t") == "section":
            yield from _walk(block["blocks"])


@pytest.mark.asyncio
async def test_connected_providers_come_first_then_alphabetical_with_a_status_each(shell):
    await shell.workflows.settings_area("providers")
    sections = _sections(shell)
    assert [s["title"] for s in sections] == ["Anthropic", "OpenAI", "Google", "Ollama"]
    assert [s["summary"] for s in sections] == ["connected", "connected", "not connected", "not connected"]
    assert [s["tone"] for s in sections] == ["success", "success", "", ""]
    assert [s["open"] for s in sections] == [True, True, False, False], "connected providers start open; the rest only show their status"
    assert shell.workflows.settings_page["scope"] is None and shell.panel_title == "Settings · Providers"


@pytest.mark.asyncio
async def test_a_specific_provider_can_be_opened_directly(shell):
    await shell.workflows.operate({"kind": "provider", "id": "google"})
    assert next(s for s in _sections(shell) if s["title"] == "Google")["open"] is True


@pytest.mark.asyncio
async def test_each_section_offers_its_methods_and_failures_are_visible(shell):
    await shell.workflows.settings_area("providers")
    sections = {s["title"]: s for s in _sections(shell)}
    anthropic = list(_walk(sections["Anthropic"]["blocks"]))
    buttons = [i["label"] for b in anthropic if b.get("t") == "buttons" for i in b["items"]]
    assert buttons == ["Sign in with browser", "Sign out…"]
    assert any(b.get("t") == "row" and b["label"] == "API key" for b in anthropic)
    google = list(_walk(sections["Google"]["blocks"]))
    assert any(b.get("t") == "note" and b["text"] == "Set a key." for b in google)
    ollama = list(_walk(sections["Ollama"]["blocks"]))
    assert any(b.get("t") == "note" and "Not running" in b["text"] and b["tone"] == "warning" for b in ollama)
    assert not any(b.get("t") == "row" for b in ollama), "a provider with no methods has nothing to configure"


@pytest.mark.asyncio
async def test_api_key_fields_are_masked_and_never_prefilled(shell):
    await shell.workflows.settings_area("providers")
    keys = [b for s in _sections(shell) for b in _walk(s["blocks"]) if b.get("t") == "row" and b["label"] == "API key"]
    assert len(keys) == 3 and all(b["control"]["secret"] and b["control"]["value"] == "" for b in keys)
    assert {b["control"]["operation"]["provider"] for b in keys} == {"anthropic", "openai", "google"}


@pytest.mark.asyncio
async def test_signing_out_asks_first_and_returns_to_the_page(shell):
    shell.client.provider_logout = AsyncMock(return_value=SimpleNamespace(message="Signed out of OpenAI"))
    await shell.workflows.settings_area("providers")
    openai = next(s for s in _sections(shell) if s["title"] == "OpenAI")
    ask = next(i["operation"] for b in _walk(openai["blocks"]) if b.get("t") == "buttons" for i in b["items"] if i["label"] == "Sign out…")
    await shell.workflows.operate(ask)
    assert shell.panel_title == "Sign out of OpenAI?"
    shell.client.provider_logout.assert_not_awaited()
    await shell.workflows.operate(shell.items[1]["operation"])
    shell.client.provider_logout.assert_awaited_once_with("openai")
    assert shell.panel_title == "Settings · Providers" and shell.toasts[-1]["title"] == "Signed out of OpenAI"
