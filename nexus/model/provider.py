"""The provider protocol and the shared provider error taxonomy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import NamedTuple, Protocol, runtime_checkable

from ..errors import MalformedToolCall, ProviderError
from .capabilities import Capabilities
from .request import ModelRequest
from .stream import StreamEvent

__all__ = ["MalformedToolCall", "Provider", "ProviderError", "ResolvedModel"]


@runtime_checkable
class Provider(Protocol):
    name: str

    def capabilities(self, model: str) -> Capabilities: ...

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]: ...

    async def count_tokens(self, req: ModelRequest) -> int | None:
        # None -> caller falls back to the Tokenizer heuristic.
        ...

    async def aclose(self) -> None: ...


class ResolvedModel(NamedTuple):
    """Provider, concrete model, and capabilities for one request.

    This lives at L1 so both the router (``nexus.model.router``) and the loop can
    share it without the model layer importing ``nexus.core``.
    """

    provider: Provider
    model: str
    capabilities: Capabilities
