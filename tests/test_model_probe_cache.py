"""Probe/catalogue caching across registry rebuilds and the tier-only reload.

A tier edit must not rebuild providers or the registry, and a route reload must
reuse a fresh ``agy models`` probe (docs/decisions.md, "Probe cache TTL").
"""
from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelsSection, ProviderSection
from nexus.model import registry as registry_mod
from nexus.model.registry import ModelRegistry, RegistryCache
from nexus.runtime import Runtime

pytestmark = pytest.mark.asyncio


class CountingCli:
    def __init__(self, models=(("gemini-3", "Gemini"),)):
        self.models_result = list(models)
        self.probes = 0

    def __call__(self, executable=None, environ=None):
        outer = self

        class Cli:
            def models(self):
                outer.probes += 1
                if isinstance(outer.models_result, Exception):
                    raise outer.models_result
                return outer.models_result

        return Cli()


def make_registry(cli, cache, *, now=lambda: 1000.0, env=None, **kw):
    return ModelRegistry(
        providers={"antigravity": ProviderSection(kind="antigravity")}, env=env or {"PATH": "/a"},
        offline=True, use_snapshot=False, antigravity_cli=cli, shared_cache=cache, now=now, **kw)


async def test_fresh_probe_survives_a_registry_rebuild():
    cli, cache = CountingCli(), RegistryCache()
    await make_registry(cli, cache).load()
    second = make_registry(cli, cache)
    await second.load()
    assert cli.probes == 1  # no TTL: reused until the model list is refreshed
    assert second.get("antigravity/gemini-3") is not None


async def test_probe_is_keyed_on_the_executable():
    cli, cache = CountingCli(), RegistryCache()
    await make_registry(cli, cache).load()
    reg = ModelRegistry(
        providers={"antigravity": ProviderSection(kind="antigravity", executable="/opt/agy")},
        env={"PATH": "/a"}, offline=True, use_snapshot=False, antigravity_cli=cli, shared_cache=cache)
    await reg.load()
    assert cli.probes == 2


async def test_refresh_forces_a_probe_and_a_failed_probe_keeps_the_last_good_list():
    cli, cache = CountingCli(), RegistryCache()
    first = make_registry(cli, cache)
    await first.load()
    cli.models_result = [("other", "Other")]
    await first.refresh_external()
    assert cli.probes == 2 and first.get("antigravity/other") is not None
    await first.load(force=True)
    assert cli.probes == 2  # a model-list reload does not re-probe
    cli.models_result = OSError("agy: gone")
    await first.refresh_external()
    assert cli.probes == 3 and first.get("antigravity/other") is not None
    assert first.external_errors()["antigravity"]
    again = make_registry(cli, cache)
    await again.load()
    assert cli.probes == 3 and again.get("antigravity/other") is not None


async def test_probe_persists_beside_the_catalogue_cache(tmp_path):
    cli = CountingCli()
    path = tmp_path / "models.dev.json"
    await make_registry(cli, RegistryCache(), cache_path=path).load()  # process 1
    assert (tmp_path / "agy-models.json").exists() and cli.probes == 1
    restarted = make_registry(cli, RegistryCache(), cache_path=path)  # process 2
    await restarted.load()
    assert cli.probes == 1 and restarted.get("antigravity/gemini-3") is not None
    (tmp_path / "agy-models.json").write_text("{corrupt")
    await make_registry(cli, RegistryCache(), cache_path=path).load()
    assert cli.probes == 2


async def test_catalogue_refetch_and_model_list_refresh_do_not_reprobe_agy():
    cli, cache = CountingCli(), RegistryCache()

    class Fetcher:
        async def fetch(self, *a, **k):
            return b"{}"

    reg = ModelRegistry(
        providers={"antigravity": ProviderSection(kind="antigravity")}, env={"PATH": "/a"},
        use_snapshot=False, antigravity_cli=cli, shared_cache=cache, fetcher=Fetcher())
    await reg.load()
    await reg.load(force=True)
    assert reg.status().source == "network" and cli.probes == 1
    info = reg.external_info()["antigravity"]
    assert info["count"] == 1 and info["refreshed_at"] > 0 and info["error"] == ""


