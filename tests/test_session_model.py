"""Per-session model switching (``/model``) end to end, offline.

Covers the durable selection primitive and the routing that consumes it:

* validation (unknown provider/model, a tier with no registry) surfaces as a
  redacted facade error rather than a silent change;
* exactly one durable ``model.selected`` event is written per selection and it
  rehydrates on reopen;
* the selected model is the one subsequent turns actually request, while a turn
  already in flight is untouched;
* two sessions stay isolated;
* the selection carries no credential/endpoint and the replay reducer reflects
  it before any turn runs;
* the configured fallback chain is reported alongside the selection.
"""
from __future__ import annotations

import asyncio

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.errors import ConfigError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, Wait, text_response
from nexus.model.tiers import TierTable
from nexus.runtime import Runtime
from nexus.view import fold


def _config(*, fallback=()) -> Config:
    return Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m", fallback=list(fallback)),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )


def _runtime(tmp_path, provider, **kwargs) -> Runtime:
    return Runtime(tmp_path, config=_config(), providers={"scripted": provider}, **kwargs)


async def _wait_for(predicate, timeout=5.0):
    async def _wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout)


class _StubModel:
    def __init__(self, provider: str, id: str, tier: str) -> None:
        self.provider = provider
        self.id = id
        self.tier = tier
        self.name = id
        self.aliases = ()
        self.cost = None

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"


class _StubRegistry:
    """The structural slice of ``ModelRegistry`` the router reads."""

    def __init__(self, models: list[_StubModel]) -> None:
        self._models = list(models)

    def get(self, ref: str):
        for model in self._models:
            if ref in (model.ref, model.id):
                return model
        return None

    def list(self, *, provider=None, tier=None, selectable_only=False, search=None):
        rows = list(self._models)
        if provider is not None:
            rows = [m for m in rows if m.provider == provider]
        if tier is not None:
            rows = [m for m in rows if m.tier == tier]
        return rows


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_unknown_model_is_refused(tmp_path):
    provider = ScriptedProvider(text_response("x"))
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s")

    result = await facade.handle(p.ModelSelect(session="s", ref="nope/x"))
    assert isinstance(result, p.ErrorResult)
    assert "nope" in result.message

    # Nothing was persisted: the session still uses the configured default.
    assert runtime.session("s").model_selection is None
    await runtime.aclose()


async def test_tier_without_a_registry_is_refused(tmp_path):
    provider = ScriptedProvider(text_response("x"))
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s")

    result = await facade.handle(p.ModelSelect(session="s", ref="high"))
    assert isinstance(result, p.ErrorResult)
    assert "registry" in result.message
    await runtime.aclose()


async def test_empty_reference_is_refused_directly(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider())
    with pytest.raises(ConfigError):
        runtime.select_session_model("s", "   ")
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Persistence and rehydration
# ---------------------------------------------------------------------------


async def test_selection_is_one_durable_event_and_survives_reopen(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider())
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.ModelSelect(session="s", ref="scripted/m2"))

    events = [e for e in runtime.session("s").events if e.type == "model.selected"]
    assert len(events) == 1
    assert events[0].data["model"] == "m2"
    assert events[0].data["provider"] == "scripted"
    await runtime.aclose()

    reopened = _runtime(tmp_path, ScriptedProvider())
    handle = reopened.session("s")
    selection = handle.model_selection
    assert selection is not None
    assert (selection.provider, selection.model, selection.reference) == (
        "scripted",
        "m2",
        "scripted/m2",
    )
    # Reopening is a pure read: no new selection event was written.
    assert len([e for e in handle.events if e.type == "model.selected"]) == 1
    await reopened.aclose()


async def test_latest_selection_wins_after_reopen(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider())
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.ModelSelect(session="s", ref="scripted/first"))
    await facade.handle(p.ModelSelect(session="s", ref="scripted/second"))
    await runtime.aclose()

    reopened = _runtime(tmp_path, ScriptedProvider())
    assert reopened.session("s").model_selection.model == "second"
    await reopened.aclose()


