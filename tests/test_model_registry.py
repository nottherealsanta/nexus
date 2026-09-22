"""Phase 5.5 model registry tests (plan section 15.3).

Everything here is offline. Acquisition is exercised through an injected
:class:`CatalogueFetcher`, cache/snapshot files in ``tmp_path``, or the
in-memory ``install`` seam; the default network fetcher is only constructed and
driven through ``httpx.MockTransport``.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import msgspec
import pytest

from nexus.errors import ConfigError, NexusError
from nexus.model.registry import (
    ADAPTER_ANTHROPIC,
    ADAPTER_GEMINI,
    ADAPTER_OLLAMA,
    ADAPTER_OPENAI,
    OPENAI_COMPATIBLE,
    CatalogueError,
    Cost,
    HttpxCatalogueFetcher,
    ModelInfo,
    ModelRegistry,
    ModelRegistryError,
    build_index,
    map_provider,
    parse_catalogue,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "models"
CATALOGUE_PATH = FIXTURES / "catalogue.json"
MISSING = FIXTURES / "no-such-file.json"

ANTHROPIC = {"ANTHROPIC_API_KEY": "1"}
OPENAI = {"OPENAI_API_KEY": "1"}
BOTH = {**ANTHROPIC, **OPENAI}

DAY = 86_400.0


def catalogue_bytes() -> bytes:
    return CATALOGUE_PATH.read_bytes()


class FakeFetcher:
    def __init__(self, payload: bytes = b"", *, error: BaseException | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def fetch(self, url, *, timeout_s, max_bytes, verify_tls):
        self.calls.append(
            {
                "url": url,
                "timeout_s": timeout_s,
                "max_bytes": max_bytes,
                "verify_tls": verify_tls,
            }
        )
        if self.error is not None:
            raise self.error
        return self.payload


def make_registry(
    *,
    raw: bytes | None = None,
    env: dict[str, str] | None = None,
    providers: dict | None = None,
    **kwargs,
) -> ModelRegistry:
    kwargs.setdefault("snapshot_path", MISSING)
    registry = ModelRegistry(env=env or {}, providers=providers, **kwargs)
    if raw is not None:
        registry.install_raw(raw)
    return registry


# --------------------------------------------------------------------------
# Frozen data model
# --------------------------------------------------------------------------


def test_cost_is_frozen():
    cost = Cost(input=1.0, output=2.0)
    with pytest.raises(AttributeError):
        cost.input = 3.0  # type: ignore[misc]


def test_model_info_is_frozen():
    info = ModelInfo(provider="anthropic", id="x")
    with pytest.raises(AttributeError):
        info.id = "y"  # type: ignore[misc]


def test_model_info_ref_and_capabilities():
    info = ModelInfo(
        provider="anthropic",
        id="claude",
        tool_call=True,
        reasoning=True,
        structured_output=True,
        input_modalities=("text", "image", "pdf"),
        output_modalities=("text",),
        context=200_000,
        max_output=32_000,
    )
    assert info.ref == "anthropic/claude"
    caps = info.capabilities()
    assert caps.tools is True
    assert caps.thinking is True
    assert caps.vision is True
    assert caps.documents is True
    assert caps.json_schema_strict is True
    assert caps.max_context_tokens == 200_000
    assert caps.max_output_tokens == 32_000
    assert caps.parallel_tool_calls is False
    assert caps.prompt_caching is False


def test_model_info_has_no_url_or_credential_fields():
    fields = set(ModelInfo.__struct_fields__)
    assert "base_url" not in fields
    assert "api_key" not in fields
    assert "url" not in fields
    assert "headers" not in fields


def test_registry_errors_are_nexus_errors():
    assert issubclass(ModelRegistryError, NexusError)
    assert issubclass(CatalogueError, ModelRegistryError)


# --------------------------------------------------------------------------
# Parse and bounds
# --------------------------------------------------------------------------


def test_parse_valid_catalogue():
    catalogue = parse_catalogue(catalogue_bytes())
    assert catalogue.license_pending is False
    ids = {p.id for p in catalogue.providers}
    assert {
        "anthropic",
        "openai",
        "openrouter",
        "google",
        "mystery",
        "quiet",
        "evil",
    } <= ids
    anthropic = next(p for p in catalogue.providers if p.id == "anthropic")
    assert anthropic.name == "Anthropic"
    assert anthropic.npm == "@ai-sdk/anthropic"
    assert anthropic.env == ("ANTHROPIC_API_KEY",)
    opus = next(m for m in anthropic.models if m.id == "claude-opus-5")
    assert opus.cost == Cost(input=15.0, output=75.0, cache_read=1.5, cache_write=18.75)
    assert (opus.context, opus.max_output) == (200_000, 32_000)
    assert opus.tool_call and opus.reasoning and opus.structured_output
    assert opus.input_modalities == ("text", "image", "pdf")
    assert opus.output_modalities == ("text",)


def test_parse_license_pending_label():
    raw = b'{"_license": "pending", "anthropic": {"models": {}}}'
    assert parse_catalogue(raw).license_pending is True


def test_parse_ignores_unknown_top_level_metadata():
    raw = b'{"_note": "hello", "anthropic": {"models": {}}}'
    catalogue = parse_catalogue(raw)
    assert [p.id for p in catalogue.providers] == ["anthropic"]


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'"text"',
        b'{"anthropic": 5}',
        b'{"anthropic": {"models": {"m": 5}}}',
        b'{"anthropic": {"models": {"m": {"cost": {"input": -1, "output": 0}}}}}',
        b'{"anthropic": {"models": {"m": {"cost": {"input": "x", "output": 0}}}}}',
        b'{"anthropic": {"models": {"m": {"modalities": {"output": "text"}}}}}',
        b'{"anthropic": {"models": {"m": {"limit": {"context": -1}}}}}',
        b'{"anthropic": {"env": "ANTHROPIC_API_KEY"}}',
        b'{"anthropic": {"env": [5]}}',
    ],
)
def test_parse_rejects_malformed_catalogue(raw):
    with pytest.raises(CatalogueError):
        parse_catalogue(raw)


def test_parse_rejects_oversized_bytes():
    with pytest.raises(CatalogueError, match="bound"):
        parse_catalogue(b"{}", max_bytes=1)


def test_parse_rejects_too_many_providers():
    raw = json.dumps({f"p{i}": {} for i in range(2001)}).encode()
    with pytest.raises(CatalogueError, match="too many providers"):
        parse_catalogue(raw)


def test_parse_rejects_too_many_env_names():
    raw = json.dumps(
        {"p": {"env": [f"VAR_{i}" for i in range(65)]}}
    ).encode()
    with pytest.raises(CatalogueError, match="env"):
        parse_catalogue(raw)


def test_parse_rejects_overlong_provider_id():
    raw = json.dumps({"p" * 300: {}}).encode()
    with pytest.raises(CatalogueError, match="length bound"):
        parse_catalogue(raw)


# --------------------------------------------------------------------------
# Provider mapping and status
# --------------------------------------------------------------------------


def test_map_provider_prefers_npm_then_direct():
    assert map_provider("anthropic", None) == ADAPTER_ANTHROPIC
    assert map_provider("anything", "@ai-sdk/anthropic") == ADAPTER_ANTHROPIC
    assert map_provider("anything", "@ai-sdk/openai") == ADAPTER_OPENAI
    assert map_provider("anything", "@ai-sdk/openai-compatible") == ADAPTER_OPENAI
    assert map_provider("anything", "@ai-sdk/google") == ADAPTER_GEMINI
    assert map_provider("anything", "@ai-sdk/google-vertex") == ADAPTER_GEMINI
    assert map_provider("anything", "@ai-sdk/ollama") == ADAPTER_OLLAMA
    assert map_provider("ollama", None) == ADAPTER_OLLAMA
    assert map_provider("unknown", "@weird/thing") is None


def test_config_kind_overrides_mapping():
    reg = make_registry(
        raw=catalogue_bytes(),
        env={"ANTHROPIC_API_KEY": "1"},
        providers={"anthropic": {"kind": OPENAI_COMPATIBLE, "base_url": "https://x/v1"}},
    )
    status = reg.provider_status("anthropic")
    assert status.adapter == ADAPTER_OPENAI
    assert status.kind == OPENAI_COMPATIBLE


def test_configured_provider_is_retained_without_env():
    reg = make_registry(
        raw=catalogue_bytes(), env={}, providers={"openai": {"base_url": "https://x"}}
    )
    assert {m.provider for m in reg.list()} == {"openai"}
    assert [s.id for s in reg.providers()] == ["openai"]


def test_reachable_provider_is_retained_by_env_name_only():
    env = {"ANTHROPIC_API_KEY": "secret-value", "MYSTERY_API_KEY": "x"}
    reg = make_registry(raw=catalogue_bytes(), env=env)
    assert {s.id for s in reg.providers()} == {"anthropic", "mystery"}


def test_empty_env_value_does_not_count_as_reachable():
    reg = make_registry(raw=catalogue_bytes(), env={"ANTHROPIC_API_KEY": ""})
    assert reg.providers() == []


def test_unconfigured_unreachable_providers_are_dropped():
    reg = make_registry(raw=catalogue_bytes(), env=ANTHROPIC)
    assert {s.id for s in reg.providers()} == {"anthropic"}


def test_unmapped_reachable_provider_is_listable_but_unselectable():
    reg = make_registry(raw=catalogue_bytes(), env={"MYSTERY_API_KEY": "x"})
    status = reg.provider_status("mystery")
    assert status is not None
    assert status.selectable is False
    assert status.adapter is None
    assert "no Nexus adapter" in status.reason
    assert "mystery" in reg.status().unselectable
    assert any(m.provider == "mystery" for m in reg.list())
    with pytest.raises(ConfigError, match="not selectable"):
        reg.resolve("mystery/m1")


def test_unmapped_provider_with_base_url_falls_back_to_openai_compatible():
    reg = make_registry(
        raw=catalogue_bytes(),
        env={},
        providers={"mystery": {"base_url": "https://mystery.example/v1"}},
    )
    status = reg.provider_status("mystery")
    assert status.selectable is True
    assert status.adapter == ADAPTER_OPENAI
    assert status.kind == OPENAI_COMPATIBLE
    assert reg.resolve("mystery/m1").provider == "mystery"


def test_catalogue_cannot_set_base_url_or_credentials():
    reg = make_registry(raw=catalogue_bytes(), env={"EVIL_API_KEY": "x"})
    status = reg.provider_status("evil")
    assert status.selectable is False
    assert status.adapter is None
    assert "attacker" not in (status.reason or "")


def test_env_values_never_enter_the_registry():
    reg = make_registry(
        raw=catalogue_bytes(), env={"ANTHROPIC_API_KEY": "super-secret-value"}
    )
    blob = msgspec.json.encode(reg.list())
    assert b"super-secret-value" not in blob


# --------------------------------------------------------------------------
# Filtering and canonicalization
# --------------------------------------------------------------------------


def test_output_text_filter_drops_non_text_models():
    reg = make_registry(raw=catalogue_bytes(), env=ANTHROPIC)
    ids = {m.id for m in reg.list()}
    assert "claude-opus-5" in ids
    assert "claude-embed" not in ids


def test_aggregator_alias_resolves_to_direct_provider():
    env = {**BOTH, "OPENROUTER_API_KEY": "1"}
    reg = make_registry(raw=catalogue_bytes(), env=env)
    opus = reg.resolve("openrouter/anthropic/claude-opus-5")
    assert opus.provider == "anthropic"
    assert opus.id == "claude-opus-5"
    assert "openrouter/anthropic/claude-opus-5" in opus.aliases
    gpt = reg.resolve("openai/gpt-5.6")
    assert gpt.aliases == ("openrouter/openai/gpt-5.6",)
    assert {m.ref for m in reg.list()} == {
        "anthropic/claude-opus-5",
        "openai/gpt-5.6",
    }


def test_aggregator_only_keeps_its_own_provider():
    reg = make_registry(
        raw=catalogue_bytes(),
        env={"OPENROUTER_API_KEY": "1"},
        providers={"openrouter": {"base_url": "https://openrouter.ai/api/v1"}},
    )
    info = reg.resolve("openrouter/openai/gpt-5.6")
    assert info.provider == "openrouter"
    assert info.id == "openai/gpt-5.6"
    assert info.aliases == ()
    assert reg.get("openai/gpt-5.6").provider == "openrouter"


def test_same_bare_id_on_two_providers_is_not_merged():
    raw = json.dumps(
        {
            "pa": {
                "env": ["PA_KEY"],
                "models": {
                    "shared": {
                        "modalities": {"output": ["text"]},
                        "cost": {"input": 1, "output": 1},
                    }
                },
            },
            "pb": {
                "env": ["PB_KEY"],
                "models": {
                    "shared": {
                        "modalities": {"output": ["text"]},
                        "cost": {"input": 1, "output": 1},
                    }
                },
            },
        }
    ).encode()
    reg = make_registry(raw=raw, env={"PA_KEY": "1", "PB_KEY": "1"})
    assert len(reg.list()) == 2
    assert reg.get("shared") is None
    assert reg.get("pa/shared").provider == "pa"
    assert reg.get("pb/shared").provider == "pb"


def test_direct_provider_wins_even_when_aggregator_has_lower_cost():
    env = {"OPENAI_API_KEY": "1", "OPENROUTER_API_KEY": "1"}
    reg = make_registry(raw=catalogue_bytes(), env=env)
    gpt = reg.get("openai/gpt-5.6")
    assert gpt.cost.input == 2.0
    assert gpt.source == "catalogue"


def test_list_filters():
    class FakeTierTable:
        def assign(self, info):
            return "high" if info.id == "claude-opus-5" else "low"

    reg = make_registry(
        raw=catalogue_bytes(), env=BOTH, tier_table=FakeTierTable()
    )
    assert {m.id for m in reg.list(tier="high")} == {"claude-opus-5"}
    assert {m.id for m in reg.list(provider="openai")} == {"gpt-5.6"}
    assert {m.id for m in reg.list(search="opus")} == {"claude-opus-5"}
    assert {m.id for m in reg.list(search="GPT-5.6")} == {"gpt-5.6"}


def test_list_selectable_only_excludes_unmapped_providers():
    reg = make_registry(raw=catalogue_bytes(), env={"ANTHROPIC_API_KEY": "1", "MYSTERY_API_KEY": "1"})
    assert {m.provider for m in reg.list()} == {"anthropic", "mystery"}
    assert {m.provider for m in reg.list(selectable_only=True)} == {"anthropic"}


def test_list_returns_a_copy():
    reg = make_registry(raw=catalogue_bytes(), env=ANTHROPIC)
    listed = reg.list()
    listed.clear()
    assert len(reg.list()) == 1


def test_tier_table_applied_during_ingest():
    class FakeTierTable:
        def assign(self, info):
            return "high"

    reg = make_registry(
        raw=catalogue_bytes(), env=ANTHROPIC, tier_table=FakeTierTable()
    )
    assert reg.get("anthropic/claude-opus-5").tier == "high"


def test_build_index_is_standalone_and_pure():
    catalogue = parse_catalogue(catalogue_bytes())
    index = build_index(catalogue, providers_config={"anthropic": {}}, env={})
    assert [m.id for m in index.models] == ["claude-opus-5"]
    assert {s.id for s in index.providers} == {"anthropic"}
    assert index.by_ref["anthropic/claude-opus-5"].provider == "anthropic"


# --------------------------------------------------------------------------
# get / resolve
# --------------------------------------------------------------------------


def test_get_and_resolve_lookup_paths():
    reg = make_registry(raw=catalogue_bytes(), env={**BOTH, "OPENROUTER_API_KEY": "1"})
    assert reg.get("anthropic/claude-opus-5").provider == "anthropic"
    assert reg.get("claude-opus-5").id == "claude-opus-5"
    assert reg.get("openrouter/openai/gpt-5.6").id == "gpt-5.6"
    assert reg.get("") is None
    assert reg.get(None) is None
    assert reg.get("nope/nope") is None
    assert reg.resolve("claude-opus-5").provider == "anthropic"


def test_resolve_rejects_empty_and_unknown():
    reg = make_registry(raw=catalogue_bytes(), env=ANTHROPIC)
    with pytest.raises(ConfigError, match="nonempty"):
        reg.resolve("")
    with pytest.raises(ConfigError, match="Unknown model reference"):
        reg.resolve("anthropic/not-a-model")
    with pytest.raises(ConfigError, match="Unknown model reference"):
        reg.resolve("nope/nope")


def test_resolve_before_load_raises():
    reg = make_registry(env={})
    with pytest.raises(ConfigError, match="Unknown model reference"):
        reg.resolve("anthropic/claude-opus-5")


def test_install_populates_status():
    reg = make_registry(raw=catalogue_bytes(), env={"ANTHROPIC_API_KEY": "1", "MYSTERY_API_KEY": "1"})
    status = reg.status()
    assert reg.loaded is True
    assert status.source == "catalogue"
    assert status.stale is False
    assert status.model_count == 2
    assert status.provider_count == 2
    assert status.selectable_provider_count == 1
    assert status.alias_count == 0
    assert status.unselectable == ("mystery",)
    assert status.license_pending is False


# --------------------------------------------------------------------------
# Constructor validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ttl_days": -1},
        {"ttl_days": float("nan")},
        {"timeout_s": 0},
        {"max_bytes": 0},
        {"catalogue_url": "file:///etc/passwd"},
        {"catalogue_url": "http://[::1"},
    ],
)
def test_constructor_rejects_invalid_options(kwargs):
    with pytest.raises(ConfigError):
        ModelRegistry(env={}, snapshot_path=MISSING, **kwargs)


def test_constructor_defaults_are_safe():
    reg = ModelRegistry(env={}, snapshot_path=MISSING)
    assert reg.catalogue_url == "https://models.dev/api.json"
    assert reg.verify_tls is True
    assert reg.offline is False
    assert reg.ttl_seconds == 7 * DAY
    assert reg.loaded is False
    assert reg.status().source == "empty"


# --------------------------------------------------------------------------
# Acquisition: cache, TTL, fallback
# --------------------------------------------------------------------------


async def test_load_returns_installed_status_without_reloading():
    reg = make_registry(raw=catalogue_bytes(), env=ANTHROPIC)
    first = reg.status()
    second = await reg.load()
    assert second is first


async def test_load_uses_fresh_cache_without_fetch(tmp_path):
    cache = tmp_path / "models.dev.json"
    cache.write_bytes(catalogue_bytes())
    fetcher = FakeFetcher(b"SHOULD NOT BE USED")
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
    )
    status = await reg.load()
    assert status.source == "cache"
    assert status.stale is False
    assert fetcher.calls == []


async def test_load_fetches_when_cache_is_stale(tmp_path):
    cache = tmp_path / "models.dev.json"
    cache.write_bytes(b'{"old": {"models": {}}}')
    old = time.time() - 8 * DAY
    os.utime(cache, (old, old))
    fetcher = FakeFetcher(catalogue_bytes())
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
    )
    status = await reg.load()
    assert status.source == "network"
    assert status.stale is False
    assert len(fetcher.calls) == 1
    assert cache.read_bytes() == catalogue_bytes()


async def test_load_falls_back_to_stale_cache_on_fetch_failure(tmp_path):
    cache = tmp_path / "models.dev.json"
    cache.write_bytes(catalogue_bytes())
    old = time.time() - 8 * DAY
    os.utime(cache, (old, old))
    fetcher = FakeFetcher(error=CatalogueError("boom"))
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
    )
    status = await reg.load()
    assert status.source == "cache"
    assert status.stale is True
    assert "boom" in status.error
    assert reg.get("anthropic/claude-opus-5") is not None


async def test_corrupt_cache_is_a_miss_not_a_stale_fallback(tmp_path):
    cache = tmp_path / "models.dev.json"
    cache.write_bytes(b"not valid json")
    fetcher = FakeFetcher(catalogue_bytes())
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
    )
    status = await reg.load()
    assert status.source == "network"
    assert len(fetcher.calls) == 1


async def test_corrupt_cache_with_failing_fetch_uses_snapshot(tmp_path):
    cache = tmp_path / "models.dev.json"
    cache.write_bytes(b"{ broken")
    snapshot = tmp_path / "snap.json"
    snapshot.write_bytes(
        b'{"_license": "pending", "anthropic": {"env": ["ANTHROPIC_API_KEY"],'
        b' "npm": "@ai-sdk/anthropic", "models": {"m": {"id": "m",'
        b' "modalities": {"output": ["text"]}}}}}'
    )
    fetcher = FakeFetcher(error=CatalogueError("down"))
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=snapshot,
        fetcher=fetcher,
    )
    status = await reg.load()
    assert status.source == "snapshot"
    assert status.stale is True
    assert status.license_pending is True
    model = reg.get("anthropic/m")
    assert model is not None
    assert model.source == "builtin"


async def test_offline_never_fetches_and_uses_snapshot(tmp_path):
    snapshot = tmp_path / "snap.json"
    snapshot.write_bytes(
        b'{"_license": "pending", "anthropic": {"env": ["ANTHROPIC_API_KEY"],'
        b' "models": {"m": {"id": "m", "modalities": {"output": ["text"]}}}}}'
    )
    fetcher = FakeFetcher(error=AssertionError("network must not be used"))
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=tmp_path / "cache.json",
        snapshot_path=snapshot,
        fetcher=fetcher,
        offline=True,
    )
    status = await reg.load()
    assert status.source == "snapshot"
    assert fetcher.calls == []


async def test_offline_prefers_any_valid_cache_over_snapshot(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_bytes(catalogue_bytes())
    old = time.time() - 30 * DAY
    os.utime(cache, (old, old))
    fetcher = FakeFetcher(error=AssertionError("network must not be used"))
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
        offline=True,
    )
    status = await reg.load()
    assert status.source == "cache"
    assert status.stale is True
    assert fetcher.calls == []


async def test_refresh_forces_a_fetch(tmp_path):
    cache = tmp_path / "models.dev.json"
    cache.write_bytes(catalogue_bytes())
    fetcher = FakeFetcher(catalogue_bytes())
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
    )
    first = await reg.load()
    assert first.source == "cache"
    assert fetcher.calls == []
    second = await reg.refresh()
    assert second.source == "network"
    assert len(fetcher.calls) == 1


async def test_missing_cache_and_snapshot_yields_empty_registry(tmp_path):
    reg = ModelRegistry(
        env={},
        cache_path=tmp_path / "cache.json",
        snapshot_path=tmp_path / "missing.json",
        offline=True,
    )
    status = await reg.load()
    assert status.source == "empty"
    assert status.model_count == 0
    assert reg.list() == []


async def test_default_vendored_snapshot_loads_and_is_labeled(tmp_path):
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=tmp_path / "absent.json",
        offline=True,
    )
    status = await reg.load()
    assert status.source == "snapshot"
    assert status.license_pending is False
    assert reg.get("anthropic/claude-opus-5") is not None
    assert reg.get("anthropic/claude-opus-5").source == "builtin"


async def test_snapshot_can_be_disabled(tmp_path):
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=tmp_path / "cache.json",
        use_snapshot=False,
        offline=True,
    )
    assert reg.snapshot_path is None
    status = await reg.load()
    assert status.source == "empty"


async def test_fetch_failure_with_no_cache_or_snapshot_is_empty(tmp_path):
    reg = ModelRegistry(
        env={},
        cache_path=tmp_path / "cache.json",
        snapshot_path=tmp_path / "missing.json",
        fetcher=FakeFetcher(error=CatalogueError("offline")),
    )
    status = await reg.load()
    assert status.source == "empty"
    assert status.stale is True
    assert "offline" in status.error


async def test_cache_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    cache_dir = tmp_path / "nested"
    cache = cache_dir / "models.dev.json"
    fetcher = FakeFetcher(catalogue_bytes())
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=cache,
        snapshot_path=MISSING,
        fetcher=fetcher,
    )
    await reg.load()
    assert cache.exists()
    assert list(cache_dir.iterdir()) == [cache]
    assert isinstance(json.loads(cache.read_text()), dict)


async def test_fetch_receives_timeout_size_and_tls(tmp_path):
    fetcher = FakeFetcher(catalogue_bytes())
    reg = ModelRegistry(
        env=ANTHROPIC,
        cache_path=tmp_path / "cache.json",
        snapshot_path=MISSING,
        fetcher=fetcher,
        timeout_s=3.5,
        max_bytes=12345,
        verify_tls=True,
    )
    await reg.load()
    assert fetcher.calls == [
        {
            "url": "https://models.dev/api.json",
            "timeout_s": 3.5,
            "max_bytes": 12345,
            "verify_tls": True,
        }
    ]


# --------------------------------------------------------------------------
# Default HTTP fetcher
# --------------------------------------------------------------------------


async def test_httpx_fetcher_rejects_non_http_scheme():
    fetcher = HttpxCatalogueFetcher()
    with pytest.raises(CatalogueError, match="scheme"):
        await fetcher.fetch(
            "file:///etc/passwd", timeout_s=1, max_bytes=10, verify_tls=True
        )


async def test_httpx_fetcher_enforces_size_bound():
    def handler(request):
        return httpx.Response(200, content=b"x" * 100)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        fetcher = HttpxCatalogueFetcher(client=client)
        with pytest.raises(CatalogueError, match="bound"):
            await fetcher.fetch(
                "https://example.test/c.json",
                timeout_s=1,
                max_bytes=10,
                verify_tls=True,
            )
    finally:
        await client.aclose()


async def test_httpx_fetcher_rejects_non_200():
    def handler(request):
        return httpx.Response(500, content=b"nope")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        fetcher = HttpxCatalogueFetcher(client=client)
        with pytest.raises(CatalogueError, match="HTTP 500"):
            await fetcher.fetch(
                "https://example.test/c.json",
                timeout_s=1,
                max_bytes=1000,
                verify_tls=True,
            )
    finally:
        await client.aclose()


async def test_httpx_fetcher_enforces_declared_content_length():
    # Regression: ``CatalogueError`` is a ``ValueError``, so a size guard that
    # raised inside its own ``try/except ValueError`` silently swallowed the
    # rejection and fetched an oversized body. The guard must reject on the
    # declared length before reading the (small, here) body.
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-length": "9999"},
            content=b"small",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        fetcher = HttpxCatalogueFetcher(client=client)
        with pytest.raises(CatalogueError, match="bound"):
            await fetcher.fetch(
                "https://example.test/c.json",
                timeout_s=1,
                max_bytes=10,
                verify_tls=True,
            )
    finally:
        await client.aclose()


async def test_httpx_fetcher_tolerates_malformed_content_length():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-length": "not-a-number"},
            content=b"ok",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        fetcher = HttpxCatalogueFetcher(client=client)
        body = await fetcher.fetch(
            "https://example.test/c.json",
            timeout_s=1,
            max_bytes=10,
            verify_tls=True,
        )
    finally:
        await client.aclose()
    assert body == b"ok"


async def test_httpx_fetcher_rejects_invalid_url_as_catalogue_error():
    fetcher = HttpxCatalogueFetcher()
    with pytest.raises(CatalogueError, match="invalid catalogue URL"):
        await fetcher.fetch(
            "http://[::1", timeout_s=1, max_bytes=10, verify_tls=True
        )


async def test_httpx_fetcher_returns_body():
    def handler(request):
        assert request.url.path == "/api.json"
        return httpx.Response(200, content=b'{"anthropic": {"models": {}}}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        fetcher = HttpxCatalogueFetcher(client=client)
        body = await fetcher.fetch(
            "https://models.dev/api.json",
            timeout_s=1,
            max_bytes=1000,
            verify_tls=True,
        )
    finally:
        await client.aclose()
    assert body == b'{"anthropic": {"models": {}}}'


async def test_registry_load_via_default_fetcher_uses_mock_transport(tmp_path):
    def handler(request):
        return httpx.Response(200, content=catalogue_bytes())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        reg = ModelRegistry(
            env=ANTHROPIC,
            cache_path=tmp_path / "cache.json",
            snapshot_path=MISSING,
            fetcher=HttpxCatalogueFetcher(client=client),
        )
        status = await reg.load()
        assert status.source == "network"
        assert reg.get("anthropic/claude-opus-5") is not None
    finally:
        await client.aclose()


# --------------------------------------------------------------------------
# Provider status: reachability is the honest env signal
# --------------------------------------------------------------------------


def test_provider_status_distinguishes_configured_from_reachable():
    reg = make_registry(
        raw=catalogue_bytes(),
        env={},
        providers={"anthropic": {"kind": "anthropic"}},
    )
    status = reg.provider_status("anthropic")
    assert status.configured is True
    assert status.reachable is False
    assert status.selectable is True


def test_provider_status_marks_a_reachable_provider():
    reg = make_registry(raw=catalogue_bytes(), env=ANTHROPIC)
    status = reg.provider_status("anthropic")
    assert status.configured is False
    assert status.reachable is True
    assert status.selectable is True


# --------------------------------------------------------------------------
# Credential redaction
# --------------------------------------------------------------------------


def test_redact_url_userinfo_strips_credentials_but_keeps_emails():
    from nexus.util import redact_url_userinfo

    redacted = redact_url_userinfo(
        "GET https://alice:supersecret@models.example/api.json failed"
    )
    assert "supersecret" not in redacted
    assert "alice" not in redacted
    assert "models.example" in redacted
    assert redact_url_userinfo("contact bob@example.com") == "contact bob@example.com"
    assert redact_url_userinfo("") == ""


def test_constructor_error_does_not_leak_userinfo():
    with pytest.raises(ConfigError) as excinfo:
        ModelRegistry(
            env={},
            snapshot_path=MISSING,
            catalogue_url="http://alice:supersecret@[::1",
        )
    assert "supersecret" not in str(excinfo.value)
