"""Per-session file observations and the read-before-edit guard.

A session may change an existing file only after a direct `read` of that file
put its content into the model's context. An observation records the resolved
absolute path, a content hash (never a timestamp), the total line count, the
line ranges the model actually saw, which lines were clipped, whether the whole
output was clipped, and the read tool call id. Observations persist in SQLite
(`file_observation`) and survive a daemon restart; they are never rebuilt from a
file's current contents.

Coverage rules:

* A line is covered when a same-revision read window contains it and that line
  was not clipped. One read output clipped as a whole establishes no coverage
  (read a smaller window instead).
* A replacement edit and each patch hunk require coverage of the original
  lines they change. A pure insertion requires coverage of its anchor line, or
  of the last line when appended, or a complete observation of an empty file.
* Overwriting, deleting or moving a whole file requires complete coverage:
  every line of the file, or a read of an empty file. Several windows add up
  only while they share one revision.
* An added file needs no observation, but its destination must not exist.

Mutations are serialized by one asyncio lock per runtime, and the revision is
checked again inside that lock immediately before writing. This orders Nexus
writers; it cannot lock out an external process (see docs/tools.md).
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .store import Store
from .tools.base import ToolError


def content_revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    ordered = sorted((start, end) for start, end in ranges if start <= end)
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((start, end) for start, end in merged)


@dataclass(frozen=True, slots=True)
class FileObservation:
    path: str
    revision: str
    lines: int
    ranges: tuple[tuple[int, int], ...] = ()
    partial: tuple[int, ...] = ()
    clipped: bool = False
    call_id: str | None = None
    updated: float = 0.0

    def covers(self, start: int, end: int) -> bool:
        return all(
            line not in self.partial and any(first <= line <= last for first, last in self.ranges)
            for line in range(start, end + 1)
        )

    @property
    def complete(self) -> bool:
        """Whether every line of the observed revision reached the model intact."""
        if self.partial:
            return False
        return self.covers(1, self.lines) if self.lines else True

    def json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "lines": self.lines,
            "ranges": [list(pair) for pair in self.ranges],
            "partial": list(self.partial),
            "clipped": self.clipped,
            "call_id": self.call_id,
            "updated": self.updated,
        }


class FileGuard:
    """One session's observations and the runtime-wide mutation lock."""

    def __init__(self, store: Store, session_id: str, lock: Any,
                 pending: list[FileObservation] | None = None) -> None:
        self.store = store
        self.session_id = session_id
        self.lock = lock
        self.pending = pending

    def get(self, path: Path | str) -> FileObservation | None:
        return self._load(str(path))

    def _load(self, key: str) -> FileObservation | None:
        data = self.store.file_observation(self.session_id, key)
        if data is None:
            return None
        return FileObservation(
            path=key,
            revision=data["revision"],
            lines=int(data["lines"]),
            ranges=tuple((int(a), int(b)) for a, b in data.get("ranges", [])),
            partial=tuple(int(n) for n in data.get("partial", [])),
            clipped=bool(data.get("clipped", False)),
            call_id=data.get("call_id"),
            updated=float(data.get("updated", 0.0)),
        )

    def record_read(self, path: Path, data: bytes, *, lines: int, ranges: Iterable[tuple[int, int]],
                    partial: Iterable[int] = (), clipped: bool = False,
                    call_id: str | None = None) -> FileObservation:
        """Remember what one direct, model-visible read actually showed.

        A different revision replaces the previous observation (the old window
        cannot authorize the new content). The same revision merges windows, so
        several reads can establish complete coverage of one revision.
        """
        observation = FileObservation(
            path=str(path), revision=content_revision(data), lines=lines,
            ranges=_merge_ranges(ranges), partial=tuple(partial), clipped=clipped,
            call_id=call_id, updated=time.time(),
        )
        if self.pending is not None:
            self.pending.append(observation)
            return observation
        return self._merge_read(observation)

    def commit_reads(self, call_ids: set[str]) -> None:
        """Promote only reads whose successful results were saved into context."""
        if self.pending is None:
            return
        for observation in self.pending[:]:
            if observation.call_id in call_ids:
                self._merge_read(observation)
                self.pending.remove(observation)

    def _merge_read(self, observed: FileObservation) -> FileObservation:
        current = self._load(observed.path)
        new_ranges = observed.ranges
        new_partial = set(observed.partial)
        if current is not None and current.revision == observed.revision:
            ranges_merged = _merge_ranges((*current.ranges, *new_ranges))
            # Repeating a clipped read cannot establish intact coverage. Conversely,
            # a later clipped read must not erase already-intact coverage.
            partial_merged = {
                line for line in set(current.partial) | new_partial
                if not (line not in current.partial and any(a <= line <= b for a, b in current.ranges))
                and not (line not in new_partial and any(a <= line <= b for a, b in new_ranges))
            }
        else:
            ranges_merged, partial_merged = new_ranges, new_partial
        observation = FileObservation(
            path=observed.path, revision=observed.revision, lines=observed.lines,
            ranges=ranges_merged, partial=tuple(sorted(partial_merged)),
            clipped=observed.clipped, call_id=observed.call_id, updated=observed.updated,
        )
        self.store.set_file_observation(self.session_id, observed.path, observation.json())
        return observation

    def forget(self, paths: Iterable[Path]) -> None:
        self.store.delete_file_observations(self.session_id, [str(path) for path in paths])

    @asynccontextmanager
    async def mutation(self) -> AsyncIterator[None]:
        """Serialize Nexus writers for the whole runtime (stronger than per path)."""
        async with self.lock:
            yield

    def require(self, display: str, path: Path, *, complete: bool = False,
                affected: Iterable[tuple[int, int]] = ()) -> FileObservation:
        """Raise a clear `ToolError` unless this session observed the current file."""
        key = str(path)
        observation = self._load(key)
        if not path.is_file():
            raise ToolError(f"{display} does not exist")
        revision = content_revision(path.read_bytes())
        if observation is None:
            raise ToolError(
                f"{display} has not been read in this session; read it before changing it "
                f"(resolved path: {key})")
        if observation.revision != revision:
            raise ToolError(
                f"{display} changed since this session read it; read it again before changing it "
                f"(resolved path: {key})")
        if complete and not observation.complete:
            raise ToolError(
                f"{display} was not read completely in this session; read the whole file before "
                f"overwriting, deleting or moving it (resolved path: {key})")
        for start, end in affected:
            if not observation.covers(start, end):
                span = f"line {start}" if start == end else f"lines {start}–{end}"
                verb = "was" if start == end else "were"
                raise ToolError(
                    f"{display} {span} {verb} not read intact in this session; read the affected "
                    f"lines again before changing them (resolved path: {key})")
        return observation
