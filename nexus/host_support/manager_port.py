"""Host-side :class:`~nexus.tools.manager_port.ManagerPort` over ``SessionManager``.

Plan 02 (docs/manager.md). The host facade builds one of these per manager turn
through ``Runtime.manager_port_factory``; the manager's six tools call nothing else.

Visibility (the only sessions the manager can ever see or name): main namespace,
``kind == "root"``, not archived, not trashed, not the manager itself, with real
user input. Everything else reports the same ``SessionNotVisible`` so existence is
not probed.

Provenance, all derived from the manager's own log (never from the model):

* ``launch`` needs a real user prompt in the **current turn**, named by its id
  (``m<seq>``), and the manager must be attended (the runtime already asks the
  viewer to approve; with nobody watching the port refuses as a second line).
  Per-turn and live-managed caps apply. Every launch first records a
  ``session.linked`` (``kind: "managed"``) in the manager log, then starts a
  normal root: workspace default model, normal root permissions, no override.
* ``result`` needs the session to be in ``managed()`` (those links) or its exact id
  to occur in the user's latest message. A GPT Live delegation counts only by its
  request text, never by the ``Voice:`` lines of the spoken transcript it carries.
* ``cancel`` only touches a session the manager launched.
* ``wait`` is bounded (600 s), ends early when the user sends the manager new
  input, and never cancels a target. Cancelling the wait abandons it only.

``manager.*`` events carry ids, counts and reason codes, never instruction or
result text.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..ext.quarantine import sanitize_text
from ..model.message import Text
from ..session.export import is_user_prompt
from ..session.manager import SessionManager, SessionSummary
from ..session.records import EventRecord, MessageRecord
from ..tools.manager_port import (
    MAX_FIND_LIMIT,
    MAX_RESULT_BYTES,
    MAX_TITLE_CHARS,
    RECENT_WINDOW_SECONDS,
    CancelResult,
    FindResult,
    LaunchRefused,
    LaunchResult,
    ManagerPortError,
    ResultNotAuthorized,
    ResultNotReady,
    Scope,
    SessionNotVisible,
    SessionResult,
    SessionRow,
    SessionStatus,
    WaitResult,
)
from ..util import redact_secrets

__all__ = [
    "FacadeManagerHost",
    "HostManagerPort",
    "ManagerHost",
    "ManagerLimits",
    "UserMessage",
    "delegated_request",
    "scan_manager_log",
]

log = logging.getLogger("nexus.manager")

#: Newest records scanned for the terminal outcome and final assistant text.
_TAIL_RECORDS = 400
_TERMINAL = {"turn.completed": "completed", "turn.failed": "failed", "turn.cancelled": "cancelled"}
#: One wait call, seconds. The tool layer enforces the same bound on its input.
MAX_WAIT_SECONDS = 600
_MAX_REASON_CHARS = 200
_VOICE_HEAD = "Voice request"
_VOICE_SECTION = re.compile(
    r"\n\n(?:Spoken conversation since the previous request\n|Note: this request came from a voice call)"
)


def _clean_block(text: str) -> str:
    """Keep newlines and tabs; drop every other control or bidi character."""
    kept = "".join(
        ch if ch in "\n\t" or not unicodedata.category(ch).startswith("C") else " " for ch in text
    )
    return redact_secrets(kept)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", "ignore"), True


# -- the manager's own log ---------------------------------------------------------


def delegated_request(body: str) -> str:
    """The user's request inside a GPT Live delegation message, without Voice lines.

    A delegation message is ``Voice request`` plus the request, then (optionally) a
    labelled transcript of **both** speakers and a speech note. Only the request is
    something the user asked for; the ``Voice:`` lines are model-authored, so they
    never authorise anything. When the request was taken from the transcript itself
    (``Voice request (taken from the spoken conversation)``) only the ``You:`` lines
    count. A message that does not have this shape yields ``""``: nothing authorised.
    """
    head, _, rest = body.partition("\n")
    if not head.startswith(_VOICE_HEAD):
        return ""
    section = _VOICE_SECTION.split(rest, maxsplit=1)[0]
    from_transcript = "taken from the spoken conversation" in head
    lines: list[str] = []
    for line in section.splitlines():
        if from_transcript:
            if line.startswith("You: "):
                lines.append(line[len("You: "):])
        elif not line.startswith("Voice:"):
            lines.append(line)
    return "\n".join(lines).strip()


@dataclass(frozen=True, slots=True)
class UserMessage:
    """A real user prompt in the manager's log (a tool reply is not one)."""

    id: str
    seq: int
    turn: str
    #: What the user said. For a Live delegation: the delegated request only.
    text: str
    live: bool


