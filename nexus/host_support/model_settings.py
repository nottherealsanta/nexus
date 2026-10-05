"""Settings -> Models and Settings -> Session titles (plans/SESSION_TITLE_PLAN.md).

Host-side reads and writes for the tier table, the subagent ceiling
(``[agents] max_tier``) and the automatic title switch (``[sessions]``). Every
write goes to the user-global ``~/.nexus/config.toml`` through
``provider_auth.write_global_keys`` (path-policed, hash-checked, read back after
the edit) and then rebuilds the model routes in place, only while no turn runs.

Everything is bounded: tier names, reference counts and reference lengths.
Nothing here returns a credential; rows carry only descriptive model references.
"""
from __future__ import annotations

import re
from typing import Any

from ..errors import ConfigError
from ..model.request import ModelRequest
from . import provider_auth

__all__ = [
    "MAX_TIER_REFS",
    "agent_max_tier_set",
    "session_title_settings",
    "session_title_settings_set",
    "tier_reset",
    "tier_rows",
    "tier_set",
]

#: References one tier may list.
MAX_TIER_REFS = 16
_MAX_REF_CHARS = 128
#: A tier key the line editor can write as a bare TOML key. A custom tier whose
#: name needs quoting (``"team/high"``) is edited in Settings > Config instead.
_TIER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*\Z")

_SOURCE_USER = "your list"
_SOURCE_BUILTIN = "built-in"
_SOURCE_PRICE = "by price"


def _tiers(runtime: object) -> Any:
    tiers = getattr(runtime, "tiers", None)
    if tiers is None:
        raise ConfigError("model tiers are not available")
    return tiers


def _resolved(runtime: object, tier: str) -> str:
    """``provider/model`` a tier runs on right now, or ``""`` when none can run."""
    router = getattr(runtime, "router", None)
    probe = getattr(router, "tier_runnable", None)
    if not callable(probe) or not probe(tier):
        return ""
    try:
        resolved = router.resolve(ModelRequest(messages=[], model=tier))
    except ConfigError:
        return ""
    return f"{getattr(resolved.provider, 'name', '')}/{resolved.model}"


def _effective_agents_max_tier(runtime: object) -> str:
    config = None
    loader = getattr(runtime, "_load_config", None)
    if callable(loader):
        try:
            config = loader()
        except (ConfigError, OSError):
            config = None
    section = getattr(getattr(config, "v2", None), "agents", None)
    value = getattr(section, "max_tier", "high")
    return value if isinstance(value, str) and value else "high"


def tier_rows(runtime: object) -> dict[str, Any]:
    """One row per tier plus the subagent ceiling, for the Models page.

    A row is ``{name, refs, source, resolved, runnable, editable}``: ``refs`` is
    the ordered list in effect (the user's, else the built-in map's, else empty
    for a price-based tier), ``resolved`` the model it runs on now.
    """
    tiers = _tiers(runtime)
    overrides = dict(tiers.overrides)
    builtin = dict(tiers.builtin)
    rows: list[dict[str, Any]] = []
    for name in tiers.order:
        mine = [ref for ref, tier in overrides.items() if tier == name]
        if mine:
            refs, source = mine, _SOURCE_USER
        else:
            refs = [ref for ref, tier in builtin.items() if tier == name]
            source = _SOURCE_BUILTIN if refs else _SOURCE_PRICE
        resolved = _resolved(runtime, name)
        candidates = getattr(getattr(runtime, "router", None), "tier_candidates", None)
        if callable(candidates):
            refs = list(candidates(name))
            if not mine and name in {"low", "medium", "high"} and getattr(runtime.tiers, "builtin", None):
                source = _SOURCE_BUILTIN
        rows.append(
            {
                "name": name,
                "refs": refs,
                "source": source,
                "resolved": resolved,
                "candidates": candidate_rows(runtime, refs, resolved),
                "message": "" if resolved else "No connected provider can serve this tier",
                "runnable": bool(resolved),
                "editable": _TIER_RE.fullmatch(name) is not None,
            }
        )
    return {
        "tiers": rows,
        "max_tier": _effective_agents_max_tier(runtime),
        "order": list(tiers.order),
    }


