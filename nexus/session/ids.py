"""Session ID validation shared by the store, lock, and migration paths.

The grammar is intentionally identical to the legacy ``nexus.store.SessionStore``
so a new-runtime lock and a legacy lock resolve to the same ``<id>.lock`` file.
"""
from __future__ import annotations

import re

_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")

_MESSAGE = (
    "Session ID must be 1-80 letters, digits, underscores or hyphens, "
    "starting with a letter or digit"
)


def validate_session_id(session_id: object) -> str:
    """Return ``session_id`` unchanged when valid, else raise :class:`ValueError`."""
    if not isinstance(session_id, str) or not _PATTERN.fullmatch(session_id):
        raise ValueError(_MESSAGE)
    return session_id


def is_valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and _PATTERN.fullmatch(session_id) is not None


__all__ = ["is_valid_session_id", "validate_session_id"]
