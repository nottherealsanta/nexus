"""A validated per-session model selection (the ``/model`` override state).

A selection is deliberately *descriptive only*: the user's requested reference
(a tier name, ``"provider/model"``, or a bare id) plus the provider, model, and
tier it resolved to. It carries no credential, endpoint, sampling value, or raw
configuration, so it is safe to persist in a session log and to cross the host
wire. Rehydrating it on open is a pure read of the append-only event log.

The type lives in the model layer (L1) because the tier vocabulary does; the
session layer only serializes and rehydrates it, and the runtime validates it.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgspec

__all__ = ["ModelSelection"]


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


class ModelSelection(msgspec.Struct, frozen=True):
    """The resolved result of ``/model <reference>`` for one session."""

    #: What the user asked for (kept verbatim for display and re-resolution).
    reference: str
    #: The runnable provider adapter name.
    provider: str
    #: The concrete model id the reference resolved to.
    model: str
    #: The model's tier (``low``/``medium``/``high`` or a custom name).
    tier: str = ""
    #: How the tier was decided (``tier``/``override``/``builtin``/``cost``/...).
    tier_source: str = ""
    #: The tier name when the reference itself named one, else ``""``.
    requested_tier: str = ""
    #: Reserved for a future configured ceiling. A user's explicit selection is
    #: never narrowed today, so this is always ``False``; it is kept so the
    #: persisted and wire shape can report a clamp without a migration.
    clamped: bool = False

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe mapping for persistence in an event's ``data``."""
        return {
            "reference": self.reference,
            "provider": self.provider,
            "model": self.model,
            "tier": self.tier,
            "tier_source": self.tier_source,
            "requested_tier": self.requested_tier,
            "clamped": self.clamped,
        }

    @classmethod
    def from_dict(cls, data: object) -> ModelSelection | None:
        """Parse a persisted selection, or ``None`` when malformed.

        Best-effort by design: a corrupt or older event is skipped rather than
        failing an open, exactly as a UI tolerates an unknown event type.
        """
        if not isinstance(data, Mapping):
            return None
        reference = data.get("reference")
        provider = data.get("provider")
        model = data.get("model")
        if not all(isinstance(value, str) and value for value in (reference, provider, model)):
            return None
        clamped = data.get("clamped", False)
        return cls(
            reference=reference,
            provider=provider,
            model=model,
            tier=_as_str(data.get("tier")),
            tier_source=_as_str(data.get("tier_source")),
            requested_tier=_as_str(data.get("requested_tier")),
            # Strict: only an actual JSON boolean counts. A hostile log could
            # carry the string ``"false"`` or ``1``, and ``bool("false")`` is
            # ``True`` -- so anything but ``True``/``False`` reads as ``False``.
            clamped=clamped if isinstance(clamped, bool) else False,
        )
