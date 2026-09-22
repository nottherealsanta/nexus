"""Small dependency-free helpers shared by the lower layers."""
from __future__ import annotations

import os
import time
import uuid


def new_id() -> str:
    """Return a UUIDv7-shaped identifier that works on Python 3.11+.

    CPython only gained ``uuid.uuid7`` in 3.14, so the 128-bit layout is built
    explicitly: a 48-bit millisecond timestamp, the version 7 nibble, the RFC
    4122 variant bits, and 74 random bits. The result is time-sortable and
    collision-resistant without relying on the newer stdlib API.
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    random_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0x2 << 62)
        | random_b
    )
    return str(uuid.UUID(int=value))
