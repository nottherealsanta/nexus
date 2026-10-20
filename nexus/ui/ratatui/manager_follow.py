"""The Live panel's own subscription to the manager session (plan 02 section 10.6).

GPT Live is always attached to the manager. The user may be looking at any other session, so
the panel follows the manager's event stream itself and folds it into a private view: the
call's rows stay on screen, and a question the manager asks reaches the user wherever they
are instead of waiting in a view nobody has open. The manager runs without approvals (its
catalog is allowed by name); the permission check in ``waiting`` is defensive only. Following never cancels
daemon work; it is read-only and ends with the call. It also hands each event to
``Manager.on_event`` so a Live request to show a session can act while another session is in front.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import aclosing

from ...ui_support.prompts import pending_questions
from ...view import apply, initial_state

APPROVAL = "approval waiting"
QUESTION = "question waiting"
_URGENT = {"permission.requested", "permission.resolved", "question.requested",
           "question.answered", "question.resolved"}


class ManagerFollow:
    def __init__(self, shell) -> None:
        self.shell = shell
        self.session = ""
        self.view = initial_state("")
        self.cursor = 0
        self.started = 0.0  # wall clock when the current follow began; older events are history
        self._task: asyncio.Task | None = None

    def start(self, session: str) -> None:
        """Follow ``session`` from its beginning (a replay, so the view is the durable state)."""
        if self._task is not None and not self._task.done() and self.session == session:
            return
        self._cancel()
        self.session, self.view, self.cursor = session, initial_state(session), 0
        self.started = time.time()
        self._task = asyncio.get_running_loop().create_task(self._run(session))

    async def stop(self) -> None:
        task = self._cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.session, self.view, self.cursor = "", initial_state(""), 0

    def waiting(self) -> str:
        """``approval waiting`` / ``question waiting`` / ```` for the followed session."""
        if self.view.pending_permissions:
            return APPROVAL
        return QUESTION if pending_questions(self.view) else ""

    def _cancel(self) -> asyncio.Task | None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        return task

    async def _run(self, session: str) -> None:
        delay = 0.25
        while self.session == session:
            try:
                async with aclosing(self.shell.client.stream(session, self.cursor, follow=True)) as events:
                    async for event in events:
                        if self.session != session:
                            return
                        # The stream replays the whole log first: only events newer than the
                        # reduced cursor and than this follow's start can ask for a switch.
                        with contextlib.suppress(Exception):
                            self.shell.manager.on_event(
                                event, session=session, fresh=event.seq > self.cursor, floor=self.started)
                        self.view = apply(self.view, event)
                        self.cursor = max(self.cursor, self.view.last_seq)
                        delay = 0.25
                        await self._changed(event.type in _URGENT)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a lost stream is retried; the call itself is unaffected
                pass
            await asyncio.sleep(delay)  # the client may have been replaced by a reconnect: read it afresh
            delay = min(5.0, delay * 2)

    async def _changed(self, urgent: bool) -> None:
        update = getattr(self.shell, "on_update", None)
        if update is not None:
            with contextlib.suppress(Exception):
                await update(immediate=urgent)
