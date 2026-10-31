"""Bounded retention for diagnostics, without destroying recovery evidence.

Three lifetimes are independent:

* conversation messages, turn context and session snapshots are canonical and
  are deleted only with their session;
* durable execution records (`turn`, `turn_request`, `turn_tool`), lifecycle
  events used by recovery and the mutation journal are evidence; they are
  retained until resolved/checkpointed and then by their own age/keep rules;
* heavy diagnostic wire payloads are expirable. A successful terminal turn's
  payload lives for at most an hour after completion, a failed or interrupted
  turn's for 24 hours, and a soft 256 MiB budget expires the oldest eligible
  payloads sooner. Expiry replaces the body with an explicit marker while the
  event envelope, durable id, type and cursor position stay put.

Whole-row prefix pruning still exists for ordinary diagnostic events after
`event_age_seconds`; it moves `floor` and is announced by `log.retention`.
Payload expiry is selective and does not move the floor, so an unfinished
session in the middle of the log cannot block unrelated expired payloads and an
expired payload is never an invisible hole.

Maintenance is bounded and resumable: each pass measures/compresses a small
migration batch, schedules deadlines, expires one payload batch, removes a few
event/turn/mutation batches, and then yields. The scheduler wakes on a terminal
turn, on storage pressure and on the periodic interval, so catch-up is not
limited to one 5,000-row batch per hour.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)
MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Settings:
    event_age_seconds: float = 14 * 24 * 3600
    event_bytes: int = 256 * MIB
    wire_success_seconds: float = 3600
    wire_failure_seconds: float = 24 * 3600
    wire_bytes: int = 256 * MIB
    wire_debug_seconds: float = 0.0
    turn_age_seconds: float = 30 * 24 * 3600
    keep_turns_per_session: int = 200
    mutation_lifetime: float = 7 * 24 * 3600
    interval_seconds: float = 60.0
    batch_size: int = 2000
    event_batches: int = 4
    catchup_delay_seconds: float = 0.05
    announce_seconds: float = 60.0

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
            wire_success_seconds=number("NEXUS10_WIRE_SUCCESS_RETENTION_SECONDS", 3600, floor=0),
            wire_failure_seconds=number("NEXUS10_WIRE_FAILURE_RETENTION_SECONDS", 24 * 3600, floor=0),
            wire_bytes=int(number("NEXUS10_WIRE_PAYLOAD_BYTES", 256 * MIB, floor=0)),
            wire_debug_seconds=number("NEXUS10_WIRE_DEBUG_RETENTION_DAYS", 0, floor=0) * 24 * 3600,
            turn_age_seconds=number("NEXUS10_TURN_RETENTION_DAYS", 30, floor=0) * 24 * 3600,
            keep_turns_per_session=int(number("NEXUS10_KEEP_TURNS", 200, floor=0)),
            mutation_lifetime=number("NEXUS10_MUTATION_RETENTION_DAYS", 7, floor=0) * 24 * 3600,
            interval_seconds=min(60.0, number("NEXUS10_RETENTION_INTERVAL_SECONDS", 60, floor=1)),
            batch_size=int(number("NEXUS10_RETENTION_BATCH_SIZE", 2000, floor=1)),
            event_batches=int(number("NEXUS10_RETENTION_EVENT_BATCHES", 4, floor=1)),
            catchup_delay_seconds=number("NEXUS10_RETENTION_CATCHUP_DELAY_SECONDS", 0.05, floor=0),
            announce_seconds=number("NEXUS10_RETENTION_ANNOUNCE_SECONDS", 60, floor=0),
        )


class Retention:
    def __init__(self, runtime: Runtime, settings: Settings | None = None) -> None:
        self.runtime = runtime
        self.settings = settings or Settings.from_env()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self.last_run: float | None = None
        self.last_duration_ms: float = 0.0
        self.last_summary: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_announce: float | None = None

    def note_terminal(self) -> None:
        """A turn reached a terminal state; schedule its payload deadline soon."""
        self._wake.set()

    def note_pressure(self) -> None:
        """Storage crossed a soft budget; run a bounded pass without waiting."""
        self._wake.set()

    def metrics(self) -> dict[str, Any]:
        return {
            "last_run": self.last_run,
            "last_duration_ms": round(self.last_duration_ms, 1),
            "last_error": self.last_error,
            "last": self.last_summary,
            "storage": self.runtime.store.storage_inventory(),
        }

    def run_once(self) -> dict[str, Any]:
        """Run one bounded, resumable maintenance pass; safe to call from tests."""
        settings = self.settings
        started = time.monotonic()
        now = time.time()
        store = self.runtime.store

        indexes = store.ensure_payload_indexes(limit=1)
        migration = store.backfill_event_payloads(limit=min(500, settings.batch_size))
        turn_links = store.backfill_wire_turn_ids(limit=min(500, settings.batch_size))
        scheduled = store.schedule_wire_expiry(
            success_seconds=settings.wire_success_seconds,
            failure_seconds=settings.wire_failure_seconds,
            debug_seconds=settings.wire_debug_seconds,
            limit=settings.batch_size,
        )
        protected = store.protected_sessions()
        protected |= {sid for sid in self.runtime.turns}
        protected |= {request.session_id for request in self.runtime.questions.values()}
        wire = store.expire_wire_payloads(
            now=now, byte_budget=settings.wire_bytes, protect_sessions=protected,
            batch_size=settings.batch_size)
        if not wire["over_budget"]:
            store._pressure_notified = False

        events = {"removed": 0, "floor": store.log_floor(), "head": store.log_head(),
                  "protected": sorted(protected), "pinned": bool(store._pins),
                  "batches": 0, "retained_bytes": 0, "over_budget": False}
        for _ in range(settings.event_batches):
            one = store.prune_events(
                now=now, age_seconds=settings.event_age_seconds, byte_budget=settings.event_bytes,
                protect_sessions=protected, batch_size=settings.batch_size)
            events["removed"] += one["removed"]
            events["floor"] = one["floor"]
            events["head"] = one["head"]
            events["pinned"] = one["pinned"]
            events["batches"] += one["batches"]
            events["retained_bytes"] = one["retained_bytes"]
            events["over_budget"] = one["over_budget"]
            if not one["removed"]:
                break
        turns = store.prune_turns(
            now=now, age_seconds=settings.turn_age_seconds, keep_per_session=settings.keep_turns_per_session)
        mutations = store.prune_mutations(now)
        inventory = store.storage_inventory()
        worked = bool(indexes["built"] or migration["rows"] or turn_links
                      or scheduled["scheduled"] or scheduled["unlinked"]
                      or wire["expired"] or events["removed"] or turns["turns_removed"] or mutations)
        summary = {
            "at": now, "elapsed_ms": 0.0,
            "events": events, "wire": wire, "indexes": indexes, "migration": migration,
            "turn_links": turn_links,
            "scheduled": scheduled, "turns": turns, "mutations": mutations,
            "storage": inventory,
            "blockers": {
                "protected_sessions": len(protected),
                "pinned_ranges": len(store._pins),
                "unmeasured_events": inventory["unmeasured_events"],
                "wire_over_budget": wire["over_budget"],
            },
            "worked": worked,
            "settings": asdict(settings),
        }
        summary["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        self.last_run = now
        self.last_duration_ms = summary["elapsed_ms"]
        self.last_summary = summary
        announce = worked or wire["over_budget"] or events["over_budget"]
        if announce and (self.last_announce is None
                         or now - self.last_announce >= settings.announce_seconds):
            # A global record, so any resyncing client can see why the floor
            # moved or a diagnostic payload became an explicit marker. It is
            # rate-limited: maintenance must not create an endless stream of
            # its own log events when a configured budget is impossible.
            self.runtime.bus.publish("log.retention", summary)
            self.last_announce = now
        return summary

    async def run_forever(self) -> None:
        while True:
            try:
                while True:
                    summary = self.run_once()
                    self.last_error = None
                    if not summary["worked"]:
                        if not (summary["wire"]["over_budget"] or summary["events"]["over_budget"]):
                            break
                        # Pressure with nothing eligible this pass: retry on a
                        # short bounded backoff instead of spinning or waiting
                        # the whole periodic interval.
                        await asyncio.sleep(min(60.0, self.settings.interval_seconds))
                        continue
                    # Short batches with an explicit yield: catch-up can remove
                    # many batches per minute without blocking the event loop,
                    # and an impossible budget cannot busy-spin at sleep(0).
                    await asyncio.sleep(max(0.0, self.settings.catchup_delay_seconds))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("retention pass failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.interval_seconds)
            except TimeoutError:
                pass

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
