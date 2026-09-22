"""Resolve a configured model reference to a provider, model, and capabilities.

Plan section 8 defines the contract: ``"anthropic/claude-opus-5"`` maps to
``provider="anthropic", model="claude-opus-5"``, and a bare name resolves
through an alias table. Config aliases (``default``, ``fast``, ``plan``) exist so
skills and subagents can request ``model: fast`` and stay portable.

Phase 1 scope is deliberately narrow:

* a fixed, injected ``providers`` map (name -> :class:`~nexus.model.provider.Provider`);
* alias lookup with cycle detection;
* clear rejection of malformed references and of providers that are not
  registered.

There is no fallback chain, no retry-on-provider-failure, and no mid-stream
rerouting; those arrive with provider breadth. ``ModelRouter`` implements the
loop's :class:`~nexus.core.loop.ProviderResolver` protocol structurally, so the
loop still imports no concrete router.
"""
from __future__ import annotations

from collections.abc import Mapping

from ..errors import ConfigError
from .capabilities import Capabilities
from .provider import Provider, ResolvedModel
from .request import ModelRequest

__all__ = ["ModelRouter"]


class ModelRouter:
    """Stateless ``ModelRequest -> ResolvedModel`` resolver."""

    def __init__(
        self,
        providers: Mapping[str, Provider],
        *,
        aliases: Mapping[str, str] | None = None,
        default: str | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._aliases = dict(aliases or {})
        if default is not None and (
            not isinstance(default, str) or not default.strip()
        ):
            raise ConfigError("default model reference must be a nonempty string")
        self._default = default

    # -- introspection -----------------------------------------------------

    @property
    def providers(self) -> dict[str, Provider]:
        return dict(self._providers)

    @property
    def aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    @property
    def default(self) -> str | None:
        return self._default

    # -- ProviderResolver protocol ----------------------------------------

    def resolve(self, request: ModelRequest, /) -> ResolvedModel:
        """Return ``(provider, model, capabilities)`` for ``request``.

        Raises :class:`~nexus.errors.ConfigError` for a missing model, a
        malformed reference, an alias cycle, or an unregistered provider.
        """
        ref = self._resolve_alias(self._reference(request))
        provider_name, model = self._split(ref)

        if provider_name is None:
            provider_name = self._default_provider_name()
        if provider_name is None and len(self._providers) == 1:
            provider_name = next(iter(self._providers))
        if provider_name is None:
            raise ConfigError(
                f"Cannot determine the provider for model reference {ref!r}"
            )

        provider = self._providers.get(provider_name)
        if provider is None:
            known = ", ".join(sorted(self._providers)) or "none"
            raise ConfigError(
                f"Unknown provider {provider_name!r} in model reference {ref!r} "
                f"(registered: {known})"
            )
        capabilities: Capabilities = provider.capabilities(model)
        return ResolvedModel(provider, model, capabilities)

    # -- internals ---------------------------------------------------------

    def _reference(self, request: ModelRequest) -> str:
        if request.provider and request.model:
            return f"{request.provider}/{request.model}"
        if request.provider and not request.model:
            raise ConfigError(
                f"Model reference for provider {request.provider!r} is missing a model"
            )
        if request.model:
            return request.model
        if self._default:
            return self._default
        raise ConfigError(
            "No model was specified and no default model is configured"
        )

    def _resolve_alias(self, ref: str) -> str:
        seen: set[str] = set()
        while ref in self._aliases:
            if ref in seen:
                raise ConfigError(f"Model alias cycle detected at {ref!r}")
            seen.add(ref)
            ref = self._aliases[ref]
        return ref

    def _default_provider_name(self) -> str | None:
        if not self._default:
            return None
        resolved = self._resolve_alias(self._default)
        if "/" not in resolved:
            return None
        provider, _, model = resolved.partition("/")
        return provider if provider and model else None

    @staticmethod
    def _split(ref: str) -> tuple[str | None, str]:
        if "/" not in ref:
            return None, ref
        provider, _, model = ref.partition("/")
        if not provider or not model:
            raise ConfigError(f"Malformed model reference: {ref!r}")
        return provider, model
