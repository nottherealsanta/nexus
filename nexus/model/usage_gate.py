"""Pure usage gate: should a provider's plan quota keep receiving requests?

Contract: no I/O, no clock reads, and nothing imported from above ``nexus.model``.
Callers pass the snapshot (the shape ``providers_usage()`` returns), the policy
and ``now``; the gate answers with a :class:`UsageVerdict`. Only windows whose
length is recognised (:func:`window_kind`) gate; ``other`` windows (per-model or
per-feature side quotas) never do. A missing, stale or failed reading is
``unknown`` and fails open: ``unknown`` never blocks.

:func:`choose_route` walks candidate references in order and picks the first one
that is not blocked. :class:`GatedFallbacks` wraps a router so the fallback chain
skips blocked providers, without changing the router itself.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

FIVE_HOURS = 18_000
WEEK = 604_800
WINDOW_KINDS = ("five_hour", "weekly", "monthly", "other")
DEFAULT_REFRESH_SECONDS = 180.0
MAX_SKIPPED_EVENTS = 8
_DAY = 86_400


def window_kind(period_seconds: float | None) -> str:
    """Classify a window length: ``five_hour``, ``weekly``, ``monthly`` or ``other``."""
    if isinstance(period_seconds, bool) or not isinstance(period_seconds, (int, float)) or not math.isfinite(period_seconds):
        return "other"
    if 16_200 <= period_seconds <= 19_800:
        return "five_hour"
    days = period_seconds / _DAY
    if 6.5 <= days <= 7.5:
        return "weekly"
    if 27.5 <= days <= 31.5:
        return "monthly"
    return "other"


@dataclass(frozen=True)
class Thresholds:
    """Minimum remaining percent per window kind; at or below it the window blocks."""

    five_hour: float = 10.0
    weekly: float = 5.0
    monthly: float = 2.0

    def for_kind(self, kind: str) -> float | None:
        """The threshold for ``kind``; ``None`` (never gated) for ``other``."""
        if kind == "five_hour":
            return self.five_hour
        if kind == "weekly":
            return self.weekly
        if kind == "monthly":
            return self.monthly
        return None


def _get(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _percent_or(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def _thresholds_from(source: Any, base: Thresholds) -> Thresholds:
    """``source`` fields override ``base``; a missing or ``None`` field inherits."""
    return Thresholds(
        five_hour=_percent_or(_get(source, "five_hour_min_remaining"), base.five_hour),
        weekly=_percent_or(_get(source, "weekly_min_remaining"), base.weekly),
        monthly=_percent_or(_get(source, "monthly_min_remaining"), base.monthly),
    )


@dataclass(frozen=True)
class UsagePolicy:
    """Whether and how the gate applies. ``max_age_seconds`` bounds snapshot staleness."""

    enabled: bool = True
    thresholds: Thresholds = field(default_factory=Thresholds)
    overrides: Mapping[str, Thresholds] = field(default_factory=dict)
    max_age_seconds: float = 3 * DEFAULT_REFRESH_SECONDS

    @classmethod
    def from_section(cls, section: Any) -> "UsagePolicy":
        """Read a config section structurally (``getattr``), so any shape with the fields works."""
        enabled = _get(section, "enabled")
        base = _thresholds_from(section, Thresholds())
        providers = _get(section, "providers")
        overrides: dict[str, Thresholds] = {}
        if isinstance(providers, Mapping):
            for name, item in providers.items():
                overrides[str(name)] = _thresholds_from(item, base)
        refresh = _percent_or(_get(section, "refresh_seconds"), DEFAULT_REFRESH_SECONDS)
        if refresh <= 0:
            refresh = DEFAULT_REFRESH_SECONDS
        return cls(enabled=True if enabled is None else bool(enabled), thresholds=base,
                   overrides=overrides, max_age_seconds=3 * refresh)

    def thresholds_for(self, quota: str) -> Thresholds:
        return self.overrides.get(quota, self.thresholds)


@dataclass(frozen=True)
class UsageWindow:
    kind: str
    used_percent: float | None
    resets_at: int | None
    label: str


@dataclass(frozen=True)
class ProviderUsage:
    quota: str
    windows: tuple[UsageWindow, ...]
    error: str = ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


@dataclass(frozen=True)
class UsageSnapshot:
    providers: Mapping[str, ProviderUsage]
    fetched_at: float

    @classmethod
    def from_result(cls, result: Mapping[str, Any]) -> "UsageSnapshot":
        """Build from ``providers_usage()``: ``{"providers": [rows with "id"], "fetched_at": ...}``."""
        providers: dict[str, ProviderUsage] = {}
        rows = result.get("providers") if isinstance(result, Mapping) else None
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
                continue
            windows: list[UsageWindow] = []
            for item in row.get("windows") or ():
                if not isinstance(item, Mapping):
                    continue
                kind = item.get("kind")
                resets = _as_number(item.get("resets_at"))
                windows.append(UsageWindow(
                    kind=kind if kind in WINDOW_KINDS else "other",
                    used_percent=_as_number(item.get("used_percent")),
                    resets_at=None if resets is None else int(resets),
                    label=str(item.get("label") or "")))
            providers[row["id"]] = ProviderUsage(row["id"], tuple(windows), str(row.get("error") or ""))
        fetched = _as_number(result.get("fetched_at")) if isinstance(result, Mapping) else None
        return cls(providers, 0.0 if fetched is None else fetched)


def quota_identity(provider: str, adapter_kind: str | None) -> str:
    """The quota a provider draws on: Claude adapters share one, as does Antigravity."""
    if adapter_kind in ("claude-agent", "claude-code"):
        return "claude-agent"
    if adapter_kind == "antigravity":
        return "antigravity"
    return provider


@dataclass(frozen=True)
class UsageVerdict:
    quota: str
    state: Literal["ok", "blocked", "unknown"]
    kind: str = ""
    label: str = ""
    remaining_percent: float | None = None
    threshold_percent: float | None = None
    resets_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"quota": self.quota, "state": self.state, "kind": self.kind, "label": self.label,
                "remaining_percent": self.remaining_percent, "threshold_percent": self.threshold_percent,
                "resets_at": self.resets_at}


def verdict(snapshot: UsageSnapshot | None, quota: str, policy: UsagePolicy, *, now: float) -> UsageVerdict:
    """Blocked iff a recognised window's remaining percent is at or below its threshold."""
    unknown = UsageVerdict(quota, "unknown")
    if not policy.enabled or snapshot is None or now - snapshot.fetched_at > policy.max_age_seconds:
        return unknown
    row = snapshot.providers.get(quota)
    if row is None or row.error:
        return unknown
    thresholds = policy.thresholds_for(quota)
    breaches: list[UsageVerdict] = []
    best: UsageVerdict | None = None
    for window in row.windows:
        threshold = thresholds.for_kind(window.kind)
        if window.used_percent is None or threshold is None:
            continue
        if window.resets_at is not None and window.resets_at <= now:
            continue
        remaining = 100 - window.used_percent
        item = UsageVerdict(quota, "blocked" if remaining <= threshold else "ok", window.kind, window.label,
                            remaining, threshold, window.resets_at)
        if item.state == "blocked":
            breaches.append(item)
        elif best is None or (remaining - threshold) < (best.remaining_percent - best.threshold_percent):  # type: ignore[operator]
            best = item
    if breaches:
        # The breach that clears last is when the provider is usable again; no reset time sorts latest.
        return max(breaches, key=lambda item: (item.resets_at is None, item.resets_at or 0))
    return best or unknown


