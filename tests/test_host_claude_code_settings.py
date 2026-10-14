"""Host ``ClaudeCodeSettings(Set)``: the main-model toggle writes the global config and takes effect."""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.runtime import Runtime


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)  # the settings writer targets ~/.nexus
    (home / ".nexus").mkdir(parents=True)
    (home / ".nexus" / "config.toml").write_text('config_version = 2\n\n[models]\ndefault = "scripted/m"\n')
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return Runtime(workspace, home=home, config_loader=lambda: Config.load(workspace, home=home, environ={})), home


async def test_toggle_round_trip_is_live_and_persisted(runtime):
    rt, home = runtime
    host = HostFacade(rt)
    first = await host.handle(p.ClaudeCodeSettings())
    assert isinstance(first, p.ClaudeCodeSettingsResult) and first.main_enabled is False
    assert "Agent SDK" in first.message and "per role" in first.message
    assert rt.claude_code_main_enabled() is False

    result = await host.handle(p.ClaudeCodeSettingsSet(main_enabled=True))
    assert result.main_enabled is True
    assert tomllib.loads((home / ".nexus" / "config.toml").read_text())["models"]["claude_code_main_enabled"] is True
    assert rt.claude_code_main_enabled() is True  # read live, no restart

    off = await host.handle(p.ClaudeCodeSettingsSet(main_enabled=False))
    assert off.main_enabled is False and rt.claude_code_main_enabled() is False
    await rt.aclose()


async def test_toggle_is_rejected_while_busy_and_writes_nothing(runtime, monkeypatch):
    rt, home = runtime
    host = HostFacade(rt)
    before = (home / ".nexus" / "config.toml").read_text()
    monkeypatch.setattr(type(host.supervisor), "running", property(lambda self: 1))
    result = await host.handle(p.ClaudeCodeSettingsSet(main_enabled=True))  # handle() maps failures to ErrorResult
    assert isinstance(result, p.ErrorResult) and result.kind == "SessionBusy"
    assert (home / ".nexus" / "config.toml").read_text() == before
    assert rt.claude_code_main_enabled() is False
    await rt.aclose()


async def test_settings_writer_itself_refuses_without_an_idle_reload(runtime):
    from nexus.errors import ConfigError
    from nexus.host_support import model_settings

    rt, home = runtime
    before = (home / ".nexus" / "config.toml").read_text()
    with pytest.raises(ConfigError, match="running"):
        await model_settings.claude_code_settings_set(rt, main_enabled=True)
    assert (home / ".nexus" / "config.toml").read_text() == before
    await rt.aclose()


async def test_subagent_backend_round_trip_and_validation(runtime):
    rt, home = runtime
    host = HostFacade(rt)
    first = await host.handle(p.ClaudeCodeSettings())
    assert first.subagent_backend == "worker" and "own tools" in first.backend_message

    result = await host.handle(p.ClaudeCodeSettingsSet(subagent_backend="sdk"))
    assert result.subagent_backend == "sdk" and "Nexus tools" in result.backend_message
    saved = tomllib.loads((home / ".nexus" / "config.toml").read_text())["models"]
    assert saved["claude_code_subagent_backend"] == "sdk"
    assert "claude_code_main_enabled" not in saved  # an unset field is left alone
    assert result.main_enabled is False

    bad = await host.handle(p.ClaudeCodeSettingsSet(subagent_backend="zmx"))
    assert isinstance(bad, p.ErrorResult)
    assert tomllib.loads((home / ".nexus" / "config.toml").read_text())["models"][
        "claude_code_subagent_backend"] == "sdk"
    await rt.aclose()
