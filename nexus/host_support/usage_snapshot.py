"""Keep the runtime's plan-usage snapshot fresh.

The service fetches ``providers_usage`` in the background, converts the result to
a :class:`~nexus.model.usage_gate.UsageSnapshot` and hands it to the runtime via
``set_usage_snapshot`` (when the runtime has one). A failed fetch is swallowed
(the gate fails open) and the previous snapshot stays in place.

Two things start a refresh, and both share one in-flight task:

* ``start`` runs a periodic poll for the daemon's lifetime. It refreshes every
  ``[models] usage_refresh_seconds`` (default 300; read live each tick, so a
  Settings change applies within one tick) whether or not the subagent usage
  gate is enabled and whether or not anyone is looking.
* ``kick`` is the synchronous, cheap opportunistic trigger used before routing a
  sub-agent: it starts at most one refresh, no more often than the gate policy's
  refresh interval, and does nothing when the gate is disabled.

``ingest`` also takes the on-demand ``ProvidersUsage`` result; a result older
than one already ingested is dropped so a slow background fetch cannot replace
the numbers a newer fetch already published.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..config.schema import DEFAULT_USAGE_REFRESH_SECONDS
from ..model.usage_gate import DEFAULT_REFRESH_SECONDS, UsagePolicy, UsageSnapshot
from .provider_usage import providers_usage

#: How often the periodic poll checks whether a refresh is due (also the latency
#: with which a changed interval takes effect).
POLL_TICK_SECONDS = 30.0


class UsageSnapshotService:
    def __init__(self, runtime: Any, *, fetch: Callable[[Any], Awaitable[Mapping[str, Any]]] = providers_usage,
                 clock: Callable[[], float] = time.time, tick: float = POLL_TICK_SECONDS) -> None:
        self._runtime = runtime
        self._fetch = fetch
        self._clock = clock
        self._tick = tick
        self._task: asyncio.Task[None] | None = None
        self._poller: asyncio.Task[None] | None = None
        self._last_attempt: float | None = None
        self._latest_fetched_at = 0.0
        self._closed = False

    def _policy(self) -> UsagePolicy | None:
        policy = getattr(self._runtime, "_usage_policy", None)
        if policy is None:
            getter = getattr(self._runtime, "usage_policy", None)
            policy = getter() if callable(getter) else None
        return policy

    def interval(self) -> float:
        """Seconds between periodic refreshes, from live config."""
        getter = getattr(self._runtime, "usage_refresh_seconds", None)
        try:
            value = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 - a bad config never stops the poll
            value = None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return float(DEFAULT_USAGE_REFRESH_SECONDS)
        return float(value)

    def ingest(self, result: Mapping[str, Any]) -> None:
        fetched_at = result.get("fetched_at")
        if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
            if fetched_at < self._latest_fetched_at:
                return  # a newer fetch already published its numbers
            self._latest_fetched_at = float(fetched_at)
        self._last_attempt = max(self._last_attempt or 0.0, self._clock())  # fresh numbers satisfy the next due check
        setter = getattr(self._runtime, "set_usage_snapshot", None)
        if callable(setter):
            setter(UsageSnapshot.from_result(result))

    def _start_refresh(self) -> None:
        if self._closed or (self._task is not None and not self._task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_attempt = self._clock()
        self._task = loop.create_task(self.refresh())

    def kick(self) -> None:
        policy = self._policy()
        if policy is not None and not policy.enabled:
            return
        refresh = policy.max_age_seconds / 3 if policy is not None else DEFAULT_REFRESH_SECONDS
        if self._last_attempt is not None and self._clock() - self._last_attempt < refresh:
            return
        self._start_refresh()

    def poll(self) -> None:
        """Start a refresh when the periodic interval has passed (the first call refreshes at once)."""
        if self._last_attempt is not None and self._clock() - self._last_attempt < self.interval():
            return
        self._start_refresh()

    def start(self) -> None:
        """Begin the periodic poll; idempotent, needs a running loop, ended by ``aclose``."""
        if self._poller is None and not self._closed:
            self._poller = asyncio.get_running_loop().create_task(self._poll_forever())

    async def _poll_forever(self) -> None:
        while True:
            self.poll()
            await asyncio.sleep(self._tick)

    async def refresh(self) -> None:
        self._last_attempt = self._clock()
        try:
            result = await self._fetch(self._runtime)
            self.ingest(result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - fail open; the previous snapshot stays
            return

    async def aclose(self) -> None:
        self._closed = True
        tasks = [task for task in (self._poller, self._task) if task is not None and not task.done()]
        self._poller = self._task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["POLL_TICK_SECONDS", "UsageSnapshotService"]
