"""Permission grammar, evaluation, path security, and approval primitives.

The grammar is deliberately tiny (plan section 5.3)::

    Tool                    whole tool, any arguments
    Tool(pattern)           glob match against the tool's permission_key
    Bundle:name             every tool in a bundle
    mcp__server__*          future-compatible wildcard tool names

Evaluation is first-match-wins in a fixed order
``deny -> session grants -> allow -> ask -> mode``. **``deny`` is absolute**: a
deny rule cannot be overridden by a session grant, a path trick, or the model.

Path security lives here because it is a permission concern: write roots and
read-deny roots are *hard boundaries evaluated before any rule allow*. A tool's
declared ``permission_key`` is re-canonicalized (``realpath``, symlinks
followed, prospective writes resolving their existing parents) before rules run,
so ``../`` and symlink escapes fail closed.

This module is UI-agnostic: it emits no terminal output and owns no session. The
approval flow is expressed as data (:class:`PermissionRequest`) plus a small
future-based :class:`ApprovalBroker`; a UI adapter drives it.
"""
from __future__ import annotations

import asyncio
import fnmatch
import functools
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from ..errors import NexusError
from ..util import new_id
from .spec import ToolCall, ToolSpec

__all__ = [
    "ApprovalBroker",
    "BatchPlan",
    "Decision",
    "Evaluation",
    "Grant",
    "Outcome",
    "PathGuard",
    "PathSecurityError",
    "PermissionEngine",
    "PermissionRequest",
    "PermissionRuleError",
    "PreparedCall",
    "ResolvedPath",
    "Rule",
    "collect_grants",
    "escape_glob",
    "exact_rule",
    "grant_for_decision",
    "parse_rule",
]

# ---------------------------------------------------------------------------
# Rule grammar
# ---------------------------------------------------------------------------

_BUNDLE_PREFIX = "Bundle:"
_TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_WILDCARD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_*?\[\]]{0,63}\Z")
_GLOB_CHARS = ("*", "?", "[", "]")

#: Glob metacharacters escaped by :func:`escape_glob`.
_GLOB_META = ("\\", "*", "?", "[")

#: Maximum key length that can back a persistable exact rule. A longer (or
#: non-UTF-8-encodable) key still evaluates normally, but it cannot be encoded
#: into an event/log/grant, so its ``*_ALWAYS`` decision degrades to ``*_ONCE``
#: rather than being truncated (truncation would broaden the rule).
MAX_PERMISSION_KEY_CHARS = 8192


def escape_glob(value: str) -> str:
    """Escape glob metacharacters in ``value`` so it matches literally.

    Session grants pin one exact action (for example a Bash command containing a
    literal ``*``); escaping keeps that from accidentally widening into a
    wildcard. The matcher understands backslash escapes.
    """
    out: list[str] = []
    for char in value:
        if char in _GLOB_META:
            out.append("\\")
        out.append(char)
    return "".join(out)


def exact_rule(tool: str, key: str) -> str:
    """The most specific exact-action rule for ``tool`` matching ``key``.

    The key is JSON-encoded inside the parentheses so arbitrary characters —
    parentheses, quotes, ``*``/``?``/``[``, control characters, newlines — are
    representable and can never be read as glob syntax. :func:`parse_rule`
    round-trips this into a :data:`RuleKind` of ``"tool_exact"``.
    """
    return f"{tool}({json.dumps(key, ensure_ascii=False)})"


