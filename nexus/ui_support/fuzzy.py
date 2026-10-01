"""Shared fuzzy matcher for the command palette and model picker.

Contract: case-insensitive subsequence matching of a query against text,
returning ``(score, positions)`` (higher score is better) or ``None``.
Scoring: every matched character earns BASE; a match at the start of the
text or after a word boundary (space, ``/ - _ . :`` or a lower->Upper camel
transition) earns BOUNDARY; a match directly after the previous one earns
CONSECUTIVE; each skipped character between matches costs GAP and each
unmatched leading character costs LEAD. A contiguous substring match gets a
modest SUBSTRING bonus on top, so it outranks a scattered match of the same
quality but not one that lands on several word boundaries (``gm`` prefers
"git merge" over the substring in "fragment"). The best
alignment is found by an O(query * text) dynamic programme. Text is capped
at 512 characters and the query at 128. Positions index ``text`` by code
point. ``nexus/ui/web/js/fuzzy.js`` ports this with the same constants, so
both surfaces rank identically; keep the two in step. Pure stdlib.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")

MAX_TEXT = 512
MAX_QUERY = 128
BASE = 16
CONSECUTIVE = 15
BOUNDARY = 30
GAP = 3
LEAD = 2
SUBSTRING = 20
SEPARATORS = " /-_.:"
_NEG = float("-inf")


def _is_lower(c: str) -> bool:
    return c.lower() == c and c.upper() != c


def _is_upper(c: str) -> bool:
    return c.upper() == c and c.lower() != c


def _boundary_flags(text: str) -> list[int]:
    return [
        BOUNDARY
        if j == 0 or text[j - 1] in SEPARATORS or (_is_lower(text[j - 1]) and _is_upper(text[j]))
        else 0
        for j in range(len(text))
    ]


def _score_positions(pos: list[int], bonus: list[int]) -> int:
    score = -LEAD * pos[0]
    for i, p in enumerate(pos):
        score += BASE + bonus[p]
        if i:
            gap = p - pos[i - 1] - 1
            score += CONSECUTIVE if gap == 0 else -GAP * gap
    return score


def fuzzy_match(query: str, text: str) -> tuple[int, tuple[int, ...]] | None:
    """Return ``(score, positions)`` for a subsequence match, else ``None``."""
    q = [c.lower() for c in query.strip()[:MAX_QUERY]]
    if not q:
        return (0, ())
    text = text[:MAX_TEXT]
    low = [c.lower() for c in text]
    n, m = len(q), len(text)
    if n > m:
        return None
    bonus = _boundary_flags(text)
    # dp[i][j]: best score with q[i] matched at text[j]; prev[i][j]: previous match.
    prev_row: list[float] = []
    back: list[list[int]] = []
    for i in range(n):
        row: list[float] = [_NEG] * m
        ptr = [-1] * m
        run, run_k = _NEG, -1
        for j in range(m):
            if low[j] != q[i]:
                if i and j >= 2:
                    run, run_k = _decay(run, run_k, prev_row, j)
                continue
            here = BASE + bonus[j]
            if i == 0:
                row[j] = here - LEAD * j
            else:
                if j >= 2:
                    run, run_k = _decay(run, run_k, prev_row, j)
                best, bk = _NEG, -1
                if j >= 1 and prev_row[j - 1] > _NEG:
                    best, bk = prev_row[j - 1] + CONSECUTIVE, j - 1
                if run > best:
                    best, bk = run, run_k
                if best > _NEG:
                    row[j] = here + best
                    ptr[j] = bk
        back.append(ptr)
        prev_row = row
    end = max(range(m), key=lambda j: (prev_row[j], -j))
    if prev_row[end] == _NEG:
        return None
    pos = [end]
    for i in range(n - 1, 0, -1):
        pos.append(back[i][pos[-1]])
    pos.reverse()
    score = int(prev_row[end])
    # Best contiguous window, if any, gets the substring bonus.
    win_best: tuple[int, list[int]] | None = None
    for s in range(m - n + 1):
        if all(low[s + i] == q[i] for i in range(n)):
            w = list(range(s, s + n))
            ws = _score_positions(w, bonus)
            if win_best is None or ws > win_best[0]:
                win_best = (ws, w)
    if win_best is not None:
        return (win_best[0] + SUBSTRING, tuple(win_best[1]))
    return (score, tuple(pos))


def _decay(run: float, run_k: int, prev_row: list[float], j: int) -> tuple[float, int]:
    """Best earlier (k <= j-2) predecessor value, with the gap to j charged."""
    run = run - GAP if run > _NEG else _NEG
    cand = prev_row[j - 2] - GAP if prev_row[j - 2] > _NEG else _NEG
    if cand > run:
        return cand, j - 2
    return run, run_k


def fuzzy_filter(
    query: str, items: Iterable[T], key: Callable[[T], str] = lambda x: x  # type: ignore[assignment,return-value]
) -> list[tuple[T, tuple[int, ...]]]:
    """Matching items by score descending; ties keep the original order."""
    hits = []
    for index, item in enumerate(items):
        found = fuzzy_match(query, key(item))
        if found is not None:
            hits.append((-found[0], index, item, found[1]))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [(item, pos) for _, _, item, pos in hits]


def highlight_spans(positions: Iterable[int]) -> list[tuple[int, int]]:
    """Merge consecutive indices into ``[start, end)`` spans."""
    spans: list[tuple[int, int]] = []
    for p in positions:
        if spans and spans[-1][1] == p:
            spans[-1] = (spans[-1][0], p + 1)
        else:
            spans.append((p, p + 1))
    return spans
