"""Layer loading, merging, and version detection (plan section 7).

Precedence, lowest to highest: built-in defaults, user config, workspace config,
``NEXUS_*`` environment, command-line flags, per-session overrides. Tables merge
deeply; scalars replace; the permission ``allow``/``ask``/``deny`` lists append.

Version bridging: a single document is either flat v1 or sectioned v2, never
both. When files disagree across layers — the realistic case being a v2
``~/.nexus/config.toml`` beside a legacy flat project ``nexus.toml`` — the
effective version is the higher one and v1 layers are translated into the v2
shape instead of failing. A v1 workspace therefore keeps working in a
v2-configured home, and a v1 home still contributes to a v2 workspace.
"""
from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from ..errors import ConfigError
from .paths import (
    user_config_path,
    workspace_config_path,
    workspace_settings_config_path,
)
from .schema import ConfigV2

LEGACY_KEYS = frozenset(
    {
        "executable",
        "model",
        "sandbox",
        "timeout_seconds",
        "context_chars",
        "instructions_file",
        "memory_file",
    }
)
V2_SECTION_KEYS = frozenset(
    {
        "agent",
        "model",
        "models",
        "providers",
        "context",
        "permissions",
        "tools",
        "ext",
        "agents",
        "hooks",
        "mcp",
        "session",
        "sessions",
        "settings",
        "telemetry",
    }
)
# ``model`` is the one name shared by both shapes: a string in v1, a table in v2.
LEGACY_ONLY_KEYS = LEGACY_KEYS - V2_SECTION_KEYS
APPEND_LIST_KEYS = ("allow", "ask", "deny")

_ENV_PREFIX = "NEXUS_"
_LEGACY_ENV = {
    "NEXUS_EXECUTABLE": "executable",
    "NEXUS_MODEL": "model",
    "NEXUS_SANDBOX": "sandbox",
    "NEXUS_TIMEOUT_SECONDS": "timeout_seconds",
    "NEXUS_CONTEXT_CHARS": "context_chars",
    "NEXUS_INSTRUCTIONS_FILE": "instructions_file",
    "NEXUS_MEMORY_FILE": "memory_file",
}

# Env coercion is schema-aware and conservative: only fields the schema declares
# as int/float/bool are converted. Provider credentials, model names, paths, and
# anything else stay strings even when they look numeric.
_LEGACY_ENV_TYPES = {"timeout_seconds": float, "context_chars": int}
_V2_NUMERIC = {
    "agent.max_iterations": int,
    "agent.max_turn_seconds": float,
    "model.params.temperature": float,
    "model.params.max_output_tokens": int,
    "model.params.thinking_budget": int,
    "models.refresh_ttl_days": float,
    "context.max_tokens": int,
    "context.safety_margin_tokens": int,
    "context.compact_at_fraction": float,
    "context.limits.memory": int,
    "context.limits.skills_index": int,
    "context.limits.environment": int,
    "context.limits.attachments": int,
    "tools.bash_timeout_s": float,
    "tools.web.search_timeout_s": float,
    "tools.web.fetch_timeout_s": float,
    "tools.web.max_results": int,
    "tools.web.max_query_length": int,
    "tools.web.max_output_bytes": int,
    "tools.max_result_tokens": int,
    "tools.max_parallel": int,
    "ext.watch_interval_ms": int,
    "ext.max_file_bytes": int,
    "agents.max_depth": int,
    "agents.max_concurrent": int,
    "agents.max_fanout": int,
    "agents.token_budget": int,
    "agents.cost_budget": float,
    "mcp.connect_timeout_s": float,
    "mcp.restart_max": int,
    "session.snapshot_every": int,
}
_V2_BOOL = frozenset(
    {
        "ext.enabled",
        "ext.quarantine",
        "agents.enabled",
        "agents.seed_roles",
        "hooks.enabled",
        "mcp.enabled",
        "models.offline",
        "tools.web.fetch_enabled",
    }
)


@dataclass(frozen=True)
class Effective:
    """The merged configuration plus the shape it resolved to."""

    version: int
    values: dict[str, Any]
    source: str | None


def builtin_defaults() -> dict[str, Any]:
    """The lowest layer. Section defaults live on the msgspec structs."""
    return {"config_version": 2}


