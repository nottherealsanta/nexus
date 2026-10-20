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

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..errors import (
    ConfigError,
    MalformedToolCall,
    ManagerModelUnavailable,
    ProviderError,
    ProviderOverloaded,
)
from .capabilities import Capabilities, CapabilityRejected
from .http import redact_secrets
from .provider import Provider, ResolvedModel
from .request import ModelRequest
from .stream import StreamEvent
from .tiers import DEFAULT_TIER_MODELS, effective_tier_effort

__all__ = ["MODEL_POLICY_PINNED", "ModelRouter", "pinned_reference"]

#: ``request.metadata["model_policy"]`` value that disables every fallback and
#: every substitution (plan 02): the request resolves to exactly
#: ``metadata["pinned_model"]`` or fails with :class:`ManagerModelUnavailable`.
MODEL_POLICY_PINNED = "pinned"


def pinned_reference(request: ModelRequest) -> str | None:
    """The exact ``provider/model`` a pinned request must run on, else ``None``."""
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    if metadata.get("model_policy") != MODEL_POLICY_PINNED:
        return None
    reference = metadata.get("pinned_model")
    if not isinstance(reference, str) or "/" not in reference:
        # A pinned policy with no usable pin must fail, never route elsewhere.
        return ""
    return reference


class _PinnedProvider:
    """Delegates to the real provider and names the pinned model on failure.

    Transient overload, malformed tool calls and capability rejections pass
    through untouched because the loop retries or degrades those on the *same*
    model; every other provider failure (sign-in, quota, unknown model)
    surfaces as :class:`ManagerModelUnavailable` naming the exact route.
    """

    def __init__(self, inner: Provider, reference: str, hint: str = "") -> None:
        self._inner = inner
        self._reference = reference
        self._hint = hint

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    def capabilities(self, model: str) -> Capabilities:
        return self._inner.capabilities(model)

    async def count_tokens(self, req: ModelRequest) -> int | None:
        return await self._inner.count_tokens(req)

    async def aclose(self) -> None:
        """No-op: the router's provider is shared and closed by its owner."""

    async def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        inner = self._inner.stream(req)
        try:
            async for event in inner:
                yield event
        except (ProviderOverloaded, MalformedToolCall, CapabilityRejected, ManagerModelUnavailable):
            raise
        except ProviderError as exc:
            reason = redact_secrets(str(exc))
            raise ManagerModelUnavailable(
                self._reference, f"{reason}; {self._hint}" if self._hint else reason
            ) from exc
        finally:
            close = getattr(inner, "aclose", None)
            if close is not None:
                await close()


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
        tier_efforts: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._tier_efforts = {tier: dict(efforts) for tier, efforts in (tier_efforts or {}).items()}
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

    def set_tiers(self, tiers: Any | None, *, tier_efforts: Mapping[str, Mapping[str, str]] | None = None) -> None:
        """Swap the tier table and per-tier efforts in place (a tier-only reload)."""
        self._tiers = tiers
        self._tier_efforts = {tier: dict(efforts) for tier, efforts in (tier_efforts or {}).items()}

    # -- introspection -----------------------------------------------------

    @property
    def tier_efforts(self) -> dict[str, dict[str, str]]:
        """The per-tier route efforts the router applies right now."""
        return {tier: dict(efforts) for tier, efforts in self._tier_efforts.items()}

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
        pinned = pinned_reference(request)
        if pinned is not None:
            return self._resolve_pinned(request, pinned)
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
        that produced no output (plan section 8). A pinned request has none:
        agent, tier and workspace fallbacks are all ignored.
        """
        if pinned_reference(request) is not None:
            return []
        agent_refs = _agent_fallback(request)
        seen: set[tuple[str, str]] = set()
        try:
            primary = self.resolve(request)
            seen.add((primary.provider.name, primary.model))
        except ConfigError:
            pass
        candidates: list[ResolvedModel] = []
        tier_defaults: tuple[str, ...] = ()
        reference = request.metadata.get("tier") or self._resolve_alias(self._reference(request))
        if self._as_tier(reference) is not None:
            tier_defaults = self.tier_candidates(reference)
        for reference in (*agent_refs, *tier_defaults, *self._fallback):
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

    def _resolve_pinned(self, request: ModelRequest, pinned: str) -> ResolvedModel:
        """Resolve exactly ``pinned``: no alias, tier, default or fallback.

        The request may name the same route (``provider`` + ``model``) but never
        a different one; a mismatch or any resolution failure raises
        :class:`ManagerModelUnavailable` naming the pinned route.
        """
        if not pinned:
            raise ManagerModelUnavailable("(unset)", "the pinned model policy has no model")
        hint = request.metadata.get("pinned_hint") if isinstance(request.metadata, dict) else None
        hint = hint if isinstance(hint, str) and len(hint) <= 200 else ""
        requested = f"{request.provider}/{request.model}" if request.provider and request.model else request.model
        if requested != pinned:
            raise ManagerModelUnavailable(pinned, f"request named {requested!r} instead")
        try:
            info = self._registry_lookup(pinned)
            if info is not None:
                resolved = self._resolve_info(info, pinned)
            else:
                resolved = self._resolve_legacy(pinned)
        except ConfigError as exc:
            raise ManagerModelUnavailable(pinned, f"{exc}; {hint}" if hint else str(exc)) from exc
        if resolved.model != pinned.partition("/")[2]:
            raise ManagerModelUnavailable(pinned, "resolved to a different route")
        return ResolvedModel(
            _PinnedProvider(resolved.provider, pinned, hint), resolved.model, resolved.capabilities
        )

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

    def tier_runnable(self, tier: str) -> bool:
        """Whether ``tier`` resolves to a model one of the providers can stream.

        ``False`` when the router has no registry and tier table (a plain config
        has no tiers: a bare tier name would be sent to the provider as a model
        id), or when no listed model for the tier has a runnable provider.
        """
        if self._as_tier(tier) is None:
            return False
        try:
            self._resolve_tier(tier, tier)
        except ConfigError:
            return False
        return True

    def _registry_lookup(self, ref: str) -> Any | None:
        if self._registry is None:
            return None
        get = getattr(self._registry, "get", None)
        if get is None:
            return None
        return get(ref)

    def _resolve_tier(self, tier: str, ref: str) -> ResolvedModel:
        for reference in self.tier_candidates(tier):
            info = self._registry_lookup(reference)
            if info is not None and getattr(info, "provider", None) in self._providers:
                return self._resolve_info(info, ref)
        registered = ", ".join(sorted(self._providers)) or "none"
        offered = ", ".join(sorted({reference.split("/", 1)[0] for reference in self.tier_candidates(tier)})) or "none"
        raise ConfigError(
            f"No runnable provider has a model for tier {tier!r} (reference {ref!r}); "
            f"registered providers: {registered}; "
            f"catalogue providers offering this tier: {offered}"
        )

    def tier_candidates(self, tier: str) -> tuple[str, ...]:
        """The shared ordered routes for Settings, resolution and retries.

        Keep disconnected candidates visible. Explicit pins are authoritative;
        built-ins use their curated route order, not classification order.
        """
        pins = [ref for ref, assigned in dict(getattr(self._tiers, "overrides", {}) or {}).items()
                if assigned == tier and ref not in getattr(self._tiers, "order", ())]
        if pins:
            return tuple(pins)
        if tier in DEFAULT_TIER_MODELS and getattr(self._tiers, "builtin", None):
            return DEFAULT_TIER_MODELS[tier]
        listed = getattr(self._registry, "list", None)
        infos = list(listed(tier=tier, selectable_only=False)) if callable(listed) else []
        infos.sort(key=lambda info: getattr(getattr(info, "cost", None), "output", None) or 0, reverse=True)
        # Price-based tiers prefer the priciest route, including skipped entries.
        return tuple(f"{info.provider}/{info.id}" for info in infos)

    def _first_pinned(self, tier: str) -> Any | None:
        """The first runnable model the user pinned to ``tier``, in list order."""
        overrides = getattr(self._tiers, "overrides", None)
        if not overrides:
            return None
        order = getattr(self._tiers, "order", ())
        for reference, assigned in dict(overrides).items():
            if assigned != tier or reference in order:
                continue
            info = self._registry_lookup(reference)
            if info is not None and getattr(info, "provider", None) in self._providers:
                return info
        return None

    def _tier_pinned(self, tier: str) -> bool:
        return any(value == tier for value in dict(getattr(self._tiers, "overrides", {}) or {}).values())

    def tier_effort(self, request: ModelRequest, candidate: ResolvedModel) -> str | None:
        """Apply per-route defaults only when no explicit effort was selected."""
        if pinned_reference(request) is not None:
            return request.params.reasoning_effort  # provider default (plan 02 U4)
        tier = request.metadata.get("tier") or self._resolve_alias(self._reference(request))
        if (
            request.metadata.get("reasoning_effort_explicit")
            or not isinstance(tier, str)
        ):
            return request.params.reasoning_effort
        reference = f"{candidate.provider.name}/{candidate.model}"
        configured = self._tier_efforts.get(tier, {}).get(reference)
        pinned = self._tier_pinned(tier)
        if configured is None and (tier not in DEFAULT_TIER_MODELS or pinned):
            return request.params.reasoning_effort
        return effective_tier_effort(
            tier, reference, configured=configured, pinned=pinned
        )

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


def _agent_fallback(request: ModelRequest) -> tuple[str, ...]:
    """The selected agent's own fallback references, carried in request metadata.

    The context assembler copies an agent definition's ``fallback`` list into
    ``metadata["agent_fallback"]``; those references are tried first, then the
    workspace chain. Malformed entries are ignored.
    """
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    refs = metadata.get("agent_fallback")
    if not isinstance(refs, list):
        return ()
    return tuple(
        ref for ref in refs[:8] if isinstance(ref, str) and ref.strip() and len(ref) <= 256
    )
