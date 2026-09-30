"""Small dependency-free helpers shared by the lower layers."""
from __future__ import annotations

import os
import re
import time
import uuid

#: URL userinfo (``scheme://user:password@host``). The userinfo section may not
#: contain ``/`` or whitespace, so a bare email address (``user@host``) is never
#: matched -- only credentials that precede a URL authority.
_URL_USERINFO = re.compile(r"(?<![a-zA-Z0-9+.\-])(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]+@")

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


#: Patterns that hide echoed credentials before a string reaches a log, an
#: event, a repr, or an error. Deliberately conservative: over-redaction of a
#: supplementary detail is preferred to leaking a token. Order matters --
#: scheme/prefix rules run first. Lives in L0 ``util`` so the config layer can
#: redact a ``command``/``args``/``base_url`` without importing the model layer.
_SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization schemes: "Bearer <token>", "Basic <token>".
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 ***"),
    # Well-known API-key prefixes.
    (re.compile(r"\bsk-[A-Za-z0-9._-]{4,}"), "***"),
    (re.compile(r"\b(?:pk|rk)_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}"), "***"),
    (re.compile(r"\bAIza[A-Za-z0-9._-]{8,}"), "***"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{6,}"), "***"),
    (re.compile(r"\bAKIA[A-Z0-9]{12,}"), "***"),
    # "<credential-name>: <value>" / "<credential-name>=<value>"; keep the name.
    (
        re.compile(
            r"(?i)(\b(?:api[-_]?key|x-api-key|authorization|access[-_]?token|"
            r"refresh[-_]?token|client[-_]?secret|secret|password|token)\b"
            r"\s*[:=]\s*[\"']?)[^\s\"',}]{4,}"
        ),
        r"\1***",
    ),
    # Long opaque tokens (JWTs, base64 blobs) with no recognizable prefix.
    (re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b"), "***"),
)


def redact_secrets(text: str) -> str:
    """Remove likely credentials from errant provider text.

    Shared by the transport's error detail, by adapters that surface a provider
    error frame which never passed through the HTTP layer, and by config/adapter
    reprs that may echo a URL or argv. URL userinfo (``scheme://user:pass@host``)
    is stripped as well, because a transport error can echo a full URL.
    """
    text = redact_url_userinfo(text)
    for pattern, replacement in _SECRET_REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


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
