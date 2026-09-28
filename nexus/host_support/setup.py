"""First-run provider and model selection behind the host boundary (plan section 7)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..auth.codex import CodexOAuthManager
from ..errors import ConfigError
from . import settings_inventory
from .settings_scope import settings_target

_PROVIDERS = ("codex", "openai", "anthropic", "google", "ollama")
_ENV_NAMES = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
}
_PROVIDER_INFO = {
    "codex": ("ChatGPT (Codex)", "Sign in with `nexus auth codex login` to use your ChatGPT account."),
    "openai": ("OpenAI", "Set OPENAI_API_KEY in the daemon environment."),
    "anthropic": ("Anthropic", "Set ANTHROPIC_API_KEY in the daemon environment."),
    "google": (
        "Google Gemini",
        "Set GEMINI_API_KEY or GOOGLE_GENERATIVE_AI_API_KEY in the daemon environment.",
    ),
    "ollama": ("Ollama", "Local provider; availability is not checked. Install and run Ollama locally."),
}
_MAX_CATALOGUE_BYTES = 256 * 1024
_MAX_MODELS = 512


def _environment(runtime: object) -> Mapping[str, str]:
    environ = getattr(runtime, "_environ", None)
    return environ if isinstance(environ, Mapping) else os.environ


async def _codex_connected(runtime: object) -> bool:
    factory = getattr(runtime, "_codex_auth_factory", None) or CodexOAuthManager
    try:
        manager = factory(profile="default")
        return bool(await manager.status())
    except Exception:  # noqa: BLE001 - auth backend errors only affect this boolean.
        return False


async def _ollama_connected() -> bool:
    """Check the local default daemon only; never probe an untrusted endpoint."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 11434), 0.4)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError):
        return False


def _catalogue_models() -> list[dict[str, str]]:
    path = Path(__file__).resolve().parents[1] / "model" / "data" / "models.min.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_CATALOGUE_BYTES + 1)
        if len(raw) > _MAX_CATALOGUE_BYTES:
            return []
        document = json.loads(raw)
    except (OSError, UnicodeError, ValueError):
        return []
    if not isinstance(document, dict):
        return []

    rows: list[dict[str, str]] = []
    for provider in _PROVIDERS:
        catalogue_provider = "openai" if provider == "codex" else provider
        details = document.get(catalogue_provider)
        model_map = details.get("models") if isinstance(details, dict) else None
        if not isinstance(model_map, dict):
            continue
        for model_key, item in sorted(model_map.items()):
            if len(rows) >= _MAX_MODELS:
                return rows
            if not isinstance(item, dict):
                continue
            model_id = item.get("id", model_key)
            name = item.get("name", model_id)
            if not isinstance(model_id, str) or not model_id or not isinstance(name, str):
                continue
            date = item.get("release_date") or item.get("last_updated") or ""
            if not isinstance(date, str):
                date = ""
            rows.append({"provider": provider, "id": model_id, "name": name, "date": date})
    return rows


def _global_model(runtime: object) -> str:
    document = _global_config(runtime)
    models = document.get("models")
    if isinstance(models, dict) and isinstance(models.get("default"), str):
        return models["default"]
    legacy = document.get("model")
    return legacy if isinstance(legacy, str) else ""


