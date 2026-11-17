"""Wait for a real condition, with a generous ceiling, instead of polling a fixed few hundred milliseconds.

A test that passes in 20 ms on an idle machine can need seconds under load (a kernel subprocess starting, a
thread pool busy). The ceiling only bounds a genuine hang; a satisfied condition returns at once.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Any

TIMEOUT = 60.0
INTERVAL = 0.005


async def until(predicate: Callable[[], Any], message: str = "condition", *, timeout: float = TIMEOUT) -> Any:
    """Return the first truthy value of `predicate()`; fail with `message` after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {message}")
        await asyncio.sleep(INTERVAL)
