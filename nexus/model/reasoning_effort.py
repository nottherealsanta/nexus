"""Durable per-session reasoning-effort selection state."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import msgspec

from .request import REASONING_EFFORTS

__all__ = ["ReasoningEffortSelection"]


class ReasoningEffortSelection(msgspec.Struct, frozen=True):
    """The session's explicit reasoning-effort override, if any.

    ``None`` represents a durable reset to the configured/default effort. The
    versioned mapping is deliberately small and contains no turn or provider
    state.
    """

    effort: str | None

    PAYLOAD_VERSION: ClassVar[int] = 1

    def __post_init__(self) -> None:
        if self.effort is not None and (
            not isinstance(self.effort, str) or self.effort not in REASONING_EFFORTS
        ):
            choices = ", ".join(sorted(REASONING_EFFORTS))
            raise ValueError(
                f"effort must be one of {choices}; got {self.effort!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh JSON-native event payload."""
        return {"effort": self.effort, "version": self.PAYLOAD_VERSION}

    @classmethod
    def from_dict(cls, data: object) -> ReasoningEffortSelection | None:
        """Parse a version-1 payload, skipping malformed/future data safely."""
        if not isinstance(data, Mapping):
            return None
        version = data.get("version")
        effort = data.get("effort")
        # ``bool`` is an ``int`` subclass, so require the exact JSON integer type.
        if type(version) is not int or version != cls.PAYLOAD_VERSION:
            return None
        if effort is not None and not isinstance(effort, str):
            return None
        if effort is not None and effort not in REASONING_EFFORTS:
            return None
        return cls(effort=effort)