@functools.lru_cache(maxsize=512)
def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a rule glob (with ``\\`` escapes) into an anchored regex."""
    out: list[str] = ["(?s:"]
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if char == "*":
            out.append(".*")
        elif char == "?":
            out.append(".")
        elif char == "[":
            j = i + 1
            if j < n and pattern[j] in ("!", "^"):
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape("["))
            else:
                inner = pattern[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                inner = inner.replace("\\", "\\\\")
                out.append("[" + inner + "]")
                i = j
        else:
            out.append(re.escape(char))
        i += 1
    out.append(")\\Z")
    return re.compile("".join(out))


def _glob_matches(pattern: str, key: str) -> bool:
    try:
        return _compile_glob(pattern).match(key) is not None
    except re.error:
        return False

RuleKind = Literal["tool", "tool_pattern", "tool_exact", "bundle", "wildcard"]
Effect = Literal["allow", "deny"]
Scope = Literal["session", "project", "user"]
UnattendedMode = Literal["deny", "allow", "fail_turn"]


class PermissionRuleError(NexusError, ValueError):
    """A permission rule is empty, malformed, ambiguous, or unsafe."""


@dataclass(frozen=True)
class Rule:
    """A parsed permission rule. Construct via :func:`parse_rule`."""

    raw: str
    kind: RuleKind
    tool: str | None = None
    pattern: str | None = None
    bundle: str | None = None

    def matches(self, tool: str, key: str | None, bundle: str | None) -> bool:
        if self.kind == "tool":
            return self.tool == tool
        if self.kind == "wildcard":
            return self.tool is not None and fnmatch.fnmatchcase(tool, self.tool)
        if self.kind == "bundle":
            return self.bundle is not None and self.bundle == bundle
        if self.kind == "tool_pattern":
            return (
                self.tool == tool
                and key is not None
                and self.pattern is not None
                and _glob_matches(self.pattern, key)
            )
        if self.kind == "tool_exact":
            return (
                self.tool == tool
                and key is not None
                and self.pattern is not None
                and key == self.pattern
            )
        return False

    def to_dict(self) -> dict[str, str | None]:
        return {
            "raw": self.raw,
            "kind": self.kind,
            "tool": self.tool,
            "pattern": self.pattern,
            "bundle": self.bundle,
        }


def _looks_like_home_path(pattern: str) -> bool:
    """Whether a rule pattern references a home directory via a bare ``~``.

    Tool permission keys are already-canonical paths, so a ``~`` segment can
    never match; rejecting it avoids a silently ineffective rule. A ``~`` inside
    a filename (for example ``notes~.md``) is left alone.
    """
    return "~" in pattern.replace("\\", "/").split("/")


def _representable_exact_key(key: str) -> bool:
    """Whether ``key`` can back a persisted exact grant.

    Exact rules match by equality, so a ``~`` (or any other character) is safe:
    it can neither be mistaken for a home path nor widen the rule. Only a key
    that cannot be encoded into a bounded event/log/grant is unrepresentable.
    """
    if len(key) > MAX_PERMISSION_KEY_CHARS:
        return False
    try:
        key.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _parse_exact_rule(raw: str) -> Rule | None:
    """Parse ``Tool("JSON-encoded key")`` into an exact rule, else ``None``.

    ``exact_rule`` produces this form for session grants. JSON encoding keeps
    arbitrary key contents (parentheses, quotes, glob metacharacters, controls)
    representable while remaining a plain string for events and logs.
    """
    if not raw.endswith(")"):
        return None
    head, separator, rest = raw.partition("(")
    if separator != "(" or "(" in head or ")" in head:
        return None
    if _TOOL_NAME.fullmatch(head) is None:
        return None
    inner = rest[:-1]
    if not (inner.startswith('"') and inner.endswith('"')):
        return None
    try:
        decoded = json.loads(inner)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, str) or not decoded:
        return None
    if "\x00" in decoded:
        return None
    return Rule(raw=raw, kind="tool_exact", tool=head, pattern=decoded)


def parse_rule(raw: object) -> Rule:
    """Strictly parse one permission rule; reject anything ambiguous."""
    if not isinstance(raw, str):
        raise PermissionRuleError("Permission rule must be a string")
    if not raw or not raw.strip():
        raise PermissionRuleError("Permission rule must not be empty")
    if raw != raw.strip():
        raise PermissionRuleError(f"Permission rule has surrounding whitespace: {raw!r}")
    if "\x00" in raw:
        raise PermissionRuleError("Permission rule must not contain a NUL byte")

    if raw.startswith(_BUNDLE_PREFIX):
        name = raw[len(_BUNDLE_PREFIX) :]
        if ":" in name or "(" in name or ")" in name:
            raise PermissionRuleError(f"Malformed bundle rule: {raw!r}")
        if _TOOL_NAME.fullmatch(name) is None:
            raise PermissionRuleError(f"Invalid bundle name in rule: {raw!r}")
        return Rule(raw=raw, kind="bundle", bundle=name)

    if ":" in raw:
        raise PermissionRuleError(
            f"Unknown rule prefix (only {_BUNDLE_PREFIX!r}) or ambiguous ':': {raw!r}"
        )

    if raw.endswith(")"):
        exact = _parse_exact_rule(raw)
        if exact is not None:
            # Equality rules are safe for any key, including one containing
            # ``~``. (Glob/pattern rules still reject ``~`` because there it is
            # either ambiguous or inert against canonical keys.)
            return exact
        if raw.count("(") != 1 or raw.count(")") != 1:
            raise PermissionRuleError(
                f"Malformed rule (nested or unbalanced parentheses): {raw!r}"
            )
        head, separator, pattern = raw[:-1].partition("(")
        if separator != "(" or "(" in head or ")" in head:
            raise PermissionRuleError(f"Malformed rule: {raw!r}")
        if _TOOL_NAME.fullmatch(head) is None:
            raise PermissionRuleError(f"Invalid tool name in rule: {raw!r}")
        if not pattern or "(" in pattern or ")" in pattern:
            raise PermissionRuleError(f"Malformed or empty pattern in rule: {raw!r}")
        if "\x00" in pattern:
            raise PermissionRuleError("Permission pattern must not contain a NUL byte")
        if _looks_like_home_path(pattern):
            raise PermissionRuleError(
                f"Permission rule pattern {raw!r} uses '~'; tool permission keys "
                "are canonical paths, so '~' would never match. Use an absolute "
                "path, or configure read_denyroots/write_roots for home locations"
            )
        return Rule(raw=raw, kind="tool_pattern", tool=head, pattern=pattern)

    if "(" in raw or ")" in raw:
        raise PermissionRuleError(f"Malformed rule (unbalanced parentheses): {raw!r}")

    if _TOOL_NAME.fullmatch(raw) is not None:
        return Rule(raw=raw, kind="tool", tool=raw)

    if _WILDCARD_NAME.fullmatch(raw) is not None and any(
        char in raw for char in _GLOB_CHARS
    ):
        return Rule(raw=raw, kind="wildcard", tool=raw)

    raise PermissionRuleError(f"Invalid permission rule: {raw!r}")


# ---------------------------------------------------------------------------
# Decisions, outcomes, and session grants
# ---------------------------------------------------------------------------


class Decision(StrEnum):
    """The four resolutions a UI may return (plan section 5.3)."""

    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY_ONCE = "deny_once"
    DENY_ALWAYS = "deny_always"

    @property
    def allows(self) -> bool:
        return self in (Decision.ALLOW_ONCE, Decision.ALLOW_ALWAYS)

    @property
    def denies(self) -> bool:
        return self in (Decision.DENY_ONCE, Decision.DENY_ALWAYS)

    @property
    def persists(self) -> bool:
        return self in (Decision.ALLOW_ALWAYS, Decision.DENY_ALWAYS)

    @classmethod
    def from_value(cls, value: object) -> Decision:
        if isinstance(value, Decision):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as exc:
                raise PermissionRuleError(f"Unknown decision: {value!r}") from exc
        raise PermissionRuleError(f"Decision must be a string or Decision: {value!r}")


class Outcome(StrEnum):
    """The engine's verdict for one call before any UI interaction."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    FAIL_TURN = "fail_turn"


