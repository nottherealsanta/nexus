"""Secret redaction for text that is spoken, shown as a caption, or sent to the Manager's model.

Core had header redaction for wire traffic but nothing for free text, and the Manager reads any session: a
session's answer can contain an API key it just printed, and the Manager would read it aloud or forward it to the
model provider. This is a conservative pattern pass, not a guarantee: it catches the common token shapes and
`key = value` assignments for secret-looking names. It runs on everything session-derived that reaches the model
(`context.data_block`, `context.quoted`), on every `manager.speech` sentence and on `POST /api/voice/speak` text.
"""
from __future__ import annotations

import re

MARK = "[redacted]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*"),
)
_ASSIGN = re.compile(
    r"(?i)\b((?:[a-z0-9_.\-]*)(?:api[_-]?key|secret|token|passwd|password|credential)s?[a-z0-9_.\-]*)"
    r"([\"']?\s*[:=]\s*)(?:\"(?:\\[^\n]|[^\"\\\n]){4,}\"|'(?:\\[^\n]|[^'\\\n]){4,}'|[^\s\"',;]{6,})")


def redact(text: str) -> str:
    """``text`` with secret-shaped substrings replaced by ``[redacted]``."""
    if not text:
        return text
    for pattern in _PATTERNS:
        text = pattern.sub(MARK, text)
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{MARK}", text)
