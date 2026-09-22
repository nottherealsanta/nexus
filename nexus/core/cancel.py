"""Cooperative cancellation (plan section 4, detail 5).

A token is idempotent (the first reason wins), awaitable, and safe to check at
every await point. Nothing here kills tasks: cancellation is a signal the loop
and tools honour, mirroring the plan's structured approach.
"""
from __future__ import annotations

import asyncio

from ..errors import OperationCancelled


class CancelToken:
    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str | None = None) -> None:
        """Request cancellation. Idempotent: later calls do not change the reason."""
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()

    async def wait(self) -> None:
        """Await until cancellation is requested."""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled(self._reason or "cancelled")

    def __await__(self):
        return self._event.wait().__await__()


__all__ = ["CancelToken", "OperationCancelled"]