@dataclass(frozen=True)
class Grant:
    """A persisted ``*_ALWAYS`` resolution, reconstructable from an event.

    The grant stores the raw rule string (``Read``, ``Read(**)``,
    ``Bundle:fs``, ``mcp__server__*``) so ``permission.resolved`` event data only
    ever contains JSON-safe strings.
    """

    effect: Effect
    rule: str
    scope: Scope = "session"

    def __post_init__(self) -> None:
        if self.effect not in ("allow", "deny"):
            raise PermissionRuleError(f"Grant effect must be allow/deny: {self.effect!r}")
        if self.scope not in ("session", "project", "user"):
            raise PermissionRuleError(f"Unknown grant scope: {self.scope!r}")
        parse_rule(self.rule)

    def compiled(self) -> Rule:
        return parse_rule(self.rule)

    def matches(self, tool: str, key: str | None, bundle: str | None) -> bool:
        return self.compiled().matches(tool, key, bundle)

    def to_dict(self) -> dict[str, str]:
        return {"effect": self.effect, "rule": self.rule, "scope": self.scope}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Grant:
        if not isinstance(data, Mapping):
            raise PermissionRuleError("Grant must be an object")
        try:
            effect = data["effect"]
            rule = data["rule"]
        except KeyError as exc:
            raise PermissionRuleError(f"Grant is missing {exc.args[0]!r}") from exc
        scope = data.get("scope", "session")
        return cls(effect=effect, rule=rule, scope=scope)


def _once_decision(decision: Decision) -> Decision:
    """The once-only counterpart of a persistence decision."""
    return (
        Decision.ALLOW_ONCE
        if decision is Decision.ALLOW_ALWAYS
        else Decision.DENY_ONCE
    )


def grant_for_decision(
    decision: Decision | str,
    *,
    rule: str,
    scope: Scope = "session",
) -> Grant | None:
    """Build the persistent grant for a ``*_ALWAYS`` decision, else ``None``."""
    resolved = Decision.from_value(decision)
    if resolved is Decision.ALLOW_ALWAYS:
        effect: Effect = "allow"
    elif resolved is Decision.DENY_ALWAYS:
        effect = "deny"
    else:
        return None
    return Grant(effect=effect, rule=rule, scope=scope)


def rule_is_exact_for(rule_raw: object, tool: object, key: object) -> bool:
    """Whether ``rule_raw`` is a ``tool_exact`` rule for exactly ``tool``/``key``.

    This is the only rule shape permitted to reconstruct or persist a keyed
    grant. A bare tool, bundle, glob/pattern, wildcard, or an exact rule for a
    different tool/key is rejected, so a keyed grant can never broaden.
    """
    if not isinstance(rule_raw, str) or not rule_raw:
        return False
    if not (isinstance(tool, str) and tool and isinstance(key, str) and key):
        return False
    try:
        rule = parse_rule(rule_raw)
    except PermissionRuleError:
        return False
    return rule.kind == "tool_exact" and rule.tool == tool and rule.pattern == key


