"""Attention: when the Manager speaks up unprompted (MANAGER_PLAN.md section 8, 10.1).

A bus subscriber with **no model**. It watches the daemon's events while a call is running, keeps a ranked queue of
things the user may want to know, and turns them into short deterministic lines:

| Priority | Item | Rule |
| --- | --- | --- |
| 1 | an approval or a question is waiting | spoken within about a second; also at a sentence break of the Manager's own reply |
| 2 | a session failed; a child escalated | spoken at the next pause |
| 3 | a session finished | only at `proactive = "all"`, coalesced on a lull ("3 sessions finished") |

Rules enforced here:

* **Never over the user.** Nothing is emitted while the client reports the microphone open (`listening`). Only
  priority 1 goes while the client is still playing speech, and while the Manager is mid-reply it goes only at a
  sentence break (`on_sentence_break`, called after each `manager.speech`), never in the middle of one.
* **At most one line per `[manager] proactive_interval_seconds`** (default 20). Items arriving close together are
  coalesced into one line; the highest priority present is spoken first.
* **No authority, no screen movement.** A line is not a turn: it has no user words, so nothing it says can be acted
  on until the user speaks, and attention never navigates. The only card it raises is the unfocused `approval` or
  `question` card of a priority-1 item (`focused: false`), and never over a focused card.
* **The context records it.** What was said and shown is written to the audit (`proactive` rows) and added to the
  next turn's input, so the Manager knows what the user heard and saw. A priority-1 item counts as *presented* for the
  approval guards once the user speaks after it.
* **`proactive`**: `off` emits nothing; `attention` priorities 1 and 2; `all` adds 3.
* On joining a call the Manager opens with what is pending (approvals, questions, blocked sessions) in one line, or
  says nothing when nothing is.

Lines are `manager.speech` events with `proactive: true` (an addition to the 10.7 data), a turn id of the form
`mp_<hex>` and `urgent: true` when they were slipped into a reply. They are text-redacted like all speech.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import stage as cards
from .redact import redact
from .tools import fingerprint
from .turn import SentenceSplitter

if TYPE_CHECKING:
    from .service import ManagerService

log = logging.getLogger(__name__)

APPROVAL, QUESTION, FAILED, BLOCKED, FINISHED = "approval", "question", "failed", "blocked", "finished"
PRIORITY = {APPROVAL: 1, QUESTION: 1, FAILED: 2, BLOCKED: 2, FINISHED: 3}
#: Seconds an item waits so that near-simultaneous ones become one line.
WINDOWS = {1: 1.0, 2: 3.0, 3: 8.0}
#: Seconds without user activity that count as a lull for priority 3.
LULL = 8.0
TICK = 0.25
NUMBERS = {2: "Two", 3: "Three", 4: "Four", 5: "Five"}


@dataclass
class Item:
    key: str
    kind: str
    session_id: str
    created: float
    ref: str | None = None  # the approval or question id
    spoken: bool = False

    @property
    def priority(self) -> int:
        return PRIORITY[self.kind]


class Attention:
    def __init__(self, service: ManagerService) -> None:
        self.svc = service
        self.items: dict[str, Item] = {}
        self.last_spoken = float("-inf")
        self.auto = True  # tests turn the background loop off and call `process` themselves
        self.windows = dict(WINDOWS)
        self.lull = LULL
        self._subscription: Any = None
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        """Begin watching for the current call, and open with what is pending."""
        self.stop()
        self.items.clear()
        self.last_spoken = float("-inf")
        self._scan_pending()
        if self.auto:
            self._subscription = self.svc.bus.open()
            self._task = asyncio.ensure_future(self._run(self._subscription))
            self.process()

    def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.items.clear()

    async def _run(self, subscription: Any) -> None:
        while True:
            try:
                event = await asyncio.wait_for(subscription.get(), TICK)
            except TimeoutError:
                event = False
            if event is None:
                return
            try:
                if event:
                    self.ingest(event)
                self.process()
            except Exception:
                log.exception("attention failed")

    # -- the queue --------------------------------------------------------------------

    def _add(self, kind: str, session_id: str, ref: str | None, *, early: bool = False) -> None:
        if self.svc.store.is_hidden(session_id):
            return
        key = f"{kind}:{ref or session_id}"
        if key in self.items:
            return
        now = self.svc.clock()
        created = now - self.windows[PRIORITY[kind]] if early else now
        self.items[key] = Item(key, kind, session_id, created, ref)

    def _scan_pending(self) -> None:
        runtime = self.svc.runtime
        for approval_id, (session_id, _, _) in runtime.approvals.items():
            self._add(APPROVAL, session_id, approval_id, early=True)
        for question_id, request in runtime.questions.items():
            self._add(QUESTION, request.session_id, question_id, early=True)
        for child_id, blocks in runtime.escalations.items():
            if blocks:
                self._add(BLOCKED, child_id, None, early=True)

    def ingest(self, event: dict[str, Any]) -> None:
        """Turn one bus event into a queue entry. Anything not listed is ignored (priority 4: never spoken)."""
        kind, session_id = event.get("type"), event.get("session_id")
        data = event.get("data") or {}
        if not isinstance(data, dict) or session_id is None:
            return
        if kind == "permission.asked":
            self._add(APPROVAL, session_id, str(data.get("id")))
        elif kind == "question.asked":
            self._add(QUESTION, session_id, str(data.get("id")))
        elif kind == "session.error" and data.get("kind") != "interrupted":
            self._add(FAILED, session_id, None)
        elif kind == "session.escalated":
            self._add(BLOCKED, session_id, None)
        elif kind == "session.status" and data.get("status") == "idle" and data.get("turn_id"):
            record = self.svc.store.turn(data["turn_id"])
            session = self.svc.store.session(session_id)
            if record and record["status"] == "completed" and session is not None and session.parent_id is None:
                self._add(FINISHED, session_id, data["turn_id"])

    def _still_relevant(self, item: Item) -> bool:
        runtime = self.svc.runtime
        if item.kind == APPROVAL:
            return item.ref in runtime.approvals
        if item.kind == QUESTION:
            return item.ref in runtime.questions
        session = self.svc.store.session(item.session_id)
        return session is not None and not session.archived

    def _prune(self) -> None:
        for key, item in list(self.items.items()):
            if not self._still_relevant(item) or item.spoken and item.kind not in (APPROVAL, QUESTION):
                del self.items[key]

    # -- deciding ---------------------------------------------------------------------

    def process(self, *, sentence_break: bool = False) -> bool:
        """Say at most one line now if the rules allow. Returns whether one was said."""
        svc = self.svc
        call = svc.call
        if call is None or not call.active:
            return False
        settings = svc.settings()
        level = settings["proactive"]
        if level == "off":
            return False
        self._prune()
        now = svc.clock()
        if now - self.last_spoken < settings["proactive_interval_seconds"] or call.listening:
            return False
        replying = svc.turn is not None
        if replying and not sentence_break:
            return False
        eligible = [i for i in self.items.values() if not i.spoken and now - i.created >= self.windows[i.priority]]
        if replying or call.speaking:
            eligible = [i for i in eligible if i.priority == 1]
        if level != "all" or now - svc.last_activity < self.lull:
            eligible = [i for i in eligible if i.priority < 3]
        if not eligible:
            return False
        top = min(i.priority for i in eligible)
        group = sorted((i for i in eligible if i.priority == top), key=lambda i: i.created)
        line = self._compose(group)
        if not line:
            for item in group:
                item.spoken = True
            return False
        self._emit(group, line, urgent=replying)
        return True

    def on_sentence_break(self) -> None:
        """The Manager just finished a sentence of its own reply: a priority-1 line may slip in here."""
        self.process(sentence_break=True)

    def on_quiet(self) -> None:
        """A turn ended or the client's activity changed: lines that were held may go now."""
        self.process()

    def on_barge_in(self) -> None:
        """The user took over: hold everything for one interval. Pending approvals and questions stay pending."""
        self.last_spoken = max(self.last_spoken, self.svc.clock())

    # -- the lines ----------------------------------------------------------------------

    def _name(self, session_id: str) -> str:
        session = self.svc.store.session(session_id)
        handle = self.svc.retriever.handle_for(session) if session else "a session"
        return "the " + handle.replace("-", " ")

    def _part(self, item: Item) -> str:
        runtime = self.svc.runtime
        name = self._name(item.session_id)
        if item.kind == APPROVAL:
            entry = runtime.approvals.get(item.ref or "")
            tool = (entry[2].get("tool") if entry else "") or "a tool"
            verb = {"bash": "run a command", "write": "change a file", "edit": "change a file",
                    "patch": "change files"}.get(tool, f"use {tool}")
            return f"{name} wants to {verb}"
        if item.kind == QUESTION:
            request = runtime.questions.get(item.ref or "")
            first = (request.questions[0] if request and request.questions else {}).get("question", "")
            text = cards.clip(" ".join(str(first).split()), 140)[0]
            return f"{name} has a question: {text}" if text else f"{name} has a question"
        if item.kind == FAILED:
            return f"{name} stopped with an error"
        if item.kind == BLOCKED:
            return f"a sub agent in {name} is blocked and needs you"
        return f"{name} finished"

    def _compose(self, group: list[Item]) -> str:
        kind = group[0].kind
        if kind == FINISHED:
            if len(group) == 1:
                return self._part(group[0]).capitalize() + "."
            return f"{NUMBERS.get(len(group), str(len(group)))} sessions finished."
        parts = [self._part(item) for item in group]
        if len(parts) == 1:
            line = parts[0][0].upper() + parts[0][1:] + "."
            return line + (" Allow it?" if group[0].kind == APPROVAL else "")
        word = NUMBERS.get(len(parts), str(len(parts)))
        shown = parts[:3]
        more = f", and {len(parts) - 3} more" if len(parts) > 3 else ""
        return f"{word} things need you: " + "; ".join(shown) + more + "."

    def _emit(self, group: list[Item], line: str, *, urgent: bool) -> None:
        svc = self.svc
        call = svc.call
        assert call is not None
        now = svc.clock()
        turn_id = f"mp_{secrets.token_hex(6)}"
        splitter = SentenceSplitter()
        sentences = [*splitter.feed(redact(line) + " "), *splitter.flush()]
        for seq, sentence in enumerate(sentences):
            svc._publish("manager.speech", {"turn_id": turn_id, "seq": seq, "text": sentence, "urgent": urgent,
                                            "proactive": True})
        self.last_spoken = now
        shown: str | None = None
        # The coalesced line names only three items; "+ N more" does not present their requests.
        reported = group if group[0].kind == FINISHED else group[:3]
        for item in reported:
            item.spoken = True
        for item in reported:
            if item.kind in (APPROVAL, QUESTION) and shown is None and self._present(item):
                shown = f"{item.kind} card for {self._name(item.session_id)}"
            elif item.kind in (APPROVAL, QUESTION):
                self._mark_presented(item)
        note = f'Unprompted, Nexus said aloud: "{redact(line)}"' + (f" and put an unfocused {shown} on the stage" if shown else "")
        svc.record_proactive(note, [item.session_id for item in group], line)

    def _mark_presented(self, item: Item) -> None:
        """Mark one item as put in front of the user (guard 4 of section 9), at the current utterance count."""
        svc = self.svc
        runtime = svc.runtime
        request: dict[str, Any] | None = None
        if item.kind == APPROVAL:
            entry = runtime.approvals.get(item.ref or "")
            request = entry[2] if entry else None
        elif item.kind == QUESTION:
            pending = runtime.questions.get(item.ref or "")
            request = pending.json() if pending else None
        if request is None or item.ref is None:
            return
        index = svc.call.turns if svc.call else 0
        known = svc.presented.get(item.ref)
        if known is None or known[0] != fingerprint(request):
            svc.presented[item.ref] = (fingerprint(request), index)

    def _present(self, item: Item) -> bool:
        """Mark it presented and put its unfocused card on the stage, unless a focused card is up."""
        self._mark_presented(item)
        svc = self.svc
        current = svc.stage.card
        if current is not None and current.payload.get("focused"):
            return False
        runtime = svc.runtime
        handle = svc.retriever.handle_for(svc.store.session(item.session_id))  # type: ignore[arg-type]
        try:
            if item.kind == APPROVAL:
                entry = runtime.approvals.get(item.ref or "")
                if entry is None:
                    return False
                body = cards.approval(session_id=item.session_id, handle=handle, approval_id=item.ref or "",
                                      request=entry[2])
            else:
                pending = runtime.questions.get(item.ref or "")
                if pending is None:
                    return False
                body = cards.question(session_id=item.session_id, handle=handle, request=pending.json())
        except cards.CardError:
            return False
        svc.stage.show(cards.finish(body, focused=False))
        return True
