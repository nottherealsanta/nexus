"""A small HTTP client for the daemon's `/api/*`. The CLI uses it; other
surfaces (TUI, NSH) speak the same API from their own languages.

`events()` yields the durable event stream. Each event carries `id` (the stored
row id) and `cursor` (`<log generation>:<id>`); pass the last one back as
`cursor=` (sent as `Last-Event-ID`) to resume without losing or duplicating an
event. A cursor this log cannot serve raises `ResyncRequired` with the current
generation and head before any event is yielded; the caller reloads state and
resumes from `head_cursor`. `bus.overflow` is yielded as an ordinary event with
`data.dropped_after`; reconnect from the last cursor you applied.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from typing import Any

import httpx

from . import daemon
from .llmhub.sse import parse_sse

RESYNC_CODES = {"invalid_cursor", "cursor_generation", "cursor_ahead", "history_unavailable"}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class ResyncRequired(ApiError):
    """The event cursor cannot be resumed; reload state from the reported head."""

    def __init__(self, status: int, message: str, *, code: str | None = None, generation: str | None = None,
                 head: int | None = None, head_cursor: str | None = None, floor: int | None = None) -> None:
        super().__init__(status, message)
        self.code = code
        self.generation = generation
        self.head = head
        self.head_cursor = head_cursor
        self.floor = floor


def _stream_error(status: int, body: str) -> ApiError:
    try:
        data = json.loads(body)
    except ValueError:
        return ApiError(status, body[:500])
    message = data.get("error", body[:500]) if isinstance(data, dict) else body[:500]
    if isinstance(data, dict) and (data.get("resync") or data.get("code") in RESYNC_CODES):
        return ResyncRequired(status, message, code=data.get("code"), generation=data.get("generation"),
                              head=data.get("head"), head_cursor=data.get("head_cursor"), floor=data.get("floor"))
    return ApiError(status, message)


class Client:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self._local_discovery = False
        self.http = httpx.AsyncClient(base_url=self.url, headers={"authorization": f"Bearer {token}"}, timeout=httpx.Timeout(60.0, connect=5.0))

    @classmethod
    def connect(cls) -> Client:
        """Find the daemon, starting it if needed."""
        found = daemon.ensure()
        client = cls(found["url"], found["token"])
        client._local_discovery = True
        return client

    async def _refresh_local_connection(self) -> bool:
        """Only locally discovered clients may refresh daemon credentials on restart."""
        if not self._local_discovery:
            return False
        found = await asyncio.to_thread(daemon.discover)
        if found is None:
            return False
        authorization = f"Bearer {found['token']}"
        url = found["url"].rstrip("/")
        if self.url == url and self.http.headers.get("authorization") == authorization:
            return False
        self.url = url
        self.http.base_url = httpx.URL(url)
        self.http.headers["authorization"] = authorization
        return True

    async def close(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None,
                   *, idempotency_key: str | None = None, **params: Any) -> Any:
        """One JSON API call.

        A keyed mutation is retried on a transport failure with the same key, so
        the server can replay the original acceptance instead of creating a second
        turn. An HTTP error (including a replayed one) is never retried here.
        """
        headers = {"idempotency-key": idempotency_key} if idempotency_key else {}
        attempts = 3 if idempotency_key else 1
        for attempt in range(1, attempts + 1):
            try:
                response = await self.http.request(
                    method, path, json=body, headers=headers,
                    params={k: v for k, v in params.items() if v is not None})
                break
            except httpx.TransportError:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(0.25 * attempt)
        if response.status_code >= 400:
            try:
                message = response.json().get("error", response.text)
            except ValueError:
                message = response.text
            raise ApiError(response.status_code, message)
        return response.json() if response.content else None

    @staticmethod
    def new_idempotency_key() -> str:
        """A fresh key for one user action; reuse it for transport retries only."""
        return uuid.uuid4().hex

    async def snapshot(self, session_id: str | None = None, *, message_limit: int | None = None) -> dict[str, Any]:
        """Projects, sessions, running/waiting state and a watermark to resume after."""
        return await self.call("GET", "/api/snapshot", session=session_id, message_limit=message_limit)

    @asynccontextmanager
    async def download(self, path: str, **params: Any) -> AsyncIterator[httpx.Response]:
        """Stream a response body; an HTTP error is raised before any bytes are used."""
        request = self.http.stream("GET", path, params={k: v for k, v in params.items() if v is not None})
        async with request as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")
                try:
                    message = json.loads(body).get("error", body)
                except (ValueError, AttributeError):
                    message = body
                raise ApiError(response.status_code, message)
            yield response

    async def events(self, session_id: str | None = None, *, cursor: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Server-sent events as dicts; the first is `server.connected`.

        `server.connected` carries `generation`, `head` and `head_cursor`; every
        later event carries `id` and `cursor` (except `bus.overflow`, which is
        stream control). `cursor` is sent as `Last-Event-ID`.
        """
        params = {"session": session_id} if session_id else {}
        headers = {"last-event-id": cursor} if cursor else {}
        async with self.http.stream("GET", "/api/event", params=params, headers=headers,
                                    timeout=httpx.Timeout(None, connect=5.0)) as response:
            if response.status_code >= 400:
                raise _stream_error(response.status_code, (await response.aread()).decode("utf-8", "replace"))
            async for name, data in parse_sse(response.aiter_lines()):
                event = json.loads(data)
                if "type" not in event:
                    event = {"type": name, **event}
                yield event

    async def watch(self, session_id: str | None = None, *, cursor: str | None = None,
                    backoff: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0),
                    on_resync: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
                    ) -> AsyncIterator[dict[str, Any]]:
        """A self-healing event stream: reconnect, deduplicate, resync explicitly.

        The cursor advances only after an event has been consumed (the next
        iteration), so an exception while applying an event replays it. A cursor
        failure or overflow fetches a fresh snapshot, emits a synthetic
        `client.resync` event carrying it, and resumes strictly after its
        watermark. Reconnecting never submits a prompt or continuation.
        """
        current = cursor
        last_id: int | None = None
        failures = 0
        while True:
            try:
                async with aclosing(self.events(session_id, cursor=current)) as stream:
                    async for event in stream:
                        if event["type"] == "server.connected":
                            if last_id is None:
                                resumed = event.get("resumed_from")
                                last_id = int(resumed if resumed is not None else (event.get("head") or 0))
                            yield event
                            if current is None:
                                current = event.get("head_cursor")
                            continue
                        if event["type"] == "bus.overflow":
                            raise ResyncRequired(409, "event subscriber overflowed", code="bus.overflow")
                        event_id = event.get("id")
                        if event_id is not None and last_id is not None and int(event_id) <= last_id:
                            continue
                        yield event
                        if event_id is not None:
                            last_id = int(event_id)
                        if event.get("cursor"):
                            current = event["cursor"]
                        failures = 0
                # The server ended the stream without an error frame (for example
                # after shutdown): reconnect from the last applied event, but do
                # not spin forever on a stream that keeps ending immediately.
                if failures >= len(backoff):
                    raise ApiError(0, "the event stream ended repeatedly; resync required")
                failures += 1
            except ResyncRequired as exc:
                deleted = False
                try:
                    state = await self.snapshot(session_id)
                except ApiError as snapshot_error:
                    if snapshot_error.status != 404 or session_id is None:
                        raise
                    # The selected session was deleted while we were away; rebuild
                    # the global view and let the caller close its session view.
                    deleted = True
                    state = await self.snapshot(None)
                    state["selected"] = None
                    state["deleted_session"] = session_id
                if on_resync is not None:
                    result = on_resync(state)
                    if result is not None:
                        await result
                yield {"type": "client.resync", "id": None,
                       "data": {"reason": exc.code, "message": exc.message, "snapshot": state}}
                current = state["watermark"]["head_cursor"]
                last_id = int(state["watermark"]["head"])
                if deleted:
                    session_id = None
                failures = 0
                continue
            except ApiError as exc:
                if exc.status == 401 and await self._refresh_local_connection():
                    continue  # keep the cursor; recovery events close the lost waits
                if exc.status == 404 and session_id is not None:
                    state = await self.snapshot(None)
                    state["selected"] = None
                    state["deleted_session"] = session_id
                    if on_resync is not None:
                        result = on_resync(state)
                        if result is not None:
                            await result
                    yield {"type": "client.resync", "id": None,
                           "data": {"reason": "session_deleted", "message": exc.message, "snapshot": state}}
                    current = state["watermark"]["head_cursor"]
                    last_id = int(state["watermark"]["head"])
                    session_id = None  # continue with the global stream
                    failures = 0
                    continue
                if failures >= len(backoff):
                    raise
                failures += 1
            except httpx.HTTPError:
                if failures >= len(backoff):
                    raise
                failures += 1
            await asyncio.sleep(backoff[min(max(failures - 1, 0), len(backoff) - 1)] if backoff else 0)