def collect_grants(records: Iterable[Mapping[str, Any]]) -> tuple[Grant, ...]:
    """Rebuild session grants from ``permission.resolved`` records/events.

    A keyed record may only reconstruct a valid ``tool_exact`` rule for the same
    tool and exact key. Any broader rule (bare tool, bundle, glob/pattern,
    wildcard) or an exact rule for a different tool/key is dropped: a persisted
    grant must never silently widen on replay. A keyless record keeps its
    explicit rule (or none).
    """
    grants: list[Grant] = []
    for record in records:
        raw_grant = record.get("grant")
        key = record.get("key")
        tool = record.get("tool")
        keyed = isinstance(key, str) and bool(key)
        if raw_grant:
            try:
                grant = Grant.from_dict(raw_grant)
            except PermissionRuleError:
                grant = None
            # Verify the persisted rule is exactly this tool/key, and that the
            # record's key was not truncated (which would make the exact
            # comparison meaningless).
            if (
                grant is not None
                and keyed
                and (
                    record.get("key_truncated")
                    or not rule_is_exact_for(grant.rule, tool, key)
                )
            ):
                grant = None
        else:
            decision = record.get("decision")
            if not (
                isinstance(decision, str)
                and decision
                in (Decision.ALLOW_ALWAYS.value, Decision.DENY_ALWAYS.value)
            ):
                continue
            rule = record.get("rule")
            if keyed:
                if record.get("key_truncated") or not _representable_exact_key(key):
                    continue
                if not rule_is_exact_for(rule, tool, key):
                    if isinstance(rule, str) and rule:
                        # An explicit but broader/mismatched rule: never replay.
                        continue
                    if not (isinstance(tool, str) and tool):
                        continue
                    # No explicit rule: derive the bounded exact rule.
                    rule = exact_rule(tool, key)
            elif not (isinstance(rule, str) and rule):
                continue
            try:
                grant = grant_for_decision(
                    decision,
                    rule=rule,
                    scope=record.get("scope", "session"),
                )
            except PermissionRuleError:
                grant = None
        if grant is not None:
            grants.append(grant)
    return tuple(grants)


# ---------------------------------------------------------------------------
# Path security
# ---------------------------------------------------------------------------


class PathSecurityError(NexusError, ValueError):
    """A path violates a hard boundary (NUL, tilde, write root, read deny)."""

    def __init__(self, message: str, *, code: str = "path_rejected") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResolvedPath:
    """The canonical result of resolving a tool-provided path."""

    absolute: Path
    key: str
    display: str
    inside_workspace: bool


def _canonical(path: Path) -> Path:
    try:
        # ``strict=False`` follows symlinks in every existing parent and appends
        # the remaining components, which is exactly "resolve existing parents".
        return path.resolve(strict=False)
    except (OSError, ValueError, RuntimeError) as exc:
        # ``Path.resolve`` raises ``RuntimeError`` for symlink loops on some
        # platforms; a loop is a security failure, not a crash, so it fails
        # closed as an unresolvable path.
        raise PathSecurityError(
            f"Unresolvable path: {str(path)!r}", code="unresolvable"
        ) from exc


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


