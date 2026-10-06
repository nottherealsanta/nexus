"""Mock scenarios end to end through a real Runtime + HostFacade (MOCK_PLAN §9).

Every non-slow scenario must finish with a passing in-band verdict, use only the
mock provider, and leave everything outside the sandbox untouched.
"""
from __future__ import annotations

import pytest

from nexus.devtools.mock import catalog
from nexus.devtools.mock.runner import run_scenario
from nexus.devtools.mock.sandbox import ensure_sandbox, tree_hash
from nexus.runtime import Runtime

SCENARIOS = sorted(n for n, s in catalog().items() if not s.slow)


@pytest.mark.parametrize("name", SCENARIOS)
async def test_scenario_passes(name, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DEV", "1")
    home = tmp_path / ".nexus"  # an explicit Runtime home resolves global config to <home>/.nexus
    sandbox = ensure_sandbox(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("untouched")
    before = tree_hash(outside)
    monkeypatch.setenv("NEXUS_HOME", str(home))
    runtime = Runtime(sandbox, home=home.parent, environ={"NEXUS_DEV": "1"})
    try:
        report = await run_scenario(catalog()[name], runtime, speed=0, timeout=90)
    finally:
        await runtime.aclose()
    assert report.passed, f"{report.verdict!r} error={report.error!r}\n{report.text[-1500:]}"
    assert tree_hash(outside) == before


@pytest.mark.parametrize("name", ["hello", "parallel-subagents"])
async def test_scenarios_survive_a_real_looking_global_config(name, tmp_path, monkeypatch):
    """The user-level config layer still applies in dev mode (a codex default, as in real setups)."""
    monkeypatch.setenv("NEXUS_DEV", "1")
    fake_home = tmp_path / "userhome"
    (fake_home / ".nexus").mkdir(parents=True)
    (fake_home / ".nexus" / "config.toml").write_text(
        'config_version = 2\n\n[models]\ndefault = "codex/gpt-6-sol"\n\n'
        '[providers.codex]\napi = "responses"\nprofile = "default"\nauth = "chatgpt_oauth"\n'
    )
    dev_home = tmp_path / "dev"
    monkeypatch.setenv("NEXUS_HOME", str(dev_home))
    sandbox = ensure_sandbox(dev_home)
    runtime = Runtime(sandbox, home=fake_home, environ={"NEXUS_DEV": "1"})
    try:
        report = await run_scenario(catalog()[name], runtime, speed=0, timeout=60)
    finally:
        await runtime.aclose()
    assert report.passed, f"{report.verdict!r} error={report.error!r}\n{report.text[-800:]}"
