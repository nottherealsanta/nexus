"""Immutable, generation-stamped registries and an atomic reference.

Used by the extension/manifest machinery later; Phase 0 provides the mechanism
and its guarantees: a registry value never mutates in place, and swapping the
held reference is a single assignment readers never observe half-done.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    """An immutable name -> item mapping with a monotonically increasing generation."""

    __slots__ = ("_generation", "_items")

    def __init__(self, items: Mapping[str, T] | None = None, *, generation: int = 0):
        self._items: Mapping[str, T] = MappingProxyType(dict(items or {}))
        self._generation = generation

    @property
    def generation(self) -> int:
        return self._generation

    def get(self, name: str, default: T | None = None) -> T | None:
        return self._items.get(name, default)

    def all(self) -> Mapping[str, T]:
        return self._items

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def replace(self, items: Mapping[str, T]) -> Registry[T]:
        """Return a new generation containing exactly ``items``."""
        return Registry(items, generation=self._generation + 1)

    def merge(self, items: Mapping[str, T]) -> Registry[T]:
        """Return a new generation with ``items`` overlaid on the current ones."""
        combined = dict(self._items)
        combined.update(items)
        return self.replace(combined)

    def without(self, *names: str) -> Registry[T]:
        remaining = {k: v for k, v in self._items.items() if k not in names}
        return self.replace(remaining)

    def __contains__(self, name: object) -> bool:
        return name in self._items

    def __len__(self) -> int:
        return len(self._items)


class RegistryRef(Generic[T]):
    """A single atomic reference to an immutable registry generation."""

    __slots__ = ("_registry",)

    def __init__(self, registry: Registry[T] | None = None):
        self._registry = registry if registry is not None else Registry()

    def get(self) -> Registry[T]:
        return self._registry

    def swap(self, registry: Registry[T]) -> Registry[T]:
        """Atomically install ``registry``; return the previous generation."""
        previous = self._registry
        self._registry = registry
        return previous


__all__ = ["Registry", "RegistryRef"]
