"""Nexus public API (PLAN section 2.2: re-exports only, import cost matters).

The root package exposes the stable *contracts* a host application composes on:

* :class:`~nexus.events.Event` — the UI boundary (eager, dependency-free);
* :class:`~nexus.config.Config` — the layered configuration value;
* :class:`~nexus.runtime.Runtime` — the object that owns the managers and wiring;
* :class:`~nexus.session.Session` / :class:`~nexus.session.SessionManager`;
* :class:`~nexus.context.ContextManager`;
* :class:`~nexus.model.router.ModelRouter`;
* :class:`~nexus.host.HostFacade` — the transport-neutral surface;
* :class:`~nexus.view.ConversationView` and its pure reducer.

Everything heavy resolves lazily through PEP 562 ``__getattr__``, so ``import
nexus`` and ``import nexus.errors`` do not load ``httpx``, the provider
adapters, the runtime, the session layer, ``fcntl``, ``nexus.host``, or the
``view``/``model`` layers. Accessing ``nexus.Runtime`` (and friends) still works
exactly as if it were a normal attribute, and the resolved name is cached.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import SessionBusy
from .events import Event

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .config import Config
    from .context import ContextManager
    from .host import HostFacade
    from .model.router import ModelRouter
    from .runtime import Runtime
    from .session import Session, SessionManager
    from .view import ConversationView, apply

__all__ = [
    "Config",
    "ContextManager",
    "ConversationView",
    "Event",
    "HostFacade",
    "ModelRouter",
    "Runtime",
    "Session",
    "SessionBusy",
    "SessionManager",
    "apply",
]

#: Lazily resolved names: ``name -> (module, attribute)``.
_LAZY = {
    "Config": (".config", "Config"),
    "ContextManager": (".context", "ContextManager"),
    "ConversationView": (".view", "ConversationView"),
    "HostFacade": (".host", "HostFacade"),
    "ModelRouter": (".model.router", "ModelRouter"),
    "Runtime": (".runtime", "Runtime"),
    "Session": (".session", "Session"),
    "SessionManager": (".session", "SessionManager"),
    "apply": (".view", "apply"),
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
