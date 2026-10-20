"""Foreground-session reporting for the native clients (plan 02 section 10.3).

Each client tells the host which session it shows, as a host-API signal that never travels
through the model. The manager reads it as data (a hint for "this session", never an
authorisation). The host command is ``SessionFocus(session, client_id)`` answered by an
``Ack`` or an error; an empty ``session`` means this client no longer claims any focus.

The report is best effort and never blocks the UI: a change is sent in the background, the
last session is resent every ``HEARTBEAT_SECONDS`` so the host can expire a stale client, and
a daemon that does not know the command switches reporting off for this process.
Terminal and desktop share it (both run ``prototype.run``).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
import uuid

HEARTBEAT_SECONDS = 15.0
_UNSUPPORTED = ("unknown", "unsupported", "unimplemented")


class Focus:
    def __init__(self, shell, client_id: str | None = None) -> None:
        self.shell = shell
        self.client_id = client_id or f"native-{uuid.uuid4().hex[:12]}"
        self.session = ""  # what this client shows now
        self.sent = ""  # the last session the host acknowledged
        self.sent_at: float | None = None
        self.supported: bool | None = None  # None until the first try; False: no SessionFocus here
        self.error = ""
        self._pending = False
        self._task: asyncio.Task | None = None

    # -- reporting -----------------------------------------------------------

    def report(self, session: str) -> None:
        """The foreground session changed (or was first shown); send it in the background."""
        self.session = session or ""
        self._pending = True
        self._wake()

    def tick(self) -> None:
        """Heartbeat: resend while the claim is about to go stale, or after a failed send."""
        if not self.session or self.supported is False:
            return
        if self.sent_at is None or time.monotonic() - self.sent_at >= HEARTBEAT_SECONDS:
            self._pending = True
        if self._pending:
            self._wake()

    async def clear(self) -> None:
        """Release the claim when the client closes (an empty session)."""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.session = ""
        if self.supported is False or not self.sent:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._send(""), 2)
        self.sent = ""

    # -- transport -----------------------------------------------------------

    def _wake(self) -> None:
        if self.supported is False or (self._task is not None and not self._task.done()):
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._flush())
        except RuntimeError:  # no loop: nothing to send on
            self._task = None

    async def _flush(self) -> None:
        while self._pending and self.supported is not False:
            self._pending = False
            target = self.session
            try:
                await self._send(target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - advisory; the next heartbeat retries
                kind = str(getattr(exc, "kind", "") or "").casefold()
                self.error = str(exc)[:200]
                if any(word in kind for word in _UNSUPPORTED):
                    self.supported = False
                return
            if self.supported is False:
                return
            self.error = ""
            self.supported = True
            self.sent, self.sent_at = target, time.monotonic()

    async def _send(self, session: str):
        client = self.shell.client
        method = getattr(client, "session_focus", None)
        if method is not None:
            return await method(session, self.client_id)
        from ...host import protocol as p

        command = getattr(p, "SessionFocus", None)
        request = getattr(client, "_request", None)
        if command is None or request is None:  # the host API has not landed in this build
            self.supported = False
            return None
        return await request(command(session=session, client_id=self.client_id))
