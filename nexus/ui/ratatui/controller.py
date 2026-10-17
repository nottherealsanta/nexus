"""Continuous native host subscription and bounded reconnect (feasibility §5).

Reuse canonical bootstrap/selection; keep idle clients subscribed so work from
other surfaces is visible. Reconnect replays durable state rather than inventing
local turns. Closing this subscription never cancels daemon work.
"""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import aclosing

from ...ui_support.session_controller import SessionController
from ...client.protocol import ClientError


class NativeController(SessionController):
    reconnect = None
    _paused = False
    _post_event = None

    def resume(self, post_event):
        self._post_event = post_event
        if self._closed or self._paused or (self._task is not None and not self._task.done()):
            return
        self._task = asyncio.create_task(self._follow(self.session, post_event))

    async def pause(self):
        """Stop following (and its auto-reconnect) until ``unpause``; the caller owns the client."""
        self._paused = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.running = False

    def unpause(self):
        """Follow again on whatever client is installed now."""
        self._paused = False
        if self._post_event is not None:
            self.resume(self._post_event)

    async def _follow(self, session, post_event):
        delay = .25
        while not self._closed and session == self.session:
            try:
                async with aclosing(self.client.stream(session, self.cursor, follow=True)) as events:
                    async for event in events:
                        if session != self.session:
                            return
                        await post_event(event)
                        delay = .25
                        self.running = self.view.phase in {"running", "awaiting_permission", "awaiting_input"}
                return
            except asyncio.CancelledError:
                raise
            except ClientError:
                if session != self.session:
                    return
                self.running = False
                await post_event(None)
                if not self.reconnect:
                    return
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)
                try:
                    replacement = await self.reconnect()
                    if session != self.session or self._closed or self._paused:
                        await replacement.aclose()
                        return
                    previous = self.replace_client(replacement)
                    await previous.aclose()
                    await self.bootstrap()
                    await post_event(False)
                except ClientError:
                    continue
