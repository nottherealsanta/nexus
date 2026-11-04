"""The Manager: control-plane agents above sessions (docs/manager-design.md).

This package starts with retrieval (docs/manager-design.md 7.1 and M1's ranking,
fuzzy pass and spoken handles).
`retrieval.py` is self-contained and read-only around `Store`; the Manager
service, turn loop, stage, voices and tools land in later milestones.
"""
from __future__ import annotations

from .retrieval import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Ambiguity,
    HandleRegistry,
    MatchReason,
    ProjectNotFound,
    RankedSession,
    RankingContext,
    RetrievalError,
    RetrievalResult,
    Retriever,
)

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Ambiguity",
    "HandleRegistry",
    "MatchReason",
    "ProjectNotFound",
    "RankedSession",
    "RankingContext",
    "RetrievalError",
    "RetrievalResult",
    "Retriever",
]