async def test_catalogue_cache_file_is_parsed_once(tmp_path, monkeypatch):
    import json
    path = tmp_path / "models.dev.json"
    path.write_text(json.dumps({}))
    calls = []
    real = registry_mod.parse_catalogue
    monkeypatch.setattr(registry_mod, "parse_catalogue", lambda raw, **kw: calls.append(1) or real(raw, **kw))
    cache, cli = RegistryCache(), CountingCli()
    for _ in range(2):
        await make_registry(cli, cache, cache_path=path).load()
    assert len(calls) == 1


def make_runtime(tmp_path, monkeypatch, cli):
    from nexus.model.providers import antigravity_cli
    monkeypatch.setattr(antigravity_cli, "AntigravityCli", cli)
    config = Config(model="anthropic/claude-opus-5", version=2, v2=ConfigV2(
        models=ModelsSection(default="anthropic/claude-opus-5", offline=True),
        providers={"anthropic": ProviderSection(kind="anthropic")}))
    return Runtime(tmp_path / "ws", config=config, home=tmp_path / "home", environ={"ANTHROPIC_API_KEY": "k", "PATH": "/a"})


async def test_tier_reload_keeps_registry_and_providers_and_does_not_probe(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    cli = CountingCli()
    runtime = make_runtime(tmp_path, monkeypatch, cli)
    try:
        await runtime.ensure_models()
        assert cli.probes == 1
        registry, providers = runtime.registry, runtime._providers
        config = Config(model="anthropic/claude-opus-5", version=2, v2=ConfigV2(
            models=ModelsSection(default="anthropic/claude-opus-5", offline=True,
                                 tiers={"low": ["anthropic/claude-haiku-4-5"]}, tier_efforts={}),
            providers={"anthropic": ProviderSection(kind="anthropic")}))
        runtime._config = config
        assert await runtime.reload_tier_config() is True
        await runtime.ensure_models()
        assert runtime.registry is registry and runtime._providers is providers
        assert cli.probes == 1
        assert runtime.tiers.overrides == {"anthropic/claude-haiku-4-5": "low"}
        assert runtime.router.tiers is runtime.tiers and runtime._assembler._tiers is runtime.tiers
    finally:
        await runtime.aclose()


async def test_route_reload_reuses_the_probe_and_only_the_provider_refresh_reprobes(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    cli = CountingCli()
    runtime = make_runtime(tmp_path, monkeypatch, cli)
    try:
        await runtime.ensure_models()
        old = runtime.registry
        assert await runtime.reload_model_routes() is True
        assert runtime.registry is not old
        await runtime.ensure_models()
        assert cli.probes == 1
        await runtime.refresh_models()
        assert cli.probes == 1  # the model-list refresh leaves agy alone
        info = await runtime.refresh_provider_models("antigravity")
        assert cli.probes == 2 and info["count"] == 1
    finally:
        await runtime.aclose()


async def test_provider_models_refresh_command_round_trip(tmp_path, monkeypatch):
    from nexus.host import protocol as p
    from nexus.host_support.provider_auth import dispatch_providers
    (tmp_path / "ws").mkdir()
    cli = CountingCli()
    runtime = make_runtime(tmp_path, monkeypatch, cli)
    try:
        result = await dispatch_providers(p.ProviderModelsRefresh(provider="antigravity"), runtime)
        assert isinstance(result, p.ProviderModelsRefreshResult)
        assert result.count == 1 and result.refreshed_at > 0 and not result.error and cli.probes == 2  # first load, then the refresh
        with pytest.raises(Exception, match="no separate model list"):
            await dispatch_providers(p.ProviderModelsRefresh(provider="openai"), runtime)
    finally:
        await runtime.aclose()
