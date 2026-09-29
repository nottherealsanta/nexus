"""Model registry: a bounded, filtered, cache-backed view of models.dev.

Plan section 15.3. The registry is data and lookup only: it describes what
models exist, which Nexus provider adapter serves them, and which of them are
usable in this installation. It performs no I/O during a turn.

Two security properties are structural, not advisory:

* **The catalogue cannot set endpoints or credentials.** Only ``id``, ``name``,
  ``env``, ``npm``, ``modalities``, ``cost``, and ``limit`` are read. A
  ``base_url`` or ``api_key`` present in catalogue JSON is ignored; endpoints and
  keys stay in ``[providers.*]`` and ``credentials.json``.
* **``env`` is treated as a name, never a value.** It is used only to decide
  whether a provider looks reachable. No value is read into the registry.

Acquisition is fetch-on-first-use with a TTL. On failure it falls back to a
valid stale cache, then to the vendored snapshot, then to an empty registry.
Every path is injectable, so the default test suite never touches the network.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import httpx
import msgspec

from ..errors import ConfigError, NexusError
from ..util import redact_url_userinfo
from .capabilities import Capabilities
from .request import REASONING_EFFORT_ORDER, REASONING_EFFORTS

__all__ = [
    "ADAPTER_ANTHROPIC",
    "ADAPTER_GEMINI",
    "ADAPTER_OLLAMA",
    "ADAPTER_OPENAI",
    "ADAPTER_OPENCODE",
    "DEFAULT_CATALOGUE_URL",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TTL_DAYS",
    "OPENAI_COMPATIBLE",
    "Catalogue",
    "CatalogueError",
    "CatalogueFetcher",
    "CatalogueModel",
    "CatalogueProvider",
    "Cost",
    "HttpxCatalogueFetcher",
    "ModelInfo",
    "ModelRegistry",
    "ModelRegistryError",
    "ProviderStatus",
    "RegistryStatus",
    "build_index",
    "map_provider",
    "normalize_adapter_kind",
    "parse_catalogue",
]

#: The published catalogue. The URL comes from config or this constant, never
#: from catalogue content.
DEFAULT_CATALOGUE_URL = "https://models.dev/api.json"

#: Cache lifetime (plan section 15.3).
DEFAULT_TTL_DAYS = 7.0

#: A hard network deadline and a hard size bound for one catalogue fetch.
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: Nexus provider adapter names (plan section 8, one adapter per wire protocol).
ADAPTER_ANTHROPIC = "anthropic"
ADAPTER_OPENAI = "openai"
ADAPTER_GEMINI = "gemini"
ADAPTER_OLLAMA = "ollama"
#: The OpenCode ACP subprocess agent. It is an agent surface, not a model wire
#: protocol, so it declares no model capabilities; the registry maps it only so
#: a catalogue listing is not reported as adapter-less.
ADAPTER_OPENCODE = "opencode"
OPENAI_COMPATIBLE = "openai_compatible"

_KNOWN_ADAPTERS = frozenset(
    {ADAPTER_ANTHROPIC, ADAPTER_OPENAI, ADAPTER_GEMINI, ADAPTER_OLLAMA, ADAPTER_OPENCODE}
)

#: ``kind`` spellings that alias a canonical adapter. ``google`` is the
#: catalogue provider id for Gemini, so a ``kind = "google"`` block must select
#: the Gemini adapter consistently rather than being reported as unknown.
_KIND_ALIASES: dict[str, str] = {
    "google": ADAPTER_GEMINI,
    "opencode_agent": ADAPTER_OPENCODE,
    "opencode-agent": ADAPTER_OPENCODE,
}

#: ``kind`` spellings that mean "any OpenAI-compatible endpoint".
_OPENAI_COMPATIBLE_KINDS = frozenset(
    {OPENAI_COMPATIBLE, "openai-compatible", "compatible"}
)


def normalize_adapter_kind(kind: object) -> str | None:
    """Map a configured ``kind`` onto a known Nexus adapter, or ``None``."""
    if not isinstance(kind, str):
        return None
    normalized = kind.strip().lower()
    if normalized in _KIND_ALIASES:
        return _KIND_ALIASES[normalized]
    if normalized in _KNOWN_ADAPTERS:
        return normalized
    return None

#: ``npm`` is the strongest signal models.dev carries for the wire protocol.
#: Google Vertex is deliberately absent: it is a different endpoint and auth
#: flow that no Nexus adapter speaks, so claiming it would route requests to an
#: adapter that cannot serve them. Unmapped Vertex providers fall back to the
#: OpenAI-compatible adapter only when a ``base_url`` is configured.
_NPM_ADAPTERS: dict[str, str] = {
    "@ai-sdk/anthropic": ADAPTER_ANTHROPIC,
    "@ai-sdk/openai": ADAPTER_OPENAI,
    "@ai-sdk/openai-compatible": ADAPTER_OPENAI,
    "@ai-sdk/google": ADAPTER_GEMINI,
    "@ai-sdk/ollama": ADAPTER_OLLAMA,
}

#: Direct provider ids, for entries whose ``npm`` is missing or unfamiliar.
#: Codex models are OpenAI Responses-API models (plan assumption #3): they are a
#: routing entry on the OpenAI adapter, not a distinct provider.
_DIRECT_ADAPTERS: dict[str, str] = {
    "anthropic": ADAPTER_ANTHROPIC,
    "openai": ADAPTER_OPENAI,
    "codex": ADAPTER_OPENAI,
    "google": ADAPTER_GEMINI,
    "gemini": ADAPTER_GEMINI,
    "ollama": ADAPTER_OLLAMA,
    "opencode": ADAPTER_OPENCODE,
}

#: Bounds that keep a hostile or corrupt catalogue from exhausting memory. The
#: real catalogue is ~4.8 MB / 223 providers / ~8k models.
_MAX_PROVIDERS = 2_000
_MAX_MODELS_PER_PROVIDER = 20_000
_MAX_TOTAL_MODELS = 100_000
_MAX_ENV_NAMES = 64
_MAX_MODALITIES = 32
_MAX_ID_LEN = 256
_MAX_NAME_LEN = 512
_MAX_FAMILY_LEN = 256
_MAX_NPM_LEN = 256
_MAX_TIER_LEN = 64
_REASONING_EFFORT_ORDER = REASONING_EFFORT_ORDER
_MAX_REASONING_EFFORTS = len(_REASONING_EFFORT_ORDER)
_MAX_REASONING_EFFORT_OVERRIDES = 20_000
_MAX_REASONING_OPTIONS = 64
_MAX_TYPED_REASONING_EFFORT_VALUES = 64


class ModelRegistryError(NexusError, ValueError):
    """The registry could not be built or queried as requested."""


class CatalogueError(ModelRegistryError):
    """Catalogue data is malformed, oversized, or unreachable."""


class Cost(msgspec.Struct, frozen=True):
    """Per-million-token pricing, as models.dev reports it (USD / Mtok)."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


