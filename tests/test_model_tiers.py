"""Phase 5.5 tier resolution tests (plan section 15.4).

Tier assignment is structural: :mod:`nexus.model.tiers` never imports the
registry's ``ModelInfo``/``Cost``, so these tests exercise dataclasses,
``SimpleNamespace``, and plain dicts interchangeably. Every case is offline and
deterministic; no provider or catalogue is touched.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.model.tiers import (
    BUILTIN_TIERS,
    COST_LOW_MAX,
    COST_MEDIUM_MAX,
    HIGH,
    LOW,
    MEDIUM,
    TIER_ORDER,
    TierOrdering,
    TierTable,
    assign_tier,
    blended_cost,
    clamp,
    model_reference_keys,
    normalize_overrides,
    ordering,
    parse_reference,
    resolve_reference,
    resolve_tier,
    tier_for_cost,
    tier_rank,
)


@dataclass(frozen=True)
class Cost:
    input: float
    output: float


@dataclass(frozen=True)
class Info:
    provider: str | None
    id: str
    cost: Cost | None = None
    aliases: tuple[str, ...] = ()


def info(provider, model_id, cost=None, aliases=()):
    return Info(provider=provider, id=model_id, cost=cost, aliases=aliases)


# -- ordering --------------------------------------------------------------


def test_canonical_ordering_is_low_medium_high():
    assert TIER_ORDER == (LOW, MEDIUM, HIGH)
    assert tier_rank(LOW) < tier_rank(MEDIUM) < tier_rank(HIGH)
    assert ordering() == TIER_ORDER


def test_ordering_accepts_custom_names_around_builtins():
    names = ordering((LOW, "cheap", MEDIUM, HIGH, "ultra"))
    assert names == (LOW, "cheap", MEDIUM, HIGH, "ultra")
    assert tier_rank("cheap", order=names) < tier_rank(MEDIUM, order=names)
    assert tier_rank("ultra", order=names) > tier_rank(HIGH, order=names)


def test_ordering_deduplicates_preserving_first_position():
    assert ordering((LOW, MEDIUM, HIGH, MEDIUM)) == TIER_ORDER


@pytest.mark.parametrize(
    "names",
    [
        (),
        (LOW, HIGH),  # medium missing
        (MEDIUM, LOW, HIGH),  # reordered
        (HIGH, MEDIUM, LOW),
    ],
)
def test_ordering_rejects_incomplete_or_reordered_builtins(names):
    with pytest.raises(ConfigError):
        ordering(names)


def test_tier_rank_unknown_tier_raises():
    with pytest.raises(ConfigError):
        tier_rank("ultra")


def test_tier_ordering_rank_returns_none_for_unknown():
    assert TierOrdering().rank("ultra") is None


# -- curated map -----------------------------------------------------------


def test_builtin_map_wins_over_cost():
    assert BUILTIN_TIERS["anthropic/claude-opus-5"] == HIGH
    # gpt-5.6 blends to 9.0 (medium by cost) but is curated high on purpose.
    model = info("openai", "gpt-5.6", Cost(8.0, 4.0))
    assert blended_cost(model) == 9.0
    assert tier_for_cost(9.0) == MEDIUM
    assert assign_tier(model) == HIGH


def test_builtin_resolves_provider_and_bare_references():
    assert resolve_tier("anthropic/claude-opus-5") == HIGH
    assert resolve_tier("claude-haiku-4-5") == LOW
    assert parse_reference("claude-haiku-4-5").provider is None
    assert resolve_tier("anthropic/claude-opus-5").source == "builtin"


def test_builtin_map_is_open_to_override():
    table = TierTable(builtin={})
    assert table.assign(info("anthropic", "claude-opus-5", Cost(0.1, 0.1))) == LOW


def test_info_aliases_resolve_to_curated_tier():
    model = info("aggregator", "proxy-model", aliases=("anthropic/claude-opus-5",))
    assert model_reference_keys(model) == (
        "aggregator/proxy-model",
        "proxy-model",
        "anthropic/claude-opus-5",
    )
    assert assign_tier(model) == HIGH


# -- overrides / custom names ---------------------------------------------


def test_user_override_beats_builtin_and_cost():
    model = info("anthropic", "claude-opus-5", Cost(0.1, 0.1))
    assert assign_tier(model) == HIGH  # builtin
    table = TierTable(overrides={"anthropic/claude-opus-5": LOW})
    assert table.assign(model) == LOW
    resolution = table.resolve("anthropic/claude-opus-5")
    assert resolution.tier == LOW
    assert resolution.source == "override"


def test_overrides_accept_config_shape_and_invert():
    normalized = normalize_overrides({"high": ["openai/gpt-5.6"], "low": ["x/y"]})
    assert normalized == {"openai/gpt-5.6": "high", "x/y": "low"}


def test_overrides_accept_canonical_shape_unchanged():
    assert normalize_overrides({"x/y": HIGH}) == {"x/y": HIGH}
    assert normalize_overrides(None) == {}


def test_overrides_reject_mixed_shapes_as_conflict():
    with pytest.raises(ConfigError):
        normalize_overrides({"high": ["a/b"], "c/d": "low"})


def test_overrides_reject_bad_value_types():
    with pytest.raises(ConfigError):
        normalize_overrides({"high": 3})


def test_conflicting_overrides_are_deterministic_last_wins():
    normalized = normalize_overrides({"low": ["a/b"], "high": ["a/b"]})
    assert normalized == {"a/b": "high"}


def test_custom_tier_name_is_ordered_after_builtins_by_default():
    table = TierTable(overrides={"ultra": ["a/max"]})
    assert table.assign(info("a", "max")) == "ultra"
    assert table.order == (LOW, MEDIUM, HIGH, "ultra")
    assert table.rank("ultra") > table.rank(HIGH)


def test_custom_tier_is_resolvable_as_a_reference():
    table = TierTable(overrides={"ultra": ["a/max"]})
    assert resolve_tier("ultra", table=table) == "ultra"
    assert resolve_tier("a/max", table=table) == "ultra"


def test_default_must_be_in_ordering():
    with pytest.raises(ConfigError):
        TierTable(default="nope")


def test_custom_default_tier_is_honored():
    table = TierTable(order=(LOW, MEDIUM, HIGH, "ultra"), default="ultra")
    assert table.resolve(None).tier == "ultra"
    assert table.resolve(None).source == "default"


# -- cost fallback ---------------------------------------------------------


@pytest.mark.parametrize(
    ("inp", "out", "expected"),
    [
        (0.0, 0.0, LOW),
        (2.5, 0.0, LOW),  # boundary: blended == 2.5 -> low
        (2.0, 2.0, LOW),  # 2.5
        (2.4, 0.4, LOW),  # 2.5
        (2.0, 2.4, MEDIUM),  # 2.6
        (10.0, 0.0, MEDIUM),  # boundary: blended == 10 -> medium
        (8.0, 8.0, MEDIUM),  # 10.0
        (8.0, 8.4, HIGH),  # 10.1
        (11.0, 1.0, HIGH),  # 11.25
        (20.0, 20.0, HIGH),  # 25.0
    ],
)
def test_cost_boundaries_classify_expected_tier(inp, out, expected):
    model = info("opaque", "some-model", Cost(inp, out))
    assert blended_cost(model) == inp + out / 4
    assert tier_for_cost(inp + out / 4) == expected
    assert assign_tier(model) == expected


def test_cost_threshold_constants_are_exact():
    assert tier_for_cost(COST_LOW_MAX) == LOW
    assert tier_for_cost(COST_LOW_MAX + 1e-9) == MEDIUM
    assert tier_for_cost(COST_MEDIUM_MAX) == MEDIUM
    assert tier_for_cost(COST_MEDIUM_MAX + 1e-9) == HIGH


def test_missing_cost_reads_as_low():
    assert blended_cost(info("local", "llama")) is None
    assert assign_tier(info("local", "llama")) == LOW
    assert resolve_tier("local/llama").tier == LOW
    assert resolve_tier("local/llama").source == "cost-missing"


def test_zero_cost_is_low_not_missing():
    model = info("local", "llama", Cost(0.0, 0.0))
    assert blended_cost(model) == 0.0
    assert assign_tier(model) == LOW


def test_cost_classifies_after_override_and_curated_miss():
    table = TierTable(builtin={})
    assert table.assign(info("x", "cheap", Cost(1.0, 1.0))) == LOW
    assert table.assign(info("x", "mid", Cost(5.0, 5.0))) == MEDIUM
    assert table.assign(info("x", "big", Cost(20.0, 20.0))) == HIGH


def test_malformed_cost_reads_as_missing():
    assert blended_cost(info("x", "y", Cost("nope", "nan"))) is None
    assert blended_cost(info("x", "y", Cost(float("nan"), 1.0))) is None
    assert blended_cost(info("x", "y", Cost(float("inf"), 1.0))) is None
    assert assign_tier(info("x", "y", Cost("nope", "nan"))) == LOW


def test_blended_cost_needs_a_cost_bearing_wrapper():
    # A bare Cost is not a ModelInfo: blended_cost reads ``.cost`` structurally.
    assert blended_cost(Cost(8.0, 4.0)) is None
    assert blended_cost(info("x", "y", Cost(8.0, 4.0))) == 9.0


def test_blended_cost_reads_mappings_and_objects():
    as_dict = {"provider": "openai", "id": "gpt-5.6", "cost": {"input": 8.0, "output": 4.0}}
    assert blended_cost(as_dict) == 9.0
    ns = SimpleNamespace(
        provider="x",
        id="y",
        cost=SimpleNamespace(input=20.0, output=20.0),
    )
    assert blended_cost(ns) == 25.0
    assert assign_tier(ns) == HIGH


# -- opaque / bare references ---------------------------------------------


def test_parse_reference_splits_provider_and_bare_id():
    parsed = parse_reference("anthropic/claude-opus-5")
    assert isinstance(parsed, str)
    assert (parsed.provider, parsed.model) == ("anthropic", "claude-opus-5")
    bare = parse_reference("claude-opus-5")
    assert (bare.provider, bare.model) == (None, "claude-opus-5")


@pytest.mark.parametrize("bad", ["", "  ", "anthropic/", "/claude", None, 7])
def test_parse_reference_rejects_empty_or_malformed(bad):
    with pytest.raises(ConfigError):
        parse_reference(bad)


def test_opaque_provider_and_model_resolve_low():
    assert parse_reference("unknown/thing").provider == "unknown"
    assert resolve_tier("unknown/thing") == LOW
    assert resolve_tier("totally-opaque").source == "cost-missing"


def test_aggregator_reference_falls_back_to_tail_segment():
    table = TierTable(builtin={"openai/o1-pro": HIGH})
    assert table.resolve("openrouter/openai/o1-pro").tier == HIGH


def test_assign_tier_accepts_reference_strings():
    assert assign_tier("anthropic/claude-opus-5") == HIGH
    assert assign_tier("unknown/model") == LOW


# -- inherit / default -----------------------------------------------------


def test_none_reference_inherits_parent_then_defaults():
    table = TierTable()
    inherited = table.resolve(None, parent=HIGH)
    assert inherited.tier == HIGH
    assert inherited.source == "inherit"
    fallback = table.resolve(None)
    assert fallback.tier == MEDIUM
    assert fallback.source == "default"
    assert table.resolve("").tier == MEDIUM


def test_info_is_classified_when_no_reference_is_given():
    assert resolve_tier(info=info("local", "llama")).tier == LOW
    assert resolve_tier(info=info("openai", "gpt-5.6")).tier == HIGH


# -- clamp -----------------------------------------------------------------


def test_clamp_never_widens():
    assert clamp(HIGH, MEDIUM) == MEDIUM
    assert clamp(MEDIUM, MEDIUM) == MEDIUM
    assert clamp(LOW, MEDIUM) == LOW
    assert clamp(LOW, HIGH) == LOW
    assert clamp(MEDIUM, HIGH) == MEDIUM


def test_clamp_unknown_request_is_capped_but_unknown_ceiling_errors():
    assert clamp("ultra", MEDIUM) == MEDIUM
    with pytest.raises(ConfigError):
        clamp(HIGH, "ultra")


def test_tier_table_clamp_uses_custom_ordering():
    table = TierTable(order=(LOW, MEDIUM, HIGH, "ultra"))
    assert table.clamp("ultra", MEDIUM) == MEDIUM
    assert table.clamp(MEDIUM, "ultra") == MEDIUM


# -- resolution contract ---------------------------------------------------


def test_tier_names_resolve_everywhere_a_model_string_is_accepted():
    for name in TIER_ORDER:
        resolution = resolve_tier(name)
        assert resolution == name
        assert resolution.tier == name
        assert resolution.source == "tier"


def test_resolution_is_a_string_subclass():
    resolution = resolve_tier("medium")
    assert isinstance(resolution, str)
    assert f"{resolution}" == MEDIUM
    assert resolution == "medium"


def test_max_tier_clamp_is_auditable_and_deterministic():
    first = resolve_tier("anthropic/claude-opus-5", max_tier=MEDIUM)
    second = resolve_reference("anthropic/claude-opus-5", max_tier=MEDIUM)
    assert first == second == MEDIUM
    assert first.clamped is True
    assert first.max_tier == MEDIUM
    assert first.reference == "anthropic/claude-opus-5"
    assert first.diagnostics == (
        "clamped 'high' to 'medium' (max_tier='medium')",
    )


def test_no_clamp_leaves_diagnostics_empty():
    resolution = resolve_tier("anthropic/claude-haiku-4-5", max_tier=HIGH)
    assert resolution.tier == LOW
    assert resolution.clamped is False
    assert resolution.diagnostics == ()


def test_unknown_reference_diagnostic_is_explicit():
    resolution = resolve_tier("no-such/model")
    assert resolution.tier == LOW
    assert resolution.source == "cost-missing"
    assert any("unknown model reference" in d for d in resolution.diagnostics)


def test_resolve_reference_matches_resolve_tier():
    assert resolve_reference("low") == resolve_tier("low") == LOW


def test_explicit_reference_beats_supplied_info():
    # A tier name is the strongest signal; info is only consulted otherwise.
    assert resolve_tier(HIGH, info=info("local", "llama")).tier == HIGH
