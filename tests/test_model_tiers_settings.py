"""Settings -> Models: editing tier lists, the subagent ceiling, title settings.

Plan: plans/SESSION_TITLE_PLAN.md, Parts 1 and 2 (settings half). Writes go to
the user-global config; reads come from the live tier table and router.
"""
from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.host_support import model_settings
from nexus.model.request import ModelRequest
from nexus.model.router import ModelRouter
from nexus.model.tiers import TierTable


def _info(provider: str, model: str):
    return SimpleNamespace(provider=provider, id=model, aliases=(), cost=None)


class _Registry:
    def __init__(self, *infos):
        self._infos = {f"{info.provider}/{info.id}": info for info in infos}

    def get(self, ref):
        return self._infos.get(ref)

    def list(self, *, tier=None, selectable_only=False):
        return [info for info in self._infos.values() if self._tier(info) == tier]

    def _tier(self, info):
        return {"cheap": "low", "mid": "medium", "big": "high"}[info.id.split("-")[0]]


class _Provider:
    def __init__(self, name):
        self.name = name

    def capabilities(self, model):
        from nexus.model.capabilities import Capabilities

        return Capabilities()


def _router(tiers, registry, providers=("openai", "anthropic")):
    return ModelRouter(
        {name: _Provider(name) for name in providers},
        default="openai/mid-1",
        registry=registry,
        tiers=tiers,
    )


def _runtime(tmp_path, monkeypatch, *, overrides=None, providers=("openai", "anthropic")):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    registry = _Registry(
        _info("openai", "cheap-1"), _info("anthropic", "cheap-2"),
        _info("openai", "mid-1"), _info("anthropic", "big-1"),
    )
    tiers = TierTable(overrides=overrides, builtin={})
    router = _router(tiers, registry, providers)
    runtime = SimpleNamespace(
        workspace=home / "workspace", _home=home, _environ={}, tiers=tiers,
        registry=registry, router=router,
        _load_config=lambda: SimpleNamespace(v2=None),
    )
    return runtime, home


def _config(home: Path) -> dict:
    return tomllib.loads((home / ".nexus" / "config.toml").read_text())


# -- router honors the user's order ----------------------------------------


def test_router_uses_the_first_runnable_model_in_the_users_list():
    tiers = TierTable(overrides={"low": ["anthropic/cheap-2", "openai/cheap-1"]}, builtin={})
    router = _router(tiers, _Registry(_info("openai", "cheap-1"), _info("anthropic", "cheap-2")))
    resolved = router.resolve(ModelRequest(messages=[], model="low"))
    assert (resolved.provider.name, resolved.model) == ("anthropic", "cheap-2")


def test_router_skips_an_unrunnable_pinned_model():
    tiers = TierTable(overrides={"low": ["anthropic/cheap-2", "openai/cheap-1"]}, builtin={})
    router = _router(
        tiers, _Registry(_info("openai", "cheap-1"), _info("anthropic", "cheap-2")),
        providers=("openai",),
    )
    resolved = router.resolve(ModelRequest(messages=[], model="low"))
    assert resolved.model == "cheap-1"


def test_tier_runnable_is_false_without_a_registry():
    router = ModelRouter({"openai": _Provider("openai")}, default="openai/x")
    assert router.tier_runnable("low") is False


# -- rows ------------------------------------------------------------------


def test_rows_show_refs_source_and_resolved_model(tmp_path, monkeypatch):
    runtime, _home = _runtime(
        tmp_path, monkeypatch, overrides={"low": ["anthropic/cheap-2", "openai/cheap-1"]}
    )
    rows = {row["name"]: row for row in model_settings.tier_rows(runtime)["tiers"]}
    assert rows["low"]["refs"] == ["anthropic/cheap-2", "openai/cheap-1"]
    assert rows["low"]["source"] == "your list"
    assert rows["low"]["resolved"] == "anthropic/cheap-2"
    assert rows["medium"]["source"] == "by price" and rows["medium"]["refs"] == []
    assert rows["medium"]["resolved"] == "openai/mid-1"
    assert model_settings.tier_rows(runtime)["max_tier"] == "high"


def test_a_tier_with_no_runnable_model_reports_it(tmp_path, monkeypatch):
    runtime, _home = _runtime(tmp_path, monkeypatch, providers=("anthropic",))
    rows = {row["name"]: row for row in model_settings.tier_rows(runtime)["tiers"]}
    assert rows["medium"]["resolved"] == "" and rows["medium"]["runnable"] is False


