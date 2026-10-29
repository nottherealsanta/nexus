"""Voice lifecycle and transcription values.

Adapted from old Nexus nexus/voice/model.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

VoiceStateName = Literal[
    "disabled", "unsupported", "absent", "downloading", "loading", "ready", "error"
]


@dataclass(frozen=True, slots=True)
class VoiceState:
    state: VoiceStateName = "absent"
    progress: float = 0.0
    cached: bool = False
    bytes_done: int = 0
    bytes_total: int = 0
    device: str = ""
    revision: str = ""
    message: str = ""
    since: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))


@dataclass(frozen=True, slots=True)
class TranscribeResult:
    text: str
    duration_s: float
    elapsed_s: float
    language: str = ""


class VoiceError(RuntimeError):
    """Safe, typed voice failure suitable for crossing the host boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
