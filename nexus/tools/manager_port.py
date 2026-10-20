"""``ManagerPort``: the narrow seam the eight manager tools call.

Plan 02 (docs/manager.md). The manager session's tools depend on this protocol
only; they never import ``host/`` or touch the database. The concrete adapter
lives host-side (``nexus/host_support/manager_port.py``); the runtime gets it from
``Runtime.manager_port_factory``, which the host facade fills in.

The read side is ``find``, ``status`` and ``result``; the acting side is
``launch``, ``wait``, ``cancel``, ``send`` and ``show``. The port enforces provenance (a
launch, send or show needs a real user message in the current turn), visibility, the
caps and the managed-link rule for cancel; the tools only validate shapes and sizes.

Every value is a frozen primitive record with a hard bound, so a port
implementation cannot hand the model an unbounded or unredacted payload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..errors import ToolError

__all__ = [
    "DEFAULT_FIND_LIMIT",
    "MAX_FIND_LIMIT",
    "MAX_QUERY_CHARS",
    "MAX_RESULT_BYTES",
    "MAX_STATUS_IDS",
    "MAX_TITLE_CHARS",
    "MAX_SEND_BYTES",
    "MAX_SENDS_PER_MINUTE",
    "MAX_SENDS_PER_TURN",
    "MAX_SHOWS_PER_TURN",
    "SHOW_TTL_SECONDS",
    "RECENT_WINDOW_SECONDS",
    "SEND_RATE_WINDOW_SECONDS",
    "CancelResult",
    "SendResult",
    "ShowResult",
    "FindResult",
    "LaunchRefused",
    "LaunchResult",
    "ManagerPort",
    "ManagerPortError",
    "ResultNotAuthorized",
    "ResultNotReady",
    "Scope",
    "SessionNotVisible",
    "SessionResult",
    "SessionRow",
    "SessionStatus",
    "WaitResult",
]

#: ``recent`` = live, or active within this many seconds of now.
RECENT_WINDOW_SECONDS = 2 * 60 * 60
DEFAULT_FIND_LIMIT = 20
MAX_FIND_LIMIT = 50
MAX_QUERY_CHARS = 256
MAX_STATUS_IDS = 20
MAX_TITLE_CHARS = 160
#: One ``session_result`` payload, after redaction (UTF-8 bytes).
MAX_RESULT_BYTES = 16 * 1024

#: ``session_send`` payload, UTF-8 bytes; sends per manager turn; sends per rolling window.
MAX_SEND_BYTES = 16 * 1024
MAX_SENDS_PER_TURN = 3
MAX_SENDS_PER_MINUTE = 6
SEND_RATE_WINDOW_SECONDS = 60.0
#: ``session_show`` requests per manager turn; seconds a ``manager.show`` stays actionable.
MAX_SHOWS_PER_TURN = 3
SHOW_TTL_SECONDS = 30.0

Scope = Literal["live", "recent"]
Outcome = Literal["completed", "failed", "cancelled"]


class ManagerPortError(ToolError):
    """A model-visible refusal from the port."""


class SessionNotVisible(ManagerPortError):
    """Unknown, archived, trashed, foreign or non-root. Deliberately one message."""


class ResultNotReady(ManagerPortError):
    """The session has no terminal turn yet, or is still running."""


class ResultNotAuthorized(ManagerPortError):
    """Not a managed session and not named by the human's current message."""


class LaunchRefused(ManagerPortError):
    """A launch or cancel the port will not perform.

    ``reason`` is the stable code recorded in ``manager.launch_refused``:
    ``no_user_message``, ``denied``, ``cap``, ``bad_agent``, ``not_managed``, ``rate``,
    ``not_sendable`` or ``error``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: str
    title: str
    state: str
    kind: str
    order_time: float
    completion_seq: int
    child_count: int
    running_children: int
    focused_clients: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FindResult:
    rows: tuple[SessionRow, ...]
    #: More rows matched than ``limit`` allowed.
    truncated: bool


@dataclass(frozen=True, slots=True)
class SessionStatus:
    id: str
    state: str
    #: Latest terminal turn outcome, or ``None`` while running / never completed.
    outcome: Outcome | None
    completion_seq: int
    #: Attention flags the human would also see: ``permission``, ``input``.
    attention: tuple[str, ...]
    last_activity: float


@dataclass(frozen=True, slots=True)
class SessionResult:
    id: str
    outcome: Outcome
    #: Redacted final assistant text, at most ``MAX_RESULT_BYTES`` UTF-8 bytes.
    text: str
    clipped: bool
    #: Size of the redacted text before clipping.
    total_bytes: int


@dataclass(frozen=True, slots=True)
class LaunchResult:
    id: str
    #: The title hint the manager gave, control-free and clipped; ``""`` when none.
    title: str
    #: ``queued``: the instruction is admitted; the root starts when the host schedules it.
    state: str = "queued"


@dataclass(frozen=True, slots=True)
class WaitResult:
    #: Ids that reached a terminal turn, in the order asked.
    completed: tuple[str, ...]
    running: tuple[str, ...]
    #: The wait ended early because the user sent the manager a new message.
    interrupted: bool
    waited_seconds: float


@dataclass(frozen=True, slots=True)
class CancelResult:
    id: str
    cancelled: bool


@dataclass(frozen=True, slots=True)
class SendResult:
    id: str
    #: The target's title, control-free and clipped.
    title: str
    #: ``queued``: the message is admitted; the target consumes it at its next boundary.
    state: str = "queued"
    bytes: int = 0


@dataclass(frozen=True, slots=True)
class ShowResult:
    id: str
    #: The target's title, control-free and clipped.
    title: str
    #: ``requested``: a client is foreground on the manager (or a Live call is open) and may
    #: switch; ``no_viewer``: nobody is known to be watching. Never "shown".
    state: str = "requested"


@runtime_checkable
class ManagerPort(Protocol):
    """The manager's facade. Implementations enforce visibility, provenance and caps."""

    async def find(
        self, *, scope: Scope, query: str | None, limit: int
    ) -> FindResult: ...

    async def status(self, ids: tuple[str, ...]) -> tuple[SessionStatus, ...]:
        """One entry per visible id; raises ``SessionNotVisible`` otherwise."""
        ...

    async def result(self, session_id: str) -> SessionResult: ...

    async def launch(
        self,
        instruction: str,
        origin_message_id: str,
        agent: str | None = None,
        title: str | None = None,
    ) -> LaunchResult:
        """Start one root session. Needs a real user message in the current turn."""
        ...

    async def wait(
        self, ids: tuple[str, ...], until: Literal["any", "all"], max_wait_seconds: int
    ) -> WaitResult:
        """Return when ``until`` is met, the bound passes, or the user writes again.

        Cancelling the call (turn cancel) abandons the wait; it never cancels a target.
        """
        ...

    async def cancel(self, session_id: str, reason: str) -> CancelResult:
        """Cancel one session this manager launched."""
        ...

    async def send(self, session_id: str, message: str, origin_message_id: str) -> SendResult:
        """Queue one user-origin message on an existing root session of this workspace.

        Needs a real user message in the current turn. Grants no read of the target's
        result and no cancel.
        """
        ...

    async def show(self, session_id: str, origin_message_id: str) -> ShowResult:
        """Ask the user's native client to switch to a visible root session.

        Needs a real user message in the current turn. It records a request only: no
        client is guaranteed to be listening, and the result never claims it was shown.
        """
        ...
