"""Capability descriptor (plan section 3.2).

The loop reads capabilities and adapts rather than assuming. A provider adapter
declares what a given model can actually do; ``conservative()`` is the honest
default for a transport whose guarantees are unknown.
"""
from __future__ import annotations

from typing import Literal

import msgspec

DegradationPolicy = Literal["drop", "to_text", "error"]


class Capabilities(msgspec.Struct, frozen=True):
    tools: bool = False
    parallel_tool_calls: bool = False
    streaming: bool = True
    thinking: bool = False
    prompt_caching: bool = False
    vision: bool = False
    documents: bool = False
    json_schema_strict: bool = False
    max_context_tokens: int = 0
    max_output_tokens: int = 0
    degradation: dict[str, DegradationPolicy] = msgspec.field(default_factory=dict)

    @classmethod
    def conservative(cls) -> "Capabilities":
        """Assume nothing beyond streaming text."""
        return cls()


__all__ = ["Capabilities", "DegradationPolicy"]
