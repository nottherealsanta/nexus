"""Checked-in wire fixtures and loaders for the recorded-decode cases.

Generated wire (see :mod:`.encoding`) proves the round trip; these recorded
frames prove the decoder handles hand-written provider output, including the
exact event framing real adapters emit. Fixtures are grouped by dialect and used
only by the matching dialect.
"""
from __future__ import annotations

from pathlib import Path

__all__ = [
    "FIXTURES_DIR",
    "load_anthropic",
    "load_gemini",
    "load_openai",
]

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(dialect: str, name: str) -> bytes:
    return (FIXTURES_DIR / dialect / name).read_bytes()


def load_anthropic(name: str) -> bytes:
    """Read one recorded Anthropic SSE fixture by file name."""
    return _load("anthropic", name)


def load_gemini(name: str) -> bytes:
    """Read one recorded Gemini SSE fixture by file name."""
    return _load("gemini", name)


def load_openai(name: str) -> bytes:
    """Read one recorded OpenAI SSE fixture by file name.

    Fixtures live under ``fixtures/openai/`` and are named by dialect:
    ``<dialect>_<scenario>.sse``.
    """
    return _load("openai", name)
