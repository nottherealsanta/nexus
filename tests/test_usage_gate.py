"""Pure usage-gate tests: window kinds, verdicts, route choice and the fallback filter."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nexus.model.usage_gate import (
    GatedFallbacks,
    ProviderUsage,
    Thresholds,
    UsagePolicy,
    UsageSnapshot,
    UsageVerdict,
    UsageWindow,
    choose_route,
    quota_identity,
    verdict,
    window_kind,
)

NOW = 10_000.0
LATER = 20_000


def _snapshot(*windows, quota="codex", error="", fetched_at=NOW):
    return UsageSnapshot({quota: ProviderUsage(quota, tuple(windows), error)}, fetched_at)


def _win(kind, used, resets_at=LATER, label=""):
    return UsageWindow(kind, used, resets_at, label or kind)


def _verdict(snapshot, policy=None, quota="codex", now=NOW):
    return verdict(snapshot, quota, policy or UsagePolicy.from_section(None), now=now)


@pytest.mark.parametrize("seconds,kind", [
    (None, "other"), (0, "other"), (16_199, "other"), (16_200, "five_hour"), (18_000, "five_hour"),
    (19_800, "five_hour"), (19_801, "other"), (6.5 * 86_400, "weekly"), (604_800, "weekly"), (7.5 * 86_400, "weekly"),
    (7.6 * 86_400, "other"), (27.5 * 86_400, "monthly"), (31.5 * 86_400, "monthly"), (32 * 86_400, "other"),
    (86_400, "other"),
])
def test_window_kind_bounds(seconds, kind):
    assert window_kind(seconds) == kind


def test_thresholds_for_kind():
    t = Thresholds()
    assert (t.for_kind("five_hour"), t.for_kind("weekly"), t.for_kind("monthly"), t.for_kind("other")) == (10.0, 5.0, 2.0, None)


def test_equality_blocks_and_just_above_is_ok():
    assert _verdict(_snapshot(_win("weekly", 95.0))).state == "blocked"
    assert _verdict(_snapshot(_win("weekly", 94.9))).state == "ok"


def test_fractional_values_compare_at_full_precision():
    policy = UsagePolicy.from_section({"weekly_min_remaining": 5.5})
    assert _verdict(_snapshot(_win("weekly", 94.5)), policy).state == "blocked"
    assert _verdict(_snapshot(_win("weekly", 94.49)), policy).state == "ok"


def test_threshold_zero_and_hundred():
    zero = UsagePolicy.from_section({"weekly_min_remaining": 0})
    assert _verdict(_snapshot(_win("weekly", 99.9)), zero).state == "ok"
    assert _verdict(_snapshot(_win("weekly", 100.0)), zero).state == "blocked"
    full = UsagePolicy.from_section({"weekly_min_remaining": 100})
    assert _verdict(_snapshot(_win("weekly", 0.0)), full).state == "blocked"


def test_monthly_only_provider_is_not_blocked_by_the_weekly_rule():
    result = _verdict(_snapshot(_win("monthly", 90.0)))
    assert result.state == "ok" and result.kind == "monthly" and result.threshold_percent == 2.0
    assert _verdict(_snapshot(_win("monthly", 98.0))).state == "blocked"


def test_other_windows_never_gate():
    result = _verdict(_snapshot(_win("other", 100.0)))
    assert result.state == "unknown"
    assert _verdict(_snapshot(_win("other", 100.0), _win("weekly", 10.0))).state == "ok"


def test_unknown_cases():
    assert _verdict(None).state == "unknown"
    assert _verdict(_snapshot(_win("weekly", 100.0)), UsagePolicy.from_section({"enabled": False})).state == "unknown"
    assert _verdict(_snapshot(_win("weekly", 100.0)), quota="other-provider").state == "unknown"
    assert _verdict(_snapshot(_win("weekly", 100.0), error="HTTP 401")).state == "unknown"
    assert _verdict(_snapshot(_win("weekly", None))).state == "unknown"
    assert _verdict(_snapshot(_win("weekly", 100.0, resets_at=int(NOW)))).state == "unknown"
    assert _verdict(_snapshot(_win("weekly", 100.0), fetched_at=NOW - 541)).state == "unknown"
    assert _verdict(_snapshot(_win("weekly", 100.0), fetched_at=NOW - 540)).state == "blocked"


def test_a_passed_window_is_skipped_but_others_still_gate():
    snapshot = _snapshot(_win("five_hour", 100.0, resets_at=int(NOW) - 1), _win("weekly", 99.0))
    assert _verdict(snapshot).kind == "weekly"


def test_latest_reset_is_reported_and_none_sorts_latest():
    snapshot = _snapshot(_win("five_hour", 100.0, resets_at=15_000), _win("weekly", 100.0, resets_at=LATER))
    assert _verdict(snapshot).kind == "weekly"
    snapshot = _snapshot(_win("weekly", 100.0, resets_at=LATER), _win("monthly", 100.0, resets_at=None))
    result = _verdict(snapshot)
    assert result.kind == "monthly" and result.resets_at is None


def test_any_blocked_window_blocks():
    assert _verdict(_snapshot(_win("five_hour", 0.0), _win("weekly", 100.0))).state == "blocked"


def test_per_provider_override_inherits_missing_kinds():
    section = SimpleNamespace(weekly_min_remaining=5, providers={
        "codex": SimpleNamespace(five_hour_min_remaining=50, weekly_min_remaining=None, monthly_min_remaining=None),
        "claude-agent": {"weekly_min_remaining": 30}})
    policy = UsagePolicy.from_section(section)
    assert policy.thresholds_for("codex") == Thresholds(50.0, 5.0, 2.0)
    assert policy.thresholds_for("claude-agent") == Thresholds(10.0, 30.0, 2.0)
    assert policy.thresholds_for("xai") == policy.thresholds
    assert _verdict(_snapshot(_win("five_hour", 60.0)), policy).state == "blocked"
    assert _verdict(_snapshot(_win("five_hour", 60.0)), UsagePolicy.from_section(None)).state == "ok"


def test_policy_defaults_and_refresh():
    policy = UsagePolicy.from_section(SimpleNamespace(refresh_seconds=60))
    assert policy.enabled and policy.max_age_seconds == 180 and policy.thresholds == Thresholds()
    assert UsagePolicy.from_section(None).max_age_seconds == 540


def test_snapshot_from_result_reads_rows_and_defaults_kind():
    result = {"fetched_at": 5.0, "providers": [
        {"id": "codex", "error": "", "windows": [
            {"label": "5-hour", "used_percent": 1, "resets_at": 9, "kind": "five_hour"},
            {"label": "Old", "used_percent": 2, "resets_at": None}]},
        {"id": "xai", "error": "HTTP 500", "windows": []}]}
    snapshot = UsageSnapshot.from_result(result)
    assert snapshot.fetched_at == 5.0 and set(snapshot.providers) == {"codex", "xai"}
    assert [w.kind for w in snapshot.providers["codex"].windows] == ["five_hour", "other"]
    assert snapshot.providers["xai"].error == "HTTP 500"


@pytest.mark.parametrize("provider,kind,expected", [
    ("claude-agent", "claude-agent", "claude-agent"), ("claude-agent", None, "claude-agent"),
    ("my-claude", "claude-code", "claude-agent"), ("antigravity", "antigravity", "antigravity"),
    ("gemini-work", "antigravity", "antigravity"), ("codex", None, "codex"), ("codex", "openai", "codex"),
])
def test_quota_identity(provider, kind, expected):
    assert quota_identity(provider, kind) == expected


def test_verdict_to_dict():
    result = UsageVerdict("codex", "blocked", "weekly", "Weekly", 3.0, 5.0, 9)
    assert result.to_dict() == {"quota": "codex", "state": "blocked", "kind": "weekly", "label": "Weekly",
                                "remaining_percent": 3.0, "threshold_percent": 5.0, "resets_at": 9}


# -- routes ---------------------------------------------------------------------------------


def _headroom(states):
    return lambda ref: UsageVerdict(ref, states.get(ref, "unknown"), "weekly", "Weekly", 1.0, 5.0, 7)


def test_choose_route_keeps_the_first_when_ok():
    decision = choose_route("a/m", ["a/m", "b/m"], _headroom({"a/m": "ok"}), observed_at=3.0)
    assert (decision.first, decision.chosen, decision.rerouted, decision.all_blocked) == ("a/m", "a/m", False, False)


def test_choose_route_reroutes_past_blocked_and_unknown_wins():
    decision = choose_route("a/m", ["a/m", "b/m", "c/m"], _headroom({"a/m": "blocked", "b/m": "unknown"}))
    assert decision.chosen == "b/m" and decision.rerouted and decision.first == "a/m"
    assert [s.reference for s in decision.skipped] == ["a/m"]
    event = decision.to_event()
    assert event["skipped"] == [{"reference": "a/m", "quota": "a/m", "kind": "weekly", "label": "Weekly",
                                 "remaining_percent": 1.0, "threshold_percent": 5.0, "resets_at": 7}]
    assert event["requested"] == "a/m" and event["chosen"] == "b/m" and event["all_blocked"] is False


def test_choose_route_all_blocked_picks_the_first_eligible():
    decision = choose_route("a/m", ["a/m", "b/m"], _headroom({"a/m": "blocked", "b/m": "blocked"}),
                            eligible=lambda ref: ref != "a/m")
    assert decision.all_blocked and decision.chosen == decision.first == "b/m" and not decision.rerouted


def test_choose_route_eligibility_and_none():
    decision = choose_route("a/m", ["a/m", "b/m"], _headroom({}), eligible=lambda ref: ref == "b/m")
    assert decision.first == "b/m" and decision.chosen == "b/m"
    assert choose_route("a/m", ["a/m"], _headroom({}), eligible=lambda ref: False) is None
    assert choose_route("a/m", [], _headroom({})) is None


def test_event_caps_skipped_entries():
    refs = [f"p{i}/m" for i in range(12)]
    decision = choose_route("p0/m", refs, _headroom({ref: "blocked" for ref in refs[:11]}))
    assert decision.chosen == "p11/m" and len(decision.skipped) == 11 and len(decision.to_event()["skipped"]) == 8


# -- GatedFallbacks -------------------------------------------------------------------------


class _Router:
    def __init__(self, names):
        self.names = names
        self.extra = "delegated"

    def resolve(self, request, /):
        return ("resolved", request)

    def fallbacks(self, request, /):
        return [SimpleNamespace(provider=SimpleNamespace(name=name), model="m") for name in self.names]


def _names(items):
    return [item.provider.name for item in items]


def test_gated_fallbacks_filters_blocked_and_keeps_order():
    gated = GatedFallbacks(_Router(["a", "b", "c", "d"]), lambda name: UsageVerdict(name, "blocked" if name in "bd" else "ok"))
    assert _names(gated.fallbacks("req")) == ["a", "c"]


def test_gated_fallbacks_returns_everything_when_all_blocked():
    gated = GatedFallbacks(_Router(["a", "b"]), lambda name: UsageVerdict(name, "blocked"))
    assert _names(gated.fallbacks("req")) == ["a", "b"]


def test_gated_fallbacks_delegates_resolve_and_attributes():
    gated = GatedFallbacks(_Router([]), lambda name: UsageVerdict(name, "ok"))
    assert gated.resolve("req") == ("resolved", "req") and gated.extra == "delegated"
