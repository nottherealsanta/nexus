"""LLM hub: every provider normalized to one conversation and stream shape.

A provider is anything with `name`, `body(Request)` (the JSON it would send)
and `stream(Request) -> AsyncIterator[Event]`. There is one module per wire
dialect (`responses.py`, `chat.py`); `providers.py` says which provider speaks
which, and with what quirks. Adapters never read credentials: they take a header
source from `nexus.llmhub.auth`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from .types import Event, Request


class Provider(Protocol):
    name: str

    def body(self, req: Request) -> dict[str, Any]: ...

    def stream(self, req: Request) -> AsyncIterator[Event]: ...
