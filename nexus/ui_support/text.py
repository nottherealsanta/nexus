"""Control-safe, credential-redacted text for terminal presentation."""
from __future__ import annotations

import re
import unicodedata

_LIMIT = 240
_KEY = (
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret"
    r"|token|secret|password|passwd|bearer)\b\s*[:=]\s*\S+"
)
_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(_KEY), r"\1=" + "\u2026"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{6,}"), "sk-ant-\u2026"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-\u2026"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "AKIA\u2026"),
    (re.compile(r"\bghp_[A-Za-z0-9]{12,}\b"), "ghp_\u2026"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{12,}\b"), "AIza\u2026"),
)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


def _escaped(text: str) -> str:
    """Escape controls (tab/newline excepted) and Unicode format characters."""
    text = _CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) == "Cf" else char
        for char in text
    )


def escape_controls(text: str) -> str:
    """Escape terminal controls while preserving streaming newlines and tabs."""
    return _escaped(text)


def sanitize(value: object, limit: int = _LIMIT) -> str:
    """Redact, control-escape and length-cap text shown in a terminal."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if value is not None and not isinstance(value, (str, int, float, bool)):
        value = type(value).__name__
    text = redact(re.sub(r"[\t\n]", " ", _escaped(str(value))))
    return text if len(text) <= limit else text[:limit] + "\u2026"


__all__ = ["escape_controls", "redact", "sanitize"]
