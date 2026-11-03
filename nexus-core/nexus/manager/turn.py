"""One Manager turn: what began it, what it may do, and how its words become speech.

``TurnState`` carries the authority of section 9. A turn that begins with the
user's own words (``source`` ``typed`` or ``speech``) may act; one that does not
(a notification wake, ``source`` ``wake``) may only inform and ask. The tools in
``tools.py`` check this in code, so it holds when the prompt fails.

``SentenceSplitter`` turns the model's streamed text into whole sentences for
``manager.speech``, using the voice stack's ``text_chunks`` for the length cap
(imported lazily so Core can run without the voice worker's dependencies).
"""
from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import ClassVar

#: Sources that carry the user's own words. Everything else is informational only.
USER_SOURCES = ("typed", "speech")
SOURCES = (*USER_SOURCES, "wake")


@dataclass
class TurnState:
    """The Manager's current turn."""

    turn_id: str
    source: str
    heard: str
    index: int  # utterance counter within the call; an approval must have been shown at a lower index
    episode_id: str | None = None
    runtime_turn_id: str | None = None
    acted: bool = False  # a tool has changed a session: the turn can no longer be cancelled safely
    muted: bool = False  # barge-in: stop speaking, keep working
    cancelled: bool = False
    seq: int = 0  # next speech sequence number
    spoken: list[str] = field(default_factory=list)
    decisions: int = 0
    delegation_id: str | None = None  # GPT-Live: the hand-off this turn answers
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def authority(self) -> bool:
        """Whether this turn began from the user's own words."""
        return self.source in USER_SOURCES


class RateLimiter:
    """Caps on acts, per turn and per minute, against a runaway loop or a bad transcript."""

    PER_TURN: ClassVar[dict[str, int]] = {
        "start_session": 2, "send_to_session": 5, "interrupt_session": 3, "archive": 3,
        "answer_question": 1, "decide_approval": 1}
    TURN_TOTAL = 8
    PER_MINUTE = 12
    PER_MINUTE_KIND: ClassVar[dict[str, int]] = {"start_session": 6, "send_to_session": 10}

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._times: deque[float] = deque()
        self._kinds: deque[tuple[float, str]] = deque()

    def check(self, turn: TurnState, kind: str) -> str | None:
        """A reason this act is refused, or ``None``. Does not count it."""
        limit = self.PER_TURN.get(kind)
        if limit is not None and turn.counts.get(kind, 0) >= limit:
            return f"too many {kind.replace('_', ' ')} acts in one turn (limit {limit}); tell the user and wait"
        if sum(turn.counts.values()) >= self.TURN_TOTAL:
            return f"too many actions in one turn (limit {self.TURN_TOTAL}); tell the user and wait"
        now = self.clock()
        while self._times and now - self._times[0] > 60:
            self._times.popleft()
        while self._kinds and now - self._kinds[0][0] > 60:
            self._kinds.popleft()
        cap = self.PER_MINUTE_KIND.get(kind)
        if cap is not None and sum(1 for _, k in self._kinds if k == kind) >= cap:
            return f"too many {kind.replace('_', ' ')} acts in the last minute (limit {cap}); tell the user and wait"
        if len(self._times) >= self.PER_MINUTE:
            return f"too many actions in the last minute (limit {self.PER_MINUTE}); tell the user and wait"
        return None

    def count(self, turn: TurnState, kind: str) -> None:
        turn.counts[kind] = turn.counts.get(kind, 0) + 1
        self._times.append(self.clock())
        self._kinds.append((self.clock(), kind))


_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")


class SentenceSplitter:
    """Feed streamed text; get whole sentences as soon as they end. ``flush`` returns the rest."""

    def __init__(self) -> None:
        self.buffer = ""

    def feed(self, text: str) -> Iterator[str]:
        self.buffer += text
        parts = _BOUNDARY.split(self.buffer)
        self.buffer = parts.pop()  # the last piece may still be growing
        for part in parts:
            yield from _chunks(part)

    def flush(self) -> Iterator[str]:
        rest, self.buffer = self.buffer, ""
        yield from _chunks(rest)

    def reset(self) -> None:
        self.buffer = ""


def _chunks(text: str) -> Iterator[str]:
    text = text.strip()
    if not text:
        return
    try:
        from ..voice.speech import text_chunks
    except ImportError:  # the voice stack is optional; fall back to one sentence unchanged
        yield text
        return
    yield from text_chunks(text)
