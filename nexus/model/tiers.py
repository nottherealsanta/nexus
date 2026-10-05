"""Tier resolution for model references (plan section 15.4).

Tiers are a small, ordered vocabulary layered on top of concrete models. A
tier name is usable wherever a model string is accepted: ``models.default``, a
skill's ``model:``, an agent definition's ``model:``, and
``Task(model="low")``.

Resolution order, first hit wins (plan section 15.4):

1. ``[models.tiers]`` user overrides -- an explicit pin.
2. The curated map shipped with Nexus (:data:`BUILTIN_TIERS`).
3. Blended-cost fallback: ``cost.input + cost.output / 4``, then
   ``low <= 2.5 < medium <= 10 < high``.
4. No cost data -> ``low``.

This module is deliberately *structural*: it never imports the registry's
``ModelInfo``/``Cost`` types (a module developed in parallel) and instead reads
``provider``, ``id``, ``aliases``, ``cost.input`` and ``cost.output`` with
``getattr``/mapping lookup. Any object carrying those attributes -- or a plain
dict -- works, which keeps tier assignment race-free and trivially testable.

A reference is either a tier name, ``"provider/model"``, or a bare model id.
Tiers resolve deterministically: there is no iteration over sets, no reliance on
hash order, and every decision carries a machine-checkable source plus optional
human-readable diagnostics.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Self

from ..errors import ConfigError

#: Ordered built-in routes, consulted only when the user has not pinned a tier.
DEFAULT_TIER_MODELS = {
    "low": (
        "github-copilot/gpt-6-luna",
        "codex/gpt-6-luna",
        "opencode-go/deepseek-v4.1-flash",
    ),
    "medium": (
        "codex/gpt-6.1-sol",
        "opencode-go/deepseek-v4.1-flash",
    ),
    "high": (
        "codex/gpt-6.1-sol",
        "claude-agent/claude-sonnet-5-5",
    ),
}
DEFAULT_TIER_EFFORTS = {
    ("medium", "codex/gpt-6.1-sol"): "low",
    ("medium", "opencode-go/deepseek-v4.1-flash"): "max",
    ("high", "codex/gpt-6.1-sol"): "low",
}

__all__ = [
    "BUILTIN_TIERS",
    "DEFAULT_TIER_MODELS",
    "DEFAULT_TIER_EFFORTS",
    "COST_LOW_MAX",
    "COST_MEDIUM_MAX",
    "DEFAULT_TIER",
    "HIGH",
    "LOW",
    "MEDIUM",
    "TIER_ORDER",
    "ModelReference",
    "TierOrdering",
    "TierResolution",
    "TierTable",
    "assign_tier",
    "blended_cost",
    "clamp",
    "model_reference_keys",
    "normalize_overrides",
    "ordering",
    "parse_reference",
    "resolve_reference",
    "resolve_tier",
    "tier_for_cost",
    "tier_rank",
]

LOW = "low"
MEDIUM = "medium"
HIGH = "high"

#: Canonical ordering. ``low < medium < high``; custom tiers slot around these.
TIER_ORDER: tuple[str, ...] = (LOW, MEDIUM, HIGH)

#: Tier used when no reference is supplied and nothing can be inherited.
DEFAULT_TIER = MEDIUM

#: Blended-cost class boundaries (plan section 15.4).
#: ``blended <= COST_LOW_MAX`` -> low, ``<= COST_MEDIUM_MAX`` -> medium, else high.
COST_LOW_MAX = 2.5
COST_MEDIUM_MAX = 10.0

#: Curated defaults for known models. The shipped map wins over cost; that is
#: the point of the map -- flagships that happen to be cheap still read ``high``.
BUILTIN_TIERS: Mapping[str, str] = {
    "anthropic/claude-opus-5": HIGH,
    "anthropic/claude-opus-4-5": HIGH,
    "openai/gpt-5.6": HIGH,
    "anthropic/claude-sonnet-5": MEDIUM,
    "anthropic/claude-sonnet-5-5": MEDIUM,
    "openai/gpt-6.1-sol": HIGH,
    "anthropic/claude-sonnet-4-5": MEDIUM,
    "openai/gpt-5": MEDIUM,
    "anthropic/claude-haiku-4-5": LOW,
    "openai/gpt-5-mini": LOW,
}

_UNKNOWN = object()


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` structurally from an object or a mapping."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


class ModelReference(str):
    """A parsed model reference.

    It is a ``str`` subclass carrying the split components so callers can keep
    passing the original string everywhere while still inspecting the parts.
    """

    __slots__ = ("model", "provider", "raw")

    def __new__(cls, raw: str, provider: str | None, model: str) -> Self:
        obj = super().__new__(cls, raw)
        obj.raw = raw
        obj.provider = provider
        obj.model = model
        return obj

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}" if self.provider else self.model

    def keys(self) -> tuple[str, ...]:
        """Candidate lookup keys, most specific first, de-duplicated."""
        keys: list[str] = []
        if self.provider:
            keys.append(f"{self.provider}/{self.model}")
        keys.append(self.model)
        tail = self.model.rsplit("/", 1)[-1]
        if tail and tail != self.model:
            keys.append(tail)
        return tuple(dict.fromkeys(keys))


def parse_reference(reference: str) -> ModelReference:
    """Parse ``"provider/model"`` or a bare id, rejecting empty/malformed refs.

    Unknown providers and unknown model ids are *opaque*, not errors: the
    reference still resolves deterministically through overrides, the curated
    map, cost, or the low fallback.
    """
    if not isinstance(reference, str) or not reference.strip():
        raise ConfigError("model reference must be a nonempty string")
    text = reference.strip()
    if "/" in text:
        provider, _, model = text.partition("/")
        if not provider or not model:
            raise ConfigError(f"Malformed model reference: {reference!r}")
        return ModelReference(text, provider, model)
    return ModelReference(text, None, text)


def model_reference_keys(info: Any) -> tuple[str, ...]:
    """Candidate lookup keys for a structural ``ModelInfo`` (or a ref string)."""
    if isinstance(info, str):
        return parse_reference(info).keys()
    provider = _field(info, "provider")
    model_id = _field(info, "id")
    keys: list[str] = []
    if provider and model_id:
        keys.append(f"{provider}/{model_id}")
    if model_id:
        keys.append(str(model_id))
    aliases = _field(info, "aliases") or ()
    if isinstance(aliases, str):
        aliases = (aliases,)
    for alias in aliases:
        keys.append(str(alias))
    return tuple(dict.fromkeys(keys))


def blended_cost(info: Any) -> float | None:
    """Return ``input + output / 4`` for a structural ``Cost``, else ``None``.

    ``None`` means *no cost data* (local/open-weight, or a malformed entry); it
    is distinct from a real ``0.0`` and deliberately maps to the low tier.
    """
    cost = _field(info, "cost")
    if cost is None:
        return None
    raw_input = _field(cost, "input", _UNKNOWN)
    raw_output = _field(cost, "output", _UNKNOWN)
    if raw_input is _UNKNOWN or raw_output is _UNKNOWN:
        return None
    try:
        value_input = float(raw_input)
        value_output = float(raw_output)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value_input) or not math.isfinite(value_output):
        return None
    return value_input + value_output / 4.0


def tier_for_cost(value: float | None) -> str:
    """Classify a blended cost; ``None`` (no data) reads as ``low``."""
    if value is None:
        return LOW
    if value <= COST_LOW_MAX:
        return LOW
    if value <= COST_MEDIUM_MAX:
        return MEDIUM
    return HIGH


def normalize_overrides(
    overrides: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Canonicalize a tier map into ``reference -> tier``.

    Both config shapes are accepted and never mixed:

    * ``{"ref": "tier"}`` -- the canonical internal shape;
    * ``{"tier": ["ref", ...]}`` -- the ``[models.tiers]`` TOML shape.

    A document that mixes the two is a conflict and raises :class:`ConfigError`.
    Later entries win, so an override set is deterministic.
    """
    if overrides is None:
        return {}
    saw_scalar = False
    saw_sequence = False
    for value in overrides.values():
        if isinstance(value, str):
            saw_scalar = True
        elif isinstance(value, (list, tuple, set, frozenset)):
            saw_sequence = True
        else:
            raise ConfigError(
                f"tier override value must be a reference or a list of "
                f"references, got {type(value).__name__}"
            )
    if saw_scalar and saw_sequence:
        raise ConfigError(
            "tier overrides cannot mix reference->tier and tier->references shapes"
        )
    canonical: dict[str, str] = {}
    if saw_sequence:
        for tier, refs in overrides.items():
            for ref in refs:
                canonical[str(ref)] = str(tier)
    else:
        for ref, tier in overrides.items():
            canonical[str(ref)] = str(tier)
    return canonical


