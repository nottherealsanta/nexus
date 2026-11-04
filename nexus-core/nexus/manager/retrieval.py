"""Session retrieval for the Manager: ranking, fuzzy matching and spoken handles.

This is the executable form of docs/manager-design.md 7.1 and section 16's M1 ranking,
fuzzy pass and spoken-handle work. It deliberately builds on the data `Store`
already has, without changing Store or its indexes:

* `Store.search` supplies full-text candidates and snippets. `title` and `user`
  kinds work today; `topic` and `final_answer` hits are used automatically once
  their indexing lands. A `Session.topic` value is also matched when a later
  Store change provides one. This module never writes `search_index`.
* A fuzzy pass always runs over title, topic, project name and a real session
  branch when one is available. `Store` has no branch column today; the session
  directory is deliberately not treated as a branch, because a worktree move
  does not update it. Branch matching therefore stays inert until real branch
  data reaches `Session.branch`.
* Archived sessions are included by default. The default window is three days,
  and the search widens to thirty days and then all time only when a window
  finds nothing. Widening and clipping are reported in `RetrievalResult.notes`.
* Session state is not computed here. Pass the unified `session_state()` result
  as `states=` to annotate/filter it; `state="archived"` works from
  `Session.archived` alone. Attention, the user's view and episode context
  arrive as `RankingContext`; this module does not subscribe to events.
* Spoken handles are derived, de-duplicated and resolved by `HandleRegistry`.
  They are stable once assigned *within that registry*. Persisting the registry
  across daemon restarts is the caller's job via `snapshot()` and the
  constructor's `handles=` argument; this module does not write SQLite.

The integration surface is `Retriever.find_sessions()` and `Retriever.resolve()`.
`RetrievalResult.choice` is deliberately `None` when close candidates make a
query ambiguous, so a caller cannot silently act on a tie.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..store import Project, Session, Store

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

DEFAULT_LIMIT = 8
MAX_LIMIT = 50
DEFAULT_WINDOW_DAYS = 3.0
DEFAULT_WIDEN_DAYS: tuple[float | None, ...] = (30.0, None)
DEFAULT_HALF_LIFE_DAYS = 1.0
#: Recency never takes a match below this share of its score. Recency is a tie-breaker between matches that fit
#: about equally well, not a way to bury an exact match from last week under a loose one from today: the eval in
#: tests/test_manager_eval.py showed a plain half-life decay doing exactly that.
DEFAULT_RECENCY_FLOOR = 0.85
#: A window whose best text score is under this is "nothing good": the next window is tried and the better kept.
STRONG_MATCH = 0.75
#: Below this text score the best result is shown but never offered as a confident choice.
DEFAULT_CONFIDENCE = 0.55
DEFAULT_AMBIGUITY_RATIO = 0.85
DEFAULT_FUZZY_THRESHOLD = 0.66
FUZZY_FIELD_COVERAGE = 0.6
SNIPPET_CHARS = 90
#: Store.search matches every word as a *prefix*, so "over" finds "overflow". A token that matched only as a much
#: longer word earns this share of a real match.
LOOSE_CREDIT = 0.35
LOOSE_PREFIX_RATIO = 0.6
_HIGHLIGHT = re.compile(r"\[([^\]]+)\]")
MAX_HANDLE_CHARS = 40

# The unified state catalog from docs/manager-design.md section 7.
SESSION_STATES = (
    "needs_approval",
    "needs_answer",
    "blocked",
    "failed",
    "working",
    "finished_unread",
    "idle",
    "archived",
)

# FTS kinds carry different weight: a title is the user's own label, a topic
# line is written for paraphrase, a final answer is the agent's own words, and
# a user message is direct.
_KIND_WEIGHTS = {"title": 1.0, "topic": 0.88, "final_answer": 0.74, "user": 0.62}
_KIND_ORDER = {"title": 4, "topic": 3, "final_answer": 2, "user": 1}

BOOST_NEEDS_ATTENTION = 0.50
BOOST_ON_SCREEN_SESSION = 0.35
BOOST_ON_SCREEN_PROJECT = 0.10
BOOST_RECENTLY_DISCUSSED = 0.20
BOOST_PROJECT_IN_FOCUS = 0.15
BOOST_PROJECT_NAMED = 0.5

# Spoken queries are sentences, while Store.search requires every word to
# match as a prefix. These words are dropped before the FTS pass; if a query is
# only stop words, the original tokens are used instead.
STOPWORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "my", "me", "i", "we", "you", "your", "our", "it", "its",
    "one", "ones", "something", "thing", "session", "sessions", "chat", "chats", "conversation", "conversations",
    "about", "regarding", "around", "of", "in", "on", "at", "for", "to", "from", "by", "with", "without", "via",
    "and", "or", "but", "not", "no", "yes", "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "have", "has", "had", "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "what", "which", "who", "whom", "where", "when", "why", "how", "please", "just", "also", "still", "yesterday", "today", "tomorrow", "last", "next", "recent",
    "recently", "week", "too", "got", "made", "make", "making", "some", "any", "all", "there", "here",
    "then", "than", "into", "over", "up", "out", "as", "if", "so", "stuff", "friday", "monday", "tuesday", "wednesday", "thursday", "saturday", "sunday",
})

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_NON_HANDLE = re.compile(r"[^\w-]+", re.UNICODE)
_MULTI_HYPHEN = re.compile(r"-+")


class RetrievalError(ValueError):
    """A retrieval request that cannot be honoured without more information."""


class ProjectNotFound(RetrievalError):
    """The `project` argument matched no project, or more than one."""


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _tokens(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(_WORDS.findall(_fold(_CAMEL.sub(" ", text))))


def _unique(values: Collection[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


#: Verbs that open a spoken request ("find the sidebar one", "search for ..."). They are dropped only at the start:
#: "search" inside the query ("the search session") is a word to look for.
COMMAND_WORDS = frozenset({"find", "show", "tell", "look", "search", "get", "give", "open", "bring", "pull", "read"})
COMMAND_FOLLOWERS = frozenset({"for", "me", "up", "us", "about", "out", "back"})


def _query_tokens(query: str) -> tuple[str, ...]:
    words = list(_tokens(query))
    while words and words[0] in COMMAND_WORDS | {"please", "can", "could", "would", "you"}:
        words.pop(0)
        while words and words[0] in COMMAND_FOLLOWERS:
            words.pop(0)
    tokens = _unique(words) or _unique(_tokens(query))
    content = tuple(token for token in tokens if len(token) > 1 and token not in STOPWORDS)
    return content or tokens


def _snippet(text: str | None) -> str | None:
    """One line for speech, without FTS highlight brackets, clipped with an ellipsis."""
    if not text:
        return None
    clean = " ".join(text.replace("[", "").replace("]", "").split())
    if not clean:
        return None
    return clean if len(clean) <= SNIPPET_CHARS else clean[: SNIPPET_CHARS - 1].rstrip() + "…"


def _human_age(seconds: float) -> str:
    value = max(0, int(seconds))
    if value < 60:
        return "just now"
    if value < 3600:
        return f"{value // 60}m ago"
    if value < 86400:
        return f"{value // 3600}h ago"
    return f"{value // 86400}d ago"


def _window_label(days: float | None) -> str:
    if days is None:
        return "all time"
    if abs(days - round(days)) < 1e-9:
        return f"{round(days)} day" if round(days) == 1 else f"{round(days)} days"
    return f"{days:.1f} days"


def _handle_base(title: str) -> str:
    tokens = _tokens(title)
    words = [token for token in tokens if token not in STOPWORDS] or list(tokens)
    picked: list[str] = []
    length = 0
    for word in words:
        if len(picked) >= 4:
            break
        if picked and length + len(word) + 1 > MAX_HANDLE_CHARS - 8:
            break
        picked.append(word)
        length += len(word) + 1
    base = "-".join(picked).strip("-")
    if not base:
        base = "session"
    return base[:MAX_HANDLE_CHARS].strip("-") or "session"


def _normalize_handle(handle: str) -> str:
    folded = _fold(handle.strip().strip("\"'"))
    folded = folded.replace("_", "-")
    return _MULTI_HYPHEN.sub("-", _NON_HANDLE.sub("-", folded)).strip("-")


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, source in enumerate(left, 1):
        current = [i]
        for j, target in enumerate(right, 1):
            current.append(min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (source != target),
            ))
        previous = current
    return previous[-1]


def _trigrams(value: str) -> set[str]:
    padded = f"  {value}  "
    return {padded[index:index + 3] for index in range(len(padded) - 2)}


def _dice(left: str, right: str) -> float:
    left_grams = _trigrams(left)
    right_grams = _trigrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return 2 * len(left_grams & right_grams) / (len(left_grams) + len(right_grams))


def _phonetic(value: str) -> str:
    """A small, deterministic sound-alike key, aimed at STT-mangled identifiers."""
    text = value
    text = re.sub(r"ph", "f", text)
    text = re.sub(r"th", "t", text)
    text = re.sub(r"ck", "k", text)
    text = re.sub(r"dg", "j", text)
    text = re.sub(r"tch", "ch", text)
    text = re.sub(r"oo", "u", text)
    text = re.sub(r"ou", "u", text)
    text = re.sub(r"ee", "i", text)
    text = re.sub(r"ea", "i", text)
    text = re.sub(r"ie", "i", text)
    text = re.sub(r"ai", "e", text)
    text = re.sub(r"ay", "e", text)
    text = re.sub(r"ey", "i", text)
    text = re.sub(r"y$", "i", text)
    text = re.sub(r"c(?=[eiy])", "s", text)
    text = re.sub(r"c", "k", text)
    text = re.sub(r"q", "k", text)
    text = re.sub(r"x", "ks", text)
    text = re.sub(r"z", "s", text)
    text = re.sub(r"wh", "w", text)
    text = re.sub(r"kn", "n", text)
    text = re.sub(r"wr", "r", text)
    text = re.sub(r"(.)\1+", r"\1", text)
    return text


def _token_similarity(
    query: str, candidate: str, *, min_similarity: float = DEFAULT_FUZZY_THRESHOLD
) -> tuple[float, str]:
    """Best score and matching method for one query token against one field token."""
    if query == candidate:
        return 1.0, "exact"
    shortest = min(len(query), len(candidate))
    longest = max(len(query), len(candidate))

    best = 0.0
    method = ""
    if shortest >= 3 and (candidate.startswith(query) or query.startswith(candidate)):
        best, method = 0.86 + 0.12 * (shortest / longest), "prefix"
    elif shortest >= 3 and (query in candidate or candidate in query):
        best, method = 0.86, "substring"

    edit = 1.0 - _levenshtein(query, candidate) / longest
    if edit > best:
        best, method = edit, "edit"
    dice = _dice(query, candidate)
    if dice > best:
        best, method = dice, "trigram"
    left_key = _phonetic(query)
    right_key = _phonetic(candidate)
    if len(left_key) >= 2 and left_key == right_key and best < 0.84:
        best, method = 0.84, "phonetic"
    return (best, method) if best >= min_similarity else (0.0, "")


def _field_similarity(
    query_tokens: tuple[str, ...],
    value: str | None,
    *,
    min_similarity: float = DEFAULT_FUZZY_THRESHOLD,
) -> tuple[float, bool, bool]:
    """Score one field: `(strength, matched, fuzzy_used)`.

    `matched` is false when fewer than 60% of the query words match, which keeps
    a single incidental word from turning a whole field into a candidate.
    `fuzzy_used` says whether a sound-alike/edit/trigram match was needed.
    """
    if not query_tokens or not value:
        return 0.0, False, False
    field_tokens = _tokens(value)
    if not field_tokens:
        return 0.0, False, False

    span = len(query_tokens)
    if span <= len(field_tokens) and any(
        field_tokens[start:start + span] == query_tokens
        for start in range(len(field_tokens) - span + 1)
    ):
        return 1.0, True, False

    scores: list[tuple[float, str]] = []
    for query_token in query_tokens:
        scores.append(max(
            (
                _token_similarity(query_token, field_token, min_similarity=min_similarity)
                for field_token in field_tokens
            ),
            key=lambda item: item[0],
        ))
    matched_count = sum(1 for score, _ in scores if score > 0)
    coverage = matched_count / len(query_tokens)
    if coverage < FUZZY_FIELD_COVERAGE:
        return 0.0, False, False
    strength = sum(score for score, _ in scores) / len(query_tokens)
    fuzzy_used = any(method in {"edit", "trigram", "phonetic"} for _, method in scores)
    return strength, True, fuzzy_used


@dataclass(frozen=True, slots=True)
class MatchReason:
    """One line explaining why a session matched, for speech and cards."""

    kind: str
    text: str | None = None
    score: float = 0.0

    @property
    def line(self) -> str:
        if self.kind == "recent":
            return "recent activity"
        label = {
            "title": "title",
            "title_fuzzy": "close to title",
            "project": "project",
            "project_fuzzy": "close to project",
            "branch": "branch",
            "branch_fuzzy": "close to branch",
            "user": "you wrote",
            "final_answer": "an answer said",
            "topic": "topic",
            "topic_fuzzy": "close to topic",
            "indexed": "indexed text",
        }.get(self.kind, self.kind.replace("_", " "))
        return f'{label} "{self.text}"' if self.text else label

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "score": round(self.score, 6), "line": self.line}


@dataclass(frozen=True, slots=True)
class RankedSession:
    """One match, already carrying its handle, why-line and score components."""

    session_id: str
    handle: str
    title: str
    project_id: str
    project_name: str
    branch: str | None
    archived: bool
    state: str | None
    updated: float
    age_days: float
    score: float
    text_score: float
    recency: float
    boost: float
    boosts: tuple[str, ...]
    reasons: tuple[MatchReason, ...]
    snippet: str | None

    @property
    def why(self) -> str:
        return self.reasons[0].line if self.reasons else "recent activity"

    def describe(self) -> str:
        flags = [self.project_name or "(unknown project)"]
        if self.archived:
            flags.append("archived")
        if self.state and not (self.archived and self.state == "archived"):
            flags.append(self.state.replace("_", " "))
        return (
            f"{self.handle}: {self.title} ({', '.join(flags)}) - "
            f"{self.why}; {_human_age(self.age_days * 86400)}"
        )

    def as_dict(self, *, include_ids: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "handle": self.handle,
            "title": self.title,
            "project_name": self.project_name,
            "branch": self.branch,
            "archived": self.archived,
            "state": self.state,
            "updated": self.updated,
            "age_days": round(self.age_days, 4),
            "score": round(self.score, 6),
            "text_score": round(self.text_score, 6),
            "recency": round(self.recency, 6),
            "boost": round(self.boost, 6),
            "boosts": list(self.boosts),
            "why": self.why,
            "reasons": [reason.as_dict() for reason in self.reasons],
            "snippet": self.snippet,
        }
        if include_ids:
            result["session_id"] = self.session_id
            result["project_id"] = self.project_id
        return result


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """Close candidates that must be put to the user as a question."""

    candidates: tuple[RankedSession, ...]
    ratio: float
    question: str

    def as_dict(self, *, include_ids: bool = False) -> dict[str, Any]:
        return {
            "ratio": round(self.ratio, 6),
            "question": self.question,
            "candidates": [candidate.as_dict(include_ids=include_ids) for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """A ranked search result with its window, clipping and ambiguity made explicit."""

    query: str
    query_tokens: tuple[str, ...]
    project_id: str | None
    project_name: str | None
    window_days: float | None
    window_since: float | None
    explicit_since: bool
    widened: bool
    widened_from_days: float | None
    include_archived: bool
    results: tuple[RankedSession, ...]
    ambiguity: Ambiguity | None
    total_matched: int
    clipped: int
    notes: tuple[str, ...]
    confidence_floor: float = DEFAULT_CONFIDENCE
    project_mismatch: bool = False

    @property
    def ambiguous(self) -> bool:
        return self.ambiguity is not None

    @property
    def weak(self) -> bool:
        """The best match is too loose to act on; the Manager says what it found and asks."""
        if self.project_mismatch:
            return True
        return bool(self.query_tokens) and self.best is not None and self.best.text_score < self.confidence_floor

    @property
    def best(self) -> RankedSession | None:
        """The highest-ranked result, for display even when the query is ambiguous."""
        return self.results[0] if self.results else None

    @property
    def choice(self) -> RankedSession | None:
        """The top result only when there is one safe, unambiguous answer: no close rival and not a weak match."""
        return None if self.ambiguity or self.weak else self.best

    def describe_lines(self) -> list[str]:
        lines = [result.describe() for result in self.results]
        lines.extend(self.notes)
        if self.ambiguity:
            lines.append(self.ambiguity.question)
        return lines or ["No matches."]

    def as_dict(self, *, include_ids: bool = False) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_tokens": list(self.query_tokens),
            "project_name": self.project_name,
            "window": {
                "days": self.window_days,
                "since": self.window_since,
                "explicit_since": self.explicit_since,
                "widened": self.widened,
                "widened_from_days": self.widened_from_days,
            },
            "archived_included": self.include_archived,
            "results": [result.as_dict(include_ids=include_ids) for result in self.results],
            "ambiguous": self.ambiguous,
            "ambiguity": self.ambiguity.as_dict(include_ids=include_ids) if self.ambiguity else None,
            "choice": self.choice.handle if self.choice else None,
            "weak": self.weak,
            "total_matched": self.total_matched,
            "shown": len(self.results),
            "clipped": self.clipped,
            "notes": list(self.notes),
            **({"project_id": self.project_id} if include_ids and self.project_id else {}),
        }


@dataclass(frozen=True, slots=True)
class RankingContext:
    """Per-turn evidence the retrieval ranker may use without computing state.

    All session values are ids. Project values may be an id, a project name or
    a path; matching is forgiving. `needs_attention` should come from the same
    unified state used by `find_sessions(state=...)`.
    """

    needs_attention: Collection[str] = frozenset()
    on_screen_session: str | None = None
    on_screen_project: str | None = None
    recently_discussed: Collection[str] = frozenset()
    project_in_focus: str | None = None


class HandleRegistry:
    """Derive, de-duplicate and resolve spoken session handles.

    A handle is stable for the life of this registry: changing a session title
    does not rename it. `snapshot()` and `handles=` let the daemon persist the
    mapping elsewhere; this class writes nothing.
    """

    def __init__(
        self,
        handles: Mapping[str, str] | None = None,
        *,
        on_assign: Callable[[str, str], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, str] = {}
        self._by_handle: dict[str, str] = {}
        self._on_assign = on_assign
        for session_id, handle in (handles or {}).items():
            normalized = _normalize_handle(handle)
            if not normalized:
                continue
            owner = self._by_handle.get(normalized)
            if owner is not None and owner != session_id:
                raise ValueError(f"duplicate handle {normalized!r} for {owner!r} and {session_id!r}")
            self._by_session[session_id] = normalized
            self._by_handle[normalized] = session_id

    def assign(self, session_id: str, title: str) -> str:
        with self._lock:
            existing = self._by_session.get(session_id)
            if existing is not None:
                return existing
            base = _handle_base(title)
            candidate = base
            suffix = 2
            while candidate in self._by_handle:
                candidate = f"{base}-{suffix}"
                suffix += 1
            self._by_session[session_id] = candidate
            self._by_handle[candidate] = session_id
        if self._on_assign is not None:
            self._on_assign(session_id, candidate)
        return candidate

    def handle(self, session_id: str) -> str | None:
        with self._lock:
            return self._by_session.get(session_id)

    def resolve(self, handle: str) -> str | None:
        normalized = _normalize_handle(handle)
        with self._lock:
            return self._by_handle.get(normalized)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._by_session)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_session)


@dataclass(slots=True)
class _FtsHit:
    kind: str
    snippet: str | None
    credit: dict[str, float]  # query token -> 1.0 for a word that really matched, LOOSE_CREDIT for a long prefix

    @property
    def weight(self) -> float:
        return _KIND_WEIGHTS.get(self.kind, 0.55)

    def coverage(self, tokens: tuple[str, ...]) -> float:
        return sum(self.credit.get(token, 0.0) for token in tokens) / len(tokens) if tokens else 0.0


@dataclass(frozen=True, slots=True)
class _Search:
    tokens: tuple[str, ...]
    project: Project | None
    cutoff: float | None
    limit: int
    include_archived: bool
    states: Mapping[str, str] | None
    state: str | None
    context: RankingContext
    now: float
    named_project: str | None = None


@dataclass(slots=True)
class _Scored:
    session: Session
    project_name: str
    branch: str | None
    state: str | None
    text_score: float
    recency: float
    boost: float
    boosts: tuple[str, ...]
    score: float
    reasons: tuple[MatchReason, ...]
    snippet: str | None


class Retriever:
    """Rank sessions for the Manager from Store's existing search data."""

    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], float] | None = None,
        handles: HandleRegistry | None = None,
        half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
        recency_floor: float = DEFAULT_RECENCY_FLOOR,
        confidence_floor: float = DEFAULT_CONFIDENCE,
        default_window_days: float = DEFAULT_WINDOW_DAYS,
        widen_days: tuple[float | None, ...] = DEFAULT_WIDEN_DAYS,
        ambiguity_ratio: float = DEFAULT_AMBIGUITY_RATIO,
        fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
        max_limit: int = MAX_LIMIT,
    ) -> None:
        if half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        if default_window_days <= 0:
            raise ValueError("default_window_days must be positive")
        if not 0 <= recency_floor < 1:
            raise ValueError("recency_floor must be in [0, 1)")
        if not 0 <= confidence_floor <= 1:
            raise ValueError("confidence_floor must be in [0, 1]")
        if not 0 < ambiguity_ratio <= 1:
            raise ValueError("ambiguity_ratio must be in (0, 1]")
        if not 0 < fuzzy_threshold <= 1:
            raise ValueError("fuzzy_threshold must be in (0, 1]")
        if max_limit < 1:
            raise ValueError("max_limit must be positive")
        self.store = store
        self.clock = clock or time.time
        # `or` would replace an empty registry (it has a __len__) and silently lose the caller's persistence hook.
        self.handles = handles if handles is not None else HandleRegistry()
        self.half_life_days = half_life_days
        self.recency_floor = recency_floor
        self.confidence_floor = confidence_floor
        self.default_window_days = default_window_days
        self.widen_days = widen_days
        self.ambiguity_ratio = ambiguity_ratio
        self.fuzzy_threshold = fuzzy_threshold
        self.max_limit = max_limit

    # -- handles ------------------------------------------------------------

    def handle_for(self, session: Session) -> str:
        return self.handles.assign(session.id, session.title)

    def resolve_id(self, handle: str) -> str | None:
        return self.handles.resolve(handle)

    def resolve(self, handle: str) -> Session | None:
        session_id = self.resolve_id(handle)
        return self.store.session(session_id) if session_id else None

    # -- search -------------------------------------------------------------

    def find_sessions(
        self,
        query: str = "",
        *,
        project: str | Project | None = None,
        state: str | None = None,
        states: Mapping[str, str] | None = None,
        since: float | None = None,
        limit: int = DEFAULT_LIMIT,
        context: RankingContext | None = None,
        include_archived: bool = True,
        widen: bool = True,
    ) -> RetrievalResult:
        """Find and rank sessions for the Manager's `find_sessions` tool.

        `project` accepts a project id, name or path. `since` is an epoch
        seconds floor and is a hard window (no auto-widening). Without it, the
        window is `default_window_days`, widening only when nothing matches.
        `state` must be one of the unified states in `SESSION_STATES`; unknown
        filters are rejected rather than silently returning nothing.
        `states` is the unified state keyed by session id; pass it when using
        `state=`, except for `state="archived"`, which reads `Session.archived`.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        requested_limit = limit
        limit = min(limit, self.max_limit)
        if state:
            state = state.strip().casefold()
            if state not in SESSION_STATES:
                raise RetrievalError(
                    f"unknown session state {state!r}; expected one of: {', '.join(SESSION_STATES)}"
                )
        if state and state != "archived" and states is None:
            raise RetrievalError(
                "state filtering needs states= from the unified session_state() result; "
                'only state="archived" can be read from Store alone'
            )

        project_ref = self._resolve_project(project)
        tokens = _query_tokens(query)
        named = None
        if project_ref is None and tokens:
            named, tokens = self._named_project(tokens)
        now = self.clock()
        base = _Search(
            tokens=tokens,
            project=project_ref,
            cutoff=None,
            limit=limit,
            include_archived=include_archived,
            states=states,
            state=state,
            context=context or RankingContext(),
            now=now,
            named_project=named.id if named else None,
        )

        windows: list[tuple[float | None, float | None]] = []
        if since is not None:
            windows.append((since, max(0.0, (now - since) / 86400)))
        elif tokens:
            # A query looks at all of history. A narrow window would hide an equally good older match and let a
            # confident wrong answer through ("the bug" with one bug session today and another last week); the
            # recency tie-break and boosts already prefer the recent one when the words fit equally.
            windows.append((None, None))
        else:
            windows.append((now - self.default_window_days * 86400, self.default_window_days))
            if widen:
                for days in self.widen_days:
                    previous_days = windows[-1][1]
                    if previous_days is None:
                        break
                    if days is not None and days <= previous_days:
                        continue
                    cutoff = None if days is None else now - days * 86400
                    windows.append((cutoff, days))

        scored: list[_Scored] = []
        selected_index = 0
        best_text = -1.0
        for index, (cutoff, _) in enumerate(windows):
            found = self._collect(replace(base, cutoff=cutoff))
            top = max((item.text_score for item in found), default=0.0)
            if found and (not scored or top > best_text):
                scored, selected_index, best_text = found, index, top
            # A browse (no query) or a strong match ends the search; a hard `since` never widens.
            if since is not None or (found and (not tokens or top >= STRONG_MATCH)):
                break
        if not scored:
            selected_index = len(windows) - 1 if since is None else 0

        cutoff, days = windows[selected_index]
        if since is not None:
            days = max(0.0, (now - since) / 86400)
        scored.sort(key=lambda item: (-item.score, -item.session.updated, item.session.title.casefold(), item.session.id))
        total_matched = len(scored)
        close_indices, ratio = self._close_candidates(scored, has_query=bool(tokens))

        keep_indices = sorted(set(range(min(limit, len(scored)))) | set(close_indices))
        ranked: dict[int, RankedSession] = {}
        for index in keep_indices:
            ranked[index] = self._ranked(scored[index], now)

        results = tuple(ranked[index] for index in range(min(limit, len(scored))))
        ambiguity = None
        if len(close_indices) >= 2:
            candidates = tuple(ranked[index] for index in close_indices)
            ambiguity = Ambiguity(
                candidates=candidates,
                ratio=ratio,
                question=self._ambiguity_question(candidates),
            )

        clipped = max(0, total_matched - len(results))
        mismatch = bool(named and results and results[0].project_id != named.id)
        notes = self._notes(
            scored=scored,
            selected_index=selected_index,
            windows=windows,
            days=days,
            since=since,
            widen=widen,
            clipped=clipped,
            total_matched=total_matched,
            limit=limit,
            requested_limit=requested_limit,
            browse=not tokens,
        )
        return RetrievalResult(
            query=query,
            query_tokens=tokens,
            project_id=project_ref.id if project_ref else None,
            project_name=project_ref.name if project_ref else None,
            window_days=days,
            window_since=cutoff,
            explicit_since=since is not None,
            widened=since is None and selected_index > 0,
            widened_from_days=self.default_window_days if since is None and selected_index > 0 else None,
            include_archived=include_archived,
            results=results,
            ambiguity=ambiguity,
            total_matched=total_matched,
            clipped=clipped,
            notes=(*notes, f"Nothing in {named.name} fits; the best match is in another project.") if mismatch and named else notes,
            confidence_floor=self.confidence_floor,
            project_mismatch=mismatch,
        )

    # -- project and state helpers -----------------------------------------

    def resolve_project(self, reference: str | Project | None) -> Project | None:
        """A project from an id, name or path. Raises `ProjectNotFound` with the known names when it is unknown or ambiguous."""
        return self._resolve_project(reference)

    def _resolve_project(self, reference: str | Project | None) -> Project | None:
        if reference is None:
            return None
        if isinstance(reference, Project):
            return self.store.project(reference.id) or reference

        raw = str(reference).strip()
        if not raw:
            return None
        projects = self.store.projects()
        for project in projects:
            if raw == project.id:
                return project
        folded = _fold(raw)
        expanded = ""
        try:
            expanded = str(Path(raw).expanduser().resolve())
        except (OSError, RuntimeError):
            expanded = ""
        matches = [
            project for project in projects
            if folded in {_fold(project.name), _fold(Path(project.path).name)}
            or folded == _fold(project.path)
            or (expanded and expanded == project.path)
        ]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(sorted(project.name for project in projects)) or "none"
        if not matches:
            raise ProjectNotFound(f"no project matches {raw!r}; known projects: {known}")
        names = ", ".join(sorted(project.name for project in matches))
        raise ProjectNotFound(f"project {raw!r} is ambiguous ({names}); use the project id")

    def _named_project(self, tokens: tuple[str, ...]) -> tuple[Project | None, tuple[str, ...]]:
        """A project the words name ("the bug in notebooks"): it boosts that project's sessions, and its word is
        not searched for in the sessions' own text. Only a whole-word match counts, and something must remain."""
        by_word: dict[str, list[Project]] = {}
        for project in self.store.projects():
            for word in {_fold(project.name), _fold(Path(project.path).name)}:
                if len(word) >= 3 and word.isalnum():
                    by_word.setdefault(word, []).append(project)
        for token in tokens:
            matches = by_word.get(token)
            if matches and len(matches) == 1:
                remaining = tuple(t for t in tokens if t != token)
                if remaining:
                    return matches[0], remaining
        return None, tokens

    @staticmethod
    def _state_matches(session: Session, state: str, states: Mapping[str, str] | None) -> bool:
        if state == "archived":
            return bool(session.archived)
        if states is None:
            return False
        value = states.get(session.id)
        return value is not None and value.casefold() == state

    @staticmethod
    def _state_value(session: Session, states: Mapping[str, str] | None) -> str | None:
        if states is None:
            return None
        value = states.get(session.id)
        return value.casefold() if value else None

    # -- collection and scoring --------------------------------------------

    def _collect(self, search: _Search) -> list[_Scored]:
        projects = {project.id: project for project in self.store.projects()}
        sessions = self.store.sessions(
            project_id=search.project.id if search.project else None,
            archived="include" if search.include_archived else "exclude",
        )
        sessions = [
            session for session in sessions
            if (search.cutoff is None or session.updated >= search.cutoff)
            and (not search.state or self._state_matches(session, search.state, search.states))
        ]
        if search.tokens:
            fts_hits = self._fts_hits(search)
        else:
            fts_hits = {}

        scored: list[_Scored] = []
        for session in sessions:
            project = projects.get(session.project_id)
            project_name = project.name if project else Path(session.directory).name
            branch = getattr(session, "branch", None)
            if not isinstance(branch, str) or not branch.strip():
                branch = None
            state = self._state_value(session, search.states)
            evidence, fts_snippet = self._evidence(
                session, project_name, branch, search.tokens, fts_hits.get(session.id),
            )
            if search.tokens and not evidence:
                continue
            if not search.tokens:
                evidence = [(1.0, MatchReason("recent", score=1.0))]
            evidence.sort(key=lambda item: (-item[0], _KIND_ORDER.get(item[1].kind.removesuffix("_fuzzy"), 0)))
            text_score = self._combine(evidence)
            reasons = self._dedupe_reasons(evidence)
            snippet = fts_snippet
            if snippet is None and search.tokens and reasons:
                snippet = reasons[0].text
            decay = 0.5 ** (max(0.0, search.now - session.updated) / 86400 / self.half_life_days)
            recency = self.recency_floor + (1.0 - self.recency_floor) * decay
            boost, boost_labels = self._boost_for(session, project_name, search.context)
            if search.named_project and session.project_id == search.named_project:
                boost += BOOST_PROJECT_NAMED
                boost_labels = (*boost_labels, "project named")
            score = text_score * recency * (1.0 + boost)
            scored.append(_Scored(
                session=session,
                project_name=project_name,
                branch=branch,
                state=state,
                text_score=text_score,
                recency=recency,
                boost=boost,
                boosts=boost_labels,
                score=score,
                reasons=reasons,
                snippet=snippet,
            ))
        return scored

    def _fts_hits(self, search: _Search) -> dict[str, _FtsHit]:
        hits: dict[str, _FtsHit] = {}
        archived = "include" if search.include_archived else "exclude"
        project_id = search.project.id if search.project else None
        row_limit = max(search.limit * 6, 40)

        phrase = " ".join(search.tokens)
        rows = self.store.search(phrase, project_id=project_id, archived=archived, limit=row_limit)
        for row in rows:
            self._take_fts(hits, row, set(search.tokens))

        # Store.search ANDs its words inside one indexed row. Natural spoken
        # queries spread words across title and messages, so always ask per
        # meaningful word too and let ranking reward coverage across the union.
        # A hit-count gate would hide a session whose title/message is behind
        # enough unrelated matches.
        if len(search.tokens) > 1:
            for token in search.tokens:
                for row in self.store.search(token, project_id=project_id, archived=archived, limit=row_limit):
                    self._take_fts(hits, row, {token})
        return hits

    @staticmethod
    def _credit(row: Mapping[str, Any], tokens: set[str]) -> dict[str, float]:
        """Which query tokens a hit really matched, read from the snippet's highlighted words."""
        raw = str(row.get("snippet") or "")
        matched = {word for part in _HIGHLIGHT.findall(raw) for word in _tokens(part)}
        if not matched:  # no highlight to judge by: trust the match
            return {token: 1.0 for token in tokens}
        credit: dict[str, float] = {}
        for token in tokens:
            if token in matched or any(word.startswith(token) and len(token) / len(word) >= LOOSE_PREFIX_RATIO for word in matched):
                credit[token] = 1.0
            elif any(word.startswith(token) for word in matched):
                credit[token] = LOOSE_CREDIT
        return credit

    @classmethod
    def _take_fts(cls, hits: dict[str, _FtsHit], row: Mapping[str, Any], tokens: set[str]) -> None:
        session_id = str(row["session_id"])
        kind = str(row.get("kind") or "user")
        snippet = _snippet(row.get("snippet"))
        credit = cls._credit(row, tokens)
        existing = hits.get(session_id)
        if existing is None:
            hits[session_id] = _FtsHit(kind=kind, snippet=snippet, credit=credit)
            return
        for token, value in credit.items():
            existing.credit[token] = max(existing.credit.get(token, 0.0), value)
        if _KIND_WEIGHTS.get(kind, 0.55) > existing.weight:
            existing.kind = kind
            existing.snippet = snippet

    def _evidence(
        self,
        session: Session,
        project_name: str,
        branch: str | None,
        tokens: tuple[str, ...],
        fts: _FtsHit | None,
    ) -> tuple[list[tuple[float, MatchReason]], str | None]:
        evidence: list[tuple[float, MatchReason]] = []
        topic = getattr(session, "topic", None)
        if not isinstance(topic, str):
            topic = None
        for value, weight, exact_kind, fuzzy_kind in (
            (session.title, 1.0, "title", "title_fuzzy"),
            (topic, 0.82, "topic", "topic_fuzzy"),
            (project_name, 0.55, "project", "project_fuzzy"),
            (branch, 0.60, "branch", "branch_fuzzy"),
        ):
            strength, matched, fuzzy_used = _field_similarity(
                tokens, value, min_similarity=self.fuzzy_threshold
            )
            if matched and strength:
                reason = MatchReason(
                    kind=fuzzy_kind if fuzzy_used else exact_kind,
                    text=_snippet(value),
                    score=strength * weight,
                )
                evidence.append((strength * weight, reason))

        snippet = None
        if fts is not None:
            coverage = fts.coverage(tokens)
            if coverage == 0.0:
                return evidence, None
            strength = fts.weight * (0.35 + 0.65 * coverage)
            kind = fts.kind if fts.kind in {"title", "topic", "final_answer", "user"} else "indexed"
            evidence.append((strength, MatchReason(kind=kind, text=fts.snippet, score=strength)))
            snippet = fts.snippet
        return evidence, snippet

    @staticmethod
    def _combine(evidence: list[tuple[float, MatchReason]]) -> float:
        if not evidence:
            return 0.0
        ordered = sorted((score for score, _ in evidence), reverse=True)
        primary = ordered[0]
        extra = sum(ordered[1:])
        return min(1.0, primary + 0.12 * extra)

    @staticmethod
    def _dedupe_reasons(evidence: list[tuple[float, MatchReason]]) -> tuple[MatchReason, ...]:
        seen: set[tuple[str, str | None]] = set()
        reasons: list[MatchReason] = []
        for _, reason in evidence:
            key = (reason.kind.removesuffix("_fuzzy"), reason.text)
            if key in seen:
                continue
            seen.add(key)
            reasons.append(reason)
            if len(reasons) == 3:
                break
        return tuple(reasons)

    @staticmethod
    def _boost_for(session: Session, project_name: str, context: RankingContext) -> tuple[float, tuple[str, ...]]:
        boost = 0.0
        labels: list[str] = []
        if session.id in context.needs_attention:
            boost += BOOST_NEEDS_ATTENTION
            labels.append("needs attention")
        if context.on_screen_session == session.id:
            boost += BOOST_ON_SCREEN_SESSION
            labels.append("on screen")
        if context.on_screen_project and _project_ref_matches(
            context.on_screen_project, session.project_id, project_name
        ):
            boost += BOOST_ON_SCREEN_PROJECT
            labels.append("on-screen project")
        if session.id in context.recently_discussed:
            boost += BOOST_RECENTLY_DISCUSSED
            labels.append("discussed this episode")
        if context.project_in_focus and _project_ref_matches(
            context.project_in_focus, session.project_id, project_name
        ):
            boost += BOOST_PROJECT_IN_FOCUS
            labels.append("project in focus")
        return boost, tuple(labels)

    # -- result assembly ----------------------------------------------------

    def _ranked(self, scored: _Scored, now: float) -> RankedSession:
        session = scored.session
        return RankedSession(
            session_id=session.id,
            handle=self.handle_for(session),
            title=session.title,
            project_id=session.project_id,
            project_name=scored.project_name,
            branch=scored.branch,
            archived=bool(session.archived),
            state=scored.state,
            updated=session.updated,
            age_days=max(0.0, (now - session.updated) / 86400),
            score=scored.score,
            text_score=scored.text_score,
            recency=scored.recency,
            boost=scored.boost,
            boosts=scored.boosts,
            reasons=scored.reasons,
            snippet=scored.snippet,
        )

    def _close_candidates(self, scored: list[_Scored], *, has_query: bool) -> tuple[list[int], float]:
        if not has_query or len(scored) < 2 or not scored[0].text_score:
            return [], 1.0
        # Closeness is judged on how well the words fit (plus real context boosts such as "on screen"), never on
        # recency: two sessions that fit "the bug" equally well are a question, whichever is newer.
        def fit(candidate: _Scored) -> float:
            return candidate.text_score * (1.0 + candidate.boost)

        top = max(fit(candidate) for candidate in scored)
        if top <= 0:
            return [], 1.0
        close = [index for index, candidate in enumerate(scored) if fit(candidate) / top >= self.ambiguity_ratio]
        if len(close) < 2:
            return [], 1.0
        return close[:3], fit(scored[close[1]]) / top

    @staticmethod
    def _ambiguity_question(candidates: tuple[RankedSession, ...]) -> str:
        first, second = candidates[0], candidates[1]
        return f"Which one did you mean: {first.handle} ({first.why}) or {second.handle} ({second.why})?"

    def _notes(
        self,
        *,
        scored: list[_Scored],
        selected_index: int,
        windows: list[tuple[float | None, float | None]],
        days: float | None,
        since: float | None,
        widen: bool,
        clipped: int,
        total_matched: int,
        limit: int,
        requested_limit: int,
        browse: bool,
    ) -> tuple[str, ...]:
        notes: list[str] = []
        noun = "sessions" if browse else "matches"
        if since is not None:
            if not scored:
                notes.append(f"No {noun} since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}.")
        elif selected_index > 0:
            previous = ", ".join(f"the last {_window_label(value)}" for _, value in windows[:selected_index])
            notes.append(f"No strong {noun} in {previous}; widened to {_window_label(days)}.")
        elif not scored and not widen:
            notes.append(f"No {noun} in the last {_window_label(days)}.")
        elif not scored:
            notes.append(f"No {noun} found in any window.")
        if clipped:
            shown = total_matched - clipped
            notes.append(f"Showing {shown} of {total_matched} {noun}; {clipped} clipped.")
        if requested_limit > limit:
            notes.append(f"Limit capped at {limit}; {requested_limit} was requested.")
        return tuple(notes)


def _project_ref_matches(reference: str, project_id: str, project_name: str) -> bool:
    if reference == project_id:
        return True
    folded = _fold(reference)
    if folded == _fold(project_name):
        return True
    try:
        return _fold(Path(reference).name) == _fold(project_name)
    except OSError:
        return False
