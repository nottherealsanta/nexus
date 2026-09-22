"""Lifecycle hooks (plan section 5.7).

Deterministic behaviour the model cannot skip. A hook attaches to a lifecycle
:class:`~nexus.hooks.model.HookEvent` and returns a
:class:`~nexus.hooks.model.HookDecision` (``allow``/``warn``/``block``/
``modify``). Command hooks are declared in ``.nexus/hooks.toml`` and run as
argv with no shell by default; in-process Python hooks live in
``.nexus/hooks/*.py`` and are loaded through the same quarantine/version-stamped
import seam as tools. The manager is manager-layer (L3) and is never imported by
``core``.
"""

from __future__ import annotations

from .manager import (
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_BYTES,
    MAX_STDIN_BYTES,
    MAX_TIMEOUT_S,
    HookLoadError,
    HookManager,
    HookModule,
    HookModuleLoader,
    HookSet,
)
from .model import (
    HOOK_EVENTS,
    MODIFIABLE_EVENTS,
    TRUSTED_CODE_WARNING,
    HookAction,
    HookContext,
    HookDecision,
    HookError,
    HookEvent,
    HookFailure,
    HookInvocation,
    HookOnNonzero,
    HookOutcome,
    HookSpec,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "HOOK_EVENTS",
    "MAX_OUTPUT_BYTES",
    "MAX_STDIN_BYTES",
    "MAX_TIMEOUT_S",
    "MODIFIABLE_EVENTS",
    "TRUSTED_CODE_WARNING",
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookError",
    "HookEvent",
    "HookFailure",
    "HookInvocation",
    "HookLoadError",
    "HookManager",
    "HookModule",
    "HookModuleLoader",
    "HookOnNonzero",
    "HookOutcome",
    "HookSet",
    "HookSpec",
]
