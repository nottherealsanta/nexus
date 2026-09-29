"""``nexus mock list|run|clean`` (dev mode only; MOCK_PLAN §3.2).

Headless twin of the chat ``/mock`` command, for terminals and CI. It only
speaks the host contract: ``MockStart`` creates the session and the kickoff turn,
then the session stream is followed to its terminal event.
"""
from __future__ import annotations

import sys
from contextlib import aclosing
from typing import Any, TextIO

from ..client.protocol import Client
from .mock_args import format_scenarios

TERMINAL_EVENTS = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})

__all__ = ["run_mock"]

_VERDICT = "mock verdict"


async def run_mock(
    client: Client, *, action: str, scenario: str = "", speed: float = 0.0, seed: int = 0,
    stdout: TextIO | None = None, stderr: TextIO | None = None, json_output: bool = False,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    await client.handshake()
    if action == "list":
        out.write(format_scenarios((await client.mock_list()).scenarios) + "\n")
        return 0
    if action == "clean":
        await client.mock_clean()
        out.write("Sandbox restored to its seeded state\n")
        return 0
    names = [scenario] if action == "run" else [
        row.name for row in (await client.mock_list()).scenarios if not row.slow and not row.interactive
    ]
    failed = 0
    for name in names:
        passed = await _run_one(client, name, speed, seed, out, err, json_output)
        failed += 0 if passed else 1
    if len(names) > 1:
        out.write(f"\n{len(names) - failed}/{len(names)} scenarios passed\n")
    return 1 if failed else 0


async def _run_one(client: Any, name: str, speed: float, seed: int, out: TextIO, err: TextIO, json_output: bool) -> bool:
    started = await client.mock_start(name, speed=speed, seed=seed)
    text: list[str] = []
    verdict = ""
    async with aclosing(client.stream(started.session, 0, follow=True)) as events:
        async for event in events:
            if event.type == "text":
                text.append(str((event.data or {}).get("text", "")))
            if event.type in TERMINAL_EVENTS:
                if event.type != "turn.completed":
                    err.write(f"{name}: turn ended with {event.type}: {(event.data or {}).get('error', '')}\n")
                break
    verdict = next((ln for ln in "".join(text).splitlines() if _VERDICT in ln), "")
    passed = verdict.startswith("✓")
    if json_output:
        import json

        out.write(json.dumps({"scenario": name, "session": started.session, "passed": passed, "verdict": verdict}) + "\n")
    else:
        out.write(f"{'PASS' if passed else 'FAIL'}  {name:<20} {verdict or '(no verdict)'}\n")
    return passed
