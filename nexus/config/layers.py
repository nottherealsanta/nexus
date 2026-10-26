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

import logging
import math
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import msgspec

from ..errors import ConfigError
from .paths import (
    legacy_workspace_settings_config_path,
    user_config_path,
    workspace_config_path,
    workspace_settings_config_path,
)
from .schema import ConfigV2, SubagentUsageSection

LEGACY_KEYS = frozenset(
    {
        "executable",
        "model",
        "sandbox",
        "timeout_seconds",
        "context_chars",
        "instructions_file",
        "memory_file",
        "agents_file",
    }
)
V2_SECTION_KEYS = frozenset(
    {
        "daemon",
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
        "voice",
        "live",
        "manager",
        "updates",
        "code_mode",
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
    "NEXUS_AGENTS_FILE": "agents_file",
}

# Env coercion is schema-aware and conservative: only fields the schema declares
# as int/float/bool are converted. Provider credentials, model names, paths, and
# anything else stay strings even when they look numeric.
_LEGACY_ENV_TYPES = {"timeout_seconds": float, "context_chars": int}
_V2_NUMERIC = {
    "code_mode.max_workers": int,
    "code_mode.max_tool_seconds": int,
    "code_mode.max_nested_calls": int,
    "code_mode.manual_budget_tokens": int,
    "code_mode.max_output_chars": int,
    "code_mode.max_live_kernels": int,
    "code_mode.idle_evict_s": int,
    "code_mode.max_snapshot_bytes": int,
    "code_mode.max_cells": int,
    "code_mode.max_sub_calls": int,
    "code_mode.max_sub_input_tokens": int,
    "code_mode.max_total_sub_tokens": int,
    "code_mode.max_depth": int,
    "agent.tool_call_nudge": int,
    "agent.max_iterations": int,
    "agent.max_turn_seconds": float,
    "model.params.temperature": float,
    "model.params.max_output_tokens": int,
    "model.params.thinking_budget": int,
    "models.refresh_ttl_days": float,
    "models.usage_refresh_seconds": int,
    "context.max_tokens": int,
    "context.safety_margin_tokens": int,
    "context.compact_at_fraction": float,
    "context.limits.memory": int,
    "context.limits.agents_md": int,
    "context.limits.skills_index": int,
    "context.limits.environment": int,
    "context.limits.attachments": int,
    "tools.bash_timeout_s": float,
    "tools.bash_yield_s": float,
    "tools.bash_max_s": float,
    "tools.shell_yield_s": float,
    "tools.shell_max_s": float,
    "tools.shell_log_max_bytes": int,
    "tools.shell_log_total_bytes": int,
    "tools.shell_log_retention_days": int,
    "tools.shell_max_auto_wakes": int,
    "tools.shell_max_persistent_per_session": int,
    "tools.shell_max_persistent": int,
    "tools.shell_orphan_s": float,
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
    "agents.max_live": int,
    "agents.max_pending_reports": int,
    "agents.max_report_batch": int,
    "agents.max_report_chars": int,
    "agents.report_coalesce_ms": int,
    "agents.max_auto_wakes": int,
    "agents.max_followup_chars": int,
    "agents.oneshot_wait_seconds": int,
    "mcp.connect_timeout_s": float,
    "mcp.restart_max": int,
    "session.snapshot_every": int,
    "voice.max_seconds": int,
    "voice.unload_after_minutes": int,
    "live.max_minutes": int,
    "live.carry_turns": int,
    "live.pause_seconds": int,
    "manager.max_launches_per_turn": int,
    "manager.max_live_managed": int,
    "manager.idle_compaction_minutes": int,
}
_V2_BOOL = frozenset(
    {
        "ext.enabled",
        "ext.quarantine",
        "agents.enabled",
        "agents.seed_roles",
        "agents.background",
        "hooks.enabled",
        "mcp.enabled",
        "models.offline",
        "tools.web.fetch_enabled",
        "voice.enabled",
        "voice.autoload",
        "voice.auto_send",
        "voice.timestamps",
        "live.enabled",
        "live.progress",
        "live.log_raw",
        "manager.enabled",
        "manager.auto_compaction",
        "manager.state_snapshot",
        "code_mode.enabled",
        "code_mode.context",
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
    if "agents_file" in doc:
        agent["agents_file"] = doc["agents_file"]
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
    if environ.get("NEXUS_VOICE", "").strip().lower() == "off":
        overlay.setdefault("voice", {})["enabled"] = False
    if environ.get("NEXUS_LIVE", "").strip().lower() == "off":
        overlay.setdefault("live", {})["enabled"] = False
    return overlay


_LOG = logging.getLogger("nexus.config")
_WARNED: set[str] = set()


def _drop_removed_keys(merged: dict[str, Any]) -> None:
    """Remove settings that no longer exist so old files keep loading.

    ``[code_mode] inline_budget_tokens`` went away with the inline catalog (kernel
    plan §3.12): one log line per process, the value is ignored.
    """
    section = merged.get("code_mode")
    if isinstance(section, dict) and "inline_budget_tokens" in section:
        merged["code_mode"] = {k: v for k, v in section.items() if k != "inline_budget_tokens"}
        if "inline_budget_tokens" not in _WARNED:
            _WARNED.add("inline_budget_tokens")
            _LOG.warning(
                "code_mode.inline_budget_tokens is deprecated and ignored; "
                "use code_mode.manual_budget_tokens")


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


#: Provider keys that grant an external CLI unmediated authority. A checked-in
#: project file must never turn them on (a cloned repo could otherwise approve
#: every agy tool action), so project layers that mention them are rejected.
USER_ONLY_PROVIDER_KEYS = ("worker_dangerously_skip_permissions",)


def _reject_project_user_only_keys(doc: Mapping[str, Any], source: str) -> None:
    if "daemon" in doc:
        raise ConfigError(f"{source}: [daemon] may only be set in the user-level config")
    worktrees = doc.get("worktrees")
    if isinstance(worktrees, Mapping):
        for key in ("max_worktrees", "auto_remove_clean", "remove_after_landing"):
            if key in worktrees:
                raise ConfigError(
                    f"{source}: worktrees.{key} may only be set in the user-level "
                    f"config (~/.nexus/config.toml), never in a project file")
    providers = doc.get("providers")
    if not isinstance(providers, Mapping):
        return
    for name, section in providers.items():
        if isinstance(section, Mapping):
            for key in USER_ONLY_PROVIDER_KEYS:
                if key in section:
                    raise ConfigError(
                        f"{source}: providers.{name}.{key} may only be set in the user-level config "
                        f"(~/.nexus/config.toml), never in a project file")


#: ``[models.subagent_usage]`` keys a project file may not set at all.
USER_ONLY_SUBAGENT_USAGE_KEYS = ("enabled", "refresh_seconds")
_SUBAGENT_USAGE_THRESHOLDS = (
    "five_hour_min_remaining",
    "weekly_min_remaining",
    "monthly_min_remaining",
)


def _reject_project_subagent_usage_keys(doc: Mapping[str, Any], source: str) -> None:
    models = doc.get("models")
    if isinstance(models, Mapping) and "usage_refresh_seconds" in models:
        raise ConfigError(
            f"{source}: models.usage_refresh_seconds may only be set in the user-level "
            f"config (~/.nexus/config.toml), never in a project file")
    section = models.get("subagent_usage") if isinstance(models, Mapping) else None
    if not isinstance(section, Mapping):
        return
    for key in USER_ONLY_SUBAGENT_USAGE_KEYS:
        if key in section:
            raise ConfigError(
                f"{source}: models.subagent_usage.{key} may only be set in the user-level "
                f"config (~/.nexus/config.toml), never in a project file")


def _is_valid_threshold(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 100
    )


def _merge_project_doc(merged: Mapping[str, Any], doc: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a project layer; ``models.subagent_usage`` thresholds may only rise."""
    result = deep_merge(merged, doc)
    models = doc.get("models")
    project = models.get("subagent_usage") if isinstance(models, Mapping) else None
    if not isinstance(project, Mapping):
        return result
    defaults = SubagentUsageSection()
    prior_models = merged.get("models")
    prior = prior_models.get("subagent_usage") if isinstance(prior_models, Mapping) else None
    prior = prior if isinstance(prior, Mapping) else {}
    section = dict(result["models"]["subagent_usage"])
    for key in _SUBAGENT_USAGE_THRESHOLDS:
        if key in project:
            val = project[key]
            current = prior.get(key, getattr(defaults, key))
            if _is_valid_threshold(val) and _is_valid_threshold(current):
                section[key] = max(current, val)
            else:
                section[key] = val
    project_providers = project.get("providers")
    prior_providers = prior.get("providers")
    if isinstance(project_providers, Mapping):
        providers = dict(section.get("providers") or {})
        for name, override in project_providers.items():
            if not isinstance(override, Mapping):
                continue
            existing = (
                prior_providers.get(name) if isinstance(prior_providers, Mapping) else None
            )
            existing = existing if isinstance(existing, Mapping) else {}
            merged_override = dict(providers.get(name) or {})
            for key in _SUBAGENT_USAGE_THRESHOLDS:
                if key in override:
                    val = override[key]
                    # Without a provider override the floor is the section value.
                    floor = existing.get(key)
                    if floor is None:
                        floor = section.get(key, getattr(defaults, key))
                    if _is_valid_threshold(val) and _is_valid_threshold(floor):
                        merged_override[key] = max(floor, val)
                    else:
                        merged_override[key] = val
            providers[name] = merged_override
        section["providers"] = providers
    result["models"] = {**result["models"], "subagent_usage": section}
    return result


def _read_docs(
    workspace: Path, home: Path
) -> list[tuple[str, dict[str, Any]]]:
    docs: list[tuple[str, dict[str, Any]]] = []
    user = user_config_path(home)
    if user.exists():
        docs.append((str(user), read_toml(user)))
    project: list[Path] = []
    workspace_file = workspace_config_path(workspace)
    if workspace_file.exists():
        project.append(workspace_file)
    # STATE_PLAN §5.4: the legacy ``.nexus/nexus.toml`` is a read-only
    # fallback, applied before (so it is outranked by) the current
    # ``.agents/nexus.toml``; Settings console writes only ever go there.
    legacy_settings_file = legacy_workspace_settings_config_path(workspace)
    if legacy_settings_file.exists():
        project.append(legacy_settings_file)
    settings_file = workspace_settings_config_path(workspace)
    if settings_file.exists():
        project.append(settings_file)
    for path in project:
        doc = read_toml(path)
        _reject_project_user_only_keys(doc, str(path))
        _reject_project_subagent_usage_keys(doc, str(path))
        docs.append((str(path), doc))
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
        user_source = str(user_config_path(Path(home)))
        for doc_source, doc, doc_version in shapes:
            if doc_version == 1:
                validate_v1(doc, source=doc_source)
                doc = normalize_v1_to_v2(doc)
            if doc_source == user_source:
                merged = deep_merge(merged, doc)
            else:
                merged = _merge_project_doc(merged, doc)
        merged = deep_merge(merged, normalize_v1_to_v2(env_overlay_v1(environ)))
        merged = deep_merge(merged, env_overlay_v2(environ))
        if flags:
            merged = deep_merge(merged, flags)
        if session:
            merged = deep_merge(merged, session)
        _drop_removed_keys(merged)
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
