"""Deterministic Manager machine memory and handoff brief (plan 10).

Two pure functions, no model and no I/O:

* :func:`extract_manager_memory` folds the Manager's own log into a small, redacted,
  bounded note: the roots it launched (id, role, launch time, outcome *as last
  observed* in the log), the messages it relayed, refusals by reason, and
  directives the human addressed to it. The note is stored in the SQLite ``kv`` table
  (never in ``<workspace>/MEMORY.md``) and injected into the Manager's system text.
* :func:`build_manager_brief` is the Manager's counterpart of
  :func:`~nexus.context.handoff.build_brief`: the Manager has no shell, file tools or
  worktrees, so the brief summarizes launches, relays, waits and the human's recent asks.

Sources are structured events and real user prompts only. Assistant prose is never read,
so a ``## Nexus State`` snapshot the model quoted cannot reach memory. Neither output
ever carries a session's *current* state, a count or an attention flag (plan 10
Invariant 6): that is the per-request snapshot's job.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..model.message import Message, Text, ToolResult, ToolUse
from ..util import redact_secrets
from .handoff import MAX_BRIEF_CHARS, safe_brief

__all__ = [
    "MAX_MEMORY_CHARS",
    "MEMORY_HEADER",
    "MEMORY_KEY",
    "STATE_POINTER",
    "build_manager_brief",
    "extract_manager_memory",
]

#: ``kv`` key (``main`` namespace) of the Manager's machine memory.
MEMORY_KEY = "manager_machine_memory"
MAX_MEMORY_CHARS = 4000
MEMORY_HEADER = "# Manager Memory (machine generated)"
STATE_POINTER = (
    "Current Nexus state is supplied fresh in the `## Nexus State` block on every request; "
    "do not rely on states mentioned earlier."
)

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DIRECTIVE = re.compile(
    r"\b(always|never|prefer(?:s|red)?|don'?t|do not|must|only|make sure|remember|from now on|"
    r"by default|avoid)\b",
    re.IGNORECASE,
)
#: Pasted snapshot or board lines in a user message are not directives.
_SNAPSHOT_LINE = re.compile(r"^(##\s|\[(?:working|review|done|idle|archived)\]|as_of\b|needs attention)", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_MAX_DIRECTIVES = 8
_MAX_DIRECTIVE_CHARS = 200
_MAX_MANAGED = 25
_MAX_RELAYS = 15
_DIRECTIVES_SECTION = "## Active Directives & Constraints"


def _clean(text: str, limit: int) -> str:
    kept = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(text))
    flat = " ".join(redact_secrets(kept).split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


def _session_id(value: Any) -> str:
    return value if isinstance(value, str) and _SESSION_ID.fullmatch(value) else ""


def _stamp(ts: float) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) or ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%MZ")


def _is_user_prompt(message: Message) -> bool:
    return (
        message.role == "user"
        and message.meta.notice is None
        and any(not isinstance(block, ToolResult) for block in message.content)
    )


def _text(message: Message) -> str:
    return "\n".join(block.text for block in message.content if isinstance(block, Text) and block.text)


@dataclass(slots=True)
class _Launch:
    child: str
    seq: int
    ts: float
    role: str
    origin: str
    wait: str = ""
    read: bool = False


@dataclass(slots=True)
class _Scan:
    launches: dict[str, _Launch] = field(default_factory=dict)
    routed: Counter = field(default_factory=Counter)
    refusals: Counter = field(default_factory=Counter)
    #: Chronological ``(seq, text)`` past events for the brief.
    events: list[tuple[int, str]] = field(default_factory=list)
    #: Directive sentences, newest first.
    directives: list[str] = field(default_factory=list)


def _scan(records: Iterable[Any], *, through: int | None = None) -> _Scan:
    from ..session.records import EventRecord, MessageRecord

    out = _Scan()
    seen: set[str] = set()
    ordered = [r for r in records if through is None or r.seq <= through]
    for record in ordered:
        if not isinstance(record, EventRecord):
            continue
        event = record.event
        data = event.data if isinstance(event.data, dict) else {}
        kind = event.type
        if kind == "session.linked" and data.get("kind") == "managed":
            child = _session_id(data.get("child"))
            if child and child not in out.launches:
                origin = data.get("origin_message_id")
                out.launches[child] = _Launch(
                    child, record.seq, record.ts, _clean(data.get("agent") or "", 40),
                    origin if isinstance(origin, str) and re.fullmatch(r"m\d{1,12}", origin) else "",
                )
                out.events.append((record.seq, f"launched {child}"))
        elif kind == "manager.wait":
            ids = [_session_id(item) for item in data.get("ids") or [] if _session_id(item)]
            outcome = _clean(data.get("outcome") or "", 20)
            if outcome and ids:
                for child in ids:
                    if child in out.launches:
                        out.launches[child].wait = outcome
                out.events.append((record.seq, f"wait on {', '.join(ids[:5])} ended: {outcome}"))
        elif kind == "manager.result_read":
            child = _session_id(data.get("session"))
            if child:
                if child in out.launches:
                    out.launches[child].read = True
                out.events.append((record.seq, f"read the result of {child}"))
        elif kind == "manager.cancelled":
            child = _session_id(data.get("session"))
            if child:
                out.events.append((record.seq, f"cancelled {child}"))
        elif kind == "manager.routed":
            target = _session_id(data.get("target"))
            if target:
                out.routed[target] += 1
                out.events.append((record.seq, f"relayed a message to {target}"))
        elif kind in ("manager.launch_refused", "manager.send_refused", "manager.show_refused"):
            reason = _clean(data.get("reason") or "error", 30)
            out.refusals[f"{kind.split('.')[1].split('_')[0]}: {reason}"] += 1
    for record in reversed(ordered):
        if not isinstance(record, MessageRecord) or not _is_user_prompt(record.message):
            continue
        body = _text(record.message)
        if body.startswith("Voice request"):
            continue  # a Live delegation carries model-authored lines; never a source
        for sentence in reversed(_SENTENCES.split(body)):
            line = _clean(sentence, _MAX_DIRECTIVE_CHARS)
            if (
                len(line) < 8
                or _SNAPSHOT_LINE.match(line)
                or not _DIRECTIVE.search(line)
                or line.casefold() in seen
            ):
                continue
            seen.add(line.casefold())
            out.directives.append(line)
            if len(out.directives) >= _MAX_DIRECTIVES:
                break
        if len(out.directives) >= _MAX_DIRECTIVES:
            break
    return out


def _existing_directives(note: str) -> list[str]:
    lines: list[str] = []
    active = False
    for raw in note.splitlines():
        if raw.startswith("## "):
            active = raw.strip() == _DIRECTIVES_SECTION
        elif active and raw.startswith("- "):
            line = _clean(raw[2:], _MAX_DIRECTIVE_CHARS)
            if line and not line.startswith("(+"):
                lines.append(line)
    return lines


def _launch_line(item: _Launch) -> str:
    parts = [f"- {item.child}"]
    if item.role:
        parts.append(f"[{item.role}]")
    when = _stamp(item.ts)
    launched = f"launched {item.origin}" if item.origin else "launched"
    parts.append(f"{launched} at {when}" if when else launched)
    line = " ".join(parts)
    observed = []
    if item.wait:
        observed.append(f"in a wait that ended: {item.wait}")
    if item.read:
        observed.append("result read")
    if observed:
        line += "; last observed: " + ", ".join(observed)
    return line


def _render(managed: list[str], relays: list[str], refusals: list[str], directives: list[str],
            omitted: dict[str, int]) -> str:
    def block(title: str, rows: list[str], key: str) -> list[str]:
        if not rows and not omitted.get(key):
            return []
        extra = [f"- (+{omitted[key]} older omitted; see the manager history)"] if omitted.get(key) else []
        return [title, *extra, *rows, ""]

    lines = [
        MEMORY_HEADER,
        'What the manager did and was told, from its own log. It holds no live state: the '
        '`## Nexus State` block wins for "now".',
        "",
        *block("## Managed Sessions (ids only; current state is in the live Nexus State block)", managed, "m"),
        *block("## Relay Links", relays, "r"),
        *block("## Refusals", refusals, "f"),
        *block(_DIRECTIVES_SECTION, directives, "d"),
    ]
    return "\n".join(lines).rstrip() + "\n"


def extract_manager_memory(
    records: Sequence[Any],
    existing_memory: str = "",
    *,
    max_chars: int = MAX_MEMORY_CHARS,
) -> str:
    """The Manager's bounded, redacted machine-memory note for ``records``.

    Deterministic: the same records and previous note give the same text. The log is
    re-read in full on each run (history is never deleted), so ``existing_memory``
    only contributes directives that the newest ones no longer cover, up to the cap.
    """
    scan = _scan(records)
    managed = [_launch_line(item) for item in sorted(scan.launches.values(), key=lambda i: i.seq)]
    relays = [
        f"- Routed to {target} ({count} message{'s' if count != 1 else ''})"
        for target, count in scan.routed.items()
    ]
    refusals = [f"- {name} x{count}" for name, count in sorted(scan.refusals.items())]
    directives = list(scan.directives)
    known = {line.casefold() for line in directives}
    for line in _existing_directives(existing_memory):
        if len(directives) >= _MAX_DIRECTIVES:
            break
        if line.casefold() not in known:
            known.add(line.casefold())
            directives.append(line)
    omitted = {"m": 0, "r": 0, "f": 0, "d": 0}
    if len(managed) > _MAX_MANAGED:
        omitted["m"] = len(managed) - _MAX_MANAGED
        managed = managed[-_MAX_MANAGED:]
    if len(relays) > _MAX_RELAYS:
        omitted["r"] = len(relays) - _MAX_RELAYS
        relays = relays[-_MAX_RELAYS:]
    # Shrink the heaviest section one entry at a time (oldest first) until the note fits.
    sections = {"m": managed, "r": relays, "f": refusals, "d": directives}
    text = _render(managed, relays, refusals, directives, omitted)
    while len(text) > max_chars:
        key = max((k for k, rows in sections.items() if rows), key=lambda k: sum(map(len, sections[k])), default=None)
        if key is None:
            break
        rows = sections[key]
        rows.pop(0 if key in ("m", "r") else -1)
        omitted[key] += 1
        text = _render(managed, relays, refusals, directives, omitted)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 14)].rstrip() + "\n[truncated]"
    return text


def build_manager_brief(
    messages: Sequence[Message],
    handle: Any,
    boundary: int,
    memory_note: str = "",
) -> str:
    """The deterministic handoff brief for a Manager cut at ``boundary``.

    ``messages`` are the retired model messages. Past outcomes are phrased as past events
    ("wait on s_x ended: completed"); no current state, count or attention flag appears.
    """
    asks = [_clean(_text(m), 300) for m in messages if _is_user_prompt(m) and not _text(m).startswith("Voice request")]
    asks = [a for a in asks if a]
    calls = Counter(b.name for m in messages for b in m.content if isinstance(b, ToolUse))
    events: list[tuple[int, str]] = []
    reader = getattr(handle, "read", None)
    if callable(reader):
        try:
            events = _scan(reader().records, through=max(0, boundary - 1)).events
        except Exception:  # noqa: BLE001 - the brief is still useful without the event list
            events = []
    parts = [
        "Manager handoff brief v1 (redacted extract; consult history for details).",
        f"Earlier history: this session, seq 1..{max(0, boundary - 1)} (not deleted).",
        STATE_POINTER,
        "Manager activity in the retired context: "
        + (", ".join(f"{name} x{count}" for name, count in sorted(calls.items())) or "no tool calls") + ".",
        "First user intent: " + (asks[0] if asks else "(none)"),
        "Latest user requests:",
        *(f"- {ask}" for ask in asks[-3:]),
        "Past events (oldest first):",
        *(f"- {text}" for _, text in events[-10:]),
    ]
    if memory_note.strip():
        parts.append("Machine memory (ids, relays, directives) is supplied in the `## Manager Memory` system block.")
    return safe_brief("\n".join(parts))[:MAX_BRIEF_CHARS]
