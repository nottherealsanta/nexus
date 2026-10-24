"""Code-mode kernel manifest for the compaction input (plan code-mode-kernel §3.9).

When a session that has code-mode kernels is compacted, the summarizer input gets one
bounded block per kernel: the kernel id, its attachments (servers, tools), approved context
targets, the cell count and the ``whos()`` table (names, types and sizes, never values). The
block tells the summary to keep it, so the model knows its variables still exist after
compaction and that ``code_start`` re-describes the kernel.

This module is pure data and rendering. The runtime supplies the blocks through a callable on
the assembler (see ``codemode/manifest.py``); ``context/`` never imports code mode.

Bounds: at most ``MAX_MANIFEST_VARIABLES`` variable rows per kernel, ``MAX_MANIFEST_NAMES``
tool or target names per kernel, and ``MAX_MANIFEST_CHARS`` characters for the whole block.
Every clip is announced in the text with its counts.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..model.message import Message, Text

MAX_MANIFEST_VARIABLES = 100
MAX_MANIFEST_CHARS = 4000
MAX_MANIFEST_NAMES = 40
MAX_MANIFEST_NAME_CHARS = 80
MAX_MANIFEST_PURPOSE_CHARS = 120

MANIFEST_OPEN = "<code_kernel_manifest>"
MANIFEST_CLOSE = "</code_kernel_manifest>"
MANIFEST_HEADER = (
    "Code-mode kernels of this session (names, types and sizes only; no values were captured). "
    "Keep this block in the summary: these variables still exist in the kernel after compaction, "
    "and code_start re-describes a running kernel (it changes nothing; code_attach adds tools)."
)
#: Reserved for the clip note at the end of a clipped block.
_CLIP_RESERVE = 160


@dataclass(frozen=True)
class KernelBlock:
    """One kernel's manifest facts. ``variables`` is ``None`` when they were not read."""

    kernel_id: str
    agent_path: str
    #: ``live``, ``evicted``, ``lost`` or ``not running``.
    status: str
    cells: int
    attached: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    purpose: str = ""
    variables: tuple[tuple[str, str, int | None], ...] | None = None
    #: Why ``variables`` is ``None`` (for example ``kernel evicted``).
    variables_note: str = ""


def _short(text: Any, limit: int) -> str:
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def _named_list(label: str, names: Sequence[str]) -> str | None:
    if not names:
        return None
    shown = [_short(name, MAX_MANIFEST_NAME_CHARS) for name in names[:MAX_MANIFEST_NAMES]]
    more = len(names) - len(shown)
    tail = f" and {more} more (not listed; manifest cap {MAX_MANIFEST_NAMES})" if more > 0 else ""
    return f"  {label} ({len(names)}): " + ", ".join(shown) + tail


def _block_lines(block: KernelBlock, max_variables: int) -> list[str]:
    head = (f"Kernel {_short(block.kernel_id, 64)} (agent {_short(block.agent_path, 80)}, {block.status}): "
            f"{block.cells} cell{'' if block.cells == 1 else 's'} run")
    if block.purpose:
        head += f". Purpose: {_short(block.purpose, MAX_MANIFEST_PURPOSE_CHARS)}"
    lines = [head]
    for line in (_named_list("Attached tools", block.attached),
                 _named_list("Servers", block.servers),
                 _named_list("Context targets", block.targets)):
        if line:
            lines.append(line)
    if block.variables is None:
        lines.append(f"  variables not listed ({block.variables_note or 'not read'})")
        return lines
    if not block.variables:
        lines.append("  Variables: none")
        return lines
    lines.append("  Variables (name, type, size):")
    rows = block.variables[:max_variables]
    for name, kind, size in rows:
        size_text = "" if size is None else f"  {size}"
        lines.append(f"    {_short(name, MAX_MANIFEST_NAME_CHARS)}  {_short(kind, 40)}{size_text}")
    if len(block.variables) > len(rows):
        lines.append(f"    ... {len(block.variables) - len(rows)} more variables not listed "
                     f"(manifest cap {max_variables}; {len(block.variables)} exist)")
    return lines


def render_kernel_manifest(blocks: Iterable[KernelBlock], *, max_variables: int = MAX_MANIFEST_VARIABLES,
                           max_chars: int = MAX_MANIFEST_CHARS) -> str:
    """The bounded manifest text, or ``""`` without kernels. Never contains a variable value."""
    items = list(blocks)
    if not items:
        return ""
    body = [MANIFEST_OPEN, MANIFEST_HEADER]
    for block in items:
        body.extend(_block_lines(block, max(0, int(max_variables))))
    # The closing tag is always written: the summarizer must see where the block ends.
    budget = max(0, max_chars - _CLIP_RESERVE - len(MANIFEST_CLOSE) - 1)
    kept: list[str] = []
    used = 0
    for line in body:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) == len(body):
        return "\n".join([*body, MANIFEST_CLOSE])
    note = (f"[kernel manifest clipped: {len(kept)} of {len(body)} lines shown within {max_chars} chars; "
            "the variables and names past that point are not listed]")
    return "\n".join([*kept, note, MANIFEST_CLOSE])


def manifest_message(blocks: Iterable[KernelBlock]) -> Message | None:
    """The summarizer-input message for ``blocks``, or ``None`` when there are no kernels."""
    text = render_kernel_manifest(blocks)
    if not text:
        return None
    return Message(role="user", content=[Text(text=text)])


__all__ = [
    "KernelBlock",
    "MANIFEST_CLOSE",
    "MANIFEST_OPEN",
    "MAX_MANIFEST_CHARS",
    "MAX_MANIFEST_VARIABLES",
    "manifest_message",
    "render_kernel_manifest",
]
