"""Runtime composition root (plan section 2.1, layer L4).

``Runtime`` wires the Phase 1 pieces together — configuration, providers, the
model router, the context manager, and the session manager — without changing
the pre-existing ``Agent``/CLI path. It is additive: nothing imports it by
default, and the CLI keeps using :class:`nexus.agent.Agent`.

Design rules:

* **Injectable seams.** Tests (and later phases) can pass a ``providers`` map, a
  ``router``/resolver, a ``context`` assembler, or a ``sessions`` manager. When a
  dependency is injected, the runtime does not own or replace it.
* **Owned lifecycle.** Providers the runtime builds from configuration are owned
  and closed by :meth:`aclose`; injected providers are the caller's to close.
* **Secrets resolve at use only.** Provider credential references such as
  ``${env:ANTHROPIC_API_KEY}`` are stored as opaque strings and handed to the
  adapter, which resolves them at request time. The runtime never reads, logs,
  or persists a secret value.
* **Per-turn configuration.** ``Config`` is re-read through a loader on every
  request, so a turn never inherits stale configuration or context.

It deliberately does not replace the legacy agent, implement fallback routing,
or expose any Phase 2+ surface.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Self

from .config import Config
from .context import ContextManager
from .core.turn import TurnLimits
from .model.provider import Provider
from .model.providers.anthropic import AnthropicProvider
from .model.router import ModelRouter
from .session import Session, SessionManager

__all__ = ["Runtime"]


class Runtime:
    """Owns the Phase 1 managers and hands out sendable sessions."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        home: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
        config: Config | None = None,
        config_loader: Callable[[], Config] | None = None,
        providers: Mapping[str, Provider] | None = None,
        router: Any | None = None,
        context: Any | None = None,
        sessions: SessionManager | None = None,
        session_dir: str | Path | None = None,
        limits: TurnLimits | Callable[[], TurnLimits] | None = None,
        http_transport: Any | None = None,
        client: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._home = Path(home) if home is not None else None
        self._environ = environ
        self._config = config
        self._config_loader = config_loader
        self._http_transport = http_transport
        self._client = client
        self._closed = False

        # Load the effective config at most once during construction: the
        # provider set and the router must agree on one snapshot. Per-turn
        # reloading is unaffected (ContextManager keeps its own loader).
        initial: Config | None = None
        if providers is not None:
            self._providers: dict[str, Provider] = dict(providers)
            # Injected providers are owned by the caller, never closed here.
            self._owned_providers: list[Provider] = []
        else:
            initial = self._load_config()
            self._providers = self._build_providers(initial)
            self._owned_providers = list(self._providers.values())

        if router is not None:
            self._router = router
        else:
            if initial is None:
                initial = self._load_config()
            self._router = self._build_router(initial)
        self._context = (
            context
            if context is not None
            else ContextManager(self.workspace, config_loader=self._load_config)
        )
        if sessions is not None:
            self._sessions = sessions
        else:
            directory = (
                Path(session_dir)
                if session_dir is not None
                else self.workspace / ".nexus" / "sessions"
            )
            self._sessions = SessionManager(
                directory,
                assemble=self._context,
                provider_for=self._router,
                limits=limits if limits is not None else self._limits_from_config,
            )

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, workspace: str | Path, **kwargs: Any) -> Runtime:
        """Convenience constructor; open a runtime rooted at ``workspace``."""
        return cls(workspace, **kwargs)

    # -- introspection -----------------------------------------------------

    @property
    def providers(self) -> dict[str, Provider]:
        return dict(self._providers)

    @property
    def router(self) -> Any:
        return self._router

    @property
    def context(self) -> Any:
        return self._context

    @property
    def sessions(self) -> SessionManager:
        return self._sessions

    @property
    def closed(self) -> bool:
        return self._closed

    # -- sessions ----------------------------------------------------------

    def session(
        self, session_id: str, *, create: bool = True, recover: bool = True
    ) -> Session:
        """Open (and migrate, and recover) a session ready for ``send``."""
        return self._sessions.open(session_id, create=create, recover=recover)

    # -- configuration -----------------------------------------------------

    def _load_config(self) -> Config:
        if self._config_loader is not None:
            return self._config_loader()
        if self._config is not None:
            return self._config
        kwargs: dict[str, Any] = {}
        if self._home is not None:
            kwargs["home"] = self._home
        if self._environ is not None:
            kwargs["environ"] = self._environ
        return Config.load(self.workspace, **kwargs)

    def _limits_from_config(self) -> TurnLimits:
        config = self._load_config()
        v2 = getattr(config, "v2", None)
        if v2 is None:
            return TurnLimits()
        return TurnLimits(
            max_iterations=v2.agent.max_iterations,
            max_seconds=v2.agent.max_turn_seconds,
        )

    # -- providers / router ------------------------------------------------

    def _build_providers(self, config: Config) -> dict[str, Provider]:
        """Construct the adapters the Phase 1 runtime knows how to own."""
        providers: dict[str, Provider] = {}
        v2 = getattr(config, "v2", None)
        sections = getattr(v2, "providers", None) or {}
        model_ref = getattr(config, "model", None) or ""
        if "anthropic" in sections or model_ref.startswith("anthropic/"):
            providers["anthropic"] = AnthropicProvider.from_config(
                config,
                http_transport=self._http_transport,
                client=self._client,
                environ=self._environ,
            )
        return providers

    def _build_router(self, config: Config) -> ModelRouter:
        aliases: dict[str, str] = {}
        v2 = getattr(config, "v2", None)
        model_section = getattr(v2, "model", None)
        if model_section is not None:
            if model_section.default:
                aliases.setdefault("default", model_section.default)
            if model_section.fast:
                aliases["fast"] = model_section.fast
            if model_section.plan:
                aliases["plan"] = model_section.plan
        return ModelRouter(
            self._providers,
            aliases=aliases,
            default=getattr(config, "model", None),
        )

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Close providers this runtime owns. Idempotent."""
        if self._closed:
            return
        self._closed = True
        for provider in self._owned_providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
        self._owned_providers = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        await self.aclose()
        return False
