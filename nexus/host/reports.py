"""Host answer to ``SessionActivity``: what keeps a session busy after a turn.

Subagents are foreground-only. A child runs inside the ``subagent`` call that
started it and its report is that call's result, so the host never delivers a
report on its own, never wakes a root, and never replays one when a session
is reopened. ``subagent_report`` inputs already in an older session's log are
plain history: their ``input.queued`` events replay like any other input.

The wire shape of ``SessionActivityResult`` is unchanged for older clients:
``running_agents`` and ``reports_pending`` stay zero (a running child keeps its
root turn active), and ``wait_seconds`` is zero because nothing can reach the
root after its turn ends; the deprecated ``[agents] oneshot_wait_seconds`` is
parsed and ignored.
"""

from __future__ import annotations

from typing import Any

from . import protocol as p

__all__ = ["session_activity"]


def session_activity(session_id: str, handle: Any) -> p.SessionActivityResult:
    """The quiescence answer for one open session handle."""
    return p.SessionActivityResult(
        session=session_id,
        turn_active=bool(getattr(handle, "active", False)),
        queued=int(getattr(handle, "queue_depth", 0)),
    )
