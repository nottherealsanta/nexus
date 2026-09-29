"""The ``⟦mock …⟧`` actor directive (MOCK_PLAN §5.1).

The mock provider is stateless: it learns *which scripted actor* a request
belongs to from a directive embedded in the first user message of the
conversation. The kickoff prompt carries ``actor=main``; every scripted
``subagent`` call embeds the directive for its child in the prompt it passes.
A request with no directive is never answered (fail closed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Directive", "format_directive", "parse_directive"]

_PATTERN = re.compile(r"⟦mock\s+([^⟧]*)⟧")
_PAIR = re.compile(r"([a-z_]+)=([^\s⟧]+)")


@dataclass(frozen=True)
class Directive:
    scenario: str
    actor: str = "main"
    speed: float = 1.0
    seed: int = 0


def format_directive(d: Directive) -> str:
    return f"⟦mock scenario={d.scenario} actor={d.actor} speed={d.speed:g} seed={d.seed}⟧"


def parse_directive(text: str) -> Directive | None:
    match = _PATTERN.search(text or "")
    if match is None:
        return None
    fields = dict(_PAIR.findall(match.group(1)))
    scenario = fields.get("scenario")
    if not scenario:
        return None
    try:
        speed = max(0.0, float(fields.get("speed", "1")))
        seed = int(fields.get("seed", "0"))
    except ValueError:
        return None
    return Directive(scenario=scenario, actor=fields.get("actor", "main"), speed=speed, seed=seed)
