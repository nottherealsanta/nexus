"""Session-scoped subagent budgets.

Subagents are foreground-only: every child runs inside the ``subagent`` tool
call that started it and ends with it (its report is the call's result), so no
child outlives its root turn and nothing here owns a task.

What does span turns is the tree :class:`~nexus.agents.runner.SubagentBudget`:
one per root session, so the concurrency bound also covers the children of a
cancelled turn that are still winding down when the next turn starts. The
token, cost and fan-out counters keep their per-root-turn meaning through
:meth:`AgentPool.budget_window`, which calls ``begin_window()`` once at the start
of each root turn, and every runner built for that turn (static, per-iteration,
code mode) shares the one budget.

This module imports only the standard library; it never imports a session, a
runtime or a host.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["AgentPool"]


@dataclass
class _Window:
    budget: Any
    signature: object
    turn_id: str | None = None


class AgentPool:
    """One subagent budget per root session, owned by the runtime."""

    def __init__(self) -> None:
        self._windows: dict[str, _Window] = {}

    def budget(self, session_id: str) -> Any | None:
        """The session's current budget, if one was built."""
        window = self._windows.get(session_id)
        return window.budget if window is not None else None

    def budget_window(
        self,
        session_id: str,
        turn_id: str,
        *,
        signature: object,
        factory: Callable[[], Any | None],
    ) -> Any | None:
        """The session's budget for ``turn_id``, resetting its window once per turn.

        ``signature`` summarizes the configured limits. A changed signature builds
        a new budget only while no child is running, so a config reload never
        loses track of a running child's concurrency slot; otherwise it applies at
        the next quiet turn.
        """
        window = self._windows.get(session_id)
        quiet = window is None or window.budget.active == 0
        if window is None or (signature != window.signature and quiet):
            budget = factory()
            if budget is None:
                return None
            window = self._windows[session_id] = _Window(budget, signature)
        if turn_id != window.turn_id:
            window.budget.begin_window()
            window.turn_id = turn_id
        return window.budget

    def discard_if_idle(self, session_id: str) -> bool:
        """Forget a session's budget unless a child still holds a slot of it."""
        window = self._windows.get(session_id)
        if window is None:
            return True
        if window.budget.active:
            return False
        del self._windows[session_id]
        return True

    def clear(self) -> None:
        """Forget every session (runtime shutdown)."""
        self._windows.clear()