# ---------------------------------------------------------------------------
# Effective model
# ---------------------------------------------------------------------------


async def test_selected_model_is_used_by_the_next_turn(tmp_path):
    provider = ScriptedProvider(text_response("one"), text_response("two"))
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.ModelSelect(session="s", ref="scripted/m2"))

    await facade.start_turn("s", "go")
    await facade.wait_idle(timeout=5.0)

    assert provider.requests[-1].model == "m2"
    started = [e for e in runtime.session("s").events if e.type == "model.started"]
    assert started[-1].data["model"] == "m2"
    await runtime.aclose()


async def test_selection_does_not_touch_a_turn_in_flight(tmp_path):
    gate = asyncio.Event()
    provider = ScriptedProvider(
        [Wait(gate), *text_response("first")], text_response("second")
    )
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s")

    await facade.start_turn("s", "go")
    await _wait_for(lambda: provider.calls == 1)

    # The turn is parked mid-stream; select a different model.
    await facade.handle(p.ModelSelect(session="s", ref="scripted/m2"))
    gate.set()
    await facade.wait_idle(timeout=5.0)

    # The in-flight turn stayed on the frozen default model...
    assert provider.requests[0].model == "m"
    started = [e for e in runtime.session("s").events if e.type == "model.started"]
    assert started[-1].data["model"] == "m"

    # ...and only the next turn picks the selection up.
    await facade.start_turn("s", "again")
    await facade.wait_idle(timeout=5.0)
    assert provider.requests[1].model == "m2"
    await runtime.aclose()


async def test_two_sessions_stay_isolated(tmp_path):
    provider = ScriptedProvider(text_response("a"), text_response("b"))
    runtime = _runtime(tmp_path, provider)
    facade = HostFacade(runtime)
    facade.open_session("s1")
    facade.open_session("s2")
    await facade.handle(p.ModelSelect(session="s1", ref="scripted/m2"))

    await facade.start_turn("s1", "one")
    await facade.wait_idle(timeout=5.0)
    await facade.start_turn("s2", "two")
    await facade.wait_idle(timeout=5.0)

    assert provider.requests[0].model == "m2"
    assert provider.requests[1].model == "m"
    assert runtime.session("s1").model_selection.model == "m2"
    assert runtime.session("s2").model_selection is None
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Tiers, replay, fallback, and no secrets
# ---------------------------------------------------------------------------


async def test_tier_name_selects_a_runnable_model(tmp_path):
    provider = ScriptedProvider(text_response("tiered"))
    models = [
        _StubModel("scripted", "opus", tier="high"),
        _StubModel("scripted", "haiku", tier="low"),
    ]
    registry = _StubRegistry(models)
    tiers = TierTable(overrides={"high": ["scripted/opus"], "low": ["scripted/haiku"]})
    runtime = _runtime(tmp_path, provider, registry=registry, tiers=tiers)
    facade = HostFacade(runtime)
    facade.open_session("s")

    result = await facade.handle(p.ModelSelect(session="s", ref="high"))
    assert isinstance(result, p.ModelSelectResult)
    assert (result.provider, result.model, result.tier) == ("scripted", "opus", "high")
    assert result.tier_source == "tier"

    await facade.start_turn("s", "go")
    await facade.wait_idle(timeout=5.0)
    assert provider.requests[-1].model == "opus"
    await runtime.aclose()


async def test_model_reference_is_classified_into_a_tier(tmp_path):
    models = [_StubModel("scripted", "opus", tier="high")]
    registry = _StubRegistry(models)
    tiers = TierTable(overrides={"high": ["scripted/opus"]})
    runtime = _runtime(tmp_path, ScriptedProvider(), registry=registry, tiers=tiers)
    facade = HostFacade(runtime)
    facade.open_session("s")

    result = await facade.handle(p.ModelSelect(session="s", ref="scripted/opus"))
    assert isinstance(result, p.ModelSelectResult)
    assert result.tier == "high" and result.tier_source == "override"
    assert result.requested_tier == ""  # a model ref, not a tier name
    await runtime.aclose()


