"""Bounded retention for diagnostics, without destroying recovery evidence (Step 11.6).

Retention removes only a contiguous *prefix* of the durable event log and then
advances `log.floor`. It never removes the durable head, never reuses an id, and
never removes events pinned by an in-flight event replay or protected by an
unfinished turn, request, wait or background job. Exports hold their own SQLite
read snapshot, so a committed prune cannot half-empty a running export.
Messages, snapshots, turn context and model history are retained independently:
a session's conversation stays complete even when its early wire events expired.

Turn/request/tool lifecycle rows are pruned separately, keeping recent turns per
session and every turn recovery still needs. Mutation journal entries live for
the deduplication lifetime after terminal resolution; unresolved entries never
expire.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import Runtime

MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Settings:
    event_age_seconds: float = 14 * 24 * 3600
    event_bytes: int = 256 * MIB
    turn_age_seconds: float = 30 * 24 * 3600
    keep_turns_per_session: int = 200
    mutation_lifetime: float = 7 * 24 * 3600
    interval_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> Settings:
        def number(name: str, default: float, *, floor: float = 0.0) -> float:
            try:
                value = float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
            return max(floor, value) if math.isfinite(value) else default

        return cls(
            event_age_seconds=number("NEXUS10_EVENT_RETENTION_DAYS", 14, floor=0) * 24 * 3600,
            event_bytes=int(number("NEXUS10_EVENT_RETENTION_BYTES", 256 * MIB, floor=0)),
            turn_age_seconds=number("NEXUS10_TURN_RETENTION_DAYS", 30, floor=0) * 24 * 3600,
            keep_turns_per_session=int(number("NEXUS10_KEEP_TURNS", 200, floor=0)),
            mutation_lifetime=number("NEXUS10_MUTATION_RETENTION_DAYS", 7, floor=0) * 24 * 3600,
            interval_seconds=number("NEXUS10_RETENTION_INTERVAL_SECONDS", 3600, floor=1),
        )


class Retention:
    def __init__(self, runtime: Runtime, settings: Settings | None = None) -> None:
        self.runtime = runtime
        self.settings = settings or Settings.from_env()
        self._task: asyncio.Task[None] | None = None

    def run_once(self) -> dict[str, Any]:
        """Prune one batch; safe to call from tests without a scheduler."""
        settings = self.settings
        now = time.time()
        protected = self.runtime.store.protected_sessions()
        protected |= {sid for sid in self.runtime.turns}
        protected |= {request.session_id for request in self.runtime.questions.values()}
        events = self.runtime.store.prune_events(
            now=now, age_seconds=settings.event_age_seconds, byte_budget=settings.event_bytes,
            protect_sessions=protected)
        turns = self.runtime.store.prune_turns(
            now=now, age_seconds=settings.turn_age_seconds, keep_per_session=settings.keep_turns_per_session)
        mutations = self.runtime.store.prune_mutations(now)
        summary = {"at": now, "events": events, "turns": turns, "mutations": mutations,
                   "settings": asdict(settings)}
        if events["removed"] or events.get("over_budget") or turns["turns_removed"] or mutations:
            # A global record, so any resyncing client can see why the floor moved.
            self.runtime.bus.publish("log.retention", summary)
        return summary

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.settings.interval_seconds)
            self.run_once()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="nexus-retention")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