@dataclass(frozen=True, slots=True)
class _Link:
    child: str
    turn: str
    seq: int


@dataclass(frozen=True, slots=True)
class LogScan:
    messages: tuple[UserMessage, ...] = ()
    links: tuple[_Link, ...] = ()


def _text_of(message: Any) -> str:
    return "\n".join(block.text for block in message.content if isinstance(block, Text) and block.text)


def scan_manager_log(records: Collection[Any]) -> LogScan:
    """Fold a manager log into its user prompts and managed links.

    A Live delegation is told apart by the ``origin`` of the ``input.queued`` event
    its ``input.consumed`` names; a message that merely *looks* like a delegation is
    reduced the same way, so a misaligned pairing can only authorise less.
    """
    origins: dict[str, str] = {}
    consumed: dict[str, list[str]] = {}
    users: list[MessageRecord] = []
    links: list[_Link] = []
    for record in records:
        if isinstance(record, EventRecord):
            event = record.event
            data = event.data if isinstance(event.data, dict) else {}
            if event.type == "input.queued":
                origin = data.get("origin")
                origins[str(data.get("queued_id") or "")] = (
                    str(origin.get("kind") or "") if isinstance(origin, dict) else ""
                )
            elif event.type == "input.consumed":
                consumed.setdefault(str(data.get("turn") or ""), []).append(str(data.get("queued_id") or ""))
            elif event.type == "session.linked" and data.get("kind") == "managed":
                child = data.get("child")
                if isinstance(child, str) and child:
                    links.append(_Link(child, str(data.get("turn") or ""), record.seq))
        elif isinstance(record, MessageRecord) and is_user_prompt(record.message):
            users.append(record)
    by_turn: dict[str, list[MessageRecord]] = {}
    for record in users:
        by_turn.setdefault(record.message.meta.turn_id or "", []).append(record)
    messages: list[UserMessage] = []
    for turn, group in by_turn.items():
        kinds = [origins.get(queued_id, "") for queued_id in consumed.get(turn, [])]
        # A directly started turn has one message with no ``input.consumed``; it comes first.
        offset = max(0, len(group) - len(kinds))
        for index, record in enumerate(group):
            slot = index - offset
            kind = kinds[slot] if 0 <= slot < len(kinds) else ""
            body = _text_of(record.message)
            live = kind == "live" or body.startswith(_VOICE_HEAD)
            messages.append(UserMessage(
                id=f"m{record.seq}", seq=record.seq, turn=turn,
                text=delegated_request(body) if live else body, live=live,
            ))
    messages.sort(key=lambda item: item.seq)
    return LogScan(tuple(messages), tuple(links))


# -- the host seam -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ManagerLimits:
    max_launches_per_turn: int = 3
    max_live_managed: int = 8


class ManagerHost(Protocol):
    """What the port needs from the host to act (the facade implements it)."""

    def limits(self) -> ManagerLimits: ...

    def new_session_id(self) -> str: ...

    def check_agent(self, name: str) -> str:
        """The canonical root role name, or :class:`LaunchRefused` ``bad_agent``."""
        ...

    async def start_root(
        self, session_id: str, instruction: str, *, agent: str | None, origin: dict[str, str], title_text: str
    ) -> None: ...

    async def cancel_root(self, session_id: str, reason: str) -> bool: ...


