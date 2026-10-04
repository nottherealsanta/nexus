"""UI-neutral host-client lifecycle and reducer seam (PLAN §14.7)."""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from typing import Any

from ..client.protocol import Client, ClientError
from ..client.turn_stream import turn_events
from ..events import Event
from ..host.protocol import SessionCancelResult
from ..view import ConversationView, apply, initial_state


class ModelEffortSelectionError(ClientError):
    """A model/effort commit failed, recording whether model selection landed."""

    def __init__(self, error: ClientError, *, model_selected: bool) -> None:
        self.model_selected = model_selected
        self.original = error
        super().__init__(str(error))


#: Sessions whose reduced view is kept while another session is shown (open tabs).
PARKED_SESSIONS = 8


class SessionController:
    """Owns only client commands, the canonical reducer, and stream tasks."""

    def __init__(self, client: Client, session: str) -> None:
        self.client = client
        self.session = session
        self.view: ConversationView = initial_state(session)
        self.cursor = 0
        self.completion_seq = 0
        # Live-only notification count; replay and session switches leave it alone.
        self.completion_bell = 0
        self.agent_name = "build"
        self.agent_source = "default"
        self.agent_color: str | None = None
        self.provider: str | None = None
        self.model: str | None = None
        self.reasoning_effort: str | None = None
        self.supported_levels: list[str] = []
        self.stored_override: str | None = None
        self.reasoning_effort_source: str | None = None
        self.agent_metadata_known = False
        self.thinking_budget: int | None = None
        self.running = False
        #: Called when a turn stream ends by any path so the UI can resync.
        self.on_stream_end: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._agent_metadata_revision = 0
        self._selection_lock = asyncio.Lock()
        self._bootstrap_revision = 0
        # Reduced views of sessions left behind: (view, cursor, completion_seq).
        # Returning to one re-reads only the events after its cursor.
        self._parked: OrderedDict[str, tuple[ConversationView, int, int]] = OrderedDict()
        self._resume_cursor = False  # set by switch_session; consumed by the next bootstrap

    def forget(self, session: str) -> None:
        """Drop a parked view (its tab was closed)."""
        self._parked.pop(session, None)

    async def _refresh_agent_metadata(
        self,
        session: str,
        revision: int,
        *,
        bootstrap_revision: int | None = None,
    ) -> Any:
        if (
            session != self.session
            or revision != self._agent_metadata_revision
            or (
                bootstrap_revision is not None
                and bootstrap_revision != self._bootstrap_revision
            )
        ):
            return None
        current = await self.client.current_agent(session)
        if (
            session != self.session
            or revision != self._agent_metadata_revision
            or (
                bootstrap_revision is not None
                and bootstrap_revision != self._bootstrap_revision
            )
        ):
            return current
        self.agent_name = str(getattr(current, "name", "build"))
        self.agent_source = str(getattr(current, "source", "default"))
        self.agent_color = getattr(current, "color", None)
        self.provider = getattr(current, "provider", None)
        self.model = getattr(current, "model", None)
        self.reasoning_effort = getattr(current, "reasoning_effort", None)
        self.supported_levels = list(getattr(current, "supported_levels", ()) or ())
        self.stored_override = getattr(current, "stored_override", None)
        self.reasoning_effort_source = getattr(current, "reasoning_effort_source", None)
        self.agent_metadata_known = True
        self.thinking_budget = getattr(current, "thinking_budget", None)
        return current

    async def refresh_agent_metadata(self) -> Any:
        """Refresh the complete root-agent presentation from host-owned state."""
        return await self._refresh_agent_metadata(
            self.session, self._agent_metadata_revision
        )

    async def bootstrap(self) -> tuple[str, Any]:
        """Open the session and obtain its authoritative baseline before tailing."""
        self._bootstrap_revision += 1
        bootstrap_revision = self._bootstrap_revision
        session = self.session
        metadata_revision = self._agent_metadata_revision
        await self.client.handshake()
        if not self._bootstrap_is_current(session, bootstrap_revision):
            return "", None
        summary = await self.client.open_session(session)
        if not self._bootstrap_is_current(session, bootstrap_revision):
            return "", summary
        # The typed reducer is hydrated from the append-only event stream, which
        # carries its own last seq. The host's full-view baseline used to be fetched
        # first and only its seq kept: a whole-session fold, serialization and
        # transfer on every session open, thrown away.
        # A parked session resumes from its cursor; the append-only log only grows.
        view, start, completion_seq = self.view, self.cursor, self.completion_seq
        resume, self._resume_cursor = self._resume_cursor, False
        if not resume or start <= 0 or view.session_id != session:
            view, start, completion_seq = initial_state(session), 0, 0
        # The agent lookup is independent of the replay and scales with session size,
        # so it overlaps the event stream instead of following it.
        metadata = asyncio.ensure_future(
            self._refresh_agent_metadata(
                session, metadata_revision, bootstrap_revision=bootstrap_revision
            )
        )
        try:
            async with aclosing(self.client.stream(session, start, follow=False)) as events:
                async for event in events:
                    if not self._bootstrap_is_current(session, bootstrap_revision):
                        metadata.cancel()
                        return "", summary
                    view = apply(view, event)
                    if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
                        completion_seq = max(completion_seq, event.seq)
            if not self._bootstrap_is_current(session, bootstrap_revision):
                metadata.cancel()
                return "", summary
            cursor = view.last_seq
            self.view = view
            self.cursor = cursor
            self.completion_seq = completion_seq
            await metadata
        except BaseException:
            metadata.cancel()
            raise
        return "", summary

    def _bootstrap_is_current(self, session: str, revision: int) -> bool:
        return session == self.session and revision == self._bootstrap_revision

    def start_turn(
        self, content: str, post_event: Callable[[Event | None], Awaitable[None] | None]
    ) -> None:
        if self._closed or self.running:
            return
        self.running = True
        self._task = asyncio.create_task(
            self._consume(
                self.session,
                turn_events(self.client, self.session, content, self.cursor),
                post_event,
            )
        )

    def resume(self, post_event: Callable[[Event | None], Awaitable[None] | None]) -> None:
        """Follow an already-running daemon turn after reconnecting."""
        if self._closed or self.running:
            return
        self.running = True
        self._task = asyncio.create_task(
            self._consume(
                self.session,
                self.client.stream(self.session, self.cursor, follow=True),
                post_event,
            )
        )

    async def _consume(
        self,
        session: str,
        events: AsyncIterator[Event],
        post_event: Callable[[Event | None], Awaitable[None] | None],
    ) -> None:
        try:
            async with aclosing(events):
                async for event in events:
                    if session != self.session:
                        return
                    terminal = event.type in {"turn.completed", "turn.failed", "turn.cancelled"}
                    if event.type == "turn.started":
                        self.running = True
                    if terminal:
                        # Clear before the UI handles the final event so its last
                        # sync stops the activity animation.
                        self.running = False
                    handled = post_event(event)
                    if handled is not None:
                        await handled
                    if terminal:
                        if not self.view.input_queue:
                            break
                        self.running = True
        except asyncio.CancelledError:
            raise
        except ClientError:
            if session == self.session:
                self.running = False
                post_event(None)
        finally:
            if session == self.session:
                self.running = False
                if self.on_stream_end is not None:
                    self.on_stream_end()

    async def switch_session(self, session: str) -> None:
        """Detach a prior session's live stream before replacing its projection."""
        self._bootstrap_revision += 1
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self.running = False
        if self.session and self.cursor > 0 and self.view.session_id == self.session:
            self._parked[self.session] = (self.view, self.cursor, self.completion_seq)
            self._parked.move_to_end(self.session)
            while len(self._parked) > PARKED_SESSIONS:
                self._parked.popitem(last=False)
        self.session = session
        self._agent_metadata_revision += 1
        resumed = self._parked.pop(session, None)
        self.view, self.cursor, self.completion_seq = resumed or (initial_state(session), 0, 0)
        self._resume_cursor = resumed is not None
        self.agent_name = "build"
        self.agent_source = "default"
        self.agent_color = None
        self.provider = None
        self.model = None
        self.reasoning_effort = None
        self.supported_levels = []
        self.stored_override = None
        self.reasoning_effort_source = None
        self.agent_metadata_known = False
        self.thinking_budget = None

    def ingest(self, event: Event) -> tuple[bool, str]:
        """Canonically reduce an event; presentation reads only this projection."""
        before = self.view
        notify = (
            event.session == self.session
            and event.seq > self.cursor
            and event.type in {"turn.completed", "turn.failed"}
        )
        self.view = apply(self.view, event)
        self.cursor = max(self.cursor, self.view.last_seq)
        if event.type in {"turn.completed", "turn.failed", "turn.cancelled"}:
            self.completion_seq = max(self.completion_seq, event.seq)
        if notify and self.view is not before:
            self.completion_bell += 1
        return self.view is not before, ""

    def replace_client(self, client: Client) -> Client:
        """Install a newly opened host client after a lost transport."""
        previous = self.client
        self.client = client
        return previous

    async def cancel(self) -> SessionCancelResult:
        return await self.client.cancel_to_composer(self.session, reason="user requested")

    async def select_model(self, ref: str) -> Any:
        return await self._select_with_metadata_refresh(
            lambda session: self.client.select_model(session, ref)
        )

    async def select_model_and_effort(self, ref: str, effort: str | None) -> Any:
        """Apply a model and its pending explicit effort as one guarded UI action."""
        async with self._selection_lock:
            self._agent_metadata_revision += 1
            revision = self._agent_metadata_revision
            session = self.session
            model_selected = False
            try:
                result = await self.client.select_model(session, ref)
                model_selected = True
                if session != self.session or revision != self._agent_metadata_revision:
                    return result
                await self.client.select_reasoning_effort(session, effort)
                return result
            except ClientError as exc:
                raise ModelEffortSelectionError(
                    exc, model_selected=model_selected
                ) from exc
            finally:
                # If either durable step succeeded (or failed part-way), render
                # the host's authoritative state instead of optimistic values.
                with contextlib.suppress(ClientError):
                    await self._refresh_agent_metadata(session, revision)

    async def select_agent(self, name: str) -> Any:
        return await self._select_with_metadata_refresh(
            lambda session: self.client.select_agent(session, name)
        )

    async def reset_agent(self) -> Any:
        return await self._select_with_metadata_refresh(
            self.client.reset_agent
        )

    async def _select_with_metadata_refresh(self, select) -> Any:
        self._agent_metadata_revision += 1
        revision = self._agent_metadata_revision
        session = self.session
        async with self._selection_lock:
            try:
                result = await select(session)
            except Exception:
                with contextlib.suppress(ClientError):
                    await self._refresh_agent_metadata(session, revision)
                raise
            await self._refresh_agent_metadata(session, revision)
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


__all__ = ["ModelEffortSelectionError", "SessionController"]