class TierOrdering:
    """A deterministic total order over built-in and custom tier names."""

    __slots__ = ("names",)

    def __init__(self, names: Sequence[str] | None = None) -> None:
        ordered = tuple(dict.fromkeys(names if names is not None else TIER_ORDER))
        if not ordered:
            raise ConfigError("tier ordering must not be empty")
        present = [name for name in ordered if name in TIER_ORDER]
        if tuple(present) != TIER_ORDER:
            raise ConfigError(
                f"tier ordering must contain {TIER_ORDER!r} in that relative "
                f"order, got {ordered!r}"
            )
        self.names = ordered

    def __iter__(self):
        return iter(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __contains__(self, name: object) -> bool:
        return name in self.names

    def rank(self, name: str) -> int | None:
        """Return the ordinal for ``name``, or ``None`` if it is unknown."""
        try:
            return self.names.index(name)
        except ValueError:
            return None

    def clamp(self, tier: str, max_tier: str) -> str:
        """Narrow ``tier`` to ``max_tier`` (or below).

        An unknown requested tier is treated as sitting above the ceiling and is
        clamped down; an unknown ceiling is a configuration error. No clamp ever
        *widens* a request.
        """
        ceiling = self.rank(max_tier)
        if ceiling is None:
            raise ConfigError(
                f"unknown max tier {max_tier!r} (known: {self.names})"
            )
        requested = self.rank(tier)
        if requested is None:
            return max_tier
        return self.names[min(requested, ceiling)]


def ordering(names: Sequence[str] | None = None) -> tuple[str, ...]:
    """Validate and return a tier ordering; default is ``low < medium < high``."""
    return TierOrdering(names).names


def tier_rank(name: str, *, order: Sequence[str] | None = None) -> int:
    """Ordinal of ``name``; raises :class:`ConfigError` for an unknown tier."""
    rank = TierOrdering(order).rank(name)
    if rank is None:
        raise ConfigError(f"unknown tier {name!r} (known: {ordering(order)})")
    return rank


def clamp(
    tier: str, max_tier: str, *, order: Sequence[str] | None = None
) -> str:
    """Clamp ``tier`` to ``max_tier`` under ``order`` (default built-ins)."""
    return TierOrdering(order).clamp(tier, max_tier)


def _index_builtin(
    builtin: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    by_key: dict[str, str] = {}
    by_model: dict[str, str] = {}
    for ref, tier in builtin.items():
        key = str(ref)
        by_key[key] = str(tier)
        model = key.split("/", 1)[1] if "/" in key else key
        by_model.setdefault(model, str(tier))
    return by_key, by_model


class TierResolution(str):
    """A resolved tier name that also carries provenance and diagnostics.

    It behaves exactly like the tier string (it *is* one) so it can be dropped
    anywhere a model/tier string is accepted, while ``.source``,
    ``.clamped``, and ``.diagnostics`` make the decision auditable.
    """

    __slots__ = (
        "clamped",
        "diagnostics",
        "max_tier",
        "reference",
        "source",
        "tier",
    )

    def __new__(
        cls,
        tier: str,
        *,
        source: str,
        reference: Any = None,
        max_tier: str | None = None,
        clamped: bool = False,
        diagnostics: Iterable[str] = (),
    ) -> Self:
        obj = super().__new__(cls, tier)
        obj.tier = tier
        obj.source = source
        obj.reference = reference
        obj.max_tier = max_tier
        obj.clamped = clamped
        obj.diagnostics = tuple(diagnostics)
        return obj

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TierResolution({self.tier!r}, source={self.source!r}, "
            f"clamped={self.clamped!r})"
        )


class TierTable:
    """Configurable tier resolver: curated map, overrides, ordering, fallback."""

    def __init__(
        self,
        *,
        builtin: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        order: Sequence[str] | None = None,
        default: str = DEFAULT_TIER,
    ) -> None:
        self._builtin = dict(BUILTIN_TIERS if builtin is None else builtin)
        self._overrides = normalize_overrides(overrides)
        base = list(order) if order is not None else list(TIER_ORDER)
        custom = sorted(
            {
                tier
                for tier in (*self._overrides.values(), *self._builtin.values())
                if tier not in base
            }
        )
        self._ordering = TierOrdering(tuple(base) + tuple(custom))
        self._order = self._ordering.names
        if default not in self._order:
            raise ConfigError(
                f"default tier {default!r} is not in the ordering {self._order}"
            )
        self._default = default
        self._by_key, self._by_model = _index_builtin(self._builtin)

    # -- introspection -----------------------------------------------------

    @property
    def order(self) -> tuple[str, ...]:
        return self._order

    @property
    def default(self) -> str:
        return self._default

    @property
    def overrides(self) -> dict[str, str]:
        return dict(self._overrides)

    @property
    def builtin(self) -> dict[str, str]:
        return dict(self._builtin)

    @property
    def ordering(self) -> TierOrdering:
        return self._ordering

    def rank(self, tier: str) -> int | None:
        return self._ordering.rank(tier)

    def clamp(self, tier: str, max_tier: str) -> str:
        return self._ordering.clamp(tier, max_tier)

    # -- assignment --------------------------------------------------------

    def _classify(self, info: Any) -> tuple[str, str]:
        keys = model_reference_keys(info)
        for key in keys:
            if key in self._overrides:
                return self._overrides[key], "override"
        for key in keys:
            if key in self._by_key:
                return self._by_key[key], "builtin"
        for key in keys:
            if key in self._by_model:
                return self._by_model[key], "builtin"
        blended = blended_cost(info)
        if blended is None:
            return LOW, "cost-missing"
        return tier_for_cost(blended), "cost"

    def _lookup_reference(self, reference: str) -> tuple[str, str] | None:
        keys = parse_reference(reference).keys()
        for key in keys:
            if key in self._overrides:
                return self._overrides[key], "override"
        for key in keys:
            if key in self._by_key:
                return self._by_key[key], "builtin"
        for key in keys:
            if key in self._by_model:
                return self._by_model[key], "builtin"
        return None

    def assign(self, info: Any) -> str:
        """Assign a tier to a structural ``ModelInfo`` (or a reference string)."""
        return self._classify(info)[0]

    # -- resolution --------------------------------------------------------

    def resolve(
        self,
        reference: Any = None,
        *,
        info: Any = None,
        parent: str | None = None,
        max_tier: str | None = None,
    ) -> TierResolution:
        """Resolve ``reference`` to a :class:`TierResolution`.

        ``reference`` may be ``None`` (inherit from ``parent`` or use the
        default), a tier name, ``"provider/model"``, or a bare id. ``info`` is
        an already-looked-up structural ``ModelInfo`` used for cost-based
        assignment when the reference itself is not a known tier. ``max_tier``
        clamps the result without ever widening it.
        """
        diagnostics: list[str] = []
        ref = reference.strip() if isinstance(reference, str) else reference

        if ref is None or ref == "":
            if info is not None:
                tier, source = self._classify(info)
            elif parent:
                tier, source = parent, "inherit"
            else:
                tier, source = self._default, "default"
        elif isinstance(ref, str) and ref in self._order:
            tier, source = ref, "tier"
        elif info is not None:
            tier, source = self._classify(info)
        elif isinstance(ref, str):
            found = self._lookup_reference(ref)
            if found is None:
                tier, source = LOW, "cost-missing"
                diagnostics.append(
                    f"unknown model reference {ref!r}; no cost data, using {LOW!r}"
                )
            else:
                tier, source = found
        else:
            tier, source = self._classify(ref)

        clamped = False
        if max_tier is not None:
            limited = self._ordering.clamp(tier, max_tier)
            if limited != tier:
                diagnostics.append(
                    f"clamped {tier!r} to {limited!r} (max_tier={max_tier!r})"
                )
                tier, clamped = limited, True

        return TierResolution(
            tier,
            source=source,
            reference=reference,
            max_tier=max_tier,
            clamped=clamped,
            diagnostics=diagnostics,
        )

    def resolve_tier(
        self,
        reference: Any = None,
        *,
        info: Any = None,
        parent: str | None = None,
        max_tier: str | None = None,
    ) -> str:
        """Resolve and return just the tier name."""
        return self.resolve(
            reference, info=info, parent=parent, max_tier=max_tier
        ).tier


#: A shared default table. Callers with config build their own :class:`TierTable`.
DEFAULT_TABLE = TierTable()


def assign_tier(
    info: Any,
    *,
    overrides: Mapping[str, Any] | None = None,
    builtin: Mapping[str, str] | None = None,
    order: Sequence[str] | None = None,
) -> str:
    """Assign a tier to ``info`` without constructing a full table."""
    return TierTable(
        builtin=builtin, overrides=overrides, order=order
    ).assign(info)


def resolve_tier(
    reference: Any = None,
    *,
    table: TierTable | None = None,
    info: Any = None,
    parent: str | None = None,
    max_tier: str | None = None,
) -> TierResolution:
    """Resolve a reference with the shared default table (or a supplied one)."""
    return (table or DEFAULT_TABLE).resolve(
        reference, info=info, parent=parent, max_tier=max_tier
    )


def resolve_reference(
    reference: Any = None,
    *,
    table: TierTable | None = None,
    info: Any = None,
    parent: str | None = None,
    max_tier: str | None = None,
) -> TierResolution:
    """Alias of :func:`resolve_tier` for callers that name the input a reference."""
    return resolve_tier(
        reference, table=table, info=info, parent=parent, max_tier=max_tier
    )
