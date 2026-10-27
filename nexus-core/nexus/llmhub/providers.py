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

from dataclasses import dataclass
from typing import Literal

from . import Provider
from .chat import ChatProvider, ChatQuirks
from .responses import HeaderSource, ResponsesProvider, ResponsesQuirks


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    id: str
    label: str
    dialect: Literal["responses", "chat"]
    base_url: str
    catalog_id: str  # provider id on models.dev
    quirks: ResponsesQuirks | ChatQuirks
    default_model: str
    verified: bool  # a live multi-step tool turn has run through this adapter


PROVIDERS: dict[str, ProviderInfo] = {
    "codex": ProviderInfo(
        "codex", "ChatGPT (Codex)", "responses", "https://chatgpt.com/backend-api/codex", "openai",
        ResponsesQuirks(encrypted_reasoning=True, replay_raw=True, prompt_cache_key=True, session_header="session_id"),
        default_model="gpt-6.1-terra", verified=True,
    ),
    "grok": ProviderInfo(
        "grok", "xAI (SuperGrok)", "responses", "https://cli-chat-proxy.grok.com/v1", "xai",
        ResponsesQuirks(session_header="x-grok-conv-id"),
        default_model="grok-4.7", verified=True,
    ),
    "copilot": ProviderInfo(
        "copilot", "GitHub Copilot", "chat", "https://api.githubcopilot.com", "github-copilot",
        ChatQuirks(initiator_header=True),
        default_model="claude-sonnet-5", verified=True,
    ),
    "opencode": ProviderInfo(
        "opencode", "OpenCode Go", "chat", "https://opencode.ai/zen/go/v1", "opencode-go",
        ChatQuirks(session_header="x-opencode-session"),
        default_model="kimi-k3", verified=True,
    ),
}


def make(provider_id: str, auth: HeaderSource) -> Provider:
    info = PROVIDERS[provider_id]
    if isinstance(info.quirks, ResponsesQuirks):
        return ResponsesProvider(info.id, info.base_url, auth, info.quirks)
    return ChatProvider(info.id, info.base_url, auth, info.quirks)
