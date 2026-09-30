"""Provider adapters behind the :class:`nexus.model.provider.Provider` protocol.

One adapter per wire protocol (plan section 8):

* :class:`~nexus.model.providers.anthropic.AnthropicProvider` — the reference
  streaming HTTP adapter for the Anthropic Messages API.
* :class:`~nexus.model.providers.openai.OpenAIProvider` — OpenAI and **every**
  OpenAI-compatible endpoint (Responses and Chat Completions dialects) plus the
  Codex models, which are Responses-API models.
* :class:`~nexus.model.providers.gemini.GeminiProvider` — the Google Gemini
  ``generateContent`` adapter. Its wire protocol differs enough from Anthropic
  that it is the adapter that proves the IR carries a second dialect.
* :class:`~nexus.model.providers.ollama.OllamaProvider` — Ollama and the
  llama.cpp server, native NDJSON or an OpenAI-compatible endpoint.
* :class:`~nexus.model.providers.scripted.ScriptedProvider` — a deterministic,
  offline provider for tests and examples.

A vendor that already speaks one of these protocols is added with a config
block; a new provider object can be dropped in ``.nexus/providers/`` and is
loaded by :mod:`nexus.model.providers.discovery` with quarantine. The package
is imported only by code that actually constructs an adapter, so ``import
nexus`` stays lazy (plan section 2.2).

Deliberate omission: there is **no raw GitHub Copilot adapter**. Its OAuth
token-exchange endpoint and the licence terms for non-editor clients are
unresolved (plan assumption #1), so shipping one would mean guessing at both an
endpoint and a legal boundary. OpenCode is integrated **only** over its
documented ACP subprocess surface (:class:`OpenCodeProvider`); Nexus never reads
its credential store.
"""
from .anthropic import AnthropicProvider
from .claude_agent import ClaudeAgentProvider
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider
from .opencode import OpenCodeProvider, SubprocessAgentProvider
from .scripted import ScriptedProvider

__all__ = [
    "AnthropicProvider",
    "ClaudeAgentProvider",
    "GeminiProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "OpenCodeProvider",
    "ScriptedProvider",
    "SubprocessAgentProvider",
]
