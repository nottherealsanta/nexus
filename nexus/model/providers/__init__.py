"""Provider adapters behind the :class:`nexus.model.provider.Provider` protocol.

Phase 1 exposes three:

* :class:`~nexus.model.providers.anthropic.AnthropicProvider` — the reference
  streaming HTTP adapter for the Anthropic Messages API.
* :class:`~nexus.model.providers.scripted.ScriptedProvider` — a deterministic,
  offline provider for tests and examples.
* :class:`~nexus.model.providers.legacy_codex_cli.LegacyCodexCLIProvider` — the
  retiring facade over the legacy Codex CLI subprocess route, kept working until
  the provider-breadth phase removes it.

Discovery and entry-point registration arrive with that same phase.
"""
from .anthropic import AnthropicProvider
from .legacy_codex_cli import LegacyCodexCLIProvider, request_to_prompt
from .scripted import ScriptedProvider

__all__ = [
    "AnthropicProvider",
    "LegacyCodexCLIProvider",
    "ScriptedProvider",
    "request_to_prompt",
]
