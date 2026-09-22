"""UI adapters (plan layer L5).

This package is intentionally import-light: importing it must not pull the
Runtime, httpx, or the Anthropic adapter. The Phase 2 native terminal adapter
lives in :mod:`nexus.ui.native` and is imported lazily by ``nexus.cli`` only
when a ``native-*`` command is invoked, so ``nexus --help`` and the legacy
Codex path stay free of the Phase 1/2 machinery.
"""
from __future__ import annotations

__all__: list[str] = []