# -- writes ----------------------------------------------------------------


def test_tier_set_writes_the_ordered_list_and_reloads_when_idle(tmp_path, monkeypatch):
    runtime, home = _runtime(tmp_path, monkeypatch)
    reloads = []

    async def reload():
        reloads.append(_config(home)["models"]["tiers"]["low"])
        return True

    runtime.reload_model_routes = reload
    result = asyncio.run(
        model_settings.tier_set(runtime, "low", ["anthropic/cheap-2", "openai/cheap-1"], reload=True)
    )
    assert _config(home)["models"]["tiers"] == {"low": ["anthropic/cheap-2", "openai/cheap-1"]}
    assert reloads == [["anthropic/cheap-2", "openai/cheap-1"]]
    assert result["restart_required"] is False

    busy = asyncio.run(model_settings.tier_set(runtime, "low", ["openai/cheap-1"], reload=False))
    assert busy["restart_required"] is True
    assert _config(home)["models"]["tiers"]["low"] == ["openai/cheap-1"]


def test_tier_reset_removes_only_that_tier(tmp_path, monkeypatch):
    runtime, home = _runtime(tmp_path, monkeypatch)
    asyncio.run(model_settings.tier_set(runtime, "low", ["openai/cheap-1"]))
    asyncio.run(model_settings.tier_set(runtime, "high", ["anthropic/big-1"]))
    asyncio.run(model_settings.tier_reset(runtime, "low"))
    assert _config(home)["models"]["tiers"] == {"high": ["anthropic/big-1"]}


@pytest.mark.parametrize(
    ("tier", "refs", "message"),
    [
        ("low", [], "at least one"),
        ("low", [f"openai/m{i}" for i in range(20)], "at most"),
        ("low", ["bad ref"], "invalid model reference"),
        ("low", ["openai/does-not-exist"], "unknown model"),
        ("low", ["medium"], "is a tier"),
        ("nope", ["openai/cheap-1"], "unknown tier"),
        ("lo w", ["openai/cheap-1"], "invalid tier"),
    ],
)
def test_tier_set_rejects_bad_input_and_writes_nothing(tmp_path, monkeypatch, tier, refs, message):
    runtime, home = _runtime(tmp_path, monkeypatch)
    with pytest.raises(ConfigError, match=message):
        asyncio.run(model_settings.tier_set(runtime, tier, refs))
    assert not (home / ".nexus" / "config.toml").exists()


def test_agent_max_tier_is_validated_and_written(tmp_path, monkeypatch):
    runtime, home = _runtime(tmp_path, monkeypatch)
    asyncio.run(model_settings.agent_max_tier_set(runtime, "medium"))
    assert _config(home)["agents"]["max_tier"] == "medium"
    with pytest.raises(ConfigError, match="unknown tier"):
        asyncio.run(model_settings.agent_max_tier_set(runtime, "huge"))


# -- session title settings -------------------------------------------------


def test_title_settings_default_and_toggle(tmp_path, monkeypatch):
    runtime, home = _runtime(tmp_path, monkeypatch)
    state = model_settings.session_title_settings(runtime)
    assert state["enabled"] is True and state["model"] == "low"
    assert state["resolved"] == "openai/cheap-1" and state["message"] == ""

    asyncio.run(model_settings.session_title_settings_set(runtime, enabled=False, model="medium"))
    assert _config(home)["sessions"] == {"auto_title": False, "title_model": "medium"}


def test_title_settings_say_when_the_model_cannot_run(tmp_path, monkeypatch):
    runtime, _home = _runtime(tmp_path, monkeypatch)
    runtime.router = _router(runtime.tiers, runtime.registry, providers=())
    state = model_settings.session_title_settings(runtime)
    assert state["resolved"] == ""
    assert "no runnable model" in state["message"] and "first message" in state["message"]


@pytest.mark.parametrize("model", ["", "two words", "x" * 200])
def test_title_model_is_validated(tmp_path, monkeypatch, model):
    runtime, home = _runtime(tmp_path, monkeypatch)
    with pytest.raises(ConfigError):
        asyncio.run(model_settings.session_title_settings_set(runtime, model=model))
    assert not (home / ".nexus" / "config.toml").exists()


def test_sessions_section_validates_title_keys():
    from nexus.config.schema import SessionsSection

    assert SessionsSection().auto_title is True and SessionsSection().title_model == "low"
    with pytest.raises(ValueError):
        SessionsSection(auto_title="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SessionsSection(title_model="a b")
