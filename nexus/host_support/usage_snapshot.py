"""Keep the runtime's plan-usage snapshot fresh for the usage gate.

The service fetches ``providers_usage`` in the background, converts the result to
a :class:`~nexus.model.usage_gate.UsageSnapshot` and hands it to the runtime via
``set_usage_snapshot`` (when the runtime has one). ``kick`` is synchronous and
cheap: it starts at most one refresh at a time, no more often than the policy's
refresh interval, and does nothing when the gate is disabled or no event loop is
running. A failed fetch is swallowed (the gate fails open) and the previous
snapshot stays in place.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..model.usage_gate import DEFAULT_REFRESH_SECONDS, UsagePolicy, UsageSnapshot
from .provider_usage import providers_usage


class UsageSnapshotService:
    def __init__(self, runtime: Any, *, fetch: Callable[[Any], Awaitable[Mapping[str, Any]]] = providers_usage,
                 clock: Callable[[], float] = time.time) -> None:
        self._runtime = runtime
        self._fetch = fetch
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._last_attempt: float | None = None

    def _policy(self) -> UsagePolicy | None:
        policy = getattr(self._runtime, "_usage_policy", None)
        if policy is None:
            getter = getattr(self._runtime, "usage_policy", None)
            policy = getter() if callable(getter) else None
        return policy

    def ingest(self, result: Mapping[str, Any]) -> None:
        setter = getattr(self._runtime, "set_usage_snapshot", None)
        if callable(setter):
            setter(UsageSnapshot.from_result(result))

    def kick(self) -> None:
        policy = self._policy()
        if policy is not None and not policy.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        refresh = policy.max_age_seconds / 3 if policy is not None else DEFAULT_REFRESH_SECONDS
        now = self._clock()
        if self._last_attempt is not None and now - self._last_attempt < refresh:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_attempt = now
        self._task = loop.create_task(self.refresh())

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
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


__all__ = ["UsageSnapshotService"]
