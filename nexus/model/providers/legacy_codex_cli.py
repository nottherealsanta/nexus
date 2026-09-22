"""New-protocol adapter over the legacy Codex subprocess transport.

Phase 0 keeps the Codex CLI as the only live route. This adapter exposes that
same transport behind :class:`nexus.model.provider.Provider` so the IR and
protocol are exercised without changing CLI behaviour: it renders a
``ModelRequest`` into the legacy prompt, reuses :class:`nexus.provider.
CodexProvider` for the actual process lifecycle, and normalizes the returned
events. The subprocess semantics (process-group SIGTERM/SIGKILL, streaming
stderr capture, timeout) are owned by the legacy transport and are not weakened
here.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing
from pathlib import Path
from typing import Any

from ...config import Config
from ...errors import ProviderError
from ...provider import CodexProvider
from ..capabilities import Capabilities
from ..message import (
    ContentBlock,
    Document,
    Image,
    Text,
    ToolResult,
    ToolUse,
)
from ..request import ModelRequest
from ..stream import MessageStart, MessageStop, Raw, StreamEvent, TextDelta, Usage


def _render_content(blocks: list[ContentBlock]) -> str:
    """Deterministic text projection of a content-block list.

    The Codex transport is text-only. Text is preserved verbatim; tool calls and
    results, images, and documents degrade to stable bracketed markers so nothing
    is silently lost. Thinking blocks are intentionally dropped: their signature
    is provider-private and replayable only to the originating provider (plan
    section 3.1/3.3).
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            parts.append(block.text)
        elif isinstance(block, ToolUse):
            arguments = json.dumps(block.input, sort_keys=True, ensure_ascii=False)
            parts.append(f"[tool_use {block.name} {arguments}]")
        elif isinstance(block, ToolResult):
            label = "tool_result_error" if block.is_error else "tool_result"
            parts.append(f"[{label} {_render_content(block.content)}]")
        elif isinstance(block, Image):
            reference = (
                f"data:{block.media_type}" if block.data is not None else (block.url or block.media_type)
            )
            parts.append(f"[image {reference}]")
        elif isinstance(block, Document):
            title = f" {block.title}" if block.title else ""
            parts.append(f"[document {block.media_type}{title}]")
        # Thinking: dropped by policy; see docstring.
    return "".join(parts)


def request_to_prompt(req: ModelRequest) -> str:
    """Render a structured request into the legacy Codex prompt JSON.

    The final user message is the active request (``user``); every earlier
    message becomes ``history``. The final user message is therefore never
    duplicated. ``metadata["prompt"]`` overrides rendering entirely, which lets
    callers pass a pre-built prompt while the adapter and transport stay
    identical.
    """
    override = req.metadata.get("prompt") if req.metadata else None
    if isinstance(override, str):
        return override
    messages = list(req.messages)
    user = ""
    prior = messages
    if messages and messages[-1].role == "user":
        user = _render_content(messages[-1].content)
        prior = messages[:-1]
    history = [
        {"role": message.role, "text": _render_content(message.content)}
        for message in prior
    ]
    return json.dumps(
        {"instructions": req.system or "", "history": history, "user": user},
        ensure_ascii=False,
    )


def _usage_from(payload: Any) -> Usage | None:
    """Best-effort conversion of a provider usage block; None when malformed."""
    if not isinstance(payload, Mapping):
        return None
    try:
        return Usage(
            input=int(payload.get("input_tokens", 0) or 0),
            output=int(payload.get("output_tokens", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


class LegacyCodexCLIProvider:
    """A :class:`Provider` facade over the existing Codex CLI transport."""

    name = "legacy-codex-cli"

    def __init__(self, *, workspace: str | Path, config: Config | None = None):
        self.workspace = Path(workspace)
        self.config = config if config is not None else Config()
        self._closed = False

    def capabilities(self, model: str) -> Capabilities:
        # Codex owns its own tool loop, so Nexus-native tools are never offered
        # to it, and it exposes no on-demand token-counting API. Streamed usage
        # is still surfaced as Usage events when the transport reports it.
        return Capabilities.conservative()

    async def count_tokens(self, req: ModelRequest) -> int | None:
        return None

    def stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(req)

    async def _stream(self, req: ModelRequest) -> AsyncIterator[StreamEvent]:
        if self._closed:
            raise ProviderError("Provider is closed")
        prompt = request_to_prompt(req)
        yield MessageStart(model=req.model or self.config.model, provider=self.name)
        transport = CodexProvider()
        # aclosing guarantees the inner generator's finally block runs (and so
        # the subprocess group is terminated) as soon as this adapter's stream is
        # closed or cancelled, instead of waiting for garbage collection.
        async with aclosing(
            transport.stream(prompt, workspace=self.workspace, config=self.config)
        ) as events:
            async for event in events:
                raw: Any = event.data
                if event.type == "message":
                    yield TextDelta(text=str(raw["text"]))
                elif event.type == "provider" and isinstance(raw, dict):
                    usage = _usage_from(raw.get("usage"))
                    if usage is not None:
                        yield usage
                    yield Raw(data=raw)
        yield MessageStop(stop_reason="end_turn")

    async def aclose(self) -> None:
        # Each stream owns and tears down its own subprocess, so closing the
        # adapter is idempotent bookkeeping only.
        self._closed = True


__all__ = ["LegacyCodexCLIProvider", "request_to_prompt"]
