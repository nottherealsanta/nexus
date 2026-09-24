"""UI-neutral lifecycle for attaching to a turn before starting it.

Attendance is derived from live subscribers, so a turn must not start before
the daemon has registered this view. Surfaces share this helper so an approval
cannot race ahead of its first responder.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Protocol

from ..events import Event


class TurnClient(Protocol):
    """The small client surface required by :func:`turn_events`."""

    def stream(
        self, session: str, from_seq: int = 0, *, follow: bool = True
    ) -> AsyncIterator[Event]:
        ...

    async def start_turn(self, session: str, content: str) -> str:
        ...


async def turn_events(
    client: TurnClient,
    session: str,
    content: str,
    from_seq: int = 0,
    *,
    follow: bool = True,
) -> AsyncIterator[Event]:
    """Yield turn events after attaching the stream and starting the turn."""
    queue: asyncio.Queue[Event | BaseException | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async with aclosing(client.stream(session, from_seq, follow=follow)) as events:
                async for event in events:
                    await queue.put(event)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await queue.put(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        # Let the pump enter the transport subscription before starting the turn.
        await asyncio.sleep(0)
        await client.start_turn(session, content)
        while (event := await queue.get()) is not None:
            if isinstance(event, BaseException):
                raise event
            yield event
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


__all__ = ["TurnClient", "turn_events"]
