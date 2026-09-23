"""Turn streaming helpers shared by the one-shot and interactive surfaces.

A turn must be *observed before it starts*: the session derives ``attended`` from
its live subscriber count, so if a turn began before the view attached, an
approval would see zero viewers and the unattended policy would answer it before
the user ever saw the prompt. :func:`turn_events` therefore attaches the
subscription in a background task, yields the loop once so the attachment
lands, and only then starts the turn.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from typing import Any, TextIO

from ...events import Event
from .approve import Approver
from .client import Client, ClientError

_SENTINEL: Event | None = None


async def turn_events(
    client: Client,
    session: str,
    content: str,
    from_seq: int = 0,
    *,
    follow: bool = True,
) -> AsyncIterator[Event]:
    """Yield a turn's events, attaching the view before starting the turn."""
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            # ``aclosing`` guarantees the subscription is released when the pump
            # is cancelled (the turn ended, or the surface went away), so the
            # daemon sees a view leave instead of holding the stream open.
            async with aclosing(
                client.stream(session, from_seq, follow=follow)
            ) as events:
                async for event in events:
                    await queue.put(event)
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(pump())
    try:
        # Let the pump reach its first await so the subscription is attached.
        await asyncio.sleep(0)
        await client.start_turn(session, content)
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def answer_permission(
    client: Client,
    session: str,
    data: Mapping[str, Any],
    approver: Approver,
    stderr: TextIO,
) -> bool:
    """Ask the user and report the answer; the daemon decides the race."""
    request_id = data.get("id")
    if not request_id:
        return False
    decision = await approver.ask(data)
    try:
        resolved = await client.resolve_permission(session, str(request_id), decision)
    except ClientError as exc:
        stderr.write(f"Error resolving permission: {exc}\n")
        stderr.flush()
        return False
    if not resolved:
        stderr.write("Another view answered this request first.\n")
        stderr.flush()
    return resolved


__all__ = ["answer_permission", "turn_events"]
