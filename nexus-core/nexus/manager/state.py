"""One unified state for a session (docs/manager-design.md section 7, M1).

The clients and the Manager need one answer to "what does this session need?".
Store has only archive state and durable turn rows; Runtime has the live facts
(busy turns, pending approvals and questions, escalations and the last failure),
and unread is a client-side notion that Core does not persist. This module
combines exactly those facts and never writes anything.

Precedence, highest first:

1. ``needs_approval``: a ``permission.asked`` is waiting.
2. ``needs_answer``: an ``ask`` question is waiting.
3. ``blocked``: a child called ``escalate`` and its report is not delivered.
4. ``failed``: the last turn failed (not an interruption).
5. ``working``: a turn is running, including a parent waiting on a child.
6. ``archived``: the tree was archived. Archive is a deliberate terminal view;
   it outranks unread so restoring a tree does not look like a new finish.
7. ``finished_unread``: a turn finished and no client has shown it yet.
8. ``idle``: none of the above.

``UNKNOWN`` is not a persisted state and is not part of the plan's catalog. It
is returned only when a source that could outrank the answer is *unavailable*,
so callers cannot mistake "Core was not told" for "this session is idle". Every
field of :class:`SessionFacts` is therefore tri-state: ``None`` means the caller
has no source for it, ``0``/``False`` means the source says there is nothing,
and a count/``True`` means the signal is present.

Current sources and their limits:

* ``archived`` comes from ``Session.archived``.
* ``busy``, approvals, questions, escalations and failure are in Runtime memory.
  ``Runtime.turns`` (with the ``_session_busy`` child roll-up),
  ``Runtime.approvals``/``pending_approvals()``,
  ``Runtime.questions``/``pending_questions()``, ``Runtime.escalations`` and
  ``Runtime.errors`` are the sources. Store's ``turn`` rows are durable
  history, not a live "children are still running" answer.
* ``Runtime.errors`` also holds user interruptions. ``failed`` must exclude an
  error whose kind is ``interrupted``; use the durable ``turn.error.kind`` or
  the ``session.error`` event, not key presence in ``errors`` alone.
* Unread is not persisted anywhere in Core: the TUI and NSH each keep their own
  set based on what the user has viewed. Pass ``unread`` when a client has
  reported it; otherwise a non-archived idle-looking session is ``UNKNOWN``,
  never a false ``idle``.
* Escalations are transient Runtime state cleared when the parent's report is
  delivered. There is no durable escalation queue to read.

Facts are per session. A root row that should show a child's approval,
question or running turn must roll the tree up before building
:class:`SessionFacts`, as both clients already do for their rail counts.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..store import Session, Store

__all__ = [
    "ARCHIVED",
    "ATTENTION_STATES",
    "BLOCKED",
    "FAILED",
    "FINISHED_UNREAD",
    "IDLE",
    "NEEDS_ANSWER",
    "NEEDS_APPROVAL",
    "PRECEDENCE",
    "STATES",
    "UNKNOWN",
    "WORKING",
    "SessionFacts",
    "SessionState",
    "session_state",
    "session_states",
    "state_map",
]

NEEDS_APPROVAL = "needs_approval"
NEEDS_ANSWER = "needs_answer"
BLOCKED = "blocked"
FAILED = "failed"
WORKING = "working"
ARCHIVED = "archived"
FINISHED_UNREAD = "finished_unread"
IDLE = "idle"

#: A source was unavailable, so no plan state can be claimed honestly.
UNKNOWN = "unknown"

#: The order in which one state wins over the others. ``IDLE`` is the default.
PRECEDENCE: tuple[str, ...] = (
    NEEDS_APPROVAL,
    NEEDS_ANSWER,
    BLOCKED,
    FAILED,
    WORKING,
    ARCHIVED,
    FINISHED_UNREAD,
    IDLE,
)

#: States the attention queue speaks at the next pause (docs/manager-design.md section 8).
ATTENTION_STATES = frozenset({NEEDS_APPROVAL, NEEDS_ANSWER, BLOCKED})

#: Every value a :class:`SessionState` may carry.
STATES = frozenset((*PRECEDENCE, UNKNOWN))


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """The live per-session inputs to the unified state.

    ``None`` means the caller has no source for that fact. A zero count or
    ``False`` is a positive statement that the signal is absent. Counts are for
    this session as the caller wants it described; roll children up before
    calling when a root row should show a child waiting.
    """

    pending_approvals: int | None = None
    pending_questions: int | None = None
    escalations: int | None = None
    failed: bool | None = None
    busy: bool | None = None
    unread: bool | None = None

    def __post_init__(self) -> None:
        for name in ("pending_approvals", "pending_questions", "escalations"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer (not bool) or None")
        for name in ("failed", "busy", "unread"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be a bool or None")


@dataclass(frozen=True, slots=True)
class SessionState:
    """One session's unified state and the facts that produced it."""

    session_id: str
    state: str
    archived: bool
    pending_approvals: int | None = None
    pending_questions: int | None = None
    escalations: int | None = None
    failed: bool | None = None
    busy: bool | None = None
    unread: bool | None = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            allowed = ", ".join(sorted(STATES))
            raise ValueError(f"state must be one of: {allowed}")

    @property
    def exact(self) -> bool:
        """Whether the state is one the plan names, rather than :data:`UNKNOWN`."""
        return self.state != UNKNOWN

    @property
    def key(self) -> str:
        """The value in a ``states`` mapping: ``{session_id: key}``."""
        return self.state

    @property
    def needs_attention(self) -> bool:
        """Whether the attention queue should consider this session (section 8)."""
        return self.state in ATTENTION_STATES

    @property
    def unavailable(self) -> tuple[str, ...]:
        """The fact names the caller did not supply, in precedence order."""
        return tuple(
            name
            for name, value in (
                ("pending_approvals", self.pending_approvals),
                ("pending_questions", self.pending_questions),
                ("escalations", self.escalations),
                ("failed", self.failed),
                ("busy", self.busy),
                ("unread", self.unread),
            )
            if value is None
        )

    def as_dict(self) -> dict[str, Any]:
        """A plain, bounded view for a tool result or the HTTP API."""
        return {
            "session_id": self.session_id,
            "state": self.state,
            "key": self.key,
            "exact": self.exact,
            "needs_attention": self.needs_attention,
            "archived": self.archived,
            "unavailable": list(self.unavailable),
            "facts": {
                "pending_approvals": self.pending_approvals,
                "pending_questions": self.pending_questions,
                "escalations": self.escalations,
                "failed": self.failed,
                "busy": self.busy,
                "unread": self.unread,
            },
        }


