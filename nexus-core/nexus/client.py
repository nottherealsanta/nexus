"""A small HTTP client for the daemon's `/api/*`. The CLI uses it; other
surfaces (TUI, NSH) speak the same API from their own languages.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from . import daemon
from .llmhub.sse import parse_sse


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


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

    async def events(self, session_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
        """Server-sent events as dicts; the first is `server.connected`."""
        params = {"session": session_id} if session_id else {}
        async with self.http.stream("GET", "/api/event", params=params, timeout=httpx.Timeout(None, connect=5.0)) as response:
            if response.status_code >= 400:
                raise ApiError(response.status_code, (await response.aread()).decode()[:500])
            async for name, data in parse_sse(response.aiter_lines()):
                event = json.loads(data)
                yield event if name != "server.connected" else {"type": name, "data": event}