class PathGuard:
    """Canonical workspace plus hard write-root / read-deny boundaries.

    All roots are canonicalized once at construction. ``resolve`` is the tool
    entry point (rejects NUL and, for workspace tools, tilde); ``recheck`` is the
    execution-time seam for a later packet to re-canonicalize immediately before
    a write, closing the plan/execute TOCTOU window.
    """

    __slots__ = ("_read_denyroots", "_workspace", "_write_roots")

    def __init__(
        self,
        workspace: str | Path,
        *,
        write_roots: Sequence[str] = ("./",),
        read_denyroots: Sequence[str] = (),
        home: str | Path | None = None,
    ) -> None:
        self._workspace = _canonical(Path(workspace))
        roots = tuple(write_roots) or ("./",)
        self._write_roots = tuple(
            self._root(root, home=home, label="write_roots") for root in roots
        )
        self._read_denyroots = tuple(
            self._root(root, home=home, label="read_denyroots")
            for root in read_denyroots
        )

    def _root(
        self, value: object, *, home: str | Path | None, label: str
    ) -> Path:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise PathSecurityError(
                f"{label} entries must be non-empty path strings", code="bad_root"
            )
        text = value
        if text.startswith("~"):
            base = Path(home) if home is not None else Path.home()
            text = str(base) + text[1:]
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        return _canonical(candidate)

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def write_roots(self) -> tuple[Path, ...]:
        return self._write_roots

    @property
    def read_denyroots(self) -> tuple[Path, ...]:
        return self._read_denyroots

    def resolve(self, raw: object, *, for_write: bool) -> ResolvedPath:
        """Resolve a tool-provided path against the workspace and boundaries."""
        if not isinstance(raw, str):
            raise PathSecurityError("Tool path must be a string", code="not_a_string")
        if "\x00" in raw:
            raise PathSecurityError("Tool path contains a NUL byte", code="nul_byte")
        if not raw or not raw.strip():
            raise PathSecurityError("Tool path must not be empty", code="empty_path")
        if raw.startswith("~"):
            raise PathSecurityError(
                "Home-directory expansion is not allowed for workspace tools",
                code="home_expansion",
            )
        if raw.startswith(("$HOME", "${HOME}")):
            raise PathSecurityError(
                "Environment expansion is not allowed for workspace tools",
                code="env_expansion",
            )
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        return self._finish(_canonical(candidate), for_write=for_write)

    def recheck(self, path: str | Path, *, for_write: bool) -> ResolvedPath:
        """Re-canonicalize immediately before execution (symlink re-check seam)."""
        return self.resolve(str(path), for_write=for_write)

    def _finish(self, resolved: Path, *, for_write: bool) -> ResolvedPath:
        inside_workspace = _is_within(resolved, self._workspace)
        display = (
            resolved.relative_to(self._workspace).as_posix()
            if inside_workspace
            else str(resolved)
        )
        if for_write and not any(
            _is_within(resolved, root) for root in self._write_roots
        ):
            raise PathSecurityError(
                f"Write denied: {display} is outside the write roots",
                code="write_root",
            )
        if any(_is_within(resolved, root) for root in self._read_denyroots):
            raise PathSecurityError(
                f"Access denied: {display} is under a read-deny root",
                code="read_deny",
            )
        return ResolvedPath(
            absolute=resolved,
            key=str(resolved),
            display=display,
            inside_workspace=inside_workspace,
        )


# ---------------------------------------------------------------------------
# Requests, evaluations, and batch plans
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionRequest:
    """UI-facing data for one pending approval. Contains no behavior."""

    id: str
    call_id: str
    tool: str
    key: str | None = None
    bundle: str | None = None
    preview: str | None = None
    suggestions: tuple[str, ...] = ()
    default_rule: str = ""
    #: False when the call cannot be represented by a persistable rule (for
    #: example an over-long or non-encodable key); the UI must then offer a
    #: once-only decision and must never suggest a whole-tool grant.
    persistence_available: bool = True

    def to_dict(self) -> dict[str, Any]:
        key = self.key
        key_truncated = False
        if key is not None and len(key) > MAX_PERMISSION_KEY_CHARS:
            # Bound the event/log payload; matching already used the full key.
            key = key[:MAX_PERMISSION_KEY_CHARS]
            key_truncated = True
        return {
            "id": self.id,
            "call_id": self.call_id,
            "tool": self.tool,
            "key": key,
            "key_truncated": key_truncated,
            "bundle": self.bundle,
            "preview": self.preview,
            "suggestions": list(self.suggestions),
            "default_rule": self.default_rule,
            "persistence_available": self.persistence_available,
        }


@dataclass(frozen=True)
class Evaluation:
    """The engine's verdict plus the audit trail used to justify it."""

    call: ToolCall
    spec: ToolSpec | None
    key: str | None
    outcome: Outcome
    decision: Decision | None
    code: str
    reason: str
    rule: Rule | None = None
    suggestions: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @property
    def denied(self) -> bool:
        return self.outcome is Outcome.DENY

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call.id,
            "tool": self.call.name,
            "key": self.key,
            "outcome": self.outcome.value,
            "decision": self.decision.value if self.decision else None,
            "code": self.code,
            "reason": self.reason,
            "rule": self.rule.raw if self.rule else None,
            "suggestions": list(self.suggestions),
        }


@dataclass(frozen=True)
class BatchPlan:
    """A pure plan over a whole model tool-call batch. Executes nothing."""

    evaluations: tuple[Evaluation, ...] = ()

    @property
    def executable(self) -> bool:
        """True when every call is allowed (no ask/fail-turn left to resolve)."""
        return all(
            item.outcome is Outcome.ALLOW for item in self.evaluations
        )

    def for_call(self, call_id: str) -> Evaluation | None:
        for item in self.evaluations:
            if item.call.id == call_id:
                return item
        return None

    def allows(self) -> tuple[Evaluation, ...]:
        return tuple(item for item in self.evaluations if item.outcome is Outcome.ALLOW)

    def denies(self) -> tuple[Evaluation, ...]:
        return tuple(item for item in self.evaluations if item.outcome is Outcome.DENY)

    def asks(self) -> tuple[Evaluation, ...]:
        return tuple(item for item in self.evaluations if item.outcome is Outcome.ASK)

    def failures(self) -> tuple[Evaluation, ...]:
        return tuple(
            item for item in self.evaluations if item.outcome is Outcome.FAIL_TURN
        )

    def prepared(self) -> tuple[PreparedCall, ...]:
        """The allowed calls, ready for the dispatcher packet to execute."""
        return tuple(
            PreparedCall(
                call=item.call,
                spec=item.spec,
                key=item.key,
                decision=item.decision or Decision.ALLOW_ONCE,
            )
            for item in self.allows()
            if item.spec is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "evaluations": [item.to_dict() for item in self.evaluations],
        }


