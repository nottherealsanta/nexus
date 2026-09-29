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
from contextlib import aclosing
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
    """
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    writer = JsonlWriter(out) if json_output else None
    renderer = None if json_output else TerminalRenderer(out, stderr=err)

    if handshake:
        await client.handshake()
    summary = await client.open_session(session)
    # Subscribe strictly after the session's current end, so a reused session
    # never replays a previous turn's terminal event (which would end this run
    # before its own turn ever starts).
    last_seq = _last_seq(summary)

    terminal = None
    try:
        async with aclosing(turn_events(client, session, content, last_seq)) as events:
            async for event in events:
                if writer is not None:
                    writer.write(event)
                else:
                    assert renderer is not None
                    renderer.render(event)
                if approver is not None and event.type == "permission.requested":
                    await answer_permission(client, session, event.data, approver, err)
                if event.type in TERMINAL_EVENTS:
                    terminal = event
                    break
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


def _last_seq(summary: Any) -> int:
    """The session's current end, or ``0`` for an empty/new session."""
    value = getattr(summary, "last_seq", 0)
    return int(value) if isinstance(value, int) and value > 0 else 0


__all__ = ["run_once"]