async def test_reducer_state_reflects_the_selection_before_a_turn(tmp_path):
    runtime = _runtime(tmp_path, ScriptedProvider())
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.ModelSelect(session="s", ref="scripted/m2"))

    view, seq = facade.state("s")
    direct = fold(runtime.session("s").events)
    assert view.to_dict() == direct.to_dict()
    assert view.model is not None
    assert view.model["selected"] is True
    assert view.model["model"] == "m2"
    assert seq == runtime.session("s").events[-1].seq
    await runtime.aclose()


async def test_select_result_reports_the_fallback_chain(tmp_path):
    provider = ScriptedProvider(text_response("f"))
    runtime = Runtime(
        tmp_path,
        config=_config(fallback=["scripted/backup"]),
        providers={"scripted": provider},
    )
    facade = HostFacade(runtime)
    facade.open_session("s")
    result = await facade.handle(p.ModelSelect(session="s", ref="scripted/m2"))
    assert isinstance(result, p.ModelSelectResult)
    assert result.fallback == ["scripted/backup"]
    await runtime.aclose()


async def test_select_result_redacts_dedupes_and_drops_the_selection(tmp_path):
    provider = ScriptedProvider(text_response("f"))
    runtime = Runtime(
        tmp_path,
        config=_config(
            fallback=[
                "scripted/backup",
                "scripted/backup",
                "scripted/m2",
                "Bearer sk-secret1234567",
            ]
        ),
        providers={"scripted": provider},
    )
    facade = HostFacade(runtime)
    facade.open_session("s")
    result = await facade.handle(p.ModelSelect(session="s", ref="scripted/m2"))
    assert isinstance(result, p.ModelSelectResult)
    # Duplicate collapsed, the selected model dropped, the secret redacted.
    assert result.fallback[0] == "scripted/backup"
    assert len(result.fallback) == 2
    assert "scripted/m2" not in result.fallback
    assert all("sk-secret1234567" not in ref for ref in result.fallback)
    await runtime.aclose()


def test_model_selection_from_dict_is_strict_about_bool():
    from nexus.model.selection import ModelSelection

    base = {"reference": "scripted/m2", "provider": "scripted", "model": "m2"}
    assert ModelSelection.from_dict({**base, "clamped": True}).clamped is True
    assert ModelSelection.from_dict({**base, "clamped": False}).clamped is False
    for hostile in ("false", "true", 1, 0, [], {}):
        parsed = ModelSelection.from_dict({**base, "clamped": hostile})
        assert parsed is not None
        assert parsed.clamped is False


async def test_selected_tier_caps_subagent_authority(tmp_path):
    models = [
        _StubModel("scripted", "opus", tier="high"),
        _StubModel("scripted", "haiku", tier="low"),
    ]
    registry = _StubRegistry(models)
    tiers = TierTable(overrides={"high": ["scripted/opus"], "low": ["scripted/haiku"]})
    runtime = _runtime(tmp_path, ScriptedProvider(), registry=registry, tiers=tiers)
    facade = HostFacade(runtime)
    facade.open_session("s")
    await facade.handle(p.ModelSelect(session="s", ref="low"))

    captured: dict[str, object] = {}
    original = runtime._make_subagent_runner

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    runtime._make_subagent_runner = spy  # type: ignore[assignment]
    await runtime.ensure_started()
    bundle = runtime._make_tool_turn(
        config=runtime._load_config(),
        session=runtime.session("s"),
        turn_id="t",
        attended=False,
    )
    # A child of this session can never exceed the session's selected tier.
    assert captured.get("parent_tier") == "low"
    assert bundle.environment_for._model_selection.tier == "low"
    await runtime.aclose()


def test_select_result_carries_only_descriptive_fields():
    fields = set(p.ModelSelectResult.__struct_fields__)
    assert fields == {
        "session",
        "accepted",
        "reference",
        "provider",
        "model",
        "tier",
        "tier_source",
        "requested_tier",
        "clamped",
        "fallback",
        "apply_next_turn",
    }
    for forbidden in ("api_key", "token", "base_url", "url", "credential"):
        assert forbidden not in fields
