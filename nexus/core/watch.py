"""Dependency-free mtime_ns + size polling watcher (plan sections 2.2, 6.3).

Polling is deliberate: a few dozen directories cost nothing, and there is no
fsevents/inotify divergence to debug. ``poll()`` is deterministic — it returns
changes sorted by path, and the first call only establishes a baseline.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ChangeKind = Literal["created", "changed", "deleted"]


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    path: Path


class DirectoryWatcher:
    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        pattern: str = "*",
        recursive: bool = False,
    ):
        self.roots = tuple(Path(root) for root in roots)
        self.pattern = pattern
        self.recursive = recursive
        self._state: dict[Path, FileState] = {}
        self._primed = False

    def _scan(self) -> dict[Path, FileState]:
        current: dict[Path, FileState] = {}
        for root in self.roots:
            if not root.exists():
                continue
            iterator = root.rglob(self.pattern) if self.recursive else root.glob(self.pattern)
            for path in iterator:
                try:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                current[path.resolve()] = FileState(stat.st_mtime_ns, stat.st_size)
        return current

    def prime(self) -> None:
        self._state = self._scan()
        self._primed = True

    def poll(self) -> list[Change]:
        current = self._scan()
        if not self._primed:
            self._state = current
            self._primed = True
            return []
        changes: list[Change] = []
        for path, state in current.items():
            old = self._state.get(path)
            if old is None:
                changes.append(Change("created", path))
            elif old != state:
                changes.append(Change("changed", path))
        for path in self._state:
            if path not in current:
                changes.append(Change("deleted", path))
        changes.sort(key=lambda change: (str(change.path), change.kind))
        self._state = current
        return changes

    def snapshot(self) -> dict[Path, FileState]:
        return dict(self._state)


__all__ = ["Change", "ChangeKind", "DirectoryWatcher", "FileState"]