@dataclass(frozen=True)
class PreparedCall:
    """An allowed call handed to the (later) dispatcher, with its audit data."""

    call: ToolCall
    spec: ToolSpec
    key: str | None
    decision: Decision

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call.id,
            "tool": self.call.name,
            "key": self.key,
            "decision": self.decision.value,
        }


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


class PermissionEngine:
    """First-match permission evaluation with hard path boundaries.

    ``deny -> session grants -> allow -> ask -> mode``; ``deny`` is absolute.
    """

    def __init__(
        self,
        *,
        mode: str = "ask",
        allow: Iterable[str] = (),
        ask: Iterable[str] = (),
        deny: Iterable[str] = (),
        write_roots: Sequence[str] = ("./",),
        read_denyroots: Sequence[str] = (),
        on_unattended: str = "deny",
        workspace: str | Path | None = None,
        home: str | Path | None = None,
        path_guard: PathGuard | None = None,
    ) -> None:
        if mode not in ("allow", "ask", "deny"):
            raise PermissionRuleError(f"Unknown permission mode: {mode!r}")
        if on_unattended not in ("deny", "allow", "fail_turn"):
            raise PermissionRuleError(
                f"Unknown on_unattended policy: {on_unattended!r}"
            )
        self.mode = mode
        self.on_unattended: UnattendedMode = on_unattended  # type: ignore[assignment]
        self._allow = tuple(parse_rule(rule) for rule in allow)
        self._ask = tuple(parse_rule(rule) for rule in ask)
        self._deny = tuple(parse_rule(rule) for rule in deny)
        if path_guard is not None:
            self.path_guard = path_guard
        elif workspace is not None:
            self.path_guard = PathGuard(
                workspace,
                write_roots=write_roots,
                read_denyroots=read_denyroots,
                home=home,
            )
        else:
            self.path_guard = None

    @classmethod
    def from_config(
        cls,
        permissions: Any,
        *,
        workspace: str | Path,
        home: str | Path | None = None,
        mode: str | None = None,
        on_unattended: str | None = None,
    ) -> PermissionEngine:
        """Build an engine from a ``PermissionsSection``-shaped config object."""
        return cls(
            mode=mode if mode is not None else permissions.mode,
            allow=permissions.allow,
            ask=permissions.ask,
            deny=permissions.deny,
            write_roots=permissions.write_roots,
            read_denyroots=permissions.read_denyroots,
            on_unattended=(
                on_unattended if on_unattended is not None else permissions.on_unattended
            ),
            workspace=workspace,
            home=home,
        )

    # -- evaluation --------------------------------------------------------

    def _first_match(
        self, rules: Sequence[Rule], tool: str, key: str | None, bundle: str | None
    ) -> Rule | None:
        for rule in rules:
            if rule.matches(tool, key, bundle):
                return rule
        return None

    def evaluate(
        self,
        call: ToolCall,
        spec: ToolSpec | None,
        *,
        grants: Iterable[Grant] = (),
        attended: bool = True,
    ) -> Evaluation:
        """Evaluate one call. Pure: performs no I/O and executes nothing."""
        suggestions = self._suggestions(call, spec)
        if spec is None:
            return Evaluation(
                call=call,
                spec=None,
                key=None,
                outcome=Outcome.DENY,
                decision=None,
                code="unknown_tool",
                reason=f"Unknown tool {call.name!r}; denied (fail closed)",
                suggestions=suggestions,
            )

        try:
            key = spec.resolve_permission_key(call.input)
        except Exception as exc:  # noqa: BLE001 - a bad key must fail closed
            return Evaluation(
                call=call,
                spec=spec,
                key=None,
                outcome=Outcome.DENY,
                decision=None,
                code="permission_key_error",
                reason=f"permission_key failed for {call.name!r}: {exc}",
                suggestions=suggestions,
            )

        bundle = spec.bundle
        # Hard path boundaries run before any rule allow.
        boundary = self._check_boundaries(spec, key)
        if boundary is not None:
            code, reason = boundary
            return Evaluation(
                call=call,
                spec=spec,
                key=key,
                outcome=Outcome.DENY,
                decision=None,
                code=code,
                reason=reason,
                suggestions=suggestions,
            )

        denied = self._first_match(self._deny, call.name, key, bundle)
        if denied is not None:
            return self._verdict(
                call, spec, key, Outcome.DENY, denied, "deny", f"Denied by rule {denied.raw!r}", suggestions
            )

        grant_effect, grant = self._first_grant(grants, call.name, key, bundle)
        if grant_effect == "deny":
            return self._verdict(
                call, spec, key, Outcome.DENY, None, "session_grant_deny",
                f"Denied by session grant {grant.rule!r}", suggestions,
            )
        if grant_effect == "allow":
            return self._verdict(
                call, spec, key, Outcome.ALLOW, None, "session_grant",
                f"Allowed by session grant {grant.rule!r}", suggestions,
            )

        allowed = self._first_match(self._allow, call.name, key, bundle)
        if allowed is not None:
            return self._verdict(
                call, spec, key, Outcome.ALLOW, allowed, "allow",
                f"Allowed by rule {allowed.raw!r}", suggestions,
            )

        ask = self._first_match(self._ask, call.name, key, bundle)
        if ask is not None:
            return self._ask_verdict(call, spec, key, ask, attended, suggestions)

        if self.mode == "deny":
            return self._verdict(
                call, spec, key, Outcome.DENY, None, "mode_deny",
                "Denied by default mode", suggestions,
            )
        if self.mode == "allow":
            return self._verdict(
                call, spec, key, Outcome.ALLOW, None, "mode_allow",
                "Allowed by default mode", suggestions,
            )
        return self._ask_verdict(call, spec, key, None, attended, suggestions)

    def plan(
        self,
        calls: Sequence[ToolCall],
        specs: Mapping[str, ToolSpec],
        *,
        grants: Iterable[Grant] = (),
        attended: bool = True,
    ) -> BatchPlan:
        """Evaluate **every** call before anything executes; never dispatches."""
        grant_list = tuple(grants)
        return BatchPlan(
            tuple(
                self.evaluate(
                    call,
                    specs.get(call.name),
                    grants=grant_list,
                    attended=attended,
                )
                for call in calls
            )
        )

    def request_for(self, evaluation: Evaluation) -> PermissionRequest:
        key = evaluation.key
        preview = key[:200] if key else None
        # A keyed request is never given a bare whole-tool default: it uses the
        # most specific exact-action rule when the key is representable, and no
        # persistable rule at all otherwise (so ``*_ALWAYS`` degrades to
        # ``*_ONCE``). Only a keyless tool may default to its whole-tool rule.
        if key is None:
            default_rule = evaluation.call.name
            persistence_available = True
        elif _representable_exact_key(key):
            default_rule = exact_rule(evaluation.call.name, key)
            persistence_available = True
        else:
            default_rule = ""
            persistence_available = False
        return PermissionRequest(
            id=new_id(),
            call_id=evaluation.call.id,
            tool=evaluation.call.name,
            key=key,
            bundle=evaluation.spec.bundle if evaluation.spec else None,
            preview=preview,
            suggestions=evaluation.suggestions,
            default_rule=default_rule,
            persistence_available=persistence_available,
        )

    # -- internals ---------------------------------------------------------

    def _check_boundaries(
        self, spec: ToolSpec, key: str | None
    ) -> tuple[str, str] | None:
        if self.path_guard is None or key is None:
            return None
        if spec.bundle != "fs" and not spec.path_mode:
            return None
        try:
            self.path_guard.recheck(key, for_write=spec.mutates)
        except PathSecurityError as exc:
            return exc.code, str(exc)
        return None

    def _first_grant(
        self,
        grants: Iterable[Grant],
        tool: str,
        key: str | None,
        bundle: str | None,
    ) -> tuple[Effect | None, Grant | None]:
        for grant in grants:
            if grant.matches(tool, key, bundle):
                return grant.effect, grant
        return None, None

    def _suggestions(
        self, call: ToolCall, spec: ToolSpec | None
    ) -> tuple[str, ...]:
        key = None
        if spec is not None:
            try:
                key = spec.resolve_permission_key(call.input)
            except Exception:  # noqa: BLE001 - suggestions are best-effort
                key = None
        suggestions: list[str] = []
        if key:
            # A keyed request only ever suggests the exact-action rule. It must
            # never suggest whole-tool or bundle scope, which would broaden.
            if _representable_exact_key(key):
                suggestions.append(exact_rule(call.name, key))
        else:
            # Keyless tools may legitimately be granted as a whole.
            suggestions.append(call.name)
            if spec is not None and spec.bundle:
                suggestions.append(f"Bundle:{spec.bundle}")
        return tuple(suggestions)

    def _verdict(
        self,
        call: ToolCall,
        spec: ToolSpec,
        key: str | None,
        outcome: Outcome,
        rule: Rule | None,
        code: str,
        reason: str,
        suggestions: tuple[str, ...],
    ) -> Evaluation:
        decision: Decision | None = None
        if outcome is Outcome.ALLOW:
            decision = Decision.ALLOW_ONCE
        elif outcome is Outcome.DENY:
            decision = Decision.DENY_ONCE
        return Evaluation(
            call=call,
            spec=spec,
            key=key,
            outcome=outcome,
            decision=decision,
            code=code,
            reason=reason,
            rule=rule,
            suggestions=suggestions,
        )

    def _ask_verdict(
        self,
        call: ToolCall,
        spec: ToolSpec,
        key: str | None,
        rule: Rule | None,
        attended: bool,
        suggestions: tuple[str, ...],
    ) -> Evaluation:
        if attended:
            return Evaluation(
                call=call,
                spec=spec,
                key=key,
                outcome=Outcome.ASK,
                decision=None,
                code="ask",
                reason=(
                    f"Rule {rule.raw!r} requires approval"
                    if rule is not None
                    else "Default mode requires approval"
                ),
                rule=rule,
                suggestions=suggestions,
            )
        if self.on_unattended == "allow":
            return self._verdict(
                call, spec, key, Outcome.ALLOW, rule, "unattended_allow",
                "No approver available; unattended policy allows", suggestions,
            )
        if self.on_unattended == "fail_turn":
            return self._verdict(
                call, spec, key, Outcome.FAIL_TURN, rule, "unattended_fail_turn",
                "No approver available; unattended policy fails the turn", suggestions,
            )
        return self._verdict(
            call, spec, key, Outcome.DENY, rule, "unattended_deny",
            "No approver available; unattended policy denies", suggestions,
        )


