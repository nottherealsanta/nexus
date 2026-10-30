"""Model catalog from models.dev, cached locally (PLAN.md step 3).

`load()` returns the catalog and where it came from: a fresh download, the
cache (younger than `TTL_S`), or a stale cache when the download fails. The
download is bounded in size and time. `models_for()` lists one provider's models
that can run an agent turn: text output and tool calls, not deprecated, and
not a realtime/audio/search variant (`NOT_AGENT_MODELS`).

Codex has no models.dev entry of its own. It uses the `openai` list filtered to
reasoning `gpt-*` models, and not every listed id is served by the Codex backend
(not verified per id).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..paths import home
from .providers import PROVIDERS

CATALOG_URL = "https://models.dev/api.json"
TTL_S = 24 * 3600
MAX_BYTES = 64 * 1024 * 1024
NOT_AGENT_MODELS = ("realtime", "audio", "transcribe", "tts", "search-preview", "embedding")


@dataclass(frozen=True, slots=True)
class ModelInfo:
    provider: str
    id: str
    name: str
    context: int | None
    max_output: int | None
    reasoning: bool
    efforts: tuple[str, ...]
    input_cost: float | None  # USD per million tokens
    output_cost: float | None
    cache_read_cost: float | None
    released: str | None
    # Prompt sizes where the input price rises (models.dev `cost.tiers`),
    # ascending. Clients use them to show occupancy of the next price tier.
    tiers: tuple[int, ...] = ()
    image_input: bool = False  # models.dev `modalities.input` has "image"; unknown models are treated as text-only

    @property
    def ref(self) -> str:
        return f"{self.provider}/{self.id}"


def cache_path() -> Path:
    return home() / "cache" / "models.dev.json"


async def _download() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client, client.stream("GET", CATALOG_URL) as response:
        if response.status_code >= 400:
            raise OSError(f"models.dev: HTTP {response.status_code}")
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise OSError("models.dev: catalog exceeds size limit")
            chunks.append(chunk)
    data = json.loads(b"".join(chunks))
    if not isinstance(data, dict):
        raise TypeError("models.dev: catalog is not an object")
    return data


# The parsed cache file, keyed by path, mtime and size. Parsing the multi-MB file
# takes hundreds of milliseconds on the event loop, and context, draft context and
# image checks all read it; callers treat the returned catalog as read-only.
_parsed: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def _read_cache(path: Path) -> dict[str, Any]:
    global _parsed
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _parsed is None or _parsed[0] != key:
        _parsed = (key, json.loads(path.read_bytes()))
    return _parsed[1]


async def load(*, refresh: bool = False) -> tuple[dict[str, Any], str]:
    path = cache_path()
    age = time.time() - path.stat().st_mtime if path.exists() else None
    if not refresh and age is not None and age < TTL_S:
        try:
            return _read_cache(path), f"cache ({int(age // 60)} min old)"
        except ValueError:
            age = None  # corrupt cache: download instead
    try:
        data = await _download()
    except (OSError, ValueError, TypeError, httpx.HTTPError) as exc:
        if age is None:
            raise OSError(f"models.dev is unreachable and there is no cache: {exc}") from exc
        return json.loads(path.read_bytes()), f"stale cache ({int(age // 3600)} h old; download failed: {exc})"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data))
    os.replace(temp, path)
    return data, "models.dev (downloaded now)"


def _efforts(entry: dict[str, Any]) -> tuple[str, ...]:
    for option in entry.get("reasoning_options") or []:
        if isinstance(option, dict) and option.get("type") == "effort":
            return tuple(str(v) for v in option.get("values") or [])
    return ()


def _tiers(entry: dict[str, Any]) -> tuple[int, ...]:
    """Prompt sizes where the price rises, from models.dev `cost.tiers`."""
    cost = entry.get("cost")
    tiers = cost.get("tiers") if isinstance(cost, dict) else None
    out: set[int] = set()
    for tier in tiers or []:
        if not isinstance(tier, dict):
            continue
        boundary = tier.get("tier")
        size = boundary.get("size") if isinstance(boundary, dict) else None
        if isinstance(size, int) and not isinstance(size, bool) and size > 0:
            out.add(size)
    return tuple(sorted(out))


def models_for(catalog: dict[str, Any], provider_id: str) -> list[ModelInfo]:
    info = PROVIDERS[provider_id]
    if provider_id == "mock":
        from ..mock.scenarios import CATALOG

        return [
            ModelInfo("mock", name, scenario.summary, 200000, 8192, True, ("low", "medium", "high"), 0.0, 0.0, 0.0, None)
            for name, scenario in sorted(CATALOG.items())
        ]
    entries = (catalog.get(info.catalog_id) or {}).get("models") or {}
    out: list[ModelInfo] = []
    for model_id, entry in entries.items():
        if not isinstance(entry, dict) or not entry.get("tool_call") or entry.get("status") == "deprecated":
            continue
        if "text" not in ((entry.get("modalities") or {}).get("output") or ["text"]):
            continue
        if any(word in model_id for word in NOT_AGENT_MODELS):
            continue
        if provider_id == "codex" and not (model_id.startswith("gpt-") and entry.get("reasoning")):
            continue
        limit, cost = entry.get("limit") or {}, entry.get("cost") or {}
        out.append(
            ModelInfo(
                provider=provider_id,
                id=model_id,
                name=str(entry.get("name") or model_id),
                context=limit.get("context"),
                max_output=limit.get("output"),
                reasoning=bool(entry.get("reasoning")),
                efforts=_efforts(entry),
                input_cost=cost.get("input"),
                output_cost=cost.get("output"),
                cache_read_cost=cost.get("cache_read"),
                released=entry.get("release_date"),
                tiers=_tiers(entry),
                image_input="image" in ((entry.get("modalities") or {}).get("input") or []),
            )
        )
    out.sort(key=lambda m: (m.released or "", m.id), reverse=True)
    return out


async def discover_ollama(base_url: str, *, client: httpx.AsyncClient | None = None) -> list[ModelInfo]:
    """Models the local Ollama server has pulled (`GET /api/tags`). The server reports no
    context window, prices or reasoning efforts, so those are unknown."""
    own = client or httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    try:
        response = await own.get(f"{base_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        entries = response.json().get("models") or []
    except (httpx.HTTPError, ValueError, AttributeError):
        return []
    finally:
        if client is None:
            await own.aclose()
    out = [
        ModelInfo("ollama", str(e["name"]), str(e["name"]), None, None, False, (), None, None, None,
                  str(e.get("modified_at") or "")[:10] or None)
        for e in entries if isinstance(e, dict) and e.get("name") and "embed" not in str(e["name"])
    ]
    out.sort(key=lambda m: (m.released or "", m.id), reverse=True)
    return out


async def list_models(data: dict[str, Any], provider_id: str) -> list[ModelInfo]:
    """`models_for`, plus the providers that list their models themselves."""
    if provider_id == "ollama":
        from .hub import sign_in

        return await discover_ollama(sign_in("ollama").base_url)
    return models_for(data, provider_id)
