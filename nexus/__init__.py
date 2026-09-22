"""Nexus public API (plan section 2.2: re-exports only, import cost matters).

Legacy exports stay eager so the pre-Phase-1 ``Agent``/CLI path is unchanged.
Phase 1 exports are resolved lazily through PEP 562 ``__getattr__``: ``import
nexus`` and ``import nexus.errors`` therefore do **not** load ``httpx``, the
Anthropic provider, the runtime, the session layer, ``fcntl``, or other heavy
Phase 1 modules. Accessing ``nexus.Runtime`` (and friends) still works exactly
as if it were a normal attribute.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .agent import Agent
from .config import Config
from .errors import SessionBusy
from .events import Event
from .provider import CodexProvider, Provider, ProviderError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .context import ContextManager
    from .model.router import ModelRouter
    from .runtime import Runtime
    from .session import Session, SessionManager

# NOTE: ``Provider`` here is the legacy Codex prompt-stream protocol exported for
# backwards compatibility. The new protocol lives at ``nexus.model.provider`` and
# is deliberately not re-exported at the root to avoid the name collision.
__all__ = [
    "Agent",
    "CodexProvider",
    "Config",
    "ContextManager",
    "Event",
    "ModelRouter",
    "Provider",
    "ProviderError",
    "Runtime",
    "Session",
    "SessionBusy",
    "SessionManager",
]

#: Phase 1 names, resolved on first access: ``name -> (module, attribute)``.
_LAZY = {
    "ContextManager": (".context", "ContextManager"),
    "ModelRouter": (".model.router", "ModelRouter"),
    "Runtime": (".runtime", "Runtime"),
    "Session": (".session", "Session"),
    "SessionManager": (".session", "SessionManager"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value  # cache for subsequent access
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
