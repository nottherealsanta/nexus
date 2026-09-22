"""Semantic token-count disk cache and generic prompt-cache boundaries.

Two independent pieces live here (plan section 5.2):

Token-count cache
    A small, dependency-free on-disk cache for token counts keyed by a
    **canonical hash of the request-relevant semantic data** rather than by raw
    content. Only the hash, the count, and a timestamp are ever written; the
    payload itself (which may contain prompts, file bodies, or credentials) is
    never persisted. Writes are atomic (temp file + :func:`os.replace`) and a
    truncated or malformed entry is treated as a miss, not an error. The cache
    is bounded by entry count and prunes oldest-first.

Cache boundaries
    A provider-neutral description of where a stable prompt prefix ends: after
    the stable system+tools block and after the last stable history boundary.
    This is deliberately *not* wire syntax for any provider — the Anthropic
    adapter (or any other) translates :class:`CacheBoundary` values into its own
    breakpoints once the provider packet lands. Boundaries are only produced when
    the injected capabilities advertise ``prompt_caching``; unsupported providers
    get an empty tuple.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "CACHE_VERSION",
    "CacheBoundary",
    "CacheScope",
    "TokenCountCache",
    "boundaries_metadata",
    "canonical_json",
    "prompt_cache_boundaries",
    "semantic_key",
]

#: Bumped when the on-disk record shape changes; a mismatch is a miss.
CACHE_VERSION = 2

CacheScope = Literal["system_tools", "history"]


def _default(value: Any) -> Any:
    """Encode the few non-JSON types that may appear in semantic payloads.

    Unknown types raise rather than fall back to ``repr`` (which embeds memory
    addresses and would make the key non-deterministic).
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"cannot canonicalize {type(value).__name__} for hashing")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, compact separators, ASCII only."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_default,
    )


def semantic_key(payload: Any) -> str:
    """A stable hex digest of ``payload``'s canonical JSON form."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheBoundary:
    """Where a stable prompt prefix ends, in provider-neutral terms.

    ``position`` is a count of leading requests/segments (0 = the boundary sits
    before the first history message, i.e. after the stable system+tools block).
    """

    position: int
    scope: CacheScope


def prompt_cache_boundaries(
    capabilities: Any,
    *,
    system_tools_position: int = 0,
    history_position: int | None = None,
    enabled: bool | None = None,
) -> tuple[CacheBoundary, ...]:
    """Return the stable prefix boundaries, or ``()`` when unsupported.

    ``enabled`` defaults to ``capabilities.prompt_caching``. A provider that does
    not advertise prompt caching always gets no boundaries, even if a caller
    passes a positive position.
    """
    if enabled is None:
        enabled = bool(getattr(capabilities, "prompt_caching", False))
    if not enabled:
        return ()
    boundaries = [CacheBoundary(system_tools_position, "system_tools")]
    if history_position is not None:
        boundaries.append(CacheBoundary(history_position, "history"))
    return tuple(boundaries)


def boundaries_metadata(
    boundaries: tuple[CacheBoundary, ...],
) -> list[dict[str, Any]]:
    """Plain, content-free metadata for the request's ``metadata`` dict."""
    return [
        {"position": boundary.position, "scope": boundary.scope}
        for boundary in boundaries
    ]


class TokenCountCache:
    """Bounded, atomic, corruption-tolerant disk cache for integer token counts.

    The cache directory need not exist; it is created on first write. All
    failures are silent misses/ignored writes so a broken cache can never fail a
    turn.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        max_entries: int = 1024,
        clock: Any = time.time,
    ) -> None:
        if type(max_entries) is not int or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.directory = Path(directory)
        self.max_entries = max_entries
        self._clock = clock

    # -- keys --------------------------------------------------------------

    @staticmethod
    def key_for(payload: Any) -> str:
        return semantic_key(payload)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    # -- reads/writes ------------------------------------------------------

    def get(self, payload: Any) -> int | None:
        return self.get_key(semantic_key(payload))

    def get_key(self, key: str) -> int | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # A non-UTF8 or unreadable entry is corruption, not a crash.
            self._discard(path)
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            self._discard(path)
            return None
        if not isinstance(record, dict):
            self._discard(path)
            return None
        if record.get("version") != CACHE_VERSION:
            self._discard(path)
            return None
        if record.get("key") != key:
            self._discard(path)
            return None
        tokens = record.get("tokens")
        if type(tokens) is not int or tokens < 0:
            self._discard(path)
            return None
        return tokens

    def source_for_key(self, key: str) -> str | None:
        """The recorded source tag (``"provider"``) or ``None`` on any miss."""
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("version") != CACHE_VERSION:
            return None
        if record.get("key") != key:
            return None
        source = record.get("source")
        return source if isinstance(source, str) else None

    def put(self, payload: Any, tokens: int, *, source: str = "provider") -> str:
        key = semantic_key(payload)
        self.put_key(key, tokens, source=source)
        return key

    def put_key(self, key: str, tokens: int, *, source: str = "provider") -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        if type(tokens) is not int or tokens < 0:
            raise ValueError("tokens must be a non-negative integer")
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a non-empty string")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            record = {
                "version": CACHE_VERSION,
                "key": key,
                "tokens": tokens,
                "source": source,
                "created": self._clock(),
            }
            blob = canonical_json(record)
            self._atomic_write(self._path(key), blob)
        except OSError:
            # A cache write must never fail the caller.
            return
        self._prune()

    def _atomic_write(self, path: Path, blob: str) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=self.directory, prefix=".tmp-", suffix=".json"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            self._discard(tmp)
            raise

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    # -- maintenance -------------------------------------------------------

    def invalidate(self, payload: Any) -> bool:
        return self.invalidate_key(semantic_key(payload))

    def invalidate_key(self, key: str) -> bool:
        path = self._path(key)
        if path.exists():
            self._discard(path)
            return True
        return False

    def clear(self) -> int:
        removed = 0
        for path in self._entries():
            self._discard(path)
            removed += 1
        return removed

    def _entries(self) -> list[Path]:
        try:
            return sorted(
                path
                for path in self.directory.glob("*.json")
                if not path.name.startswith(".tmp-")
            )
        except OSError:
            return []

    def _prune(self) -> None:
        entries = self._entries()
        if len(entries) <= self.max_entries:
            return
        def _age(path: Path) -> tuple[float, str]:
            try:
                return (path.stat().st_mtime, path.name)
            except OSError:
                return (0.0, path.name)

        entries.sort(key=_age)
        for path in entries[: len(entries) - self.max_entries]:
            self._discard(path)

    def __len__(self) -> int:
        return len(self._entries())

    def __contains__(self, payload: Any) -> bool:
        return self.get(payload) is not None
