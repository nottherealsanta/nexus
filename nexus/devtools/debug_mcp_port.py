"""Debug-only :class:`~nexus.tools.manager_port.ManagerPort` over the host daemon protocol.

The debug MCP server (:mod:`nexus.devtools.manager_debug_mcp`) runs the manager's own
tool functions (``nexus/tools/manager_tools.py``) against this port. It is a plain
protocol *client* of the workspace daemon: it never opens the session database, never
imports the runtime and listens on nothing. Every action is a documented host command
(``SessionList``, ``SessionState``, ``SessionEnqueue``, ``SessionCancel``,
``AgentSelect``, ``ManagerState``), so the daemon's own checks (reserved ids, supervisor
cap, workspace default model, normal root permissions) still apply.

Differences from :class:`~nexus.host_support.manager_port.HostManagerPort`, all because
the caller is a developer's debugging agent and not the in-app manager session:

* No manager log exists, so there is no user-message provenance or on-screen approval.
  The MCP client's own tool-call approval is the human gate; its tools are labelled
  debug-only.
* ``launch``, ``send`` and ``cancel`` are rate-limited per server process, ``launch``
  honours the daemon's ``[manager] max_live_managed`` cap, and ``cancel`` only reaches
  roots this server process launched (the managed-link rule, kept in memory).
* ``result`` reads any visible, finished root (redacted, 16 KiB), not only launched ones.
* Host commands carry no message origin, so a ``send`` is prefixed with a visible line
  naming the debug MCP. A launch ``title`` is applied with ``SessionRename`` right after
  the enqueue (best effort: the reply's ``title`` is empty if that failed).
* ``show`` is always refused: there is no UI here and no manager log to carry the request.
* ``session_wait`` has no user-interruption signal (``interrupted`` is always false); the
  MCP client ends a wait with ``notifications/cancelled``.

Visibility, bounds, redaction and the "recent" window are the same as the manager's.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ..client.protocol import ClientError, TransportClosed
from ..ext.quarantine import sanitize_text
from ..host.transports import DaemonUnavailable, TransportError
from ..host_support.manager_port import _clean_block, _clip
from ..tools.manager_port import (
    MAX_FIND_LIMIT,
    MAX_RESULT_BYTES,
    MAX_SEND_BYTES,
    MAX_SENDS_PER_MINUTE,
    MAX_TITLE_CHARS,
    RECENT_WINDOW_SECONDS,
    SEND_RATE_WINDOW_SECONDS,
    WORKTREE_NAME_RE,
    CancelResult,
    FindResult,
    LaunchRefused,
    LaunchResult,
    ManagerPortError,
    ResultNotReady,
    Scope,
    SendResult,
    ShowResult,
    SessionNotVisible,
    SessionResult,
    SessionRow,
    SessionStatus,
    WaitResult,
)
from ..util import redact_secrets

__all__ = ["DEBUG_ID_PREFIX", "DEBUG_SEND_PREFIX", "DaemonManagerPort"]

#: Ids of roots launched through the debug MCP, so they are recognisable in any list.
DEBUG_ID_PREFIX = "dbg-"
#: First line of every message sent into a session, so its reader knows the source.
DEBUG_SEND_PREFIX = "[Sent through the Nexus debug MCP, not typed by the user]\n"

MAX_WAIT_SECONDS = 600
MAX_LAUNCHES_PER_MINUTE = 3
DEFAULT_MAX_LIVE = 8
_MAX_REASON_CHARS = 200
_MANAGER_ID = "nexus-manager"
_TERMINAL = {"completed", "failed", "cancelled"}
_OUTCOME_CACHE = 256
_LAUNCHED_MAX = 512


def _why(exc: BaseException) -> str:
    return sanitize_text(f"{type(exc).__name__}: {exc}", limit=200)


class DaemonManagerPort:
    """Manager port backed by one lazily opened daemon client.

    ``connect`` returns a ready :class:`~nexus.client.protocol.Client`; it is called on
    first use and again after the connection dies. It must not start a daemon.
    """

    def __init__(
        self,
        connect: Callable[[], Awaitable[Any]],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval: float = 1.0,
    ) -> None:
        self._connect = connect
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._clock = clock
        self._monotonic = monotonic
        self._poll = max(0.01, float(poll_interval))
        self._launched: dict[str, None] = {}
        self._stamps: dict[str, deque[float]] = {"launch": deque(), "send": deque()}
        self._outcomes: dict[str, tuple[int, str | None, str]] = {}

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass

    # -- plumbing -------------------------------------------------------------

    async def _call(self, name: str, /, *args: Any, **kwargs: Any) -> Any:
        """One host command. Failures become model-visible errors; a dead connection is dropped.

        The command is never retried (a half-sent ``enqueue`` could otherwise be sent twice);
        the next call opens a fresh connection.
        """
        try:
            async with self._lock:
                if self._client is None:
                    self._client = await self._connect()
                client = self._client
        except (ClientError, TransportError, OSError) as exc:
            raise ManagerPortError(f"no Nexus daemon is reachable for this workspace: {_why(exc)}") from exc
        try:
            return await getattr(client, name)(*args, **kwargs)
        except (TransportClosed, DaemonUnavailable) as exc:
            self._client = None
            raise ManagerPortError(f"the Nexus daemon connection was lost: {_why(exc)}") from exc
        except (ClientError, TransportError, OSError, ValueError) as exc:
            raise ManagerPortError(_why(exc)) from exc

    def _rate(self, kind: str, limit: int, error: str) -> None:
        now = self._monotonic()
        stamps = self._stamps[kind]
        while stamps and now - stamps[0] >= SEND_RATE_WINDOW_SECONDS:
            stamps.popleft()
        if len(stamps) >= limit:
            raise LaunchRefused("rate", error)
        stamps.append(now)

    async def _visible(self) -> dict[str, Any]:
        sessions = await self._call("list_sessions")
        return {
            item.id: item
            for item in sessions
            if getattr(item, "namespace", "main") == "main"
            and getattr(item, "kind", "root") == "root"
            and item.id != _MANAGER_ID
        }

    async def _require_visible(self, session_id: str) -> Any:
        item = (await self._visible()).get(session_id)
        if item is None:
            raise SessionNotVisible(f"session {session_id!r} is not visible to the debug MCP")
        return item

    async def _tail(self, item: Any) -> tuple[str | None, str]:
        """Latest terminal outcome and its final assistant text (cached per ``last_seq``)."""
        cached = self._outcomes.get(item.id)
        if cached is not None and cached[0] == item.last_seq:
            return cached[1], cached[2]
        view, _seq = await self._call("state", item.id)
        outcome: str | None = None
        text = ""
        turns = view.get("turns") or []
        if turns and isinstance(turns[-1], dict) and turns[-1].get("phase") in _TERMINAL:
            turn = turns[-1]
            outcome = str(turn["phase"])
            for message in reversed(turn.get("messages") or []):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = "\n".join(
                        str(block.get("text") or "")
                        for block in message.get("blocks") or []
                        if isinstance(block, dict) and block.get("kind") == "text" and block.get("text")
                    )
                    if text:
                        break
        if len(self._outcomes) >= _OUTCOME_CACHE:
            self._outcomes.pop(next(iter(self._outcomes)))
        self._outcomes[item.id] = (item.last_seq, outcome, text)
        return outcome, text

    # -- ManagerPort: read side -----------------------------------------------

    async def find(self, *, scope: Scope, query: str | None, limit: int) -> FindResult:
        limit = max(1, min(limit, MAX_FIND_LIMIT))
        floor = self._clock() - RECENT_WINDOW_SECONDS
        needle = (query or "").casefold()
        matched: list[SessionRow] = []
        for item in (await self._visible()).values():
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

    async def status(self, ids: tuple[str, ...]) -> tuple[SessionStatus, ...]:
        visible = await self._visible()
        out = []
        for session_id in ids:
            item = visible.get(session_id)
            if item is None:
                raise SessionNotVisible(f"session {session_id!r} is not visible to the debug MCP")
            outcome = (await self._tail(item))[0] if item.state == "idle" else None
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

    async def result(self, session_id: str) -> SessionResult:
        item = await self._require_visible(session_id)
        outcome, text = await self._tail(item) if item.state == "idle" else (None, "")
        if outcome is None:
            raise ResultNotReady(f"session {session_id!r} has no finished turn yet")
        cleaned = _clean_block(text)
        clipped_text, clipped = _clip(cleaned, MAX_RESULT_BYTES)
        return SessionResult(
            id=session_id, outcome=outcome,  # type: ignore[arg-type]
            text=clipped_text, clipped=clipped, total_bytes=len(cleaned.encode("utf-8")),
        )

    # -- ManagerPort: acting side ---------------------------------------------

    async def launch(
        self,
        instruction: str,
        origin_message_id: str,
        agent: str | None = None,
        title: str | None = None,
        worktree: bool = False,
    ) -> LaunchResult:
        if worktree and not (isinstance(title, str) and re.fullmatch(WORKTREE_NAME_RE, title)):
            raise LaunchRefused("error", "a worktree launch needs a title that is a valid worktree name")
        cap = DEFAULT_MAX_LIVE
        try:
            cap = int((await self._call("manager_state")).max_live_managed)
        except (ManagerPortError, TypeError, ValueError, AttributeError):
            pass  # an unreachable daemon fails the enqueue below; otherwise keep the default cap
        visible = await self._visible()
        live = sum(1 for sid in self._launched if sid in visible and visible[sid].state != "idle")
        if live >= cap:
            raise LaunchRefused("cap", f"{live} launched sessions are still running; the limit is {cap}")
        self._rate("launch", MAX_LAUNCHES_PER_MINUTE,
                   f"at most {MAX_LAUNCHES_PER_MINUTE} launches per {int(SEND_RATE_WINDOW_SECONDS)} seconds")
        session_id = f"{DEBUG_ID_PREFIX}{secrets.token_hex(6)}"
        hint = sanitize_text(title, limit=MAX_TITLE_CHARS) if title else ""
        try:
            if agent:
                try:
                    await self._call("select_agent", session_id, agent)
                except ManagerPortError as exc:
                    raise LaunchRefused("bad_agent", f"{agent!r} is not a usable root role: {exc}") from exc
            if worktree:
                # Created while the session is still an idle draft, so before the first enqueue.
                # The named create needs the host's SessionPromote to carry ``name``.
                try:
                    await self._call("promote_session", session_id,
                                     reason="worktree requested by the launching tool", name=title)
                except ManagerPortError as exc:
                    raise LaunchRefused(
                        "error", f"could not create worktree {title!r}; session {session_id} was "
                        f"not started: {exc}") from exc
            await self._call("enqueue", session_id, instruction, mode="queue")
        except LaunchRefused:
            raise
        except ManagerPortError as exc:
            raise LaunchRefused("error", f"could not start the session: {exc}") from exc
        self._launched[session_id] = None
        while len(self._launched) > _LAUNCHED_MAX:
            self._launched.pop(next(iter(self._launched)))
        applied = ""
        if worktree:
            applied = str(title)  # the host set the title together with the worktree
        elif hint:
            # The session exists once the enqueue is accepted; a user title also stops
            # auto-titling. The launch already happened, so a rename failure only drops the hint.
            try:
                await self._call("rename_session", session_id, hint)
                applied = hint
            except ManagerPortError:
                pass
        return LaunchResult(id=session_id, title=applied, state="queued",
                            worktree_name=str(title) if worktree else "")

    async def wait(
        self, ids: tuple[str, ...], until: Literal["any", "all"], max_wait_seconds: int
    ) -> WaitResult:
        if until not in ("any", "all"):
            raise ManagerPortError("'until' must be 'any' or 'all'")
        ids = tuple(dict.fromkeys(ids))
        if not ids:
            raise ManagerPortError("wait needs at least one session id")
        bound = float(max(1, min(int(max_wait_seconds), MAX_WAIT_SECONDS)))
        start = self._monotonic()
        while True:
            statuses = await self.status(ids)
            finished = {item.id for item in statuses if item.outcome is not None}
            completed = tuple(item for item in ids if item in finished)
            running = tuple(item for item in ids if item not in finished)
            waited = self._monotonic() - start
            if (until == "any" and completed) or not running or waited >= bound:
                break
            await asyncio.sleep(min(self._poll, bound - waited))
        return WaitResult(completed, running, False, round(waited, 1))

    async def cancel(self, session_id: str, reason: str) -> CancelResult:
        if session_id not in self._launched:
            raise LaunchRefused("not_managed", f"session {session_id!r} was not launched by this debug MCP server")
        await self._require_visible(session_id)
        cleaned = redact_secrets(sanitize_text(reason, limit=_MAX_REASON_CHARS))
        cancelled, dropped = await self._call("cancel", session_id, reason=cleaned, drop_queue=True)
        return CancelResult(id=session_id, cancelled=bool(cancelled) or dropped > 0)

    async def show(self, session_id: str, origin_message_id: str) -> ShowResult:
        raise ManagerPortError(
            "session_show is not supported here: the debug MCP has no UI to switch; "
            "it never writes a manager.show request"
        )

    async def send(self, session_id: str, message: str, origin_message_id: str) -> SendResult:
        size = len(message.encode("utf-8"))
        if not message.strip() or size > MAX_SEND_BYTES:
            raise LaunchRefused("error", f"a message must be non-empty and at most {MAX_SEND_BYTES} bytes")
        item = await self._require_visible(session_id)
        now = self._clock()
        if item.state == "idle" and max(item.last_activity, item.order_time) < now - RECENT_WINDOW_SECONDS:
            raise LaunchRefused(
                "not_sendable", f"session {session_id!r} has been idle for over 2 hours; ask the user to open it"
            )
        self._rate("send", MAX_SENDS_PER_MINUTE,
                   f"at most {MAX_SENDS_PER_MINUTE} sends per {int(SEND_RATE_WINDOW_SECONDS)} seconds")
        try:
            await self._call("enqueue", session_id, DEBUG_SEND_PREFIX + message, mode="queue")
        except ManagerPortError as exc:
            raise LaunchRefused("error", f"could not send the message: {exc}") from exc
        return SendResult(
            id=session_id, title=sanitize_text(item.title, limit=MAX_TITLE_CHARS), state="queued", bytes=size
        )
