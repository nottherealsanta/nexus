"""The stage: the one card the Manager is showing (MANAGER_PLAN.md 10.4 and 10.7).

A card is a typed payload that Core builds and validates and each client draws
natively; there is no free HTML. The Manager's `show` tool names *what* to show
and Core fills the facts from the source, so a card can never carry words the
Manager invented in a session's name:

* ``approval`` and ``question`` read the pending request itself (the exact
  command or diff, the real options);
* ``excerpt`` is cut from the session's own stored text, flagged when clipped;
* ``board`` and ``sessions`` rows come from the retriever and the unified state;
* only ``diagram`` and ``note`` carry the Manager's own text, bounded.

Exactly the shapes of 10.7 leave this module (``Card.payload``). One card at a
time: ``show`` replaces, ``clear`` removes, and everything is bounded with the
clipping stated in the card (``more``, ``clipped``).
"""
from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

CARD_TYPES = ("board", "sessions", "approval", "question", "diagram", "excerpt", "note")

#: Rows per board column and in a sessions card before "+N more".
BOARD_ROWS = 4
SESSION_ROWS = 8
TITLE_CHARS = 80
APPROVAL_SUMMARY_CHARS = 200
APPROVAL_DETAIL_CHARS = 4000
QUESTION_CHARS = 600
OPTION_CHARS = 100
MAX_OPTIONS = 10
DIAGRAM_CHARS = 4000
EXCERPT_CHARS = 1200
NOTE_CHARS = 1500


class CardError(ValueError):
    """The Manager asked for a card Core cannot build; the message goes back to the model."""


