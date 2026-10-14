"""Projection of the running turn's status line (GROK_TUI_PLAN WS-D).

Contract: ``turn_status(view)`` is ``None`` while idle, else a bounded dict
``{activity, activity_since, turn_since, output_tokens, output_estimated}``. Times are epoch seconds
taken from durable event timestamps, so replay reproduces them; the native client
computes elapsed values against its own clock and redraws while the turn runs.
"""
from __future__ import annotations

from ...ui_support.text import escape_controls, sanitize

_RUNNING = {"running", "awaiting_permission", "awaiting_input"}


def _activity(turn, phase: str) -> tuple[str, float | None]:
    if phase == "awaiting_permission":
        return "Waiting for permission", turn.updated_ts
    if phase == "awaiting_input":
        return "Waiting for your answer", turn.updated_ts
    running = [tool for tool in turn.tools if tool.status == "running"]
    if running:
        tool = running[-1]
        label = f"Running {sanitize(tool.name, 40)}" if len(running) == 1 else f"Running {len(running)} tools"
        return label, tool.started_ts or tool.requested_ts or turn.updated_ts
    message = next((m for m in reversed(turn.messages) if m.role == "assistant"), None)
    if message is not None and not message.done and message.blocks:
        last = message.blocks[-1]
        if last.kind == "thinking" and not last.finalized:
            return "Thinking", last.started_ts or turn.updated_ts
        if last.kind == "text" and last.text:
            return "Writing", turn.updated_ts
    return "Waiting for response", turn.updated_ts or turn.started_ts


_CHARS_PER_TOKEN = 4
_MAX_ESTIMATE = 10_000_000


def _output_tokens(turn) -> tuple[int, bool]:
    """``(tokens, estimated)``: usage lands per model call, so while text/thinking streams the exact
    count lags. The estimate is chars/4 of every assistant text/thinking block this turn; it is shown
    only while it exceeds usage (no double counting) and is exact again once usage catches up."""
    exact = max(0, int(turn.usage.output_tokens or 0))
    chars = 0
    for message in turn.messages:
        if message.role == "assistant":
            chars += sum(len(block.text or "") for block in message.blocks if block.kind in ("text", "thinking"))
    guess = min(_MAX_ESTIMATE, -(-chars // _CHARS_PER_TOKEN))
    return (guess, True) if guess > exact else (exact, False)


def turn_status(view) -> dict | None:
    phase = getattr(view, "phase", "")
    turn = getattr(view, "active_turn", None)
    if phase not in _RUNNING or turn is None:
        return None
    label, since = _activity(turn, phase)
    started = turn.started_ts or since
    tokens, estimated = _output_tokens(turn)
    return {"activity": escape_controls(label), "activity_since": float(since or started or 0.0),
            "turn_since": float(started or 0.0),
            "output_tokens": tokens, "output_estimated": estimated}