def candidate_rows(runtime: object, refs: list[str], resolved: str = "") -> list[dict[str, Any]]:
    router = getattr(runtime, "router", None)
    providers = getattr(router, "providers", {})
    return [{"ref": ref, "connected": ref.split("/", 1)[0] in providers,
             "selected": ref == resolved,
             "reason": "" if ref.split("/", 1)[0] in providers else "provider not connected"}
            for ref in refs[:MAX_TIER_REFS]]


def default_settings(runtime: object) -> dict[str, Any]:
    loader = getattr(runtime, "_load_config", None)
    config = loader() if callable(loader) else None
    section = getattr(config, "v2", None)
    router = getattr(runtime, "router", None)
    default = section.model_default() if section is not None else getattr(router, "default", None)
    fallback = section.model_fallback() if section is not None else list(getattr(router, "fallback", ()))
    refs = ([default] if default else []) + fallback
    resolved, message = "", ""
    try:
        model = router.resolve(ModelRequest(messages=[]))
        resolved = f"{model.provider.name}/{model.model}"
    except (ConfigError, AttributeError) as exc:
        message = str(exc)
    return {"refs": refs, "resolved": resolved, "message": message,
            "candidates": candidate_rows(runtime, refs, resolved)}


async def default_models_set(runtime: object, refs: list[str], *, reload: bool = False) -> dict[str, Any]:
    cleaned = _validate_refs(refs)
    if len(cleaned) != len(refs):
        raise ConfigError("default model chain contains duplicate references")
    _check_refs_known(runtime, cleaned)
    _write(runtime, (("models", "default", cleaned[0]), ("models", "fallback", cleaned[1:])))
    reloaded = await _reload(runtime, reload)
    return {**default_settings(runtime), "restart_required": not reloaded}


def _validate_refs(refs: list[str]) -> list[str]:
    if not isinstance(refs, list) or not refs:
        raise ConfigError("choose at least one model for the tier")
    if len(refs) > MAX_TIER_REFS:
        raise ConfigError(f"a tier lists at most {MAX_TIER_REFS} models")
    cleaned: list[str] = []
    for ref in refs:
        if not isinstance(ref, str):
            raise ConfigError("model references must be strings")
        ref = ref.strip()
        if not ref or len(ref) > _MAX_REF_CHARS or _REF_RE.fullmatch(ref) is None:
            raise ConfigError("invalid model reference")
        if ref not in cleaned:
            cleaned.append(ref)
    return cleaned


def _validate_tier(runtime: object, tier: str) -> str:
    if not isinstance(tier, str) or _TIER_RE.fullmatch(tier) is None:
        raise ConfigError("invalid tier name; edit custom tiers in Settings > Config")
    if tier not in _tiers(runtime).order:
        raise ConfigError(f"unknown tier {tier!r}")
    return tier


async def _reload(runtime: object, reload: bool) -> bool:
    reloader = getattr(runtime, "reload_model_routes", None)
    if not reload or not callable(reloader):
        return False
    try:
        return bool(await reloader())
    except (ConfigError, OSError, ValueError):
        return False


def _write(runtime: object, updates: tuple[tuple[str, str, Any], ...]) -> None:
    try:
        provider_auth.write_global_keys(runtime, updates)
    except ConfigError as exc:
        if "v1" in str(exc):
            raise ConfigError("global config is v1; convert it to config_version = 2 first") from None
        raise


def _check_refs_known(runtime: object, refs: list[str]) -> None:
    """Refuse a reference the registry (when there is one) cannot find."""
    registry = getattr(runtime, "registry", None)
    get = getattr(registry, "get", None)
    if not callable(get):
        return
    tiers = _tiers(runtime)
    for ref in refs:
        if ref in tiers.order:
            raise ConfigError(f"{ref!r} is a tier, not a model")
        if get(ref) is None:
            raise ConfigError(f"unknown model {ref!r}")


