"""Tool shape and the shared workspace path check.

A tool is a `ToolSpec` (the schema and description sent with the request), its
`guidance` (how to use it, for the system prompt's tools section) and an async
`run(args, ctx)` that returns text or raises `ToolError`. Guidance is rendered
only for the tools a session actually has, so a disabled tool is never
described anywhere the model can see. Every output is bounded, and any
clipping is stated in the output itself.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..llmhub.types import ToolSpec

if TYPE_CHECKING:
    from ..observations import FileGuard

MAX_OUTPUT_CHARS = 30_000


class ToolError(Exception):
    """A failure reported back to the model as an error result."""


@dataclass(frozen=True, slots=True)
class ToolContext:
    workspace: Path
    call_id: str | None = None  # the model's tool call id, for tools that record it
    files: FileGuard | None = None  # the session's read observations; None outside a session
    model_visible: bool = True  # False for calls retained inside a code kernel
    cwd_changed: bool = False  # the session moved; report absolute paths and bash's cwd


@dataclass(frozen=True, slots=True)
class Tool:
    spec: ToolSpec
    run: Callable[[dict[str, Any], ToolContext], Awaitable[str]]
    guidance: str = ""  # one or two sentences for the system prompt; see agents.render_prompt


def resolve(ctx: ToolContext, path: Any) -> Path:
    """A path inside the workspace; relative paths are relative to it."""
    if not isinstance(path, str) or not path:
        raise ToolError("path must be a non-empty string")
    root = ctx.workspace.resolve()
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"{path} is outside the workspace {root}")
    return target


def clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n[… clipped {len(text) - limit} of {len(text)} characters …]\n{tail}"
