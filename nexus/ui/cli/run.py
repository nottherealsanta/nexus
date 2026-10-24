"""One-shot runs (``nexus run "..."`` over the daemon).

A one-shot run opens a session, starts exactly one turn, streams its events to
the terminal (or, with ``json_output``, to JSONL), and returns a process exit
code. In JSONL mode there is no approver at all, so the run is unattended by
construction. In human mode an optional approver answers permission requests
while the turn is live; the subscription stays open, so the producer is never
parked without a responder.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from contextlib import aclosing
from collections.abc import AsyncIterator
from typing import Any, TextIO

from ...client.protocol import Client, ClientError
from ..jsonl import JsonlWriter
from .approve import Approver
from .render import TERMINAL_EVENTS, TerminalRenderer, exit_code
from .stream import answer_permission, turn_events


async def run_once(
    client: Client,
    *,
    session: str,
    content: str,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    json_output: bool = False,
    approver: Approver | None = None,
    handshake: bool = True,
) -> int:
    """Run one turn to completion and return an exit code.

    ``0`` on ``turn.completed``, ``1`` on failure or a missing terminal event,
    and ``130`` when interrupted (which also cancels the turn daemon-side).

    Background subagents (docs/agents.md): when the turn ends while children still
    run (or their reports are queued or being delivered), the run keeps following
    the session until it is quiescent, bounded by the daemon's ``[agents]
    oneshot_wait_seconds``. Woken turns stream like the first; the exit code is the
    last turn's. ``0`` seconds, or giving up at the bound, prints a "still running"
    note to stderr and exits at the first terminal event as before.
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    writer = JsonlWriter(out) if json_output else None
    renderer = None if json_output else TerminalRenderer(out, stderr=err)

    if handshake:
        await client.handshake()
    try:  # one-time "new session database" notice (docs/sessions.md); advisory only
        notice = await client.database_notice()
        if notice:
            err.write(notice + "\n")
    except Exception:  # noqa: BLE001
        pass
    summary = await client.open_session(session)
    # Subscribe strictly after the session's current end, so a reused session
    # never replays a previous turn's terminal event (which would end this run
    # before its own turn ever starts).
    last_seq = _last_seq(summary)

    terminal = None
    try:
        async with aclosing(turn_events(client, session, content, last_seq)) as events:
            async for event in _follow(client, session, events, err):
                if writer is not None:
                    writer.write(event)
                else:
                    assert renderer is not None
                    renderer.render(event)
                if approver is not None and event.type == "permission.requested":
                    await answer_permission(client, session, event.data, approver, err)
                if event.type in TERMINAL_EVENTS:
                    terminal = event
    except asyncio.CancelledError:
        # A real SIGINT surfaces here as task cancellation (asyncio.run turns it
        # into KeyboardInterrupt at the boundary). Cancel the daemon-side turn so
        # an interrupt does not leave work running with no view.
        with contextlib.suppress(Exception):
            await client.cancel(session, reason="interrupted")
        raise
    except KeyboardInterrupt:
        with contextlib.suppress(Exception):
            await client.cancel(session, reason="interrupted")
        err.write("Cancelled.\n")
        err.flush()
        return 130
    except ClientError as exc:
        err.write(f"Error: {exc}\n")
        err.flush()
        return 1

    if terminal is None:
        err.write("Error: the turn ended without a terminal event\n")
        err.flush()
    return exit_code(terminal)


#: Seconds between quiescence checks while waiting for background subagents.
_ACTIVITY_POLL_SECONDS = 0.5


def _busy(activity: Any, *, with_turn: bool) -> bool:
    """True while background work can still reach the root.

    The turn-active flag only counts once the wait began: right after a terminal
    event it can lag by a moment, and a plain run must not pay a poll for it.
    """
    return bool(
        activity.queued
        or activity.running_agents
        or activity.reports_pending
        or (with_turn and activity.turn_active)
    )


async def _follow(
    client: Client, session: str, events: AsyncIterator[Any], err: TextIO
) -> AsyncIterator[Any]:
    """Yield the turn's events; after a terminal event, keep going while children work.

    Ends at the first terminal event unless the session still has running
    subagents, queued input or undelivered reports and the daemon's one-shot wait
    allows following it; then it ends when the session is quiescent or the wait
    runs out (with a note on ``err``).
    """
    iterator = events.__aiter__()
    pending: asyncio.Future | None = None
    deadline: float | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            if deadline is None:
                done = await asyncio.wait({pending})
            else:
                done = await asyncio.wait({pending}, timeout=_ACTIVITY_POLL_SECONDS)
            if not done[0]:
                activity = await _activity(client, session)
                if activity is None or not _busy(activity, with_turn=True):
                    return
                if time.monotonic() >= deadline:
                    _note_still_running(err, activity, activity.wait_seconds)
                    return
                continue
            future, pending = pending, None
            try:
                event = future.result()
            except StopAsyncIteration:
                return
            yield event
            if event.type in TERMINAL_EVENTS and deadline is None:
                activity = await _activity(client, session)
                if activity is None or not _busy(activity, with_turn=False):
                    return
                if activity.wait_seconds <= 0:
                    _note_still_running(err, activity, 0)
                    return
                deadline = time.monotonic() + activity.wait_seconds
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending


async def _activity(client: Client, session: str) -> Any | None:
    """The daemon's view of what keeps ``session`` busy, or ``None`` if unavailable."""
    try:
        return await client.session_activity(session)
    except Exception:  # noqa: BLE001 - an older daemon or a transport error ends the wait
        return None


def _note_still_running(err: TextIO, activity: Any, waited: int) -> None:
    parts = []
    if activity.running_agents:
        parts.append(f"{activity.running_agents} subagent(s) still running")
    held = activity.queued + activity.reports_pending
    if held:
        parts.append(f"{held} report(s) or message(s) not yet delivered")
    if not parts:
        parts.append("the session is still working")
    reason = "waiting is off ([agents] oneshot_wait_seconds = 0)" if waited <= 0 else f"gave up after {waited}s"
    err.write(f"Note: {'; '.join(parts)}; {reason}. Reopen the session to continue.\n")
    err.flush()


def _last_seq(summary: Any) -> int:
    """The session's current end, or ``0`` for an empty/new session."""
    value = getattr(summary, "last_seq", 0)
    return int(value) if isinstance(value, int) and value > 0 else 0


__all__ = ["run_once"]