def _global_config(runtime: object) -> dict[str, Any]:
    target = settings_target(runtime, "global", "config", "config")
    try:
        with target.path.open("rb") as stream:
            raw = stream.read(settings_inventory.MAX_BODY + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if len(raw) > settings_inventory.MAX_BODY:
        return {}
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def _effective_model(runtime: object) -> str:
    config = getattr(runtime, "_config", None)
    loader = getattr(runtime, "_load_config", None)
    if callable(loader):
        try:
            config = loader()
        except (ConfigError, OSError):
            pass
    model = getattr(config, "model", "")
    return model if isinstance(model, str) else ""


async def setup_status(runtime: object) -> dict[str, Any]:
    """Return bounded first-run options without exposing credential values."""
    environ = _environment(runtime)
    providers: list[dict[str, Any]] = []
    for provider in _PROVIDERS:
        label, instruction = _PROVIDER_INFO[provider]
        if provider == "codex":
            connected = await _codex_connected(runtime)
        elif provider == "ollama":
            connected = await _ollama_connected()
        else:
            connected = any(bool(environ.get(name)) for name in _ENV_NAMES[provider])
        providers.append(
            {"id": provider, "label": label, "connected": connected, "instruction": instruction}
        )
    document = _global_config(runtime)
    global_model = _global_model(runtime)
    selected_provider = global_model.partition("/")[0]
    configured = isinstance(document.get("providers"), dict) and selected_provider in document["providers"]
    connected = next((row["connected"] for row in providers if row["id"] == selected_provider), False)
    return {
        "required": not (bool(global_model) and configured and connected),
        "global_model": global_model,
        "effective_model": _effective_model(runtime),
        "providers": providers,
        "models": _catalogue_models(),
    }


async def setup_save(runtime: object, provider: str, model: str) -> dict[str, Any]:
    """Persist one validated model selection in the user-global v2 config."""
    if provider not in _PROVIDERS:
        raise ConfigError("unknown setup provider")
    if not isinstance(model, str) or not any(
        row["provider"] == provider and row["id"] == model for row in _catalogue_models()
    ):
        raise ConfigError("model is not in the setup catalogue for this provider")

    if provider == "codex":
        connected = await _codex_connected(runtime)
    elif provider == "ollama":
        connected = await _ollama_connected()
    else:
        environ = _environment(runtime)
        connected = any(bool(environ.get(name)) for name in _ENV_NAMES[provider])
    if not connected:
        raise ConfigError("provider credentials are not available")

    target = settings_target(runtime, "global", "config", "config")
    try:
        with target.path.open("rb") as stream:
            old = stream.read(settings_inventory.MAX_BODY + 1)
    except FileNotFoundError:
        old = b""
    except OSError as exc:
        raise ConfigError("cannot read global config") from exc
    if len(old) > settings_inventory.MAX_BODY:
        raise ConfigError("global config exceeds 256 KiB")
    try:
        text = old.decode("utf-8")
        document = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("cannot update invalid global config") from exc
    if document and document.get("config_version") != 2:
        raise ConfigError("global config is v1; convert it to config_version = 2 before setup")
    if not document:
        text = "config_version = 2\n\n"

    reference = f"{provider}/{model}"
    body = settings_inventory.set_toml_key(text, "models", "default", reference)
    provider_values = _provider_config(provider, runtime)
    for key, value in provider_values:
        body = settings_inventory.set_toml_key(body, f"providers.{provider}", key, value)

    expected = hashlib.sha256(old).hexdigest() if old else ""
    try:
        result = settings_inventory.write(
            runtime, "global", "config", "config", body, expected
        )
    except (ConfigError, OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("could not save global setup config") from exc
    if result.get("status") != "written":
        raise ConfigError("global config changed while saving; try setup again")
    return {"global_model": reference, "restart_required": True}


def _provider_config(provider: str, runtime: object) -> tuple[tuple[str, str], ...]:
    if provider == "codex":
        return (("auth", "chatgpt_oauth"), ("profile", "default"), ("api", "responses"))
    if provider == "openai":
        return (("api_key", "${env:OPENAI_API_KEY}"), ("api", "responses"))
    if provider == "anthropic":
        return (("api_key", "${env:ANTHROPIC_API_KEY}"),)
    if provider == "google":
        name = next(
            (name for name in _ENV_NAMES[provider] if _environment(runtime).get(name)),
            _ENV_NAMES[provider][0],
        )
        return (("api_key", f"${{env:{name}}}"),)
    return (("api", "ollama"),)


__all__ = ["setup_save", "setup_status"]