def _reading(session: Session, state: str, facts: SessionFacts) -> SessionState:
    return SessionState(
        session_id=session.id,
        state=state,
        archived=bool(session.archived),
        pending_approvals=facts.pending_approvals,
        pending_questions=facts.pending_questions,
        escalations=facts.escalations,
        failed=facts.failed,
        busy=facts.busy,
        unread=facts.unread,
    )


def session_state(session: Session, facts: SessionFacts | None = None) -> SessionState:
    """Combine one session's live facts into the unified state.

    Missing facts are not treated as absent. The first unavailable input that
    could outrank the state reached so far produces :data:`UNKNOWN`; a missing
    input below the answer (for example unread on an archived session) does not.
    """
    facts = facts or SessionFacts()

    def reading(state: str) -> SessionState:
        return _reading(session, state, facts)

    for state, value in (
        (NEEDS_APPROVAL, facts.pending_approvals),
        (NEEDS_ANSWER, facts.pending_questions),
        (BLOCKED, facts.escalations),
        (FAILED, facts.failed),
        (WORKING, facts.busy),
    ):
        if value is None:
            return reading(UNKNOWN)
        if value:
            return reading(state)

    if session.archived:
        return reading(ARCHIVED)
    if facts.unread is None:
        return reading(UNKNOWN)
    return reading(FINISHED_UNREAD if facts.unread else IDLE)


def session_states(
    store: Store,
    facts: Mapping[str, SessionFacts] | None = None,
    *,
    project_id: str | None = None,
    archived: str = "include",
) -> dict[str, SessionState]:
    """State for every session Store returns, keyed by session id.

    ``facts`` is keyed by session id; a session with no entry gets all live
    facts unavailable. Archive state itself comes from the `Session`, never from
    the caller. `archived` is Store's filter: ``include``, ``exclude`` or
    ``only``.
    """
    if archived not in ("include", "exclude", "only"):
        raise ValueError("archived must be 'include', 'exclude' or 'only'")
    supplied = facts or {}
    return {
        session.id: session_state(session, supplied.get(session.id))
        for session in store.sessions(project_id=project_id, archived=archived)
    }


def state_map(states: Iterable[SessionState]) -> dict[str, str]:
    """Session id to state, for ``Retriever.find_sessions(states=...)``.

    :data:`UNKNOWN` is included verbatim; it never equals a requested plan state,
    so a filter cannot accidentally select a session whose state is unknown.
    """
    return {reading.session_id: reading.state for reading in states}
