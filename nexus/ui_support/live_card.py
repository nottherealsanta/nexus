"""Rows of the GPT Live timeline card (GPT_LIVE_PLAN section 5; docs/live.md).

A pure projection of one ``LiveCallView`` (folded from the durable ``live.*`` events,
so replay equals live state) into labelled, toned rows that any client can draw:
spoken captions, delegations ("Sent to agent"), appends ("Told voice") and the
started/ended rows. Nothing is dropped: commentary is collapsed to a counted row until
the card is opened, long text is clipped only while collapsed and the clip is announced.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .text import sanitize

#: Collapsed cards clip a single row's text here (and say so); opened cards show it all.
COLLAPSED_CLIP = 600
#: Hard bound per card, a guard on top of the reducer's own bounds.
MAX_ROWS = 600

TONES = ("you", "voice", "caption", "sent", "told", "note", "dim", "bad")


@dataclass(frozen=True)
class CardRow:
    tone: str
    label: str
    text: str


def card_title(call: Any) -> str:
    """The text beside the native ``Live`` chip: model and voice."""
    parts = [value for value in (call.model, call.voice) if value]
    return " · ".join(parts) or "call"


def card_status(call: Any) -> str:
    if call.status == "ended":
        return "ended"
    return call.phase or "active"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


def _clip(text: str, expanded: bool) -> str:
    if expanded or len(text) <= COLLAPSED_CLIP:
        return text
    return text[:COLLAPSED_CLIP].rstrip() + f" … +{len(text) - COLLAPSED_CLIP} more characters · open the card to read all"


def _lines(label: str, text: str, tone: str) -> list[CardRow]:
    """One row per line of ``text``; only the first carries the label."""
    rows: list[CardRow] = []
    for number, line in enumerate((text or "").split("\n") or [""]):
        rows.append(CardRow(tone, label if number == 0 else "", line.replace("\t", "    ")))
    return rows


def card_rows(call: Any, *, expanded: bool = False, turns: Mapping[str, int] | None = None) -> list[CardRow]:
    """Chronological rows of one call. ``turns`` maps a delegation id to the turn number of
    the user message it created, for the "see turn #N" link."""
    turns = turns or {}
    started = [f"instructions: {call.instructions_source or 'bundled'}"]
    started.append("new conversation" if call.new_conversation else
                   f"{call.carried_turns} earlier turn{'s' if call.carried_turns != 1 else ''} carried over")
    rows = _lines("Started", " · ".join(started), "dim")
    if call.instructions:
        if expanded:
            rows.extend(CardRow("dim", "", "  " + line) for line in call.instructions.split("\n"))
        else:
            rows.append(CardRow("dim", "", f"  ({len(call.instructions)} characters of voice instructions hidden · open the card to read)"))

    items: list[tuple[float, int, int, Any]] = []
    for index, row in enumerate(call.transcript):
        items.append((row.ts or 0.0, 0, index, row))
    for index, row in enumerate(call.delegations):
        items.append((row.ts or 0.0, 1, index, row))
    commentary = [row for row in call.appends if row.channel == "commentary"]
    for index, row in enumerate(call.appends):
        items.append((row.ts or 0.0, 2, index, row))
    items.sort(key=lambda item: item[:3])

    summarized = False
    for _ts, kind, _index, row in items:
        if kind == 0:
            who, tone = ("You", "you") if row.role == "user" else ("Voice", "voice")
            text = row.text + (" [truncated]" if row.truncated else "")
            rows.extend(_lines(who, _clip(text, expanded), tone))
        elif kind == 1:
            label = f"Sent to agent ({row.mode})"
            number = turns.get(row.delegation_id)
            text = row.text + (f"  → turn #{number}" if number else "")
            rows.extend(_lines(label, _clip(text, expanded), "sent"))
        elif row.channel == "commentary" and not expanded:
            if not summarized:
                summarized = True
                count = len(commentary)
                rows.append(CardRow(
                    "note", "Told voice",
                    f"{count} progress note{'s' if count != 1 else ''} (commentary) hidden · open the card to read"))
        else:
            tag = {"commentary": "commentary", "final": "final", "notice": "notice"}.get(row.channel, row.channel)
            rows.extend(_lines(f"Told voice [{tag}]", _clip(row.text, expanded), "told"))

    if call.status == "active" and call.caption and call.caption.get("text"):
        who, tone = ("You…", "caption") if call.caption.get("role") == "user" else ("Voice…", "caption")
        rows.extend(_lines(who, call.caption["text"], tone))
    for message in call.errors:
        rows.extend(_lines("Error", message, "bad"))
    if call.status == "ended":
        detail = [call.end_reason or "ended"]
        if call.duration_s is not None:
            detail.append(_duration(call.duration_s))
        if call.error:
            detail.append(f"error: {call.error}")
        rows.extend(_lines("Ended", " · ".join(detail), "bad" if call.error else "dim"))
    if len(rows) > MAX_ROWS:
        hidden = len(rows) - MAX_ROWS
        rows = [*rows[:MAX_ROWS], CardRow("note", "", f"… {hidden} more rows not shown")]
    return rows


def encode(rows: list[CardRow]) -> str:
    """``tone TAB label TAB text`` per line, the wire form the native client draws."""
    return "\n".join(f"{row.tone}\t{sanitize(row.label, 80)}\t{row.text}" for row in rows)
