"""Tier priority edits apply to the live router without a daemon restart.

A subagent session is created when the model calls the spawn tool; its model is
resolved then from the router's current tier table. So reordering a tier in
Settings -> Models must change the very next resolution, even while a turn runs
(docs/models.md "Live tier edits").
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.model.request import ModelRequest
from nexus.runtime import Runtime


async def test_reordering_a_tier_applies_to_the_next_resolution_without_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # No injected config: like the daemon, the runtime re-reads the files on reload.
    (workspace / "nexus.toml").write_text(
        'config_version = 2\n[models]\ndefault = "anthropic/claude-opus-5"\noffline = true\n'
        '[providers.anthropic]\nkind = "anthropic"\n', encoding="utf-8")
    runtime = Runtime(workspace, home=tmp_path / "home", environ={"ANTHROPIC_API_KEY": "k"})
    facade = HostFacade(runtime)
    try:
        await runtime.ensure_models()
        refs = [f"anthropic/{info.id}" for info in runtime.registry.list(provider="anthropic")][:2]
        assert len(refs) == 2, "the offline catalogue should list two anthropic models"
        first, second = refs
        # A turn is running: the old behavior refused to reload and demanded a restart.
        facade.supervisor = SimpleNamespace(running=True, max_concurrent=1)

        result = await facade.handle(p.ModelTierSet(tier="low", refs=[first, second]))
        assert result.restart_required is False
        assert runtime.router.tier_candidates("low")[:2] == (first, second)
        model = runtime.router.resolve(ModelRequest(messages=[], model="low")).model
        assert model == first.split("/", 1)[1]

        # Swap positions 1 and 2: the next resolution (the next subagent spawn) follows.
        result = await facade.handle(p.ModelTierSet(tier="low", refs=[second, first]))
        assert result.restart_required is False
        assert runtime.router.tier_candidates("low")[:2] == (second, first)
        model = runtime.router.resolve(ModelRequest(messages=[], model="low")).model
        assert model == second.split("/", 1)[1]
        low = next(row for row in result.tiers if row["name"] == "low")
        assert low["refs"][:2] == [second, first] and low["resolved"] == second

        reset = await facade.handle(p.ModelTierReset(tier="low"))
        assert reset.restart_required is False
        assert runtime.router.tier_candidates("low")[:2] != (second, first)
    finally:
        await runtime.aclose()
