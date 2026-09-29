"""First-run setup behind the host boundary (plan section 7).

Setup only asks the user to connect a provider. Saving picks that provider's
newest tool-calling model from the models.dev catalogue (the workspace cache,
refreshed like the model registry; the packaged snapshot when offline), writes
it as the user-global default, and reloads the daemon's model routes so the
next turn uses it without a restart.
"""

from __future__ import annotations

import asyncio
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config.paths import nexus_home
from ..errors import ConfigError
from ..model.registry import DEFAULT_CATALOGUE_URL, ModelInfo, ModelRegistry
from . import provider_auth, settings_inventory
from .settings_scope import settings_target

_PROVIDERS = ("codex", "github-copilot", "opencode-go", "openai", "anthropic", "google", "ollama")
#: Signed in from Settings → Providers; credentials live in the keychain.
_SIGNED_IN = ("codex", "github-copilot", "opencode-go")
_ENV_NAMES = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
}
_PROVIDER_INFO = {
    "codex": ("ChatGPT (Codex)", "Sign in with your ChatGPT account in Settings → Providers."),
    "github-copilot": ("GitHub Copilot", "Nexus does not sign in through OpenCode's GitHub app."),
    "opencode-go": ("OpenCode Go", "Paste your OpenCode Go API key in Settings → Providers."),
    "openai": ("OpenAI", "Set OPENAI_API_KEY in the daemon environment."),
    "anthropic": ("Anthropic", "Set ANTHROPIC_API_KEY in the daemon environment."),
    "google": (
        "Google Gemini",
        "Set GEMINI_API_KEY or GOOGLE_GENERATIVE_AI_API_KEY in the daemon environment.",
    ),
    "ollama": ("Ollama", "Local provider; availability is not checked. Install and run Ollama locally."),
}
#: Setup providers whose models.dev entry has another id.
_CATALOGUE_IDS = {"codex": "openai"}
#: Last resort when neither the catalogue nor the packaged snapshot lists the provider.
_FALLBACK_MODELS = {"github-copilot": "claude-sonnet-5", "opencode-go": "kimi-k3"}
#: Ollama serves whatever is pulled locally, so setup never picks its model.
_AUTO_PICK = tuple(provider for provider in _PROVIDERS if provider != "ollama")
_CATALOGUE_TIMEOUT_S = 10.0
_MAX_MODELS = 512


def _environment(runtime: object) -> Mapping[str, str]:
    environ = getattr(runtime, "_environ", None)
    return environ if isinstance(environ, Mapping) else os.environ


async def _connected(runtime: object, provider: str) -> bool:
    if provider in _SIGNED_IN:
        return await provider_auth.connected(runtime, provider)
    if provider == "ollama":
        return await _ollama_connected()
    environ = _environment(runtime)
    return any(bool(environ.get(name)) for name in _ENV_NAMES[provider])


async def _ollama_connected() -> bool:
    """Check the local default daemon only; never probe an untrusted endpoint."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 11434), 0.4)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, TimeoutError):
        return False


def _registry(runtime: object) -> ModelRegistry:
    """A registry over every setup provider, honouring ``[models]`` catalogue settings."""
    config = getattr(getattr(_load_config(runtime), "v2", None), "models", None)
    return ModelRegistry(
        providers={provider: {} for provider in _PROVIDERS},
        env={},
        # Not project-specific: one shared catalogue cache under the home root
        # (STATE_PLAN §5.4), same location ``Runtime`` uses.
        cache_path=nexus_home(getattr(runtime, "_home", None)) / "cache" / "models.dev.json",
        catalogue_url=getattr(config, "catalogue_url", DEFAULT_CATALOGUE_URL),
        offline=bool(getattr(config, "offline", False)),
        timeout_s=_CATALOGUE_TIMEOUT_S,
        provider_aliases=_CATALOGUE_IDS,
    )


def _setup_usable(provider: str, model: ModelInfo) -> bool:
    """A chat model that can call tools over the route setup writes."""
    if not model.tool_call or "text" not in model.input_modalities:
        return False
    # Copilot is routed over chat completions; its GPT-5+ models need Responses.
    match = re.match(r"gpt-(\d+)", model.id)
    return not (provider == "github-copilot" and match and int(match[1]) >= 5 and model.id != "gpt-5-mini")


def _newest_first(model: ModelInfo) -> tuple:
    date = model.release_date or model.last_updated or ""
    output_cost = model.cost.output if model.cost is not None and model.cost.output else 0.0
    # Same-day releases: the pricier model is the flagship.
    return (date, model.last_updated or "", output_cost, model.id)


async def _models(runtime: object, provider: str) -> list[str]:
    """Usable model ids for ``provider``, newest first (bounded)."""
    registry = _registry(runtime)
    await registry.load()
    rows = [model for model in registry.list(provider=provider) if _setup_usable(provider, model)]
    ids = [model.id for model in sorted(rows, key=_newest_first, reverse=True)][:_MAX_MODELS]
    fallback = _FALLBACK_MODELS.get(provider)
    return ids or ([fallback] if fallback else [])


def _load_config(runtime: object) -> object | None:
    loader = getattr(runtime, "_load_config", None)
    if callable(loader):
        try:
            return loader()
        except (ConfigError, OSError):
            return None
    return getattr(runtime, "_config", None)


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
    providers: list[dict[str, Any]] = []
    for provider in _PROVIDERS:
        label, instruction = _PROVIDER_INFO[provider]
        connected = await _connected(runtime, provider)
        providers.append(
            {"id": provider, "label": label, "connected": connected, "instruction": instruction,
             # Whether connecting it completes setup with its newest model.
             "auto": provider in _AUTO_PICK}
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
    }


async def setup_save(runtime: object, provider: str, model: str = "", *, reload: bool = False) -> dict[str, Any]:
    """Save ``provider`` with ``model`` (its newest when blank) as the global default.

    With ``reload`` (the host passes it only while no turn runs) the daemon's
    model routes are rebuilt so the next turn uses the new default.
    """
    if provider not in _PROVIDERS:
        raise ConfigError("unknown setup provider")
    if not isinstance(model, str) or len(model) > 256:
        raise ConfigError("invalid setup model")
    if not await _connected(runtime, provider):
        raise ConfigError("provider credentials are not available")
    if provider == "ollama" and not model:
        raise ConfigError("choose an installed Ollama model with /model")
    models = await _models(runtime, provider)
    model = model or (models[0] if models else "")
    if model not in models:
        raise ConfigError("model is not in the setup catalogue for this provider")

    reference = f"{provider}/{model}"
    updates = [("models", "default", reference)]
    domain = await provider_auth.copilot_domain(runtime) if provider == "github-copilot" else None
    updates.extend((f"providers.{provider}", key, value) for key, value in _provider_config(provider, runtime, domain))
    try:
        provider_auth.write_global_keys(runtime, tuple(updates))
    except ConfigError as exc:
        if "v1" in str(exc):
            raise ConfigError("global config is v1; convert it to config_version = 2 before setup") from None
        if "changed while saving" in str(exc):
            raise ConfigError("global config changed while saving; try setup again") from None
        raise ConfigError("could not save global setup config") from None
    reloader = getattr(runtime, "reload_model_routes", None)
    reloaded = False
    if reload and callable(reloader):
        try:
            reloaded = bool(await reloader())
        except (ConfigError, OSError, ValueError):
            reloaded = False
    return {"global_model": reference, "restart_required": not reloaded}


def _provider_config(provider: str, runtime: object, domain: str | None = None) -> tuple[tuple[str, str], ...]:
    if provider in _SIGNED_IN:
        return provider_auth.provider_route(provider, domain)
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
