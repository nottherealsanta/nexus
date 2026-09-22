"""The provider protocol and the shared provider error taxonomy.

The protocol lives here; the legacy root ``nexus.provider.Provider`` is a
separate, narrower protocol used by the current Codex-backed agent and is kept
untouched during Phase 0.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..errors import MalformedToolCall, ProviderError
from .capabilities import Capabilities
from .request import ModelRequest
from .stream import StreamEvent

__all__ = ["Provider", "ProviderError", "MalformedToolCall"]


@runtime_checkable
class Provider(Protocol):
    name: str

    def capabilities(self, model: str) -> Capabilities: ...

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...

    async def count_tokens(self, req: ModelRequest) -> int | None:
        # None -> caller falls back to the Tokenizer heuristic.
        ...

    async def aclose(self) -> None: ...