# ---------------------------------------------------------------------------
# Approval primitives (no terminal I/O; any UI adapter drives these)
# ---------------------------------------------------------------------------


class ApprovalBroker:
    """Pending-approval futures plus a serialization-safe audit trail.

    ``request`` must be called from a running event loop; it returns a future
    the caller awaits while the turn is parked. ``resolve`` is what a UI calls
    with a :class:`Decision`; the resulting record carries a reconstructable
    :class:`Grant` for ``*_ALWAYS`` resolutions.
    """

    def __init__(
        self,
        *,
        scope: Scope = "session",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if scope not in ("session", "project", "user"):
            raise PermissionRuleError(f"Unknown scope: {scope!r}")
        self._scope: Scope = scope
        self._clock = clock
        self._futures: dict[str, asyncio.Future[Decision]] = {}
        self._requests: dict[str, PermissionRequest] = {}
        self._records: list[dict[str, Any]] = []

    def request(self, request: PermissionRequest) -> asyncio.Future[Decision]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Decision] = loop.create_future()
        self._futures[request.id] = future
        self._requests[request.id] = request
        return future

    def resolve(
        self,
        request_id: str,
        decision: Decision | str,
        *,
        rule: str | None = None,
    ) -> bool:
        request = self._requests.pop(request_id, None)
        future = self._futures.pop(request_id, None)
        if request is None or future is None or future.done():
            return False
        resolved = Decision.from_value(decision)
        chosen_rule = rule if rule is not None else request.default_rule
        # A keyed request may only persist the *exact* rule for its own tool and
        # key. Any caller-supplied broader or mismatched rule (bare tool, bundle,
        # glob/wildcard, or an exact rule for another tool/key), and any
        # unrepresentable key, is treated as unpersistable rather than persisted.
        if request.key is not None and (
            not _representable_exact_key(request.key)
            or not rule_is_exact_for(chosen_rule, request.tool, request.key)
        ):
            chosen_rule = ""
        effective = resolved
        grant: Grant | None = None
        if resolved.persists:
            if chosen_rule:
                try:
                    grant = grant_for_decision(
                        resolved, rule=chosen_rule, scope=self._scope
                    )
                except PermissionRuleError:
                    grant = None
            if grant is None:
                # Deterministically degrade to the corresponding ONCE decision
                # with no persisted grant; never broaden to the whole tool.
                effective = _once_decision(resolved)
        key = request.key
        key_truncated = False
        if key is not None and len(key) > MAX_PERMISSION_KEY_CHARS:
            key = key[:MAX_PERMISSION_KEY_CHARS]
            key_truncated = True
        record: dict[str, Any] = {
            "id": request_id,
            "call_id": request.call_id,
            "tool": request.tool,
            "key": key,
            "key_truncated": key_truncated,
            "bundle": request.bundle,
            "decision": effective.value,
            "scope": self._scope,
            "ts": self._clock(),
        }
        if grant is not None:
            record["grant"] = grant.to_dict()
        self._records.append(record)
        future.set_result(effective)
        return True

    def cancel(self, request_id: str) -> bool:
        future = self._futures.pop(request_id, None)
        self._requests.pop(request_id, None)
        if future is None or future.done():
            return False
        future.cancel()
        return True

    @property
    def pending(self) -> tuple[PermissionRequest, ...]:
        return tuple(
            request
            for request_id, request in self._requests.items()
            if request_id in self._futures
        )

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._records)

    def take_records(self) -> list[dict[str, Any]]:
        records, self._records = self._records, []
        return records

    def grants(self) -> tuple[Grant, ...]:
        return collect_grants(self._records)
