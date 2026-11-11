"""Tool shape and the shared workspace path check.

A tool is a `ToolSpec` (the schema and description sent with the request) and an
async `run(args, ctx)` that returns text or raises `ToolError`. The spec is the
only place a tool is described: the system prompt does not repeat it, so a
disabled tool is never described anywhere the model can see. Every output is
bounded, and any clipping is stated in the output itself.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from ..llmhub.types import Media, ToolCall, ToolSpec

MAX_OUTPUT_CHARS = 30_000


class ToolError(Exception):
    """A failure reported back to the model as an error result."""


class ToolOutput(str):
    """A tool's text plus images it returned (`Media`). Still a `str`, so anything that only wants text, such as a
    code cell, a hook or a clip, treats it as one; the loop sends `media` along with the result."""

    media: tuple[Media, ...]

    def __new__(cls, text: str, media: tuple[Media, ...] = ()) -> Self:
        self = super().__new__(cls, text)
        self.media = media
        return self


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    call_id: str | None = None  # the model's tool call id, for tools that record it
    file_lock: asyncio.Lock | None = None  # serializes file mutations; None outside a session
    model_visible: bool = True  # False for calls retained inside a code kernel
    cwd_changed: bool = False  # the session moved; report absolute paths and bash's cwd
    save_media: Callable[[bytes, str, str], str] | None = None  # (bytes, mime, name) -> artifact id; None outside a session
    execute: Callable[[ToolCall, dict[str, Any], ToolContext], Awaitable[str]] | None = None


@dataclass(frozen=True, slots=True)
class Tool:
    spec: ToolSpec
    run: Callable[[dict[str, Any], ToolContext], Awaitable[str]]


def resolve(ctx: ToolContext, path: Any, *, outside: bool = False) -> Path:
    """A path inside the workspace; relative paths are relative to it. `outside` allows any path (reads only)."""
    if not isinstance(path, str) or not path:
        raise ToolError("path must be a non-empty string")
    root = ctx.workspace.resolve()
    target = (root / path).resolve()
    if not outside and target != root and root not in target.parents:
        raise ToolError(f"{path} is outside the workspace {root}")
    return target


MAX_PATCH_CHARS = 1_000_000


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n[… clipped {len(text) - limit} of {len(text)} characters …]\n{tail}"

