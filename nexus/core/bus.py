"""Bounded async event fanout (plan sections 2.2, 3.5).

Every subscriber gets its own bounded buffer. Overload behaviour is explicit and
deterministic: with ``drop_newest`` the incoming event is counted and discarded;
with ``drop_oldest`` the oldest buffered event is evicted to make room. Closing
is safe and drains whatever is already buffered before iteration ends.
"""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from ..errors import BusClosed

DROP_NEWEST = "drop_newest"
DROP_OLDEST = "drop_oldest"
_POLICIES = (DROP_NEWEST, DROP_OLDEST)


class Subscription(AsyncIterator[Any]):
    """A single subscriber's bounded view of the bus."""

    def __init__(self, maxsize: int, policy: str):
        self._buffer: deque[Any] = deque()
        self._signal = asyncio.Event()
        self._closed = False
        self._maxsize = maxsize
        self._policy = policy
        self.dropped = 0

    def push(self, event: Any) -> None:
        if self._closed:
            return
        if len(self._buffer) >= self._maxsize:
            self.dropped += 1
            if self._policy == DROP_OLDEST:
                self._buffer.popleft()
            else:
                return
        self._buffer.append(event)
        self._signal.set()

    def close(self) -> None:
        self._closed = True
        self._signal.set()

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Any:
        while not self._buffer:
            if self._closed:
                raise StopAsyncIteration
            self._signal.clear()
            if not self._buffer and not self._closed:
                await self._signal.wait()
        return self._buffer.popleft()

    async def get(self) -> Any:
        return await self.__anext__()


class Bus:
    """Publish events to any number of bounded subscribers."""

    def __init__(self, *, maxsize: int = 256, policy: str = DROP_NEWEST):
        if policy not in _POLICIES:
            raise ValueError(f"policy must be one of {_POLICIES}")
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._policy = policy
        self._subs: list[Subscription] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self) -> Subscription:
        if self._closed:
            raise BusClosed("Bus is closed")
        sub = Subscription(self._maxsize, self._policy)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
            sub.close()

    def publish(self, event: Any) -> int:
        """Deliver synchronously; return the number of drops this publish caused."""
        if self._closed:
            raise BusClosed("Bus is closed")
        dropped = 0
        for sub in tuple(self._subs):
            before = sub.dropped
            sub.push(event)
            dropped += sub.dropped - before
        return dropped

    async def aclose(self) -> None:
        """Idempotent close. Buffered events remain drainable."""
        if self._closed:
            return
        self._closed = True
        for sub in self._subs:
            sub.close()
        self._subs.clear()


__all__ = ["Bus", "Subscription", "DROP_NEWEST", "DROP_OLDEST"]
