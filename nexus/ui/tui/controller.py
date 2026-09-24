"""Daemon-client event bridge and reducer seam for the Textual shell."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace
from typing import Any, Awaitable, Callable

from ...events import Event
from ...view import ConversationView, apply, initial_state
from ..cli.client import Client, ClientError
from ..turn_stream import turn_events


class TuiController:
    """Owns only client commands, the canonical reducer, and stream tasks."""

    def __init__(self, client: Client, session: str) -> None:
        self.client = client
        self.session = session
        self.view: ConversationView = initial_state(session)
        self.cursor = 0
        self.agent_name = "general"
        self.agent_source = "default"
        self.connected = False
        self.running = False
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._streamed_text = False

    async def bootstrap(self) -> tuple[str, Any]:
        """Open the session and obtain its authoritative baseline before tailing."""
        await self.client.handshake()
        summary = await self.client.open_session(self.session)
        baseline, seq = await self.client.state(self.session, 0)
        # The host projection establishes the baseline seq; hydrate the typed
        # reducer through the same append-only event protocol before tailing.
        self.view = initial_state(self.session)
        async with aclosing(self.client.stream(self.session, 0, follow=False)) as events:
            async for event in events:
                self.view = apply(self.view, event)
        self.cursor = max(max(0, int(seq)), self.view.last_seq)
        if self.view.last_seq < self.cursor:
            self.view = replace(self.view, last_seq=self.cursor)
        current = await self.client.current_agent(self.session)
        self.agent_name = str(getattr(current, "name", "general"))
        self.agent_source = str(getattr(current, "source", "default"))
        self.connected = True
        return "", summary

    def start_turn(
        self, content: str, post_event: Callable[[Event | None], Awaitable[None] | None]
    ) -> None:
        if self._closed or self.running:
            return
        self.running = True
        self._streamed_text = False
        self._task = asyncio.create_task(self._consume_turn(self.session, content, post_event))

    def resume(self, post_event: Callable[[Event | None], Awaitable[None] | None]) -> None:
        """Follow an already-running daemon turn after reconnecting."""
        if self._closed or self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._consume_existing(self.session, post_event))

    async def _consume_existing(
        self, session: str, post_event: Callable[[Event | None], Awaitable[None] | None]
    ) -> None:
        try:
            async with aclosing(self.client.stream(session, self.cursor, follow=True)) as events:
                async for event in events:
                    if session != self.session:
                        return
                    handled = post_event(event)
                    if handled is not None:
                        await handled
                    if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                        break
        except asyncio.CancelledError:
            raise
        except ClientError:
            self.connected = False
            if session == self.session:
                post_event(None)
        finally:
            if session == self.session:
                self.running = False

    async def _consume_turn(
        self,
        session: str,
        content: str,
        post_event: Callable[[Event | None], Awaitable[None] | None],
    ) -> None:
        try:
            async with aclosing(
                turn_events(self.client, session, content, self.cursor)
            ) as events:
                async for event in events:
                    if session != self.session:
                        return
                    handled = post_event(event)
                    if handled is not None:
                        await handled
                    if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                        break
            if session == self.session:
                self.connected = True
        except asyncio.CancelledError:
            raise
        except ClientError:
            if session == self.session:
                self.connected = False
                post_event(None)
            return
        finally:
            if session == self.session:
                self.running = False

    async def switch_session(self, session: str) -> None:
        """Detach a prior session's live stream before replacing its projection."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self.running = False
        self.session = session
        self.view = initial_state(session)
        self.cursor = 0

    def ingest(self, event: Event) -> tuple[bool, str]:
        """Canonically reduce an event; presentation reads only this projection."""
        before = self.view
        self.view = apply(self.view, event)
        self.cursor = max(self.cursor, self.view.last_seq)
        return self.view is not before, ""

    async def run_reconnect(
        self, post_event: Callable[[Event | None], Awaitable[None] | None]
    ) -> int:
        """Replay the missed event tail from the last canonical cursor."""
        count = 0
        async with aclosing(self.client.stream(self.session, self.cursor, follow=False)) as events:
            async for event in events:
                handled = post_event(event)
                if handled is not None:
                    await handled
                count += 1
        self.connected = True
        return count

    def replace_client(self, client: Client) -> Client:
        """Install a newly opened host client after a lost transport."""
        previous = self.client
        self.client = client
        return previous

    async def cancel(self) -> tuple[bool, int]:
        return await self.client.cancel(self.session, reason="user requested")

    async def select_model(self, ref: str) -> Any:
        result = await self.client.select_model(self.session, ref)
        return result

    async def select_agent(self, name: str) -> Any:
        result = await self.client.select_agent(self.session, name)
        self.agent_name = str(result.name)
        self.agent_source = str(result.source)
        return result

    async def get_agent(self, agent_id: str) -> Any | None:
        """Check the host's reducer-backed transcript contract for an agent."""
        result = await self.client.agent_transcript(self.session, agent_id)
        return result if result.get("found") else None

    def find_agent(self, agent_id: str):
        """Locate an AgentView recursively in the canonical reduced session."""
        def locate(view: ConversationView):
            direct = view.agents.get(agent_id)
            if direct is not None:
                return direct
            for candidate in view.agents.values():
                found = locate(candidate.body)
                if found is not None:
                    return found
            return None

        return locate(self.view)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.client.aclose()


__all__ = ["TuiController"]
