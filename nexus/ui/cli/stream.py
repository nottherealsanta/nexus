"""Turn streaming helpers shared by the one-shot and interactive surfaces.

A turn must be *observed before it starts*: the session derives ``attended`` from
its live subscriber count, so if a turn began before the view attached, an
approval would see zero viewers and the unattended policy would answer it before
the user ever saw the prompt. :func:`turn_events` therefore attaches the
subscription in a background task, yields the loop once so the attachment
lands, and only then starts the turn.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TextIO

from ...client.protocol import Client, ClientError
from ...client.turn_stream import turn_events
from .approve import Approver


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