def clip(text: str, limit: int) -> tuple[str, bool]:
    """``text`` cut to ``limit`` characters; the flag says it was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)].rstrip() + "…", True


def row(n: int, session: Mapping[str, Any], *, why: str | None = None) -> dict[str, Any]:
    """One numbered row: ``{n, session_id, handle, title, project, state, why?}``."""
    title, _ = clip(str(session.get("title") or ""), TITLE_CHARS)
    out = {"n": n, "session_id": session["session_id"], "handle": session["handle"], "title": title,
           "project": str(session.get("project") or ""), "state": str(session.get("state") or "unknown")}
    if why:
        out["why"] = clip(why, 120)[0]
    return out


def board(columns: Mapping[str, list[dict[str, Any]]], *, project: str | None = None,
          rows_per_column: int = BOARD_ROWS) -> dict[str, Any]:
    """Needs you / running / finished. ``n`` numbers across the whole card, so "2" is one row."""
    shown: dict[str, list[dict[str, Any]]] = {}
    more: dict[str, int] = {}
    n = 0
    for name in ("needs_you", "running", "finished"):
        items = list(columns.get(name, ()))
        shown[name] = []
        for item in items[:rows_per_column]:
            n += 1
            shown[name].append(row(n, item, why=item.get("why")))
        more[name] = max(0, len(items) - rows_per_column)
    payload: dict[str, Any] = {"type": "board", "columns": shown, "more": more}
    if project:
        payload["project"] = project
    return payload


def sessions(items: list[dict[str, Any]], *, limit: int = SESSION_ROWS, more: int = 0) -> dict[str, Any]:
    rows = [row(i, item, why=item.get("why")) for i, item in enumerate(items[:limit], 1)]
    return {"type": "sessions", "rows": rows, "more": max(0, len(items) - limit) + max(0, more)}


def approval(*, session_id: str, handle: str, approval_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """The pending permission request as the user needs to see it: the tool and the exact call."""
    tool = str(request.get("tool") or "tool")
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    if tool == "bash" and isinstance(args.get("command"), str):
        summary = f"run: {args['command'].strip().splitlines()[0] if args['command'].strip() else ''}"
        detail = args["command"]
    elif isinstance(args.get("path"), str) or isinstance(args.get("file_path"), str):
        target = args.get("path") or args.get("file_path")
        summary = f"{tool}: {target}"
        detail = _render_args(args)
    else:
        summary = f"{tool}"
        detail = _render_args(args)
    summary, summary_cut = clip(summary, APPROVAL_SUMMARY_CHARS)
    detail, detail_cut = clip(detail, APPROVAL_DETAIL_CHARS)
    return {"type": "approval", "session_id": session_id, "handle": handle, "approval_id": approval_id,
            "summary": summary, "detail": detail, "clipped": summary_cut or detail_cut}


def _render_args(args: Mapping[str, Any]) -> str:
    import json

    return json.dumps(args, indent=2, ensure_ascii=False, default=str) if args else ""


def question(*, session_id: str, handle: str, request: Mapping[str, Any]) -> dict[str, Any]:
    """The pending ``ask``: its first question and that question's option labels.

    A request with several questions shows the first and says how many follow.
    """
    items = [q for q in request.get("questions", []) if isinstance(q, dict)]
    if not items:
        raise CardError("that question request has no questions")
    first = items[0]
    text = str(first.get("question") or "")
    if len(items) > 1:
        text += f"\n(and {len(items) - 1} more question{'s' if len(items) > 2 else ''})"
    text, _ = clip(text, QUESTION_CHARS)
    options = [clip(str(o.get("label") or o.get("id") or ""), OPTION_CHARS)[0]
               for o in first.get("options", []) if isinstance(o, dict)][:MAX_OPTIONS]
    return {"type": "question", "session_id": session_id, "handle": handle, "question": text, "options": options}


def diagram(source: Any) -> dict[str, Any]:
    """A Mermaid diagram the Manager wrote. Too long is refused rather than cut: half a diagram is a wrong one."""
    if not isinstance(source, str) or not source.strip():
        raise CardError("diagram needs a non-empty Mermaid source string")
    source = source.strip()
    if source.startswith("```"):
        lines = source.splitlines()
        source = "\n".join(lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]).strip()
    if len(source) > DIAGRAM_CHARS:
        raise CardError(f"the diagram is {len(source)} characters; the limit is {DIAGRAM_CHARS}. "
                        "Draw a smaller one and offer to show the rest")
    return {"type": "diagram", "source": source, "clipped": False}


def excerpt(*, handle: str, session_id: str, text: str) -> dict[str, Any]:
    markdown, cut = clip(text, EXCERPT_CHARS)
    if not markdown:
        raise CardError("there is no text to excerpt")
    return {"type": "excerpt", "handle": handle, "session_id": session_id, "markdown": markdown, "clipped": cut}


def note(markdown: Any) -> dict[str, Any]:
    if not isinstance(markdown, str) or not markdown.strip():
        raise CardError("note needs non-empty markdown text")
    text, cut = clip(markdown, NOTE_CHARS)
    return {"type": "note", "markdown": text, "clipped": cut}


@dataclass(frozen=True, slots=True)
class Card:
    """A built card with its identity. ``payload`` is the exact 10.7 shape plus ``id``, ``focused``, ``title?``."""

    payload: dict[str, Any]

    @property
    def id(self) -> str:
        return self.payload["id"]

    @property
    def type(self) -> str:
        return self.payload["type"]

    def rows(self) -> list[dict[str, Any]]:
        """Every numbered row of a board or sessions card, in ``n`` order."""
        if self.type == "board":
            return sorted((r for column in self.payload["columns"].values() for r in column), key=lambda r: r["n"])
        if self.type == "sessions":
            return list(self.payload["rows"])
        return []


def finish(body: dict[str, Any], *, focused: bool, title: Any = None) -> Card:
    """Give a built body its card identity."""
    payload = {"id": f"card_{secrets.token_hex(6)}", "focused": bool(focused), **body}
    if isinstance(title, str) and title.strip():
        payload["title"] = clip(title, TITLE_CHARS)[0]
    return Card(payload)


class Stage:
    """The current card and what the user did to it. Nothing here is durable.

    ``publish`` is called with the new card payload (or ``None``) whenever the
    card changes; the service turns that into a ``manager.card`` event.
    """

    def __init__(self, publish: Callable[[dict[str, Any] | None], None]) -> None:
        self._publish = publish
        self.card: Card | None = None
        #: Set when the user closed the card, until the next delta reports it.
        self.dismissed: str | None = None

    def show(self, card: Card) -> Card:
        self.card = card
        self.dismissed = None
        self._publish(card.payload)
        return card

    def clear(self) -> bool:
        """Remove the card. Returns whether there was one."""
        if self.card is None:
            return False
        self.card = None
        self._publish(None)
        return True

    def dismiss(self, card_id: str) -> bool:
        """The user closed the card. A stale id (an older card) changes nothing."""
        if self.card is None or self.card.id != card_id:
            return False
        self.dismissed = f"{self.card.type} card"
        self.card = None
        self._publish(None)
        return True

    def take_dismissed(self) -> str | None:
        value, self.dismissed = self.dismissed, None
        return value

    def row(self, n: int) -> dict[str, Any] | None:
        """The row the user saw numbered ``n`` on the current card, if any."""
        if self.card is None:
            return None
        return next((r for r in self.card.rows() if r["n"] == n), None)

    @property
    def payload(self) -> dict[str, Any] | None:
        return self.card.payload if self.card else None
