"""Durable descriptive root-agent selection for one session."""
from __future__ import annotations

from collections.abc import Mapping

import msgspec


class AgentSelection(msgspec.Struct, frozen=True):
    """The selected root agent name; definitions remain daemon-owned."""

    name: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {"name": self.name}

    @classmethod
    def from_dict(cls, data: object) -> AgentSelection | None:
        if not isinstance(data, Mapping):
            return None
        name = data.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            return None
        return cls(name=name)


__all__ = ["AgentSelection"]
