"""Global first-run provider/model setup behind the host boundary."""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.errors import ConfigError
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.host_support import setup
from nexus.model.registry import ModelRegistry
from nexus.runtime import Runtime


def _model(release, *, tools=True, output_cost=1.0, modalities=("text",)):
    return {"tool_call": tools, "release_date": release, "cost": {"input": 1.0, "output": output_cost},
            "modalities": {"input": list(modalities), "output": ["text"]}}


#: A models.dev-shaped catalogue; setup must pick from it, never from the network.
CATALOGUE = {
    "openai": {"npm": "@ai-sdk/openai", "env": ["OPENAI_API_KEY"], "models": {
        "gpt-5.6": _model("2026-07-09"),
        "gpt-6-luna": _model("2026-09-22", output_cost=5.0),
        "gpt-6-sol": _model("2026-09-22", output_cost=20.0),
        "gpt-image-3": _model("2026-09-25", tools=False),
    }},
    "github-copilot": {"npm": "@ai-sdk/openai-compatible", "models": {
        "claude-opus-5.5": _model("2026-09-22"),
        "gpt-6-sol": _model("2026-09-23"),
        "gpt-5-mini": _model("2025-08-07"),
    }},
    "opencode-go": {"npm": "@ai-sdk/openai-compatible", "models": {
        "kimi-k3": _model("2026-07-16"),
        "glm-5.3": _model("2026-08-14"),
    }},
}


@pytest.fixture(autouse=True)
def _offline_catalogue(monkeypatch):
    def registry(runtime):
        registry = ModelRegistry(
            providers={provider: {} for provider in setup._PROVIDERS}, env={}, offline=True,
            use_snapshot=False, provider_aliases=setup._CATALOGUE_IDS,
        )
        registry.install_raw(json.dumps(CATALOGUE))
        return registry
    monkeypatch.setattr(setup, "_registry", registry)


class _OAuth:
    def __init__(self, connected: bool):
        self.connected = connected

    async def status(self):
        return self.connected


class _Keychain:
    """Signed-in providers without touching the real keychain."""

    def __init__(self, connected: bool, domain: str | None = None):
        self.connected, self._domain = connected, domain

    async def status(self):
        return self.connected

    async def domain(self):
        return self._domain if self.connected else None


def _runtime(home: Path, *, environ=None, connected=False, model="workspace/model", signed_in=()):
    return SimpleNamespace(
        workspace=home / "workspace",
        _home=home,
        _environ=environ or {},
        _codex_auth_factory=lambda **kwargs: _OAuth(connected),
        _copilot_auth_factory=lambda **kwargs: _Keychain("github-copilot" in signed_in, "company.ghe.com"),
        _api_key_auth_factory=lambda provider, **kwargs: _Keychain(provider in signed_in),
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

    assert set(result) == {"required", "global_model", "effective_model", "providers"}
    assert result["required"] is True
    assert result["global_model"] == ""
    assert result["effective_model"] == "workspace/model"
    assert [row["id"] for row in result["providers"]] == [
        "codex", "github-copilot", "opencode-go", "openai", "anthropic", "google", "ollama"
    ]
    assert not next(row for row in result["providers"] if row["id"] == "github-copilot")["connected"]
    assert all(set(row) == {"id", "label", "connected", "instruction", "auto"} for row in result["providers"])
    # Connecting any provider but Ollama (whatever is pulled locally) completes setup.
    assert [row["id"] for row in result["providers"] if not row["auto"]] == ["ollama"]
    assert next(row for row in result["providers"] if row["id"] == "openai")["connected"]
    assert next(row for row in result["providers"] if row["id"] == "ollama")["connected"]
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("provider", "newest"),
    [
        # Same-day releases: the pricier model is the flagship; no image models.
        ("openai", "gpt-6-sol"),
        ("codex", "gpt-6-sol"),
        # Copilot routes over chat completions, so its GPT-5+ models are skipped.
        ("github-copilot", "claude-opus-5.5"),
        ("opencode-go", "glm-5.3"),
    ],
)
def test_setup_save_without_model_picks_newest_tool_model(tmp_path, monkeypatch, provider, newest):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    runtime = _runtime(home, environ={"OPENAI_API_KEY": "secret"}, connected=True, signed_in=(provider,))

    result = asyncio.run(setup.setup_save(runtime, provider))

    assert result == {"global_model": f"{provider}/{newest}", "restart_required": True}


def test_setup_save_reloads_routes_only_when_asked(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    runtime = _runtime(home, environ={"OPENAI_API_KEY": "secret"})
    calls = []
    async def reload():
        calls.append(tomllib.loads((home / ".nexus" / "config.toml").read_text())["models"]["default"])
        return True
    runtime.reload_model_routes = reload

    busy = asyncio.run(setup.setup_save(runtime, "openai"))
    idle = asyncio.run(setup.setup_save(runtime, "openai", reload=True))

    assert busy["restart_required"] is True and idle["restart_required"] is False
    # Routes reload after the config is written.
    assert calls == ["openai/gpt-6-sol"]


def test_setup_save_never_picks_an_ollama_model(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    async def local_ollama():
        return True
    monkeypatch.setattr(setup, "_ollama_connected", local_ollama)

    with pytest.raises(ConfigError, match="Ollama"):
        asyncio.run(setup.setup_save(_runtime(home), "ollama"))


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


@pytest.mark.parametrize(
    ("provider", "model", "route"),
    [
        ("github-copilot", "claude-opus-5.5",
         {"auth": "github_copilot", "base_url": "https://copilot-api.company.ghe.com", "api": "chat"}),
        ("opencode-go", "kimi-k3",
         {"auth": "keychain", "base_url": "https://opencode.ai/zen/go/v1", "api": "chat"}),
    ],
)
def test_setup_save_routes_signed_in_providers_without_credentials(tmp_path, monkeypatch, provider, model, route):
    home = tmp_path / "home"
    home.mkdir()
    _home(monkeypatch, home)
    runtime = _runtime(home, signed_in=(provider,))

    result = asyncio.run(setup.setup_save(runtime, provider, model))

    saved = tomllib.loads((home / ".nexus" / "config.toml").read_text(encoding="utf-8"))
    assert result["global_model"] == f"{provider}/{model}"
    assert saved["providers"][provider] == route
    assert "api_key" not in saved["providers"][provider]


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
        saved = await facade.handle(p.SetupSave(provider="openai"))
        assert isinstance(saved, p.SetupSaveResult)
        # No turn is running, so the daemon reloads its routes instead of restarting.
        assert saved.global_model == "openai/gpt-6-sol" and not saved.restart_required
        assert runtime.router.default == "openai/gpt-6-sol"
        ready = await facade.handle(p.SetupStatus())
        assert isinstance(ready, p.SetupStatusResult) and not ready.required
    finally:
        await runtime.aclose()

    from nexus.config import Config
    assert Config.load(second, home=home, environ={}).model == "openai/gpt-6-sol"