class ModelInfo(msgspec.Struct, frozen=True):
    """One canonical model, as understood by Nexus (plan section 15.3)."""

    provider: str
    id: str
    catalogue_provider: str = ""
    name: str = ""
    family: str | None = None
    aliases: tuple[str, ...] = ()
    context: int = 0
    max_output: int = 0
    #: models.dev ``limit.input``: the most prompt tokens the API accepts, when
    #: lower than ``context - output`` (0 = not stated).
    max_input: int = 0
    tool_call: bool = False
    reasoning: bool = False
    structured_output: bool = False
    temperature: bool = False
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    cost: Cost | None = None
    tier: str = "low"
    source: Literal["catalogue", "config", "builtin"] = "catalogue"
    reasoning_efforts: tuple[str, ...] = ()
    last_updated: str | None = None
    release_date: str | None = None

    @property
    def ref(self) -> str:
        """Fully-qualified ``provider/id`` reference."""
        return f"{self.provider}/{self.id}"

    def capabilities(self) -> Capabilities:
        """Populate the loop's capability descriptor from registry data.

        The registry is authoritative (plan section 15.5); there is no adapter
        override table. Capabilities the catalogue does not describe (parallel
        tool calls, prompt caching) stay conservative.
        """
        return Capabilities(
            tools=self.tool_call,
            thinking=self.reasoning,
            json_schema_strict=self.structured_output,
            vision="image" in self.input_modalities,
            documents="pdf" in self.input_modalities,
            max_context_tokens=self.context,
            max_output_tokens=self.max_output,
            max_input_tokens=self.max_input,
        )


class ProviderStatus(msgspec.Struct, frozen=True):
    """Whether a catalogue provider can be selected, and why not if not."""

    id: str
    name: str = ""
    adapter: str | None = None
    kind: str | None = None
    configured: bool = False
    reachable: bool = False
    selectable: bool = False
    reason: str | None = None


class RegistryStatus(msgspec.Struct, frozen=True):
    """A snapshot of what the registry currently holds and where it came from."""

    source: str = "empty"
    stale: bool = False
    model_count: int = 0
    provider_count: int = 0
    selectable_provider_count: int = 0
    alias_count: int = 0
    unselectable: tuple[str, ...] = ()
    cache_path: str | None = None
    fetched_at: float | None = None
    license_pending: bool = False
    error: str | None = None


class CatalogueModel(msgspec.Struct, frozen=True):
    """A validated catalogue model entry."""

    id: str
    name: str = ""
    family: str | None = None
    context: int = 0
    max_output: int = 0
    #: models.dev ``limit.input``: the most prompt tokens the API accepts, when
    #: lower than ``context - output`` (0 = not stated).
    max_input: int = 0
    tool_call: bool = False
    reasoning: bool = False
    structured_output: bool = False
    temperature: bool = False
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    cost: Cost | None = None
    reasoning_efforts: tuple[str, ...] = ()
    last_updated: str | None = None
    release_date: str | None = None


class CatalogueProvider(msgspec.Struct, frozen=True):
    """A validated catalogue provider entry."""

    id: str
    name: str = ""
    env: tuple[str, ...] = ()
    npm: str | None = None
    models: tuple[CatalogueModel, ...] = ()


class Catalogue(msgspec.Struct, frozen=True):
    """A validated catalogue, plus whether its licence is still unverified."""

    providers: tuple[CatalogueProvider, ...] = ()
    license_pending: bool = False


def map_provider(provider_id: str, npm: str | None = None) -> str | None:
    """Map a models.dev provider id to a Nexus adapter name, or ``None``.

    ``npm`` is the strongest signal; a small explicit table covers direct
    providers whose ``npm`` is missing or unfamiliar. An unmapped provider is
    still listable -- the caller decides whether a configured ``base_url`` lets
    it fall back to the OpenAI-compatible adapter.
    """
    if npm and npm in _NPM_ADAPTERS:
        return _NPM_ADAPTERS[npm]
    return _DIRECT_ADAPTERS.get(provider_id)


def _require_mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CatalogueError(f"{what} must be a JSON object")
    return value


