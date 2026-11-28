"""Standalone recorder storage (plan 54, B1).

The recorder keeps a microphone recording as checksummed PCM chunks outside the workspace and the session log. See
`nexus.recorder.store` for the durable commit order, recovery rules and WAV streaming.
"""
from __future__ import annotations

from .store import (
    BYTES_PER_SECOND,
    CHANNELS,
    DEFAULT_CHUNK_SAMPLES,
    FORMAT,
    MAX_CHUNK_BYTES,
    SAMPLE_RATE,
    ChunkReceipt,
    RecorderConflict,
    RecorderCorrupt,
    RecorderError,
    RecorderInvalid,
    RecorderNotFound,
    RecorderSecurityError,
    RecorderState,
    RecorderStore,
    recordings_root,
)

__all__ = [
    "BYTES_PER_SECOND",
    "CHANNELS",
    "DEFAULT_CHUNK_SAMPLES",
    "FORMAT",
    "MAX_CHUNK_BYTES",
    "SAMPLE_RATE",
    "ChunkReceipt",
    "RecorderConflict",
    "RecorderCorrupt",
    "RecorderError",
    "RecorderInvalid",
    "RecorderNotFound",
    "RecorderSecurityError",
    "RecorderState",
    "RecorderStore",
    "recordings_root",
]
