"""Resolve a configured model reference to a provider, model, and capabilities.

Plan sections 8 and 15.3-15.5 define the contract: ``"anthropic/claude-opus-5"``
maps to ``provider="anthropic", model="claude-opus-5"``; a bare name resolves
through an alias table; and a **tier name** (``low``/``medium``/``high`` or a
custom ``[models.tiers]`` name) is usable anywhere a model string is accepted.

Resolution order, first hit wins:

1. Config aliases (``default``/``fast``/``plan``, plus any explicit alias).
2. A tier name, when a registry and tier table are wired: the first selectable
   model whose provider this router can actually stream resolves to its
   :class:`~nexus.model.registry.ModelInfo`; a tier with no runnable model is a
   clear :class:`~nexus.errors.ConfigError`.
3. A registry reference -- ``provider/model``, a bare id, or an aggregator
   alias -- resolving to :class:`ModelInfo`.
4. The Phase 1 fallback: split ``provider/model`` and take the adapter's own
   capability descriptor.

The registry is authoritative for the capabilities it describes (section 15.5);
:meth:`~nexus.model.capabilities.Capabilities.overridden_by` layers those over
the adapter's transport-only fields.

:meth:`ModelRouter.fallbacks` enumerates the configured ``model.fallback`` chain
as candidate :class:`ResolvedModel` triples. The router only *lists* them: the
loop (:mod:`nexus.core.loop`) decides when to try one, and only after a
provider-level failure that produced no output. A refusal or a partial stream
never falls back, and there is no mid-stream rerouting.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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
        fallback: Sequence[str] | None = None,
        registry: Any | None = None,
        tiers: Any | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._aliases = dict(aliases or {})
        if default is not None and (
            not isinstance(default, str) or not default.strip()
        ):
            raise ConfigError("default model reference must be a nonempty string")
        self._default = default
        #: Ordered fallback references. A fallback is only ever tried after a
        #: provider-level failure that produced no output (see ``core.loop``);
        #: the router only enumerates them.
        self._fallback: tuple[str, ...] = tuple(fallback or ())
        if any(not isinstance(ref, str) or not ref.strip() for ref in self._fallback):
            raise ConfigError("fallback references must be nonempty strings")
        #: An injected registry/tier table (plan section 15.3). Structural:
        #: only ``get``/``list`` and ``order`` are read, so the router imports
        #: no concrete registry and the model layer stays acyclic.
        self._registry = registry
        self._tiers = tiers

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

    @property
    def fallback(self) -> tuple[str, ...]:
        return self._fallback

    @property
    def registry(self) -> Any | None:
        return self._registry

    @property
    def tiers(self) -> Any | None:
        return self._tiers

    # -- ProviderResolver protocol ----------------------------------------

    def resolve(self, request: ModelRequest, /) -> ResolvedModel:
        """Return ``(provider, model, capabilities)`` for ``request``.

        Raises :class:`~nexus.errors.ConfigError` for a missing model, a
        malformed reference, an alias cycle, an unregistered provider, or a
        tier with no selectable model.
        """
        ref = self._resolve_alias(self._reference(request))

        tier = self._as_tier(ref)
        if tier is not None:
            return self._resolve_tier(tier, ref)

        info = self._registry_lookup(ref)
        if info is not None:
            return self._resolve_info(info, ref)

        return self._resolve_legacy(ref)

    def fallbacks(self, request: ModelRequest, /) -> list[ResolvedModel]:
        """Resolve the configured fallback chain for ``request``, in order.

        The primary target is never repeated (by ``(provider, model)``). A
        fallback reference that cannot resolve is skipped rather than aborting
        the chain: the primary failure is the actionable error, and a broken
        fallback must not mask it. The returned list is *candidates*, not a
        commitment -- the loop only tries one after a provider-level failure
        that produced no output (plan section 8).
        """
        if not self._fallback:
            return []
        seen: set[tuple[str, str]] = set()
        try:
            primary = self.resolve(request)
            seen.add((primary.provider.name, primary.model))
        except ConfigError:
            pass
        candidates: list[ResolvedModel] = []
        for reference in self._fallback:
            candidate = ModelRequest(messages=[], model=reference)
            try:
                resolved = self.resolve(candidate)
            except ConfigError:
                continue
            key = (resolved.provider.name, resolved.model)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(resolved)
        return candidates

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

    def _as_tier(self, ref: str) -> str | None:
        """Return ``ref`` if it names a tier this router can resolve, else ``None``.

        Tier resolution needs both a registry (to find a model) and a tier table
        (to know the names/order); without them a bare id stays a model string,
        which preserves the Phase 1 behaviour exactly.

        The tier table is open, so a custom tier name may legally contain ``/``
        (``[models.tiers."team/high"]``). An exact tier-name match therefore
        wins over the ``provider/model`` split; a user who names a tier after a
        model reference accepts that it shadows the model reference.
        """
        if self._registry is None or self._tiers is None:
            return None
        order = getattr(self._tiers, "order", None) or getattr(
            self._tiers, "names", ()
        )
        return ref if ref in order else None

    def _registry_lookup(self, ref: str) -> Any | None:
        if self._registry is None:
            return None
        get = getattr(self._registry, "get", None)
        if get is None:
            return None
        return get(ref)

    def _resolve_tier(self, tier: str, ref: str) -> ResolvedModel:
        listed = getattr(self._registry, "list", None)
        candidates = (
            list(listed(tier=tier, selectable_only=True))
            if callable(listed)
            else []
        )
        # Only a provider this router can actually stream is runnable. A
        # catalogue entry whose provider has no adapter (or whose provider was
        # not constructed) must never be chosen just because it sorts first.
        runnable = [
            info
            for info in candidates
            if getattr(info, "provider", None) in self._providers
        ]
        if not runnable:
            registered = ", ".join(sorted(self._providers)) or "none"
            available = (
                ", ".join(
                    sorted(
                        {
                            str(getattr(info, "provider", "?"))
                            for info in candidates
                        }
                    )
                )
                or "none"
            )
            raise ConfigError(
                f"No runnable provider has a model for tier {tier!r} "
                f"(reference {ref!r}); registered providers: {registered}; "
                f"catalogue providers offering this tier: {available}"
            )
        return self._resolve_info(runnable[0], ref)

    def _resolve_info(self, info: Any, ref: str) -> ResolvedModel:
        provider_name = getattr(info, "provider", None)
        model = getattr(info, "id", None)
        provider = self._providers.get(provider_name)
        if provider is None:
            known = ", ".join(sorted(self._providers)) or "none"
            raise ConfigError(
                f"Unknown provider {provider_name!r} in model reference {ref!r} "
                f"(registered: {known})"
            )
        capabilities = self._merge_capabilities(provider, model, info)
        return ResolvedModel(provider, model, capabilities)

    @staticmethod
    def _merge_capabilities(
        provider: Provider, model: str, info: Any
    ) -> Capabilities:
        base = provider.capabilities(model)
        registry_caps = info.capabilities() if hasattr(info, "capabilities") else None
        if registry_caps is None:
            return base
        override = getattr(base, "overridden_by", None)
        if override is None:
            return registry_caps
        return override(registry_caps)

    def _resolve_legacy(self, ref: str) -> ResolvedModel:
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
