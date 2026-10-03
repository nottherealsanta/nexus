"""Voice lifecycle and transcription values (VOICE_PLAN.md §4 and §7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    since: datetime = datetime.min.replace(tzinfo=timezone.utc)


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