@dataclass(frozen=True)
class SkippedRoute:
    reference: str
    verdict: UsageVerdict


@dataclass(frozen=True)
class RouteDecision:
    requested: str
    first: str
    chosen: str
    skipped: tuple[SkippedRoute, ...]
    all_blocked: bool
    observed_at: float

    @property
    def rerouted(self) -> bool:
        return self.chosen != self.first

    def to_event(self) -> dict[str, Any]:
        return {
            "requested": self.requested, "first": self.first, "chosen": self.chosen,
            "all_blocked": self.all_blocked, "observed_at": self.observed_at,
            "skipped": [{"reference": item.reference, "quota": item.verdict.quota, "kind": item.verdict.kind,
                         "label": item.verdict.label, "remaining_percent": item.verdict.remaining_percent,
                         "threshold_percent": item.verdict.threshold_percent, "resets_at": item.verdict.resets_at}
                        for item in self.skipped[:MAX_SKIPPED_EVENTS]],
        }


def choose_route(requested: str, candidates: Any, headroom: Callable[[str], UsageVerdict], *,
                 eligible: Callable[[str], bool] = lambda ref: True, observed_at: float = 0.0) -> RouteDecision | None:
    """Pick the first eligible candidate that is not blocked.

    ``headroom`` receives the candidate *reference string* (not a provider name)
    and returns its verdict. ``ok`` and ``unknown`` win. If every eligible
    candidate is blocked the first eligible one is chosen with ``all_blocked``.
    ``None`` when no candidate is eligible.
    """
    usable = [ref for ref in candidates if eligible(ref)]
    if not usable:
        return None
    skipped: list[SkippedRoute] = []
    for ref in usable:
        result = headroom(ref)
        if result.state == "blocked":
            skipped.append(SkippedRoute(ref, result))
            continue
        return RouteDecision(requested, usable[0], ref, tuple(skipped), False, observed_at)
    return RouteDecision(requested, usable[0], usable[0], tuple(skipped), True, observed_at)


class GatedFallbacks:
    """Child-owned router wrapper whose ``fallbacks`` drop candidates on blocked providers.

    ``headroom`` receives the candidate's provider name (``candidate.provider.name``).
    """

    def __init__(self, router: Any, headroom: Callable[[str], UsageVerdict]) -> None:
        self._router = router
        self._headroom = headroom

    def resolve(self, request: Any, /) -> Any:
        return self._router.resolve(request)

    def fallbacks(self, request: Any, /) -> list:
        candidates = list(self._router.fallbacks(request))
        kept = [item for item in candidates if self._headroom(item.provider.name).state != "blocked"]
        return kept or candidates

    def __getattr__(self, name: str) -> Any:
        return getattr(self._router, name)


__all__ = [
    "FIVE_HOURS",
    "WEEK",
    "WINDOW_KINDS",
    "GatedFallbacks",
    "ProviderUsage",
    "RouteDecision",
    "SkippedRoute",
    "Thresholds",
    "UsagePolicy",
    "UsageSnapshot",
    "UsageVerdict",
    "UsageWindow",
    "choose_route",
    "quota_identity",
    "verdict",
    "window_kind",
]
