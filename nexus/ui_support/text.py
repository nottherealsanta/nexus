"""Control-safe text for terminal presentation. Nothing is redacted: the user sees what the agent sees."""
from __future__ import annotations

import re
import unicodedata

_LIMIT = 240
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _escaped(text: str) -> str:
    """Escape controls (tab/newline excepted) and Unicode format characters."""
    text = _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)
    if text.isascii():  # no ASCII character is a Unicode format character (Cf)
        return text
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) == "Cf" else char
        for char in text
    )


def escape_controls(text: str) -> str:
    """Escape terminal controls while preserving streaming newlines and tabs."""
    return _escaped(text)


def sanitize(value: object, limit: int = _LIMIT) -> str:
    """Control-escape and length-cap text shown in a terminal."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if value is not None and not isinstance(value, (str, int, float, bool)):
        value = type(value).__name__
    text = re.sub(r"[\t\n]", " ", _escaped(str(value)))
    return text if len(text) <= limit else text[:limit] + "\u2026"


__all__ = ["escape_controls", "sanitize"]
