"""One-line, redacted error text for toasts (DESIGN OVERHAUL step 15 follow-up).

A client that catches a failure from the host or from starting another project's daemon shows
this reason instead of a bare ``str(exc)``: credentials are redacted, whitespace collapses, the
length is bounded, and an exception with no message (``TimeoutError()``) still names its type, so a
failure is never an empty, silent toast.
"""

from __future__ import annotations

from ..util import redact_secrets


def error_text(exc: BaseException, limit: int = 400) -> str:
    return redact_secrets(" ".join(str(exc).split()) or type(exc).__name__)[:limit]
