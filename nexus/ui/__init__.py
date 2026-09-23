"""UI adapters (plan layer L5).

This package is intentionally import-light: importing it must not pull the
Runtime, httpx, or the provider adapters. The canonical terminal surface is
:mod:`nexus.ui.cli`, a pure client of the workspace daemon; ``nexus.cli``
imports it lazily only when an interactive or one-shot command is selected, so
``nexus --help`` stays free of the Phase 1/2 machinery. :mod:`nexus.ui.jsonl`
is the standalone JSONL writer.
"""
from __future__ import annotations

__all__: list[str] = []