def read_toml(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config root must be a table: {path}")
    return data


def _has_v2_sections(doc: Mapping[str, Any]) -> bool:
    return any(
        key in doc and isinstance(doc[key], Mapping) for key in V2_SECTION_KEYS
    )


def detect_version(doc: Mapping[str, Any], *, source: str) -> int:
    explicit = doc.get("config_version")
    if explicit is not None:
        if explicit == 2:
            mixed = bool(LEGACY_ONLY_KEYS & doc.keys()) or (
                "model" in doc and not isinstance(doc["model"], Mapping)
            )
            if mixed:
                raise ConfigError(f"Mixed v1/v2 configuration in {source}")
            return 2
        if explicit == 1:
            if _has_v2_sections(doc):
                raise ConfigError(f"Mixed v1/v2 configuration in {source}")
            return 1
        raise ConfigError(f"Unsupported config_version in {source}: {explicit!r}")
    if _has_v2_sections(doc):
        raise ConfigError(
            f"v2 sections require config_version = 2 in {source}"
        )
    return 1


def validate_v1(doc: Mapping[str, Any], *, source: str) -> None:
    """Reject unknown keys in a flat document. ``config_version`` is the discriminator."""
    unknown = set(doc) - LEGACY_KEYS - {"config_version"}
    if unknown:
        raise ConfigError(
            f"Unknown nexus.toml settings in {source}: {', '.join(sorted(unknown))}"
        )


def normalize_v1_to_v2(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a flat v1 document into the equivalent v2 section fragment."""
    result: dict[str, Any] = {}
    codex: dict[str, Any] = {}
    if "executable" in doc:
        codex["executable"] = doc["executable"]
    if "timeout_seconds" in doc:
        codex["timeout_seconds"] = doc["timeout_seconds"]
    if codex:
        result["providers"] = {"codex": codex}
    if "model" in doc:
        result["model"] = {"default": doc["model"]}
    agent: dict[str, Any] = {}
    if "agent_name" in doc:
        agent["name"] = doc["agent_name"]
    if "sandbox" in doc:
        agent["sandbox"] = doc["sandbox"]
    if "instructions_file" in doc:
        agent["instructions_file"] = doc["instructions_file"]
    if "memory_file" in doc:
        agent["memory_file"] = doc["memory_file"]
    if agent:
        result["agent"] = agent
    if "context_chars" in doc:
        chars = doc["context_chars"]
        if isinstance(chars, bool) or not isinstance(chars, int) or chars < 1024:
            raise ConfigError("context_chars must be an integer >= 1024")
        # v2 budgets are in tokens; the legacy character budget round-trips
        # through the same 4 chars/token approximation the context builder uses.
        result["context"] = {"max_tokens": max(1, round(chars / 4))}
    return result


def _append_unique(base: list[Any], extra: list[Any]) -> list[Any]:
    merged = list(base)
    for item in extra:
        if item not in merged:
            merged.append(item)
    return merged


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(value, Mapping) and isinstance(existing, Mapping):
            result[key] = deep_merge(existing, value)
        elif (
            key in APPEND_LIST_KEYS
            and isinstance(value, list)
            and isinstance(existing, list)
        ):
            result[key] = _append_unique(existing, value)
        else:
            result[key] = value
    return result


def _to_int(raw: str) -> Any:
    try:
        return int(raw)
    except ValueError:
        return raw


def _to_float(raw: str) -> Any:
    try:
        return float(raw)
    except ValueError:
        return raw


def _coerce_legacy(field: str, raw: str) -> Any:
    target = _LEGACY_ENV_TYPES.get(field)
    if target is int:
        return _to_int(raw)
    if target is float:
        return _to_float(raw)
    return raw


def _coerce_v2(keys: list[str], raw: str) -> Any:
    path = ".".join(keys)
    if path in _V2_BOOL:
        lowered = raw.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        return raw
    target = _V2_NUMERIC.get(path)
    if target is None and len(keys) == 3 and keys[0] == "providers" and keys[2] == "timeout_seconds":
        target = float
    if target is int:
        return _to_int(raw)
    if target is float:
        return _to_float(raw)
    return raw


def env_overlay_v1(environ: Mapping[str, str]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for name, field in _LEGACY_ENV.items():
        if name in environ:
            overlay[field] = _coerce_legacy(field, environ[name])
    return overlay


def env_overlay_v2(environ: Mapping[str, str]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for name, raw in environ.items():
        if not name.startswith(_ENV_PREFIX):
            continue
        path = name[len(_ENV_PREFIX) :]
        if "__" not in path:
            continue
        keys = [part.lower() for part in path.split("__") if part]
        if not keys:
            continue
        cursor = overlay
        for key in keys[:-1]:
            existing = cursor.get(key)
            if not isinstance(existing, dict):
                existing = {}
                cursor[key] = existing
            cursor = existing
        cursor[keys[-1]] = _coerce_v2(keys, raw)
    return overlay


def build_v2(values: Mapping[str, Any]) -> ConfigV2:
    payload = dict(values)
    payload.setdefault("config_version", 2)
    try:
        return msgspec.convert(payload, ConfigV2, strict=True)
    except msgspec.ValidationError as exc:
        raise ConfigError(f"Invalid v2 configuration: {exc}") from exc
    except ValueError as exc:
        # Section ``__post_init__`` hooks raise plain ValueError for numeric or
        # enumerated constraints msgspec's type check cannot express.
        raise ConfigError(f"Invalid v2 configuration: {exc}") from exc


def _read_docs(
    workspace: Path, home: Path
) -> list[tuple[str, dict[str, Any]]]:
    docs: list[tuple[str, dict[str, Any]]] = []
    user = user_config_path(home)
    if user.exists():
        docs.append((str(user), read_toml(user)))
    workspace_file = workspace_config_path(workspace)
    if workspace_file.exists():
        docs.append((str(workspace_file), read_toml(workspace_file)))
    settings_file = workspace_settings_config_path(workspace)
    if settings_file.exists():
        docs.append((str(settings_file), read_toml(settings_file)))
    return docs


def load_effective(
    workspace: Path,
    home: Path,
    environ: Mapping[str, str],
    *,
    flags: Mapping[str, Any] | None = None,
    session: Mapping[str, Any] | None = None,
) -> Effective:
    workspace = Path(workspace)
    docs = _read_docs(workspace, Path(home))
    shapes = [
        (source, doc, detect_version(doc, source=source))
        for source, doc in docs
    ]
    # A v2 layer anywhere promotes the whole configuration to v2; v1 layers are
    # bridged rather than rejected, so a legacy workspace keeps working.
    version = 2 if any(shape == 2 for _, _, shape in shapes) else 1
    source = docs[-1][0] if docs else None

    if version == 2:
        merged = builtin_defaults()
        for doc_source, doc, doc_version in shapes:
            if doc_version == 1:
                validate_v1(doc, source=doc_source)
                doc = normalize_v1_to_v2(doc)
            merged = deep_merge(merged, doc)
        merged = deep_merge(merged, normalize_v1_to_v2(env_overlay_v1(environ)))
        merged = deep_merge(merged, env_overlay_v2(environ))
        if flags:
            merged = deep_merge(merged, flags)
        if session:
            merged = deep_merge(merged, session)
        return Effective(2, merged, source)

    merged = {}
    for _, doc, _ in shapes:
        doc = dict(doc)
        doc.pop("config_version", None)
        merged = deep_merge(merged, doc)
    merged = deep_merge(merged, env_overlay_v1(environ))
    if flags:
        merged = deep_merge(merged, flags)
    if session:
        merged = deep_merge(merged, session)
    unknown = set(merged) - LEGACY_KEYS
    if unknown:
        raise ConfigError(
            f"Unknown nexus.toml settings: {', '.join(sorted(unknown))}"
        )
    return Effective(1, merged, source)


__all__ = [
    "LEGACY_KEYS",
    "V2_SECTION_KEYS",
    "Effective",
    "build_v2",
    "builtin_defaults",
    "deep_merge",
    "detect_version",
    "env_overlay_v1",
    "env_overlay_v2",
    "load_effective",
    "normalize_v1_to_v2",
    "read_toml",
    "validate_v1",
]
