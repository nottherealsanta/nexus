"""Local-only voice inference with bounded, transient audio processing."""

from .manager import VoiceManager
from .model import TranscribeResult, VoiceError, VoiceState

__all__ = ["TranscribeResult", "VoiceError", "VoiceManager", "VoiceState"]
