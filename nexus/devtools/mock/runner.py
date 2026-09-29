"""Headless scenario runner (MOCK_PLAN §3.2 ``nexus mock run``, and CI).

Drives a scenario through a real :class:`~nexus.runtime.Runtime` and
:class:`~nexus.host.HostFacade` — the same path the TUI and web use — and reads
the in-band verdict the scenario's final step wrote. Interactive scenarios get an
``auto`` behaviour: questions are answered and a parked turn is cancelled, then
resumed with a follow-up message.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from .directive import Directive, format_directive
from .dsl import Scenario

__all__ = ["RunReport", "kickoff_prompt", "run_scenario"]

_VERDICT_MARK = "mock verdict"


def kickoff_prompt(scenario: Scenario, *, speed: float = 1.0, seed: int = 0) -> str:
    """The user message that starts ``scenario`` (prompt, then the actor directive)."""
    directive = format_directive(Directive(scenario.name, "main", speed, seed))
    return f"{scenario.prompt}\n{directive}"


@dataclass
class RunReport:
    scenario: str
    session: str
    passed: bool
    verdict: str = ""
    error: str = ""
    seconds: float = 0.0
    events: int = 0
    tool_calls: int = 0
    agents: int = 0
    text: str = field(default="", repr=False)


async def run_scenario(
    scenario: Scenario,
    runtime: Any,
    *,
    session: str | None = None,
    speed: float = 0.0,
    seed: int = 0,
    timeout: float = 120.0,
    auto: bool = True,
) -> RunReport:
    from ...host import HostFacade
    from ...host import protocol as p

    facade = HostFacade(runtime)
    session_id = session or f"mock-{scenario.name}-{int(time.time() * 1000) % 10_000_000}"
    started = time.monotonic()
    events: list[Any] = []
    cancelled_once = False
    cancel_done = False
    retried = False
    streamed: list[str] = []

    async def watch() -> None:
        nonlocal cancelled_once, cancel_done
        async for event in facade.subscribe(session_id, 0, follow=True):
            events.append(event)
            if not auto:
                continue
            if event.type == "question.requested":
                data = event.data or {}
                options = data.get("options") or data.get("choices") or []
                first = options[0] if options else None
                answer = (first.get("id") or first.get("label")) if isinstance(first, dict) else (first or "Blue")
                await facade.handle(p.QuestionAnswer(
                    session=session_id, answer=str(answer), call_id=str(data.get("call_id") or data.get("tool_call_id") or ""),
                    question_id=str(data.get("id") or data.get("question_id") or "")))
            elif event.type == "text.delta":
                streamed.append(str((event.data or {}).get("text", "")))
                if "run /cancel" in "".join(streamed[-12:]) and not cancel_done:
                    cancel_done = cancelled_once = True

    watcher = asyncio.create_task(watch())
    error = ""
    try:
        await facade.start_turn(session_id, kickoff_prompt(scenario, speed=speed, seed=seed))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if cancelled_once:
                cancelled_once = False
                await facade.cancel(session_id, reason="mock auto-cancel")
                await asyncio.sleep(0.2)
                await facade.start_turn(session_id, "Please continue.")
            if auto and not retried and any(e.type == "turn.failed" for e in events) and scenario.interactive:
                retried = True
                await asyncio.sleep(0.2)
                await facade.start_turn(session_id, "Please retry.")
                continue
            if _settled(events, facade, session_id):
                break
        else:
            error = f"timed out after {timeout:g}s"
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await watcher
    text = "".join(str((e.data or {}).get("text", "")) for e in events if e.type == "text")
    verdict = next((ln for ln in text.splitlines() if _VERDICT_MARK in ln), "")
    failed = [e for e in events if e.type in ("turn.failed",)]
    if failed and retried and verdict:
        failed = []  # an expected, recovered failure
    if failed and not error:
        error = str((failed[-1].data or {}).get("error") or (failed[-1].data or {}))[:300]
    tool_calls = sum(1 for e in events if e.type == "tool.requested")
    agents = sum(1 for e in events if e.type == "agent.spawned")
    return RunReport(
        scenario=scenario.name, session=session_id,
        passed=verdict.startswith("✓") and not error,
        verdict=verdict, error=error, seconds=time.monotonic() - started,
        events=len(events), tool_calls=tool_calls, agents=agents, text=text,
    )


def _settled(events: list[Any], facade: Any, session_id: str) -> bool:
    """A verdict was written and no turn is still running."""
    if not any(e.type == "text" and _VERDICT_MARK in str((e.data or {}).get("text", "")) for e in events):
        return False
    return facade.supervisor.queued_for(session_id) == 0 and facade.supervisor.running_for(session_id) == 0
