"""Minimal server-sent events parser over an httpx line stream.

Yields `(event_name, data_text)` per dispatched event. Bounded: a single event
larger than `MAX_EVENT_CHARS` raises.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

MAX_EVENT_CHARS = 8_000_000


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    name = ""
    data: list[str] = []
    size = 0
    async for line in lines:
        if line == "":
            if data:
                yield name or "message", "\n".join(data)
            name, data, size = "", [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            size += len(value)
            if size > MAX_EVENT_CHARS:
                raise ValueError("server-sent event exceeds size limit")
            data.append(value)
    if data:
        yield name or "message", "\n".join(data)
