"""Shared ``/mock`` argument handling for the chat surfaces (MOCK_PLAN §3.2)."""
from __future__ import annotations

__all__ = ["MockArgs", "format_scenarios", "parse_mock_args"]


class MockArgs:
    """Parsed ``/mock [list|clean|NAME] [--speed N] [--seed N]``."""

    __slots__ = ("action", "error", "scenario", "seed", "speed")

    def __init__(self) -> None:
        self.action = "list"
        self.scenario = ""
        self.speed = 1.0
        self.seed = 0
        self.error = ""


def parse_mock_args(args: tuple[str, ...] | list[str]) -> MockArgs:
    out = MockArgs()
    rest: list[str] = []
    items = list(args)
    i = 0
    while i < len(items):
        item = items[i]
        if item in ("--speed", "--seed"):
            if i + 1 >= len(items):
                out.error = f"{item} needs a number"
                return out
            try:
                if item == "--speed":
                    out.speed = max(0.0, float(items[i + 1]))
                else:
                    out.seed = int(items[i + 1])
            except ValueError:
                out.error = f"{item} needs a number"
                return out
            i += 2
            continue
        rest.append(item)
        i += 1
    if not rest or rest[0] == "list":
        out.action = "list"
    elif rest[0] == "clean":
        out.action = "clean"
    else:
        out.action, out.scenario = "run", rest[0]
    return out


def format_scenarios(rows: list) -> str:
    """One line per scenario: name, duration, flags, summary."""
    width = max((len(row.name) for row in rows), default=0)
    lines = ["Mock scenarios (run with /mock NAME [--speed N]):"]
    for row in rows:
        flags = ("interactive " if row.interactive else "") + ("slow" if row.slow else "")
        lines.append(f"  {row.name:<{width}}  ~{row.est_seconds}s  {row.summary}" + (f"  [{flags.strip()}]" if flags else ""))
    return "\n".join(lines)
