"""Provider adapters.

Discovery and entry-point registration arrive with the provider breadth phase;
for now the package exposes the one adapter that wraps today's Codex route.
"""
from .legacy_codex_cli import LegacyCodexCLIProvider, request_to_prompt

__all__ = ["LegacyCodexCLIProvider", "request_to_prompt"]
