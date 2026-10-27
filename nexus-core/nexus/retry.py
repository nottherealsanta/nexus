"""Bounded, visible provider-request retries (PLAN.md Step 11.4).

A retry here is one more attempt at the *same* provider request inside one agent
step. It is not a client mutation retry and not manual continuation: the other
two have their own durable records (`mutation`) and never answer a question or
approval, nor re-run a tool.

Only a failure before any response stream event has been received is retried,
and only a transport failure or an explicitly transient HTTP status. A failed
attempt is always visible in the event log; every attempt is recorded under its
turn/request. Once a stream event has arrived, failure is terminal for the step
and its partial output stays diagnostic history.
"""
from __future__ import annotations

import math
import os
import random
from collections.abc import Callable
from dataclasses import dataclass

from .llmhub.types import ProviderError

TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
RETRYABLE_PHASES = frozenset({"transport", "response"})
MAX_ATTEMPTS_CEILING = 10
MAX_WAIT_CEILING = 300.0
MAX_ELAPSED_CEILING = 900.0


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_wait: float = 30.0
    max_elapsed: float = 60.0

    @classmethod
    def from_env(cls) -> RetryPolicy:
        def number(name: str, default: float, ceiling: float) -> float:
            try:
                value = float(os.environ.get(name, default))
            except (TypeError, ValueError):
                return default
            return max(0.0, min(value, ceiling)) if math.isfinite(value) else default

        return cls(
            max_attempts=max(1, min(int(number("NEXUS_RETRY_MAX_ATTEMPTS", 3, MAX_ATTEMPTS_CEILING)), MAX_ATTEMPTS_CEILING)),
            base_delay=number("NEXUS_RETRY_BASE_DELAY", 1.0, MAX_WAIT_CEILING),
            max_wait=number("NEXUS_RETRY_MAX_WAIT", 30.0, MAX_WAIT_CEILING),
            max_elapsed=number("NEXUS_RETRY_MAX_ELAPSED", 60.0, MAX_ELAPSED_CEILING),
        )

    def retryable(self, error: ProviderError) -> bool:
        if error.phase not in RETRYABLE_PHASES:
            return False
        return error.status is None or error.status in TRANSIENT_STATUS

    def delay(self, failed_attempt: int, *, retry_after: float | None = None,
              remaining: float | None = None, rng: Callable[[], float] | None = None) -> float | None:
        """The wait before the next attempt, or `None` when the budget cannot fit it."""
        if failed_attempt >= self.max_attempts:
            return None
        if retry_after is not None:
            wait = max(0.0, retry_after)
            if wait > self.max_wait:
                return None  # the provider asked for longer than one wait may be
        else:
            exponent = min(failed_attempt - 1, 20)
            ceiling = min(self.max_wait, self.base_delay * (2 ** exponent))
            draw = (rng or random.random)()
            wait = max(0.0, min(ceiling, self.base_delay * (2 ** exponent) * (0.5 + draw / 2)))
        if remaining is not None and wait > remaining:
            return None
        return wait
