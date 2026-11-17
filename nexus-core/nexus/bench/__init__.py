"""Synthetic benchmark fixtures and `nexus bench` (plan 50, Phase 0)."""
from __future__ import annotations


def enable_mock_provider() -> None:
    """Register the offline `mock` provider in this process (normally only with `NEXUS10_DEV=1`)."""
    from ..llmhub.providers import PROVIDERS, ChatQuirks, ProviderInfo
    PROVIDERS.setdefault("mock", ProviderInfo(
        "mock", "Offline mock", "scripted", "mock://", "mock", ChatQuirks(), verified=True))
