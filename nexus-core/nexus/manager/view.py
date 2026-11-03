"""The holder's view, and moving it (MANAGER_PLAN.md 10.5).

Clients report where they are with ``POST /api/client/view`` (kept per event
stream in ``server.ClientViews``). Only the call's holder counts: this module
reads that one stream's view and sends ``manager.navigate`` to it. ``navigate``
returns only after the client answers (``POST /api/manager/navigate/result``),
or after the timeout, so the Manager never says "opened it" for a move that did
not happen.

Back: every move the Manager makes pushes the view the user was in. ``back``
pops it. The event carries ``back: true`` plus the resolved target fields, so a
client may use its own short history or the fields Core resolved; both name the
same view.
"""
from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

NAVIGATE_TIMEOUT = 5.0
BACK_DEPTH = 10


class ViewSource(Protocol):
    """What the Manager needs from the server's per-stream view store."""

    def view_for(self, stream_id: str) -> dict[str, Any] | None: ...


class NavigationError(Exception):
    """A move that did not happen; the message is what the Manager may say."""


@dataclass(frozen=True, slots=True)
class View:
    project: str | None = None
    session: str | None = None
    panel: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"project": self.project, "session": self.session, "panel": self.panel}

    @property
    def empty(self) -> bool:
        return self.project is None and self.session is None


class HolderView:
    """The call holder's view, its back stack and the navigation handshake."""

    def __init__(self, views: ViewSource | None = None, *, timeout: float = NAVIGATE_TIMEOUT) -> None:
        self.views = views
        self.timeout = timeout
        self.back_stack: list[View] = []
        self._pending: dict[str, tuple[str, asyncio.Future[tuple[bool, str | None]]]] = {}

    def reset(self) -> None:
        """A new call, or the call moved to another client: the old history means nothing there."""
        self.back_stack.clear()
        for _, future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    # -- reading --------------------------------------------------------------

    def current(self, stream_id: str | None) -> View | None:
        """The holder's last reported view, or ``None`` when it has reported none."""
        if stream_id is None or self.views is None:
            return None
        reported = self.views.view_for(stream_id)
        if reported is None:
            return None
        return View(reported.get("project"), reported.get("session"), reported.get("panel"))

    # -- moving ---------------------------------------------------------------

    async def navigate(self, stream_id: str | None, publish: Callable[[dict[str, Any]], None], *,
                       target: View | None, back: bool = False) -> View | None:
        """Move the holder and wait for its answer. Returns the view the user was in before the move.

        Raises :class:`NavigationError` when there is no holder, nothing to go back to, the client
        refuses (with its reason) or does not answer in time.
        """
        if stream_id is None:
            raise NavigationError("no client is holding the call")
        before = self.current(stream_id)
        if back:
            if not self.back_stack:
                raise NavigationError("there is no earlier view to go back to")
            target = self.back_stack[-1]
        if target is None:
            raise NavigationError("navigate needs a project, a session or a panel")
        request_id = f"nav_{secrets.token_hex(6)}"
        future: asyncio.Future[tuple[bool, str | None]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (stream_id, future)
        event: dict[str, Any] = {"request_id": request_id}
        if back:
            event["back"] = True
        for key, value in target.as_dict().items():
            if value is not None:
                event[key] = value
        publish(event)
        try:
            ok, reason = await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            raise NavigationError("the client did not confirm the move in time") from None
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and not task.cancelling():  # `reset` cancelled the wait, not this task
                raise NavigationError("the call moved to another client before the move was confirmed") from None
            raise
        finally:
            self._pending.pop(request_id, None)
        if not ok:
            raise NavigationError(reason or "the client could not open that")
        if back:
            self.back_stack.pop()
        elif before is not None and not before.empty and before != target:
            self.back_stack.append(before)
            del self.back_stack[:-BACK_DEPTH]
        return before

    def result(self, stream_id: str, request_id: str, ok: bool, reason: str | None) -> bool:
        """The client's answer. Returns False for an unknown or already answered request."""
        entry = self._pending.get(request_id)
        if entry is None or entry[0] != stream_id or entry[1].done():
            return False
        entry[1].set_result((bool(ok), reason))
        return True
