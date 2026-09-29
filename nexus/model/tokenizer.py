"""Token accounting (plan section 5.2).

Budgets are advisory. A provider that offers ``count_tokens`` is preferred;
otherwise this calibrated character heuristic is used with a safety margin.
"""
from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class Tokenizer(Protocol):
    def count_tokens(self, text: str) -> int: ...


class HeuristicTokenizer:
    """Character-count heuristic: ~3.7 chars/token prose, ~2.9 code/JSON."""

    PROSE_CHARS_PER_TOKEN = 3.7
    CODE_CHARS_PER_TOKEN = 2.9

    def count_tokens(self, text: str, *, code: bool = False) -> int:
        if not text:
            return 0
        ratio = self.CODE_CHARS_PER_TOKEN if code else self.PROSE_CHARS_PER_TOKEN
        return max(1, math.ceil(len(text) / ratio))


DEFAULT_TOKENIZER = HeuristicTokenizer()

__all__ = ["DEFAULT_TOKENIZER", "HeuristicTokenizer", "Tokenizer"]
