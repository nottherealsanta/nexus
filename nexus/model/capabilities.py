"""Capability descriptor (plan sections 3.2 and 15.5).

The loop reads capabilities and adapts rather than assuming. A provider adapter
declares what a given model can actually do; ``conservative()`` is the honest
default for a transport whose guarantees are unknown.

The registry is the *authoritative* source for the capabilities models.dev
describes (tool use, context/output limits, reasoning, structured output, and
modalities). ``overridden_by`` layers those fields over an adapter's descriptor
while leaving the transport-only fields the catalogue cannot know
(``parallel_tool_calls``, ``prompt_caching``, ``streaming``) intact.

When a provider rejects a request for a capability the registry claimed, the
adapter raises :class:`CapabilityRejected`; the loop treats that as a
recoverable degradation (plan section 15.5) and retries once with the offending
feature disabled.
"""
from __future__ import annotations

from typing import Literal

import msgspec

from ..errors import ProviderError

DegradationPolicy = Literal["drop", "to_text", "error"]

#: The capability fields the registry describes and therefore owns. A provider's
#: descriptor is overridden on exactly these; everything else is transport-local.
REGISTRY_CAPABILITY_FIELDS: tuple[str, ...] = (
    "tools",
    "thinking",
    "json_schema_strict",
    "vision",
    "documents",
)

#: Numeric limits the registry owns, but only when it actually carries a value
#: (``0`` means "the catalogue did not say", not "no context allowed").
REGISTRY_LIMIT_FIELDS: tuple[str, ...] = (
    "max_context_tokens",
    "max_output_tokens",
    "max_input_tokens",
)

#: Every feature a degradation retry can disable, in a stable order.
CAPABILITY_FEATURES: tuple[str, ...] = (
    "tools",
    "parallel_tool_calls",
    "thinking",
    "prompt_caching",
    "vision",
    "documents",
    "json_schema_strict",
)


class CapabilityRejected(ProviderError):
    """A provider rejected a request because a claimed capability is unsupported.

    Adapters raise this (or a :class:`~nexus.errors.ProviderError` carrying a
    ``feature`` attribute) when the registry claimed a capability the provider
    refuses. ``feature`` names the capability to disable for the single retry.
    """

    def __init__(self, feature: str, message: str = "") -> None:
        super().__init__(message or f"provider rejected capability {feature!r}")
        self.feature = feature


class Capabilities(msgspec.Struct, frozen=True):
    tools: bool = False
    parallel_tool_calls: bool = False
    streaming: bool = True
    thinking: bool = False
    prompt_caching: bool = False
    vision: bool = False
    documents: bool = False
    json_schema_strict: bool = False
    max_context_tokens: int = 0
    max_output_tokens: int = 0
    default_max_output_tokens: int = 0
    #: The provider's prompt cap when it is below ``context - output``.
    max_input_tokens: int = 0
    #: Registry pricing (``Cost.pricing()``: rates plus context tiers, USD/Mtok),
    #: ``None`` when unknown. Informational: published in the context metadata.
    pricing: dict[str, object] | None = None
    degradation: dict[str, DegradationPolicy] = msgspec.field(default_factory=dict)

    @classmethod
    def conservative(cls) -> Capabilities:
        """Assume nothing beyond streaming text."""
        return cls()

    def overridden_by(self, registry: Capabilities) -> Capabilities:
        """Layer registry-owned fields over this (adapter) descriptor.

        The registry wins for the fields it describes (plan section 15.5); the
        adapter keeps the transport-only fields and any numeric limit the
        catalogue left at ``0``.
        """
        updates: dict[str, object] = {
            name: getattr(registry, name) for name in REGISTRY_CAPABILITY_FIELDS
        }
        for name in REGISTRY_LIMIT_FIELDS:
            value = getattr(registry, name)
            if value:
                updates[name] = value
        if registry.pricing is not None:
            updates["pricing"] = registry.pricing
        return msgspec.structs.replace(self, **updates)

    def disabled(self, feature: str) -> Capabilities:
        """Return a copy with ``feature`` turned off.

        ``tools``/``parallel_tool_calls``/``thinking``/``prompt_caching``/
        ``vision``/``documents``/``json_schema_strict`` are the degradation
        features; an unknown name is returned unchanged so a bad catalogue
        claim can never make the retry path raise.
        """
        if feature not in CAPABILITY_FEATURES:
            return self
        return msgspec.structs.replace(self, **{feature: False})


__all__ = [
    "CAPABILITY_FEATURES",
    "REGISTRY_CAPABILITY_FIELDS",
    "REGISTRY_LIMIT_FIELDS",
    "Capabilities",
    "CapabilityRejected",
    "DegradationPolicy",
]
