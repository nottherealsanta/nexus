"""Host dispatch for the dev-mode ``Mock*`` commands (MOCK_PLAN §4.1).

Outside dev mode every ``Mock*`` command is an error, so a normal daemon can
never start a scenario. The scenario catalogue and sandbox live in
:mod:`nexus.devtools.mock`, imported lazily and only in dev mode.
"""
from __future__ import annotations

import re
from typing import Any

from ..host import protocol as p

__all__ = ["dispatch_mock"]

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_MAX_SPEED = 100.0


async def dispatch_mock(command: Any, facade: Any) -> p.Result | None:
    """Handle ``Mock*`` commands; return ``None`` for anything else."""
    if not isinstance(command, (p.MockList, p.MockStart, p.MockClean)):
        return None
    from ..devtools import dev_enabled

    if not dev_enabled():
        return p.ErrorResult(kind="DevModeRequired", message="mock scenarios require dev mode (nexus --dev)")
    from ..devtools.mock import catalog
    from ..devtools.mock.runner import kickoff_prompt

    scenarios = catalog()
    if isinstance(command, p.MockList):
        return p.MockListResult(scenarios=[
            p.MockScenarioInfo(
                name=s.name, summary=s.summary, tags=list(s.tags), est_seconds=s.est_seconds,
                interactive=s.interactive, slow=s.slow,
            )
            for s in sorted(scenarios.values(), key=lambda item: item.name)
        ])
    if isinstance(command, p.MockClean):
        from ..devtools.mock.sandbox import restore_sandbox

        restored = restore_sandbox(facade.runtime.workspace)
        return p.MockCleanResult(restored=restored)
    scenario = scenarios.get(command.scenario)
    if scenario is None:
        known = ", ".join(sorted(scenarios))
        return p.ErrorResult(kind="UnknownScenario", message=f"unknown mock scenario {command.scenario!r}; known: {known}")
    speed = min(max(float(command.speed), 0.0), _MAX_SPEED)
    seed = int(command.seed)
    session = command.session or _new_session_id(facade, scenario.name)
    if not _SESSION_ID.match(session):
        return p.ErrorResult(kind="ValueError", message="invalid session id")
    turn_id = await facade.start_turn(session, kickoff_prompt(scenario, speed=speed, seed=seed))
    return p.MockStartResult(session=session, scenario=scenario.name, turn_id=turn_id, interactive=scenario.interactive)


def _new_session_id(facade: Any, name: str) -> str:
    existing = {getattr(s, "id", None) or getattr(s, "session_id", None) for s in facade.list_sessions()}
    number = 1
    while f"mock-{name}-{number}" in existing:
        number += 1
    return f"mock-{name}-{number}"