async def tier_set(runtime: object, tier: str, refs: list[str], *, reload: bool = False) -> dict[str, Any]:
    """Write ``[models.tiers] <tier> = [refs]`` (the first runnable one is used)."""
    tier = _validate_tier(runtime, tier)
    refs = _validate_refs(refs)
    _check_refs_known(runtime, refs)
    _write(runtime, (("models.tiers", tier, refs),))
    reloaded = await _reload(runtime, reload)
    return {**tier_rows(runtime), "restart_required": not reloaded}


async def tier_reset(runtime: object, tier: str, *, reload: bool = False) -> dict[str, Any]:
    """Remove the user's list so the tier returns to the built-in or price rule."""
    tier = _validate_tier(runtime, tier)
    _write(runtime, (("models.tiers", tier, None),))
    reloaded = await _reload(runtime, reload)
    return {**tier_rows(runtime), "restart_required": not reloaded}


async def agent_max_tier_set(runtime: object, tier: str, *, reload: bool = False) -> dict[str, Any]:
    """Write ``[agents] max_tier``; subagents never run above it."""
    if not isinstance(tier, str) or tier not in _tiers(runtime).order:
        raise ConfigError(f"unknown tier {tier!r}")
    if _TIER_RE.fullmatch(tier) is None:
        raise ConfigError("invalid tier name")
    _write(runtime, (("agents", "max_tier", tier),))
    # max_tier is read when a turn builds its subagent runner, so no route
    # rebuild is needed for it to apply to the next turn.
    return {**tier_rows(runtime), "restart_required": False}


def session_title_settings(runtime: object) -> dict[str, Any]:
    """``{enabled, model, resolved, message}`` for the Session titles page."""
    config = None
    loader = getattr(runtime, "_load_config", None)
    if callable(loader):
        try:
            config = loader()
        except (ConfigError, OSError):
            config = None
    section = getattr(getattr(config, "v2", None), "sessions", None)
    enabled = bool(getattr(section, "auto_title", True))
    model = str(getattr(section, "title_model", "low") or "low")
    resolved, message = resolve_title_model(runtime, model)
    return {"enabled": enabled, "model": model, "resolved": resolved, "message": message}


def resolve_title_model(runtime: object, model: str) -> tuple[str, str]:
    """``(provider/model, "")`` or ``("", why not)`` for a title model setting."""
    tiers = getattr(runtime, "tiers", None)
    if tiers is not None and model in tiers.order:
        resolved = _resolved(runtime, model)
        if resolved:
            return resolved, ""
        return "", f"The {model} tier has no runnable model; titles use your first message."
    router = getattr(runtime, "router", None)
    try:
        resolved_model = router.resolve(ModelRequest(messages=[], model=model))
    except (ConfigError, AttributeError):
        return "", f"{model} cannot be resolved; titles use your first message."
    return f"{getattr(resolved_model.provider, 'name', '')}/{resolved_model.model}", ""


async def session_title_settings_set(
    runtime: object,
    *,
    enabled: bool | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Write ``[sessions] auto_title`` and/or ``title_model``."""
    updates: list[tuple[str, str, Any]] = []
    if enabled is not None:
        if type(enabled) is not bool:
            raise ConfigError("auto_title must be true or false")
        updates.append(("sessions", "auto_title", enabled))
    if model is not None:
        model = model.strip() if isinstance(model, str) else ""
        if not model or len(model) > _MAX_REF_CHARS or _REF_RE.fullmatch(model) is None:
            raise ConfigError("title model must be a tier or a model reference")
        updates.append(("sessions", "title_model", model))
    if updates:
        _write(runtime, tuple(updates))
    return session_title_settings(runtime)
