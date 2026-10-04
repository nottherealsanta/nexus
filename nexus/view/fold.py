"""Delta accumulation, dedup, and log folding for the pure view reducer.

A provider streams deltas and then one finalized event carrying the full text;
rendering both would print every response twice. This module owns the one place
that accumulates a delta run and folds the finalized event in without
duplication. :func:`fold` is deterministic and idempotent: a snapshot plus a tail
equals the whole log, because the reducer ignores an already applied ``seq``.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from ..events import Event
from .model import BlockView, ConversationView

__all__ = [
    "accumulate",
    "finalize_text",
    "finalize_thinking",
    "fold",
    "fold_into",
]

def accumulate(
    blocks: list[BlockView], kind: str, text: str, ts: float | None = None
) -> list[BlockView]:
    """Append a delta to the trailing block of ``kind``, or start a new one.

    Returns a new list; the input is never mutated. A finalized block is never
    extended, so a later delta begins a fresh block in wire order.
    """
    out = list(blocks)
    if out and out[-1].kind == kind and not out[-1].finalized:
        last = out[-1]
        out[-1] = replace(last, text=last.text + (text or ""), streamed=True)
    elif text:
        out.append(BlockView(kind=kind, text=text, streamed=True, started_ts=ts))
    return out

def finalize_text(blocks: list[BlockView], text: str) -> list[BlockView]:
    """Fold a finalized ``text`` event into ``blocks`` without duplicating it.

    If any block was built from deltas, those deltas already hold the full text,
    so the blocks are simply marked finalized. Only a response that streamed no
    text (a provider that emits a lone ``text``) appends the final text.
    """
    out = list(blocks)
    streamed = [
        i
        for i, block in enumerate(out)
        if block.kind == "text" and block.streamed and not block.finalized
    ]
    if streamed:
        for i in streamed:
            out[i] = replace(out[i], finalized=True)
        return out
    if text:
        out.append(BlockView(kind="text", text=text, finalized=True))
    return out

def finalize_thinking(
    blocks: list[BlockView], text: str, signature: str | None
) -> list[BlockView]:
    """Fold a finalized ``thinking`` event, attaching the signature once.

    Mirrors :func:`finalize_text`: a streamed run is finalized in place and the
    signature lands on its last block; a lone ``thinking`` event appends one.
    """
    out = list(blocks)
    streamed = [
        i
        for i, block in enumerate(out)
        if block.kind == "thinking" and block.streamed and not block.finalized
    ]
    if streamed:
        last = streamed[-1]
        for i in streamed:
            replacement = {"finalized": True}
            if i == last and signature is not None:
                replacement["signature"] = signature
            out[i] = replace(out[i], **replacement)
        return out
    # The loop publishes thinking.end as each run closes, then the legacy
    # aggregate thinking event at response completion. That aggregate must not
    # append the same text/signature a second time after every run was finalized.
    thinking = [block for block in out if block.kind == "thinking"]
    if (
        thinking
        and all(block.finalized for block in thinking)
        and text == "".join(block.text for block in thinking)
        and (
            any(block.streamed for block in thinking)
            or (not text and signature == thinking[-1].signature)
        )
    ):
        return out
    if text or signature is not None:
        out.append(
            BlockView(kind="thinking", text=text, signature=signature, finalized=True)
        )
    return out

def fold_into(
    state: ConversationView, events: Iterable[Event]
) -> ConversationView:
    """Apply ``events`` to ``state`` in order; the reducer's replay primitive."""
    from .reduce import apply

    view = state
    for event in events:
        view = apply(view, event)
    return view

def fold(
    events: Iterable[Event], state: ConversationView | None = None
) -> ConversationView:
    """Deterministically fold a whole log into a :class:`ConversationView`.

    ``state`` may be a previously folded prefix (a snapshot plus a tail), which
    is how a reconnecting view catches up without recomputing history.
    """
    return fold_into(state if state is not None else ConversationView(), events)