class HostManagerPort:
    def __init__(
        self,
        sessions: SessionManager,
        *,
        self_id: str = "",
        managed: Callable[[], Collection[str]] | None = None,
        user_text: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
        handle: Any | None = None,
        host: ManagerHost | None = None,
        poll_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if sessions.store.namespace != "main":
            raise ValueError("the manager port reads the main namespace only")
        self._sessions = sessions
        self._handle = handle
        self._host = host
        self._self_id = self_id or (str(handle.id) if handle is not None else "")
        # Without a manager handle nothing is authorised unless the caller injects it
        # (tests); with one, both come from the manager's own log.
        self._managed = managed if managed is not None else (self._log_managed if handle is not None else (lambda: ()))
        self._user_text = user_text if user_text is not None else (self._log_user_text if handle is not None else (lambda: ""))
        self._clock = clock
        self._poll = max(0.01, float(poll_interval))
        self._monotonic = monotonic
        self._launch_lock = asyncio.Lock()

    # -- ManagerPort: read side ----------------------------------------------

    async def find(self, *, scope: Scope, query: str | None, limit: int) -> FindResult:
        return await asyncio.to_thread(self._find, scope, query, limit)

    async def status(self, ids: tuple[str, ...]) -> tuple[SessionStatus, ...]:
        return await asyncio.to_thread(self._status, ids)

    async def result(self, session_id: str) -> SessionResult:
        result = await asyncio.to_thread(self._result, session_id)
        self._emit("manager.result_read", {
            "session": session_id, "bytes": result.total_bytes, "clipped": result.clipped,
        })
        return result

    # -- ManagerPort: acting side --------------------------------------------

    async def launch(
        self,
        instruction: str,
        origin_message_id: str,
        agent: str | None = None,
        title: str | None = None,
    ) -> LaunchResult:
        host, handle = self._acting()
        async with self._launch_lock:
            turn = str(getattr(handle, "active_turn_id", None) or "")
            try:
                return await self._launch(host, handle, turn, instruction, origin_message_id, agent, title)
            except LaunchRefused as exc:
                self._emit("manager.launch_refused", {"reason": exc.reason, "turn": turn})
                raise

    async def _launch(
        self,
        host: ManagerHost,
        handle: Any,
        turn: str,
        instruction: str,
        origin_message_id: str,
        agent: str | None,
        title: str | None,
    ) -> LaunchResult:
        if not getattr(handle, "attended", False):
            raise LaunchRefused(
                "denied", "no viewer is attached to approve a launch; open the manager in a client first"
            )
        if not turn:
            raise LaunchRefused("no_user_message", "a launch needs a user message in the current turn")
        scan = await asyncio.to_thread(self._scan)
        current = [item for item in scan.messages if item.turn == turn]
        if not current:
            raise LaunchRefused("no_user_message", "a launch needs a user message in the current turn")
        if origin_message_id not in {item.id for item in current}:
            raise LaunchRefused(
                "no_user_message",
                "origin_message_id must be the id of a user message in this turn; "
                f"the latest one is {current[-1].id!r}",
            )
        limits = host.limits()
        if sum(1 for link in scan.links if link.turn == turn) >= limits.max_launches_per_turn:
            raise LaunchRefused("cap", f"at most {limits.max_launches_per_turn} launches per turn")
        live = await asyncio.to_thread(self._live_managed, {link.child for link in scan.links})
        if live >= limits.max_live_managed:
            raise LaunchRefused(
                "cap", f"{live} launched sessions are still running; the limit is {limits.max_live_managed}"
            )
        role = host.check_agent(agent) if agent else None
        hint = sanitize_text(title, limit=MAX_TITLE_CHARS) if title else ""
        session_id = host.new_session_id()
        digest = hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:16]
        # The supervision edge first: a started root can never lack its link.
        try:
            handle.emit_session_event("session.linked", {
                "kind": "managed", "child": session_id, "turn": turn,
                "origin_message_id": origin_message_id,
                "instruction_digest": digest, "agent": role or "",
            })
        except Exception as exc:  # noqa: BLE001 - nothing was started
            raise LaunchRefused("error", f"could not record the launch: {_why(exc)}") from exc
        origin = {"kind": "manager", "manager": self._self_id, "origin_message_id": origin_message_id}
        try:
            await host.start_root(
                session_id, instruction, agent=role, origin=origin, title_text=hint or instruction
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LaunchRefused("error", f"could not start the session: {_why(exc)}") from exc
        return LaunchResult(id=session_id, title=hint, state="queued")

    async def wait(
        self, ids: tuple[str, ...], until: Literal["any", "all"], max_wait_seconds: int
    ) -> WaitResult:
        if until not in ("any", "all"):
            raise ManagerPortError("'until' must be 'any' or 'all'")
        ids = tuple(dict.fromkeys(ids))
        if not ids:
            raise ManagerPortError("wait needs at least one session id")
        bound = float(max(1, min(int(max_wait_seconds), MAX_WAIT_SECONDS)))
        baseline = set(getattr(self._handle, "queued_ids", ()) or ())
        start = self._monotonic()
        interrupted = False
        # The manager's turn only waits here, so it lends its supervisor slot to the
        # sessions it waits for; at max_concurrent=1 they could never start otherwise.
        lend = getattr(self._host, "lend_slot", None)
        async with lend(self._self_id) if callable(lend) and self._self_id else contextlib.nullcontext():
            while True:
                statuses = await asyncio.to_thread(self._status, ids)
                finished = {item.id for item in statuses if item.outcome is not None}
                completed = tuple(item for item in ids if item in finished)
                running = tuple(item for item in ids if item not in finished)
                waited = self._monotonic() - start
                if (until == "any" and completed) or not running:
                    break
                if set(getattr(self._handle, "queued_ids", ()) or ()) - baseline:
                    interrupted = True  # the user wrote again; hand control back
                    break
                if waited >= bound:
                    break
                await asyncio.sleep(min(self._poll, bound - waited))
        met = bool(completed) if until == "any" else not running
        outcome = "interrupted" if interrupted else ("completed" if met else "timeout")
        self._emit("manager.wait", {
            "ids": list(ids), "outcome": outcome, "waited_seconds": round(waited, 1),
        })
        return WaitResult(completed, running, interrupted, round(waited, 1))

    async def cancel(self, session_id: str, reason: str) -> CancelResult:
        host, _handle = self._acting()
        if not getattr(_handle, "attended", False):
            raise LaunchRefused(
                "denied", "no viewer is attached to approve a cancellation; open the manager in a client first"
            )
        scan = await asyncio.to_thread(self._scan)
        if session_id not in {link.child for link in scan.links}:
            raise LaunchRefused("not_managed", f"session {session_id!r} was not launched by this manager")
        await asyncio.to_thread(self._require_visible, session_id)
        cleaned = redact_secrets(sanitize_text(reason, limit=_MAX_REASON_CHARS))
        cancelled = bool(await host.cancel_root(session_id, cleaned))
        self._emit("manager.cancelled", {"session": session_id, "cancelled": cancelled})
        return CancelResult(id=session_id, cancelled=cancelled)

    # -- internals: the manager's log ----------------------------------------

    def _acting(self) -> tuple[ManagerHost, Any]:
        if self._host is None or self._handle is None:
            raise ManagerPortError("this manager port cannot launch, wait or cancel")
        return self._host, self._handle

    def _scan(self) -> LogScan:
        if not self._self_id:
            return LogScan()
        return scan_manager_log(self._sessions.store.read(self._self_id).records)

    def _log_managed(self) -> Collection[str]:
        return tuple(dict.fromkeys(link.child for link in self._scan().links))

    def _log_user_text(self) -> str:
        messages = self._scan().messages
        return messages[-1].text if messages else ""

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Best-effort audit record in the manager's log (the launch link is not best-effort)."""
        if self._handle is None:
            return
        try:
            self._handle.emit_session_event(event_type, {**data, "turn": str(getattr(self._handle, "active_turn_id", None) or "")})
        except Exception:  # noqa: BLE001 - an audit write never fails a tool call
            log.debug("manager audit event %s not recorded", event_type, exc_info=True)

    def _live_managed(self, children: Collection[str]) -> int:
        visible = self._visible()
        return sum(1 for child in children if child in visible and visible[child].state != "idle")

    # -- visibility ----------------------------------------------------------

    def _visible(self) -> dict[str, SessionSummary]:
        return {
            item.id: item
            for item in self._sessions.list(include_archived=False)
            if item.namespace == "main" and item.kind == "root" and item.id != self._self_id
        }

    def _require_visible(self, session_id: str) -> SessionSummary:
        item = self._visible().get(session_id)
        if item is None:
            raise SessionNotVisible(f"session {session_id!r} is not visible to the manager")
        return item

    # -- implementations -----------------------------------------------------

    def _find(self, scope: Scope, query: str | None, limit: int) -> FindResult:
        limit = max(1, min(limit, MAX_FIND_LIMIT))
        floor = self._clock() - RECENT_WINDOW_SECONDS
        needle = (query or "").casefold()
        matched: list[SessionRow] = []
        for item in self._visible().values():  # list() order: newest user prompt first
            live = item.state != "idle"
            if scope == "live" and not live:
                continue
            if scope == "recent" and not live and max(item.last_activity, item.order_time) < floor:
                continue
            title = sanitize_text(item.title, limit=MAX_TITLE_CHARS)
            if needle and needle not in title.casefold() and needle not in item.id.casefold():
                continue
            matched.append(SessionRow(
                id=item.id, title=title, state=item.state, kind=item.kind,
                order_time=item.order_time, completion_seq=item.completion_seq,
                child_count=item.child_count, running_children=item.running_children,
            ))
        matched.sort(key=lambda row: (-row.order_time, row.id))
        return FindResult(rows=tuple(matched[:limit]), truncated=len(matched) > limit)

    def _tail(self, session_id: str) -> tuple[str | None, str]:
        """Latest terminal outcome and the final assistant text before/at it."""
        outcome: str | None = None
        text = ""
        records = self._sessions.store.read_tail(session_id, max_records=_TAIL_RECORDS).records
        for record in reversed(records):
            if isinstance(record, EventRecord):
                agent: Any = record.event.data.get("agent")
                if isinstance(agent, dict) and agent.get("role") == "subagent":
                    continue
                if outcome is None and record.event.type in _TERMINAL:
                    outcome = _TERMINAL[record.event.type]
                elif record.event.type == "turn.started":
                    if outcome is None:
                        return None, ""  # the newest turn has not finished
                    break  # earlier turns' assistant text is not this outcome's report
            elif isinstance(record, MessageRecord) and record.message.role == "assistant" and not text:
                text = "\n".join(
                    block.text for block in record.message.content if isinstance(block, Text) and block.text
                )
            if outcome is not None and text:
                break
        return outcome, text

    def _status(self, ids: tuple[str, ...]) -> tuple[SessionStatus, ...]:
        visible = self._visible()
        out = []
        for session_id in ids:
            item = visible.get(session_id)
            if item is None:
                raise SessionNotVisible(f"session {session_id!r} is not visible to the manager")
            outcome = self._tail(session_id)[0] if item.state == "idle" else None
            attention = tuple(
                flag for state, flag in (("awaiting_permission", "permission"), ("awaiting_input", "input"))
                if item.state == state
            )
            out.append(SessionStatus(
                id=item.id, state=item.state, outcome=outcome,  # type: ignore[arg-type]
                completion_seq=item.completion_seq, attention=attention,
                last_activity=item.last_activity,
            ))
        return tuple(out)

    def _result(self, session_id: str) -> SessionResult:
        item = self._require_visible(session_id)
        named = re.search(rf"(?<![\w.-]){re.escape(session_id)}(?![\w.-])", self._user_text())
        if session_id not in set(self._managed()) and not named:
            raise ResultNotAuthorized(
                f"session {session_id!r} was not launched by the manager and is not named in "
                "the user's latest message"
            )
        outcome, final_text = self._tail(session_id) if item.state == "idle" else (None, "")
        if outcome is None:
            raise ResultNotReady(f"session {session_id!r} has no finished turn yet")
        cleaned = _clean_block(final_text)
        text, clipped = _clip(cleaned, MAX_RESULT_BYTES)
        return SessionResult(
            id=session_id, outcome=outcome, text=text,  # type: ignore[arg-type]
            clipped=clipped, total_bytes=len(cleaned.encode("utf-8")),
        )


def _why(exc: BaseException) -> str:
    return sanitize_text(f"{type(exc).__name__}: {exc}", limit=200)


class FacadeManagerHost:
    """:class:`ManagerHost` over a :class:`~nexus.host.facade.HostFacade`.

    Launched roots take the ordinary path: ``facade.enqueue`` on a fresh session id,
    so the supervisor cap, the workspace default model, the normal root permissions
    and automatic titling all apply exactly as for a root started from a client.
    """

    def __init__(self, facade: Any) -> None:
        self._facade = facade

    def limits(self) -> ManagerLimits:
        section = self._facade.manager_config()
        return ManagerLimits(
            max_launches_per_turn=int(section.max_launches_per_turn),
            max_live_managed=int(section.max_live_managed),
        )

    def new_session_id(self) -> str:
        store = self._facade.runtime.sessions.store
        for _ in range(32):
            candidate = f"mgr-{secrets.token_hex(6)}"
            if not store.row_exists(candidate):
                return candidate
        raise ManagerPortError("could not allocate a session id")  # pragma: no cover

    def check_agent(self, name: str) -> str:
        definition = self._facade._root_agent(name)
        if definition is None:
            raise LaunchRefused("bad_agent", f"{name!r} is not a root role in this workspace")
        return str(getattr(definition, "name", name) or name)

    async def start_root(
        self, session_id: str, instruction: str, *, agent: str | None, origin: dict[str, str], title_text: str
    ) -> None:
        facade = self._facade
        if agent:
            facade.select_agent(session_id, agent)
        handle = facade._session(session_id)
        handle.emit_session_event("session.opened", {
            "id": session_id,
            "launched_by": {"manager": origin["manager"], "origin_message_id": origin["origin_message_id"]},
        })
        await facade.enqueue(
            session_id, instruction, mode="queue", origin=origin, title_text=title_text
        )

    def lend_slot(self, session_id: str) -> Any:
        """Async context manager: the manager's supervisor slot is free while it waits."""
        return self._facade.supervisor.lent_slot(session_id)

    async def cancel_root(self, session_id: str, reason: str) -> bool:
        cancelled, dropped = await self._facade.cancel(session_id, reason=reason or None, drop_queue=True)
        return bool(cancelled) or dropped > 0