def _opt_str(
    entry: Mapping[str, object],
    key: str,
    *,
    max_len: int,
) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogueError(f"{key!r} must be a string")
    if len(value) > max_len:
        raise CatalogueError(f"{key!r} exceeds {max_len} characters")
    return value


def _opt_date(entry: Mapping[str, object], key: str) -> str | None:
    """Read one optional ISO date; ``YYYY-MM`` (used by models.dev) reads as the 1st.

    Anything else is treated as unknown: optional metadata never rejects the catalogue.
    """
    value = entry.get(key)
    if not isinstance(value, str) or len(value) not in (7, 10):
        return None
    try:
        return date.fromisoformat(value if len(value) == 10 else f"{value}-01").isoformat()
    except ValueError:
        return None


def _opt_bool(entry: Mapping[str, object], key: str) -> bool:
    value = entry.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise CatalogueError(f"{key!r} must be a boolean")
    return value


def _opt_nonneg_int(entry: Mapping[str, object], key: str) -> int:
    value = entry.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogueError(f"{key!r} must be a non-negative integer")
    return value


def _opt_number(entry: Mapping[str, object], key: str) -> float:
    value = entry.get(key)
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogueError(f"{key!r} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise CatalogueError(f"{key!r} must be a finite non-negative number")
    return number


def _parse_cost(value: object) -> Cost | None:
    if value is None:
        return None
    entry = _require_mapping(value, "cost")
    return Cost(
        input=_opt_number(entry, "input"),
        output=_opt_number(entry, "output"),
        cache_read=_opt_number(entry, "cache_read"),
        cache_write=_opt_number(entry, "cache_write"),
    )


def _parse_modalities(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return (), ()
    entry = _require_mapping(value, "modalities")
    parsed: list[tuple[str, ...]] = []
    for side in ("input", "output"):
        raw = entry.get(side, [])
        if raw is None:
            raw = []
        if not isinstance(raw, list) or len(raw) > _MAX_MODALITIES:
            raise CatalogueError(f"modalities.{side} must be a bounded list")
        values: list[str] = []
        for item in raw:
            if not isinstance(item, str) or len(item) > _MAX_ID_LEN:
                raise CatalogueError(f"modalities.{side} entries must be strings")
            values.append(item)
        parsed.append(tuple(values))
    return parsed[0], parsed[1]


def _parse_effort_values(raw: object, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > _MAX_REASONING_EFFORTS:
        raise CatalogueError(f"{field} must be a bounded list")
    values: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or len(item) > _MAX_ID_LEN:
            raise CatalogueError(
                f"{field} reasoning_effort entries must be bounded strings"
            )
        if item not in REASONING_EFFORTS:
            raise CatalogueError(f"unknown reasoning_effort value {item!r}")
        if item in values:
            raise CatalogueError(f"duplicate reasoning_effort value {item!r}")
        values.add(item)
    return tuple(effort for effort in _REASONING_EFFORT_ORDER if effort in values)


def _parse_reasoning_efforts(entry: Mapping[str, object]) -> tuple[str, ...]:
    # Preserve models.dev's legacy/custom flat field. Its presence explicitly
    # wins over the typed representation, but its values remain strict.
    if "reasoning_efforts" in entry:
        return _parse_effort_values(entry["reasoning_efforts"], "'reasoning_efforts'")

    options = entry.get("reasoning_options")
    if not isinstance(options, list) or len(options) > _MAX_REASONING_OPTIONS:
        return ()

    values: set[str] = set()
    for option in options:
        # Typed catalogue metadata is best-effort: malformed and future
        # options are ignored rather than invalidating unrelated model data.
        if not isinstance(option, Mapping) or option.get("type") != "effort":
            continue
        raw_efforts = option.get("values")
        if (
            not isinstance(raw_efforts, list)
            or len(raw_efforts) > _MAX_TYPED_REASONING_EFFORT_VALUES
        ):
            continue
        for effort in raw_efforts:
            if (
                isinstance(effort, str)
                and len(effort) <= _MAX_ID_LEN
                and effort in REASONING_EFFORTS
            ):
                # Multiple provider records commonly repeat the same typed
                # levels; retaining their unique known union is safe.
                values.add(effort)
    return tuple(effort for effort in _REASONING_EFFORT_ORDER if effort in values)


def _validate_reasoning_effort_overrides(
    overrides: Mapping[str, object] | None,
) -> dict[str, tuple[str, ...]]:
    """Validate and canonically order explicit ``provider/model`` overrides."""
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise ConfigError("models.reasoning_efforts must be a mapping")
    if len(overrides) > _MAX_REASONING_EFFORT_OVERRIDES:
        raise ConfigError("models.reasoning_efforts has too many model references")
    parsed: dict[str, tuple[str, ...]] = {}
    for reference, raw_efforts in overrides.items():
        if (
            not isinstance(reference, str)
            or len(reference) > 2 * _MAX_ID_LEN + 1
            or reference.strip() != reference
            or any(ord(char) < 32 or char.isspace() for char in reference)
        ):
            raise ConfigError("models.reasoning_efforts keys must be bounded model references")
        provider, separator, model_id = reference.partition("/")
        if (
            not separator
            or not provider
            or not model_id
            or any(not part for part in reference.split("/"))
            or len(provider) > _MAX_ID_LEN
            or any(len(part) > _MAX_ID_LEN for part in model_id.split("/"))
        ):
            raise ConfigError(
                f"models.reasoning_efforts key {reference!r} must be provider/model"
            )
        if not isinstance(raw_efforts, (list, tuple)) or len(raw_efforts) > _MAX_REASONING_EFFORTS:
            raise ConfigError(
                f"models.reasoning_efforts.{reference} must be a bounded list"
            )
        values: set[str] = set()
        for effort in raw_efforts:
            if not isinstance(effort, str) or effort not in REASONING_EFFORTS:
                raise ConfigError(
                    f"models.reasoning_efforts.{reference} has unknown effort {effort!r}"
                )
            if effort in values:
                raise ConfigError(
                    f"models.reasoning_efforts.{reference} has duplicate effort {effort!r}"
                )
            values.add(effort)
        parsed[reference] = tuple(
            effort for effort in _REASONING_EFFORT_ORDER if effort in values
        )
    return parsed


def _parse_model(key: str, value: object) -> CatalogueModel:
    entry = _require_mapping(value, f"model {key!r}")
    model_id = _opt_str(entry, "id", max_len=_MAX_ID_LEN) or key
    if len(model_id) > _MAX_ID_LEN:
        raise CatalogueError("model id exceeds the length bound")
    name = _opt_str(entry, "name", max_len=_MAX_NAME_LEN) or model_id
    family = _opt_str(entry, "family", max_len=_MAX_FAMILY_LEN)
    input_modalities, output_modalities = _parse_modalities(entry.get("modalities"))
    limit = entry.get("limit")
    context = 0
    max_output = 0
    max_input = 0
    if limit is not None:
        limit_entry = _require_mapping(limit, "limit")
        context = _opt_nonneg_int(limit_entry, "context")
        max_output = _opt_nonneg_int(limit_entry, "output")
        max_input = _opt_nonneg_int(limit_entry, "input")
    return CatalogueModel(
        id=model_id,
        name=name,
        family=family,
        context=context,
        max_output=max_output,
        max_input=max_input,
        tool_call=_opt_bool(entry, "tool_call"),
        reasoning=_opt_bool(entry, "reasoning"),
        structured_output=_opt_bool(entry, "structured_output"),
        temperature=_opt_bool(entry, "temperature"),
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        cost=_parse_cost(entry.get("cost")),
        reasoning_efforts=_parse_reasoning_efforts(entry),
        last_updated=_opt_date(entry, "last_updated"),
        release_date=_opt_date(entry, "release_date"),
    )


def _parse_provider(provider_id: str, value: object) -> CatalogueProvider:
    entry = _require_mapping(value, f"provider {provider_id!r}")
    name = _opt_str(entry, "name", max_len=_MAX_NAME_LEN) or provider_id
    npm = _opt_str(entry, "npm", max_len=_MAX_NPM_LEN)
    raw_env = entry.get("env")
    env: tuple[str, ...] = ()
    if raw_env is not None:
        if not isinstance(raw_env, list) or len(raw_env) > _MAX_ENV_NAMES:
            raise CatalogueError("provider.env must be a bounded list")
        names: list[str] = []
        for item in raw_env:
            if not isinstance(item, str) or not item or len(item) > _MAX_ID_LEN:
                raise CatalogueError("provider.env entries must be nonempty strings")
            names.append(item)
        env = tuple(names)
    raw_models = entry.get("models", {})
    if raw_models is None:
        raw_models = {}
    models_entry = _require_mapping(raw_models, "provider.models")
    if len(models_entry) > _MAX_MODELS_PER_PROVIDER:
        raise CatalogueError("provider has too many models")
    models = tuple(
        _parse_model(str(key), item) for key, item in models_entry.items()
    )
    return CatalogueProvider(
        id=provider_id, name=name, env=env, npm=npm, models=models
    )


def parse_catalogue(
    raw: bytes | bytearray | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Catalogue:
    """Validate a models.dev-shaped document and return a bounded catalogue.

    Unknown provider/model fields are ignored, so a malicious or evolving
    catalogue cannot smuggle a ``base_url`` or ``api_key`` into Nexus. A
    top-level key beginning with ``_`` is metadata; ``_license: "pending"``
    marks a vendored snapshot whose redistribution terms are unverified.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise CatalogueError("catalogue must be bytes or text")
    if len(raw) > max_bytes:
        raise CatalogueError(f"catalogue exceeds the {max_bytes} byte bound")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CatalogueError(f"catalogue is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogueError("catalogue must be a JSON object at the top level")
    license_pending = False
    providers: list[CatalogueProvider] = []
    total_models = 0
    for key, value in data.items():
        if not isinstance(key, str):
            raise CatalogueError("catalogue provider ids must be strings")
        if key.startswith("_"):
            if key == "_license" and isinstance(value, str):
                license_pending = value.strip().lower().startswith("pending")
            continue
        if not key or len(key) > _MAX_ID_LEN:
            raise CatalogueError("provider id exceeds the length bound")
        if len(providers) >= _MAX_PROVIDERS:
            raise CatalogueError("catalogue has too many providers")
        provider = _parse_provider(key, value)
        total_models += len(provider.models)
        if total_models > _MAX_TOTAL_MODELS:
            raise CatalogueError("catalogue has too many models")
        providers.append(provider)
    return Catalogue(providers=tuple(providers), license_pending=license_pending)


def _cfg_value(cfg: object, key: str) -> object:
    if cfg is None:
        return None
    if isinstance(cfg, Mapping):
        return cfg.get(key)
    return getattr(cfg, key, None)


def _env_present(env: Mapping[str, str], name: str) -> bool:
    if not name:
        return False
    return bool(env.get(name))


def _provider_status(
    provider: CatalogueProvider,
    cfg: object,
    *,
    configured: bool,
    reachable: bool,
) -> ProviderStatus:
    adapter: str | None = None
    kind: str | None = None
    cfg_kind = _cfg_value(cfg, "kind")
    base_url = _cfg_value(cfg, "base_url")
    normalized_kind = (
        cfg_kind.strip().lower() if isinstance(cfg_kind, str) else None
    )
    known = normalize_adapter_kind(cfg_kind)
    if known is not None:
        adapter = known
        kind = known
    elif normalized_kind in _OPENAI_COMPATIBLE_KINDS:
        if base_url:
            adapter = ADAPTER_OPENAI
            kind = OPENAI_COMPATIBLE
    else:
        adapter = map_provider(provider.id, provider.npm)
        kind = adapter
        if adapter is None and base_url:
            adapter = ADAPTER_OPENAI
            kind = OPENAI_COMPATIBLE
    reason: str | None = None
    if adapter is None:
        npm = f" (npm {provider.npm!r})" if provider.npm else ""
        reason = (
            f"no Nexus adapter for provider {provider.id!r}{npm}; "
            f"add [providers.{provider.id}] with base_url to use the "
            "OpenAI-compatible adapter"
        )
    return ProviderStatus(
        id=provider.id,
        name=provider.name,
        adapter=adapter,
        kind=kind,
        configured=configured,
        #: ``reachable`` is the honest env/credential signal: a provider that is
        #: only listed in ``[providers.*]`` without credentials present is
        #: *configured* but not reached. Kept separate from ``selectable`` so a
        #: UI can tell "we have an adapter" from "we have what it needs".
        reachable=reachable,
        selectable=adapter is not None,
        reason=reason,
    )


def _canonical_key(
    provider_id: str,
    model_id: str,
    known_providers: set[str],
) -> tuple[str, str]:
    """Group a re-listed model with its direct listing.

    ``openrouter``'s ``openai/o1-pro`` and ``openai``'s ``o1-pro`` share the key
    ``("openai", "o1-pro")``. Two unrelated providers that happen to use the same
    bare id do *not* share a key, because the implied provider differs.
    """
    if "/" in model_id:
        head, _, tail = model_id.partition("/")
        if tail and head in known_providers:
            return (head, tail)
    return (provider_id, model_id)


def _preference(
    info: ModelInfo,
    key: tuple[str, str],
    selectable: Mapping[str, bool],
) -> tuple[int, int, int, str]:
    direct = 1 if info.provider == key[0] else 0
    usable = 1 if selectable.get(info.provider, False) else 0
    return (direct, usable, -len(info.id), info.provider)


@dataclass(frozen=True, slots=True)
class _ModelIndex:
    models: tuple[ModelInfo, ...]
    providers: tuple[ProviderStatus, ...]
    by_ref: dict[str, ModelInfo]
    by_id: dict[str, tuple[ModelInfo, ...]]
    aliases: dict[str, ModelInfo]


def build_index(
    catalogue: Catalogue,
    *,
    providers_config: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
    tier_table: object | None = None,
    default_tier: str = "low",
    source: Literal["catalogue", "config", "builtin"] = "catalogue",
    provider_aliases: Mapping[str, str] | None = None,
    reasoning_effort_overrides: Mapping[str, object] | None = None,
) -> _ModelIndex:
    """Filter, canonicalize, and index a validated catalogue.

    Providers are retained only when configured or apparently reachable. Models
    are retained only when their output modalities include ``text``. Duplicate
    re-listings collapse onto the direct provider, with the others recorded as
    aliases.
    """
    provider_cfg, aliases, env_map = providers_config or {}, provider_aliases or {}, os.environ if env is None else env
    effort_overrides = _validate_reasoning_effort_overrides(reasoning_effort_overrides)
    known_providers = {p.id for p in catalogue.providers}
    statuses: list[ProviderStatus] = []; retained: set[str] = set()
    catalogue_by_id = {provider.id: provider for provider in catalogue.providers}
    runtime_catalogue: dict[str, str] = {}
    for provider in catalogue.providers:
        cfg = provider_cfg.get(provider.id)
        configured = cfg is not None
        reachable = any(_env_present(env_map, name) for name in provider.env)
        if configured or reachable:
            runtime_catalogue[provider.id] = provider.id
    for runtime_id, catalogue_id in aliases.items():
        if catalogue_by_id.get(catalogue_id) is not None and (cfg := provider_cfg.get(runtime_id)) is not None:
            runtime_catalogue[runtime_id] = catalogue_id
    for runtime_id, catalogue_id in runtime_catalogue.items():
        provider = catalogue_by_id[catalogue_id]
        cfg = provider_cfg.get(runtime_id)
        configured = cfg is not None
        reachable = any(_env_present(env_map, name) for name in provider.env)
        status = _provider_status(provider, cfg, configured=configured, reachable=reachable)
        if runtime_id != catalogue_id:
            status = msgspec.structs.replace(status, id=runtime_id)
        statuses.append(status); retained.add(runtime_id)
    raw_models: list[ModelInfo] = []
    for runtime_id, catalogue_id in runtime_catalogue.items():
        if runtime_id not in retained:
            continue
        provider = catalogue_by_id[catalogue_id]
        for model in provider.models:
            if "text" not in model.output_modalities:
                continue
            info = ModelInfo(
                runtime_id, model.id, catalogue_id,
                name=model.name,
                family=model.family,
                context=model.context,
                max_output=model.max_output,
                max_input=model.max_input,
                tool_call=model.tool_call,
                reasoning=model.reasoning,
                structured_output=model.structured_output,
                temperature=model.temperature,
                input_modalities=model.input_modalities,
                output_modalities=model.output_modalities,
                cost=model.cost,
                tier=default_tier,
                source=source,
                reasoning_efforts=model.reasoning_efforts,
                last_updated=model.last_updated,
                release_date=model.release_date,
            )
            if tier_table is not None:
                tier = str(tier_table.assign(info))
                if len(tier) <= _MAX_TIER_LEN:
                    info = msgspec.structs.replace(info, tier=tier)
            raw_models.append(info)
    groups: dict[tuple[str, str], list[ModelInfo]] = {}
    for info in raw_models:
        key = _canonical_key(info.provider, info.id, known_providers)
        groups.setdefault(key, []).append(info)
    selectable = {status.id: status.selectable for status in statuses}
    models: list[ModelInfo] = []; aliases: dict[str, ModelInfo] = {}
    resolved_overrides: set[str] = set()
    for key, items in groups.items():
        winner = max(items, key=lambda item: _preference(item, key, selectable))
        loser_refs = tuple(
            sorted({item.ref for item in items if item.ref != winner.ref})
        )
        # Runtime provider aliases (for example codex -> openai) project the
        # catalogue's reference onto another provider id. Accept either explicit
        # reference, but always attach the override to the runtime model record.
        catalogue_refs = tuple(
            f"{item.catalogue_provider}/{item.id}"
            for item in items
            if item.catalogue_provider
        )
        override_refs = tuple(dict.fromkeys((winner.ref, *loser_refs, *catalogue_refs)))
        resolved_overrides.update(ref for ref in override_refs if ref in effort_overrides)
        override = next(
            (effort_overrides[ref] for ref in override_refs if ref in effort_overrides),
            None,
        )
        if override is not None:
            winner = msgspec.structs.replace(winner, reasoning_efforts=override)
        if loser_refs:
            winner = msgspec.structs.replace(winner, aliases=loser_refs)
        models.append(winner)
        for ref in loser_refs:
            aliases[ref] = winner
    unresolved_overrides = sorted(set(effort_overrides) - resolved_overrides)
    if unresolved_overrides:
        references = ", ".join(repr(ref) for ref in unresolved_overrides[:5])
        if len(unresolved_overrides) > 5:
            references += f", and {len(unresolved_overrides) - 5} more"
        raise ConfigError(
            "models.reasoning_efforts references do not resolve to retained "
            f"catalogue models: {references}"
        )
    models.sort(key=lambda info: (info.provider, info.id))
    by_ref = {info.ref: info for info in models}
    by_id: dict[str, list[ModelInfo]] = {}
    for info in models:
        by_id.setdefault(info.id, []).append(info)
    return _ModelIndex(tuple(models), tuple(statuses), by_ref, {key: tuple(value) for key, value in by_id.items()}, aliases)

@runtime_checkable
class CatalogueFetcher(Protocol):
    """The one thing acquisition needs from the network, injectable for tests."""

    async def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_bytes: int,
        verify_tls: bool,
    ) -> bytes: ...


class HttpxCatalogueFetcher:
    """Default fetcher: one bounded, TLS-verified GET.

    TLS verification is on unless the caller explicitly disables it, redirects
    are never followed, and the body is rejected as soon as it exceeds
    ``max_bytes``. An injected client keeps its own transport and TLS settings.
    """

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient | None:
        return self._client

    async def fetch(
        self,
        url: str,
        *,
        timeout_s: float,
        max_bytes: int,
        verify_tls: bool,
    ) -> bytes:
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL as exc:
            raise CatalogueError(
                f"invalid catalogue URL: {redact_url_userinfo(str(exc))}"
            ) from exc
        if parsed.scheme not in ("https", "http"):
            raise CatalogueError(
                f"unsupported catalogue URL scheme: {parsed.scheme!r}"
            )
        timeout = httpx.Timeout(timeout_s)
        client = self._client
        owned = client is None
        if client is None:
            client = httpx.AsyncClient(
                timeout=timeout,
                verify=verify_tls,
                follow_redirects=False,
            )
        try:
            async with client.stream("GET", url, timeout=timeout) as response:
                if response.status_code != 200:
                    raise CatalogueError(
                        f"catalogue fetch failed: HTTP {response.status_code}"
                    )
                declared = response.headers.get("content-length")
                if declared is not None:
                    # Parse first, then test: CatalogueError is itself a
                    # ValueError, so a ``raise`` inside a ``try/except
                    # ValueError`` would be swallowed by the guard meant to
                    # tolerate a malformed header.
                    try:
                        declared_length = int(declared)
                    except ValueError:
                        declared_length = None
                    if declared_length is not None and declared_length > max_bytes:
                        raise CatalogueError(
                            f"catalogue exceeds the {max_bytes} byte bound"
                        )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise CatalogueError(
                            f"catalogue exceeds the {max_bytes} byte bound"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise CatalogueError(
                f"catalogue fetch failed: {type(exc).__name__}"
            ) from exc
        finally:
            if owned:
                await client.aclose()


def _default_snapshot_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "models.min.json"


class ModelRegistry:
    """Acquire, index, and query the model catalogue (plan section 15.3)."""

    def __init__(
        self,
        *,
        providers: Mapping[str, object] | None = None,
        env: Mapping[str, str] | None = None,
        cache_path: str | os.PathLike[str] | None = None,
        snapshot_path: str | os.PathLike[str] | None = None,
        use_snapshot: bool = True,
        fetcher: CatalogueFetcher | None = None,
        catalogue_url: str = DEFAULT_CATALOGUE_URL,
        ttl_days: float = DEFAULT_TTL_DAYS,
        offline: bool = False,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        verify_tls: bool = True,
        now: Callable[[], float] = time.time,
        tier_table: object | None = None,
        default_tier: str = "low",
        provider_aliases: Mapping[str, str] | None = None,
        reasoning_effort_overrides: Mapping[str, object] | None = None,
    ) -> None:
        if (
            isinstance(ttl_days, bool)
            or not isinstance(ttl_days, (int, float))
            or not math.isfinite(ttl_days)
            or ttl_days < 0
        ):
            raise ConfigError("models.refresh_ttl_days must be finite and >= 0")
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise ConfigError("catalogue timeout must be a positive finite number")
        if max_bytes <= 0:
            raise ConfigError("catalogue max_bytes must be positive")
        try:
            scheme = httpx.URL(catalogue_url).scheme
        except (httpx.InvalidURL, TypeError) as exc:
            raise ConfigError(
                f"invalid models.catalogue_url: {redact_url_userinfo(str(exc))}"
            ) from exc
        if scheme not in ("https", "http"):
            raise ConfigError(f"unsupported catalogue URL scheme: {scheme!r}")

        self._providers = dict(providers or {})
        self._env = env
        self._cache_path = Path(cache_path) if cache_path is not None else None
        if snapshot_path is None:
            self._snapshot_path = _default_snapshot_path()
        else:
            self._snapshot_path = Path(snapshot_path)
        self._use_snapshot = use_snapshot
        self._fetcher = fetcher
        self._catalogue_url = catalogue_url
        self._ttl_seconds = float(ttl_days) * 86_400.0
        self._offline = offline
        self._timeout_s = float(timeout_s)
        self._max_bytes = int(max_bytes)
        self._verify_tls = verify_tls
        self._now = now
        self._tier_table = tier_table
        self._default_tier = default_tier
        self._reasoning_effort_overrides = _validate_reasoning_effort_overrides(
            reasoning_effort_overrides
        )
        self._provider_aliases, self._index = dict(provider_aliases or {}), _ModelIndex((), (), {}, {}, {})
        self._loaded = False
        #: Single-flight guard so N concurrent turn boundaries share one
        #: acquisition instead of racing duplicate network fetches and duplicate
        #: cache writes. Never held across a turn; only around acquisition.
        #: Created lazily so it binds to the loop that actually awaits it.
        self._load_lock: asyncio.Lock | None = None
        self._status = RegistryStatus(
            cache_path=str(self._cache_path) if self._cache_path else None
        )

    # -- introspection -----------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def cache_path(self) -> Path | None:
        return self._cache_path

    @property
    def snapshot_path(self) -> Path | None:
        return self._snapshot_path if self._use_snapshot else None

    @property
    def catalogue_url(self) -> str:
        return self._catalogue_url

    @property
    def offline(self) -> bool:
        return self._offline

    @property
    def verify_tls(self) -> bool:
        return self._verify_tls

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def status(self) -> RegistryStatus:
        return self._status

    def providers(self) -> list[ProviderStatus]:
        return list(self._index.providers)

    def provider_status(self, provider_id: str) -> ProviderStatus | None:
        for status in self._index.providers:
            if status.id == provider_id:
                return status
        return None

    def list(
        self,
        *,
        provider: str | None = None,
        tier: str | None = None,
        selectable_only: bool = False,
        search: str | None = None,
    ) -> list[ModelInfo]:
        """Return indexed models, optionally filtered. The result is a copy."""
        selectable = {s.id: s.selectable for s in self._index.providers}
        needle = search.lower() if search else None
        out: list[ModelInfo] = []
        for info in self._index.models:
            if provider is not None and info.provider != provider:
                continue
            if tier is not None and info.tier != tier:
                continue
            if selectable_only and not selectable.get(info.provider, False):
                continue
            if needle is not None and (
                needle not in info.id.lower() and needle not in info.name.lower()
            ):
                continue
            out.append(info)
        return out

    def get(self, ref: str) -> ModelInfo | None:
        """Resolve a reference to a model, or ``None``. Never raises."""
        return self._lookup(ref)

    def model_cost(self, provider: str, model: str) -> Cost | None:
        """The pricing for ``provider/model``, or ``None`` when unknown.

        Used by the subagent budget to price a child turn from its measured
        token usage. Never raises: an unknown model or a provider without cost
        data returns ``None`` (the caller treats unpriced usage conservatively).
        """
        if not isinstance(provider, str) or not provider:
            return None
        if not isinstance(model, str) or not model:
            return None
        info = self._lookup(f"{provider}/{model}")
        if info is None:
            info = self._lookup(model)
        return info.cost if info is not None else None

    def resolve(self, ref: str) -> ModelInfo:
        """Resolve a reference to a selectable model.

        Raises :class:`~nexus.errors.ConfigError` for an unknown reference or a
        model whose provider is listed but not selectable (with the reason).
        """
        if not isinstance(ref, str) or not ref.strip():
            raise ConfigError("model reference must be a nonempty string")
        info = self._lookup(ref)
        if info is None:
            provider_id = ref.strip().partition("/")[0] if "/" in ref else None
            status = self.provider_status(provider_id) if provider_id else None
            if status is not None and not status.selectable:
                raise ConfigError(
                    f"Provider {status.id!r} is listed but not selectable: "
                    f"{status.reason}"
                )
            raise ConfigError(f"Unknown model reference {ref!r}")
        status = self.provider_status(info.provider)
        if status is not None and not status.selectable:
            raise ConfigError(
                f"Provider {status.id!r} is listed but not selectable: "
                f"{status.reason}"
            )
        return info

    def _lookup(self, ref: str) -> ModelInfo | None:
        if not isinstance(ref, str):
            return None
        ref = ref.strip()
        if not ref:
            return None
        info = self._index.by_ref.get(ref)
        if info is not None:
            return info
        info = self._index.aliases.get(ref)
        if info is not None:
            return info
        matches = self._index.by_id.get(ref)
        if matches is not None and len(matches) == 1:
            return matches[0]
        return None

    # -- acquisition -------------------------------------------------------

    async def load(self, *, force: bool = False) -> RegistryStatus:
        """Populate the registry, fetching only when the cache is stale.

        Acquisition is single-flight: concurrent callers wait for the one
        in-progress load and then observe its result rather than starting a
        second fetch. The lock is released before this coroutine returns.
        """
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self._loaded and not force:
                return self._status
            return await self._acquire(force=force)

    async def _acquire(self, *, force: bool) -> RegistryStatus:
        cache = self._read_cache()
        if cache is not None and not force and self._is_fresh(cache[1]):
            return self._install(cache[0], source="cache", stale=False)

        error: str | None = None
        if not self._offline:
            try:
                raw = await self._fetch_raw()
                catalogue = parse_catalogue(raw, max_bytes=self._max_bytes)
                self._write_cache(raw)
                return self._install(catalogue, source="network", stale=False)
            except Exception as exc:  # noqa: BLE001 - any fetch/parse failure falls back
                # Redact at the source: ``status.error`` is surfaced by
                # ``nexus doctor`` and lifecycle events, and a transport error
                # can echo a URL with embedded credentials.
                error = redact_url_userinfo(f"{type(exc).__name__}: {exc}")

        if cache is not None:
            return self._install(cache[0], source="cache", stale=True, error=error)

        snapshot = self._read_snapshot()
        if snapshot is not None:
            return self._install(
                snapshot, source="snapshot", stale=True, error=error
            )

        return self._install(Catalogue(), source="empty", stale=True, error=error)

    async def refresh(self) -> RegistryStatus:
        """Force a fetch, falling back exactly as :meth:`load` does."""
        return await self.load(force=True)

    def install(self, catalogue: Catalogue, *, source: str = "catalogue") -> RegistryStatus:
        """Install an already-validated catalogue without any I/O.

        This is the seam tests and callers use to build a registry from a
        fixture. ``source`` is recorded on the status and on each model.
        """
        return self._install(catalogue, source=source, stale=False)

    def install_raw(
        self,
        raw: bytes | bytearray | str,
        *,
        source: str = "catalogue",
    ) -> RegistryStatus:
        """Validate and install raw catalogue bytes without any I/O."""
        catalogue = parse_catalogue(raw, max_bytes=self._max_bytes)
        return self._install(catalogue, source=source, stale=False)

    async def _fetch_raw(self) -> bytes:
        fetcher = self._fetcher
        if fetcher is None:
            fetcher = HttpxCatalogueFetcher()
            self._fetcher = fetcher
        return await fetcher.fetch(
            self._catalogue_url,
            timeout_s=self._timeout_s,
            max_bytes=self._max_bytes,
            verify_tls=self._verify_tls,
        )

    def _install(
        self,
        catalogue: Catalogue,
        *,
        source: str,
        stale: bool,
        error: str | None = None,
    ) -> RegistryStatus:
        model_source: Literal["catalogue", "builtin"]
        if source == "snapshot":
            model_source = "builtin"
        else:
            model_source = "catalogue"
        index = build_index(
            catalogue,
            providers_config=self._providers,
            env=self._env,
            tier_table=self._tier_table,
            default_tier=self._default_tier,
            source=model_source,
            provider_aliases=self._provider_aliases,
            reasoning_effort_overrides=self._reasoning_effort_overrides,
        )
        self._index = index
        self._loaded = True
        self._status = RegistryStatus(
            source=source,
            stale=stale,
            model_count=len(index.models),
            provider_count=len(index.providers),
            selectable_provider_count=sum(
                1 for status in index.providers if status.selectable
            ),
            alias_count=sum(len(info.aliases) for info in index.models),
            unselectable=tuple(
                status.id for status in index.providers if not status.selectable
            ),
            cache_path=str(self._cache_path) if self._cache_path else None,
            fetched_at=self._now(),
            license_pending=catalogue.license_pending,
            error=error,
        )
        return self._status

    # -- cache and snapshot ------------------------------------------------

    def _is_fresh(self, mtime: float) -> bool:
        return (self._now() - mtime) < self._ttl_seconds

    def _read_cache(self) -> tuple[Catalogue, float] | None:
        if self._cache_path is None:
            return None
        try:
            stat = self._cache_path.stat()
            raw = self._cache_path.read_bytes()
        except OSError:
            return None
        try:
            catalogue = parse_catalogue(raw, max_bytes=self._max_bytes)
        except CatalogueError:
            return None
        return catalogue, stat.st_mtime

    def _read_snapshot(self) -> Catalogue | None:
        if not self._use_snapshot or self._snapshot_path is None:
            return None
        try:
            raw = self._snapshot_path.read_bytes()
        except OSError:
            return None
        try:
            return parse_catalogue(raw, max_bytes=self._max_bytes)
        except CatalogueError:
            return None

    def _write_cache(self, raw: bytes) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(
                dir=self._cache_path.parent, prefix=".models-"
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self._cache_path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        except OSError:
            # The cache is an optimization; a write failure must not fail a load.
            return
