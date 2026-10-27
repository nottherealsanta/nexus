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

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
        self.http = httpx.AsyncClient(base_url=self.url, headers={"authorization": f"Bearer {token}"}, timeout=httpx.Timeout(60.0, connect=5.0))

    @classmethod
    def connect(cls) -> Client:
        """Find the daemon, starting it if needed."""
        found = daemon.ensure()
        return cls(found["url"], found["token"])

    async def close(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None, **params: Any) -> Any:
        response = await self.http.request(method, path, json=body, params={k: v for k, v in params.items() if v is not None})
        if response.status_code >= 400:
            try:
                message = response.json().get("error", response.text)
            except ValueError:
                message = response.text
            raise ApiError(response.status_code, message)
        return response.json() if response.content else None

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
