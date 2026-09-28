"""Global first-run provider/model setup behind the host boundary."""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host_support import setup
from nexus.runtime import Runtime


class _OAuth:
    def __init__(self, connected: bool):
        self.connected = connected

    async def status(self):
        return self.connected


def _runtime(home: Path, *, environ=None, connected=False, model="workspace/model"):
    return SimpleNamespace(
        workspace=home / "workspace",
        _home=home,
        _environ=environ or {},
        _codex_auth_factory=lambda **kwargs: _OAuth(connected),
        _load_config=lambda: SimpleNamespace(model=model),
    )


def _home(monkeypatch, home: Path):
    monkeypatch.setattr(Path, "home", lambda: home)


def test_setup_status_exposes_bounded_choices_without_environment_values(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    secret = "never-return-this-key"
    runtime = _runtime(home, environ={"OPENAI_API_KEY": secret}, connected=True)
    async def local_ollama():
        return True
    monkeypatch.setattr(setup, "_ollama_connected", local_ollama)

    result = asyncio.run(setup.setup_status(runtime))

    assert set(result) == {"required", "global_model", "effective_model", "providers", "models"}
    assert result["required"] is True
    assert result["global_model"] == ""
    assert result["effective_model"] == "workspace/model"
    assert [row["id"] for row in result["providers"]] == [
        "codex", "openai", "anthropic", "google", "ollama"
    ]
    assert all(set(row) == {"id", "label", "connected", "instruction"} for row in result["providers"])
    assert next(row for row in result["providers"] if row["id"] == "openai")["connected"]
    assert next(row for row in result["providers"] if row["id"] == "ollama")["connected"]
    assert all(set(row) == {"provider", "id", "name", "date"} for row in result["models"])
    assert any(row["provider"] == "codex" and row["id"] == "gpt-5.6" for row in result["models"])
    assert len(result["models"]) <= 512
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("provider", "model"),
    [("custom", "gpt-5.6"), ("openai", "made-up-model"), ("../openai", "gpt-5.6")],
)
def test_setup_save_rejects_provider_and_model_outside_catalogue(
    tmp_path, monkeypatch, provider, model
):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    runtime = _runtime(home, environ={"OPENAI_API_KEY": "secret"})

    with pytest.raises(ConfigError):
        asyncio.run(setup.setup_save(runtime, provider, model))

    assert not (home / ".nexus" / "config.toml").exists()


def test_setup_save_requires_provider_credential(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    runtime = _runtime(home)

    with pytest.raises(ConfigError, match="credentials"):
        asyncio.run(setup.setup_save(runtime, "openai", "gpt-5.6"))

    assert not (home / ".nexus" / "config.toml").exists()


def test_setup_save_preserves_unrelated_toml_and_writes_env_reference(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".nexus"
    config_dir.mkdir(parents=True)
    _home(monkeypatch, home)
    secret = "do-not-write-this-secret"
    config_path = config_dir / "config.toml"
    config_path.write_text(
        'config_version = 2\n\n[agent]\nname = "quick"\n\n[context]\nmax_tokens = 40000\n',
        encoding="utf-8",
    )
    runtime = _runtime(home, environ={"OPENAI_API_KEY": secret})

    result = asyncio.run(setup.setup_save(runtime, "openai", "gpt-5.6"))

    text = config_path.read_text(encoding="utf-8")
    saved = tomllib.loads(text)
    assert result == {"global_model": "openai/gpt-5.6", "restart_required": True}
    assert saved["models"]["default"] == "openai/gpt-5.6"
    assert saved["providers"]["openai"] == {
        "api_key": "${env:OPENAI_API_KEY}", "api": "responses"
    }
    assert saved["agent"]["name"] == "quick"
    assert saved["context"]["max_tokens"] == 40000
    assert secret not in text and secret not in repr(result)

    status = asyncio.run(setup.setup_status(runtime))
    assert status["required"] is False
    assert status["global_model"] == "openai/gpt-5.6"
    disconnected = _runtime(home, environ={})
    assert asyncio.run(setup.setup_status(disconnected))["required"] is True


def test_setup_save_rejects_nonempty_v1_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".nexus"
    config_dir.mkdir(parents=True)
    _home(monkeypatch, home)
    config_path = config_dir / "config.toml"
    original = 'model = "old/model"\n'
    config_path.write_text(original, encoding="utf-8")
    runtime = _runtime(home, environ={"OPENAI_API_KEY": "secret"})

    with pytest.raises(ConfigError, match="v1"):
        asyncio.run(setup.setup_save(runtime, "openai", "gpt-5.6"))

    assert config_path.read_text(encoding="utf-8") == original


def test_setup_save_reports_write_conflict(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".nexus"
    config_dir.mkdir(parents=True)
    _home(monkeypatch, home)
    config_path = config_dir / "config.toml"
    original = 'config_version = 2\n\n[agent]\nname = "quick"\n'
    config_path.write_text(original, encoding="utf-8")
    runtime = _runtime(home, environ={"OPENAI_API_KEY": "secret"})
    monkeypatch.setattr(setup.settings_inventory, "write", lambda *_args: {"status": "conflict"})

    with pytest.raises(ConfigError, match="changed while saving"):
        asyncio.run(setup.setup_save(runtime, "openai", "gpt-5.6"))

    assert config_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_host_setup_persists_global_default_across_unrelated_workspaces(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    runtime = Runtime(first, home=home, environ={"OPENAI_API_KEY": "secret"})
    try:
        facade = HostFacade(runtime)
        initial = await facade.handle(p.SetupStatus())
        assert isinstance(initial, p.SetupStatusResult) and initial.required
        assert any(row["provider"] == "openai" for row in initial.models)
        saved = await facade.handle(p.SetupSave(provider="openai", model="gpt-5.6"))
        assert isinstance(saved, p.SetupSaveResult)
        assert saved.global_model == "openai/gpt-5.6" and saved.restart_required
        ready = await facade.handle(p.SetupStatus())
        assert isinstance(ready, p.SetupStatusResult) and not ready.required
    finally:
        await runtime.aclose()

    from nexus.config import Config
    assert Config.load(second, home=home, environ={}).model == "openai/gpt-5.6"
