"""Small dependency-free helpers shared by the lower layers."""
from __future__ import annotations

import os
import re
import time
import uuid

#: URL userinfo (``scheme://user:password@host``). The userinfo section may not
#: contain ``/`` or whitespace, so a bare email address (``user@host``) is never
#: matched -- only credentials that precede a URL authority.
_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")

_CREDENTIAL = "<redacted>"


def redact_url_userinfo(text: str) -> str:
    """Strip ``user:password@`` userinfo from every URL in ``text``.

    Used on strings that may be surfaced in events, logs, or error results: a
    configured catalogue or provider URL may carry embedded credentials, and an
    exception from an HTTP client can echo the full URL back. The userinfo is
    replaced (not merely truncated) so a short password cannot survive as a
    fragment, and bare email addresses are left alone.
    """
    if not text:
        return text
    return _URL_USERINFO.sub(rf"\g<scheme>{_CREDENTIAL}@", text)


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
