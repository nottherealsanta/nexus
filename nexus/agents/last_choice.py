"""Remembered model and effort per root agent (``~/.nexus/agent_models.json``).

Machine state, not config: the last model reference and reasoning effort the
user picked while a given root agent (``build``, ``orchestrator``, ...) was in
effect. Selecting that agent in a session restores them. Bounded (64 agents,
256-char values, 64 KiB file); a missing or corrupt file reads as empty.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_MAX_AGENTS = 64
_MAX_TEXT = 256
_MAX_BYTES = 64 * 1024


class AgentChoiceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, dict[str, str | None]]:
        try:
            if self.path.stat().st_size > _MAX_BYTES:
                return {}
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, str | None]] = {}
        for agent, row in data.items():
            if not (isinstance(agent, str) and isinstance(row, dict)):
                continue
            model, effort = row.get("model"), row.get("effort")
            if isinstance(model, str) and model and len(model) <= _MAX_TEXT:
                out[agent] = {"model": model,
                              "effort": effort if isinstance(effort, str) and len(effort) <= _MAX_TEXT else None}
        return out

    def get(self, agent: str) -> tuple[str, str | None] | None:
        row = self._read().get(agent.casefold())
        return (row["model"], row["effort"]) if row else None

    def put(self, agent: str, model: str, effort: str | None) -> None:
        if not agent or not model or len(model) > _MAX_TEXT:
            return
        data = self._read()
        data[agent.casefold()] = {"model": model, "effort": effort}
        while len(data) > _MAX_AGENTS:
            data.pop(next(iter(data)))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2))
            os.replace(temporary, self.path)
        except OSError:
            pass
