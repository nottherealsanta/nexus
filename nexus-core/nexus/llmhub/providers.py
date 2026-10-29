"""The provider table: which wire dialect each provider speaks, where, and its
quirks as data (PLAN.md step 3).

| id       | dialect   | endpoint                              | models.dev id  |
| -------- | --------- | ------------------------------------- | -------------- |
| codex    | responses | chatgpt.com/backend-api/codex         | openai         |
| grok     | responses | cli-chat-proxy.grok.com/v1            | xai            |
| copilot  | chat      | api.githubcopilot.com                 | github-copilot |
| opencode | chat      | opencode.ai/zen/go/v1 (OpenCode Go)   | opencode-go    |

Credentials are not here: `make()` takes a header source from `nexus.llmhub.auth`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Literal

from . import Provider
from .chat import ChatProvider, ChatQuirks
from .responses import HeaderSource, ResponsesProvider, ResponsesQuirks


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    id: str
    label: str
    dialect: Literal["responses", "chat", "scripted"]
    base_url: str
    catalog_id: str  # provider id on models.dev
    quirks: ResponsesQuirks | ChatQuirks
    default_model: str
    verified: bool  # a live multi-step tool turn has run through this adapter


PROVIDERS: dict[str, ProviderInfo] = {
    "codex": ProviderInfo(
        "codex",
        "ChatGPT (Codex)",
        "responses",
        "https://chatgpt.com/backend-api/codex",
        "openai",
        ResponsesQuirks(encrypted_reasoning=True, replay_raw=True, prompt_cache_key=True, session_header="session_id"),
        default_model="gpt-6.1-terra",
        verified=True,
    ),
    "grok": ProviderInfo(
        "grok",
        "xAI (SuperGrok)",
        "responses",
        "https://cli-chat-proxy.grok.com/v1",
        "xai",
        ResponsesQuirks(session_header="x-grok-conv-id"),
        default_model="grok-4.7",
        verified=True,
    ),
    "copilot": ProviderInfo(
        "copilot",
        "GitHub Copilot",
        "chat",
        "https://api.githubcopilot.com",
        "github-copilot",
        ChatQuirks(initiator_header=True),
        default_model="claude-sonnet-5",
        verified=True,
    ),
    "opencode": ProviderInfo(
        "opencode",
        "OpenCode Go",
        "chat",
        "https://opencode.ai/zen/go/v1",
        "opencode-go",
        ChatQuirks(session_header="x-opencode-session"),
        default_model="kimi-k3",
        verified=True,
    ),
}


if os.environ.get("NEXUS10_DEV", "").lower() in {"1", "true", "yes", "on"}:
    PROVIDERS["mock"] = ProviderInfo(
        "mock", "Offline mock", "scripted", "mock://", "mock", ChatQuirks(), default_model="hello", verified=True
    )


def copilot_uses_responses(model: str | None) -> bool:
    """Copilot serves its GPT models on `/responses` only (`/chat/completions` answers
    `unsupported_api_for_model`). models.dev marks those with `provider.npm = @ai-sdk/openai`."""
    if not model:
        return False
    try:
        from .catalog import cache_path

        entry = json.loads(cache_path().read_text())["github-copilot"]["models"][model]
        return (entry.get("provider") or {}).get("npm") == "@ai-sdk/openai"
    except (OSError, ValueError, KeyError, TypeError):
        return model.startswith(("gpt-5.", "gpt-6", "grok-", "mai-"))


def make(provider_id: str, auth: HeaderSource, model: str | None = None) -> Provider:
    info = PROVIDERS[provider_id]
    if info.dialect == "scripted":
        from ..mock.provider import MockProvider

        return MockProvider(model or info.default_model)
    if provider_id == "copilot" and copilot_uses_responses(model):
        return ResponsesProvider(info.id, info.base_url, auth, ResponsesQuirks(initiator_header=True))
    if isinstance(info.quirks, ResponsesQuirks):
        return ResponsesProvider(info.id, info.base_url, auth, info.quirks)
    return ChatProvider(info.id, info.base_url, auth, info.quirks)
