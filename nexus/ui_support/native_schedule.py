"""Bounded native snapshot scheduling (TUI_LOCAL_INTERACTION_PLAN §1)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class UpdateCoalescer:
    """Coalesce scheduled updates into one latest-state emission per frame."""

    def __init__(self, emit: Callable[[], Awaitable[None]], delay: float = 0.016):
        self._emit = emit
        self._delay = delay
        self._worker: asyncio.Task | None = None
        self._pending = False
        self._force = False
        self._wake = asyncio.Event()

    def schedule(self) -> None:
        """Schedule an emission, retaining at most one timer/worker."""
        self._pending = True
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._pending:
            if not self._force:
                try:
                    await asyncio.wait_for(self._wake.wait(), self._delay)
                except asyncio.TimeoutError:
                    pass
            self._wake.clear()
            self._force = False
            if not self._pending:
                continue
            self._pending = False
            await self._emit()

    async def flush(self) -> bool:
        """Emit pending latest state now; return whether it was emitted."""
        worker = self._worker
        if worker is None:
            return False
        was_pending = self._pending
        if was_pending:
            self._force = True
            self._wake.set()
        await worker
        if self._worker is worker:
            self._worker = None
        return was_pending

    @property
    def pending(self) -> bool:
        return self._pending
