"""Developer tooling that only runs in dev mode (MOCK_PLAN).

Nothing here is imported by the normal product path. ``nexus.runtime`` and
``nexus.host`` import :mod:`nexus.devtools.mock` lazily, and only when dev mode
(``NEXUS_DEV=1`` / ``--dev``) is on.
"""
from __future__ import annotations

import os

__all__ = ["DEV_ENV", "dev_enabled"]

DEV_ENV = "NEXUS_DEV"


def dev_enabled(environ: os._Environ[str] | dict[str, str] | None = None) -> bool:
    """Whether dev mode is on for this process (``NEXUS_DEV`` truthy)."""
    env = os.environ if environ is None else environ
    return str(env.get(DEV_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}
