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

from ..config.paths import state_db_path
from ..errors import NexusError
from ..util import new_id
from .names import (
    LEGACY_BASH_PERMISSION_ACTIONS,
    canonical_permission_rule,
    canonical_tool_name,
    permission_bundle_matches,
)
from .spec import PathTarget, ToolCall, ToolSpec

__all__ = [
    "ApprovalBroker",
    "BatchPlan",
    "Decision",
    "Evaluation",
    "Grant",
    "Outcome",
    "PathGuard",
    "PathSecurityError",
    "PathTargetEvaluation",
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
MAX_PERMISSION_TARGETS = 64
MAX_PERMISSION_TARGET_REQUEST_CHARS = 65536


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

    def matches(
        self,
        tool: str,
        key: str | None,
        bundle: str | None,
        *,
        input_data: Mapping[str, Any] | None = None,
    ) -> bool:
        tool = canonical_tool_name(tool)
        if self.tool in LEGACY_BASH_PERMISSION_ACTIONS and tool == "bash":
            # The legacy names are intentionally not dispatch aliases. Translate
            # their permission rules only for explicit unified-job actions, and
            # use the structured action as well as the qualified key so a run
            # command such as ``status:job_1`` cannot inherit a job permission.
            actions = LEGACY_BASH_PERMISSION_ACTIONS[self.tool]
            action = input_data.get("action") if input_data is not None else None
            if (
                bundle != "shell"
                or action not in actions
                or key is None
                or not key.startswith(f"{action}:")
                or not key[len(action) + 1 :]
            ):
                return False
            job_key = key[len(action) + 1 :]
            if self.kind == "tool":
                return True
            if self.kind == "tool_pattern":
                return self.pattern is not None and _glob_matches(
                    self.pattern, job_key
                )
            if self.kind == "tool_exact":
                return self.pattern == job_key
            return False
        if self.kind == "tool":
            return self.tool == tool
        if self.kind == "wildcard":
            return self.tool is not None and fnmatch.fnmatchcase(tool, self.tool)
        if self.kind == "bundle":
            return self.bundle is not None and permission_bundle_matches(
                self.bundle, bundle
            )
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
    return Rule(
        raw=raw,
        kind="tool_exact",
        tool=canonical_tool_name(head),
        pattern=decoded,
    )


def _validate_legacy_job_pattern(tool: str, pattern: str, raw: str) -> None:
    """Reject action-qualified legacy keys, which are not job IDs."""
    actions = LEGACY_BASH_PERMISSION_ACTIONS.get(tool, ())
    if pattern.partition(":")[0] in actions:
        raise PermissionRuleError(
            f"Legacy job permission {raw!r} must use an unqualified job ID; "
            f"remove the action prefix (for example {tool}({pattern.split(':', 1)[1]}))"
        )


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

    # Tool-name compatibility applies only to a whole exact tool head;
    # wildcard heads and every argument/pattern retain their original semantics.
    # Bundle heads stay spelled as written; the bounded historical unions are
    # applied only when the parsed bundle rule is matched.
    canonical_raw = canonical_permission_rule(raw)

    if canonical_raw.startswith(_BUNDLE_PREFIX):
        name = canonical_raw[len(_BUNDLE_PREFIX) :]
        if ":" in name or "(" in name or ")" in name:
            raise PermissionRuleError(f"Malformed bundle rule: {raw!r}")
        if _TOOL_NAME.fullmatch(name) is None:
            raise PermissionRuleError(f"Invalid bundle name in rule: {raw!r}")
        return Rule(raw=raw, kind="bundle", bundle=name)

    if ":" in canonical_raw and "(" not in canonical_raw:
        raise PermissionRuleError(
            f"Unknown rule prefix (only {_BUNDLE_PREFIX!r}) or ambiguous ':': {raw!r}"
        )

    if canonical_raw.endswith(")"):
        exact = _parse_exact_rule(raw)
        if exact is not None:
            _validate_legacy_job_pattern(exact.tool or "", exact.pattern or "", raw)
            # Equality rules are safe for any key, including one containing
            # ``~``. (Glob/pattern rules still reject ``~`` because there it is
            # either ambiguous or inert against canonical keys.)
            return exact
        if raw.count("(") != 1 or raw.count(")") != 1:
            raise PermissionRuleError(
                f"Malformed rule (nested or unbalanced parentheses): {raw!r}"
            )
        head, separator, pattern = canonical_raw[:-1].partition("(")
        if separator != "(" or "(" in head or ")" in head:
            raise PermissionRuleError(f"Malformed rule: {raw!r}")
        if _TOOL_NAME.fullmatch(head) is None:
            raise PermissionRuleError(f"Invalid tool name in rule: {raw!r}")
        if not pattern or "(" in pattern or ")" in pattern:
            raise PermissionRuleError(f"Malformed or empty pattern in rule: {raw!r}")
        _validate_legacy_job_pattern(head, pattern, raw)
        if "\x00" in pattern:
            raise PermissionRuleError("Permission pattern must not contain a NUL byte")
        if _looks_like_home_path(pattern):
            raise PermissionRuleError(
                f"Permission rule pattern {raw!r} uses '~'; tool permission keys "
                "are canonical paths, so '~' would never match. Use an absolute "
                "path, or configure read_denyroots/write_roots for home locations"
            )
        original_head = raw[:-1].partition("(")[0]
        return Rule(
            raw=raw,
            kind="tool_pattern",
            tool=canonical_tool_name(original_head),
            pattern=pattern,
        )

    if "(" in canonical_raw or ")" in canonical_raw:
        raise PermissionRuleError(f"Malformed rule (unbalanced parentheses): {raw!r}")

    if _TOOL_NAME.fullmatch(canonical_raw) is not None:
        return Rule(raw=raw, kind="tool", tool=canonical_raw)

    if _WILDCARD_NAME.fullmatch(canonical_raw) is not None and any(
        char in canonical_raw for char in _GLOB_CHARS
    ):
        return Rule(raw=raw, kind="wildcard", tool=canonical_raw)

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

    def matches(
        self,
        tool: str,
        key: str | None,
        bundle: str | None,
        *,
        input_data: Mapping[str, Any] | None = None,
    ) -> bool:
        return self.compiled().matches(
            tool, key, bundle, input_data=input_data
        )

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
    return (
        rule.kind == "tool_exact"
        and rule.tool == canonical_tool_name(tool)
        and rule.pattern == key
    )


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
        raw_targets = record.get("targets")
        if raw_targets is not None:
            raw_target_grants = record.get("target_grants")
            tool = record.get("tool")
            decision = record.get("decision")
            if not (
                isinstance(raw_targets, list)
                and isinstance(raw_target_grants, list)
                and 0 < len(raw_targets) <= MAX_PERMISSION_TARGETS
                and len(raw_target_grants) == len(raw_targets)
                and isinstance(tool, str)
                and tool
                and decision in (
                    Decision.ALLOW_ALWAYS.value,
                    Decision.DENY_ALWAYS.value,
                )
            ):
                continue
            target_paths: list[str] = []
            target_roles: list[str] = []
            valid = True
            for target in raw_targets:
                if not isinstance(target, Mapping):
                    valid = False
                    break
                path, role = target.get("path"), target.get("role")
                if not (
                    isinstance(path, str)
                    and path
                    and _representable_exact_key(path)
                    and isinstance(role, str)
                    and role
                ):
                    valid = False
                    break
                target_paths.append(path)
                target_roles.append(role)
            reconstructed: list[Grant] = []
            if valid:
                for index, item in enumerate(raw_target_grants):
                    if not isinstance(item, Mapping):
                        valid = False
                        break
                    raw_grant = item.get("grant")
                    if (
                        item.get("path") != target_paths[index]
                        or item.get("role") != target_roles[index]
                    ):
                        valid = False
                        break
                    try:
                        grant = Grant.from_dict(raw_grant)
                    except PermissionRuleError:
                        valid = False
                        break
                    if not rule_is_exact_for(
                        grant.rule, tool, target_paths[index]
                    ) or grant.effect != (
                        "allow"
                        if decision == Decision.ALLOW_ALWAYS.value
                        else "deny"
                    ):
                        valid = False
                        break
                    reconstructed.append(grant)
            if valid and len(reconstructed) == len(raw_targets):
                grants.extend(reconstructed)
            # Plural records are atomic: never fall through to a scalar grant.
            continue

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
                    rule = exact_rule(canonical_tool_name(tool), key)
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

    __slots__ = (
        "_home",
        "_read_denyroot_specs",
        "_read_denyroots",
        "_settings_scopes",
        "_state_db_paths",
        "_workspace",
        "_worktree_boundary",
        "_write_root_specs",
        "_write_roots",
    )

    def __init__(
        self,
        workspace: str | Path,
        *,
        write_roots: Sequence[str] = ("./",),
        read_denyroots: Sequence[str] = (),
        home: str | Path | None = None,
        _allow_empty_write_roots: bool = False,
        _worktree_boundary: bool = False,
        settings_scopes: Sequence[str | Path] = (),
    ) -> None:
        self._workspace = _canonical(Path(workspace))
        self._home = Path(home) if home is not None else Path.home()
        roots = tuple(write_roots) or ("./",)
        self._write_root_specs = roots
        self._write_roots = tuple(
            self._root(root, home=home, label="write_roots") for root in roots
        )
        self._read_denyroot_specs = tuple(read_denyroots)
        self._read_denyroots = tuple(
            self._root(root, home=home, label="read_denyroots")
            for root in read_denyroots
        )
        state_db = state_db_path(home)
        self._state_db_paths = frozenset(
            _canonical(Path(f"{state_db}{suffix}"))
            for suffix in ("", "-wal", "-shm")
        )
        # A rebased policy can have no writable intersection with the child.
        # The normal constructor's empty-list default remains unchanged.
        if _allow_empty_write_roots and not write_roots:
            self._write_root_specs = ()
            self._write_roots = ()
        self._worktree_boundary = _worktree_boundary
        self._settings_scopes = tuple(_canonical(Path(root)) for root in settings_scopes)

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

    def for_worktree(self, child_workspace: str | Path) -> PathGuard:
        """Return a child-checkout guard with authority rebased conservatively.

        Relative write roots retain their configured spelling and are resolved
        under the child checkout. Absolute write roots are clipped to their
        intersection with the child; disjoint roots are discarded. The child
        workspace is also an unconditional write boundary, including when a
        configured relative root contains ``..`` or resolves through symlinks.
        Read-deny roots keep their canonical parent locations; relative deny
        roots are also rebased under the child so matching child paths stay
        denied. PermissionEngine rules are not broadened or rewritten.
        """
        child = _canonical(Path(child_workspace))
        if child == self._workspace or _is_within(self._workspace, child):
            raise PathSecurityError(
                "A worktree workspace cannot contain its parent checkout",
                code="worktree_workspace",
            )

        rebased_write_roots: list[str] = []
        for spec, canonical_root in zip(
            self._write_root_specs, self._write_roots
        ):
            candidate = Path(spec)
            if spec.startswith("~"):
                # Tilde roots are explicit home-scoped absolute authority.
                candidate = canonical_root
            elif not candidate.is_absolute():
                # Keep the original relative scope; the child constructor will
                # resolve it against the child workspace, not this guard.
                rebased_write_roots.append(spec)
                continue
            else:
                candidate = canonical_root

            # Tree intersections are representable only when one canonical
            # directory contains the other. Keep the narrower side of that
            # intersection and discard disjoint absolute authority.
            if _is_within(candidate, child):
                rebased_write_roots.append(str(candidate))
            elif _is_within(child, candidate):
                rebased_write_roots.append(str(child))

        # Keep every canonical deny root so parent secrets stay denied, and
        # also retain relative specs so the corresponding child paths are
        # denied. Tilde and absolute roots remain canonical absolute paths.
        rebased_read_denyroots: list[str] = []
        for spec, root in zip(
            self._read_denyroot_specs, self._read_denyroots
        ):
            rebased_read_denyroots.append(str(root))
            candidate = Path(spec)
            if not spec.startswith("~") and not candidate.is_absolute():
                rebased_read_denyroots.append(spec)
        return PathGuard(
            child,
            write_roots=tuple(rebased_write_roots),
            read_denyroots=tuple(dict.fromkeys(rebased_read_denyroots)),
            home=self._home,
            _allow_empty_write_roots=True,
            _worktree_boundary=True,
            settings_scopes=self._settings_scopes,
        )

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
        if resolved in self._state_db_paths:
            raise PathSecurityError(
                f"Access denied: {display} is shared Nexus state",
                code="state_db",
            )
        if for_write and self._worktree_boundary and not inside_workspace:
            raise PathSecurityError(
                f"Write denied: {display} is outside the child workspace",
                code="write_root",
            )
        if for_write and not any(
            _is_within(resolved, root) for root in self._write_roots
        ):
            raise PathSecurityError(
                f"Write denied: {display} is outside the write roots",
                code="write_root",
            )
        for root in self._settings_scopes:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            if any(
                part
                in {
                    "credentials.json",
                    "sessions",
                    "cache",
                    "daemon",
                    "daemon.sock",
                    "daemon.pid",
                    "daemon.lock",
                    # The shared session state database (STATE_PLAN §5.4) is
                    # machine state, never editable through a tool call.
                    "nexus.db",
                }
                or part.startswith("trash")
                for part in relative.parts
            ):
                raise PathSecurityError(
                    f"Access denied: {display} is outside the Settings agent's allowed files",
                    code="settings_scope",
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
    targets: tuple[PermissionTargetRequest, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        key = self.key
        key_truncated = False
        if key is not None and len(key) > MAX_PERMISSION_KEY_CHARS:
            # Bound the event/log payload; matching already used the full key.
            key = key[:MAX_PERMISSION_KEY_CHARS]
            key_truncated = True
        payload = {
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
        if self.targets:
            payload["targets"] = [target.to_dict() for target in self.targets]
        return payload


@dataclass(frozen=True)
class PermissionTargetRequest:
    """A bounded, UI-facing detail for one path awaiting approval."""

    role: str
    path: str
    reason: str
    suggested_rule: str

    def to_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "path": self.path,
            "reason": self.reason,
            "suggested_rule": self.suggested_rule,
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
    target_evaluations: tuple[PathTargetEvaluation, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    @property
    def denied(self) -> bool:
        return self.outcome is Outcome.DENY

    def to_dict(self) -> dict[str, Any]:
        payload = {
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
        if self.target_evaluations:
            payload["targets"] = [
                item.to_dict() for item in self.target_evaluations
            ]
        return payload


@dataclass(frozen=True)
class PathTargetEvaluation:
    """The permission verdict and audit trail for one multi-path target."""

    role: str
    key: str
    outcome: Outcome
    decision: Decision | None
    code: str
    reason: str
    rule: Rule | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "key": self.key,
            "outcome": self.outcome.value,
            "decision": self.decision.value if self.decision else None,
            "code": self.code,
            "reason": self.reason,
            "rule": self.rule.raw if self.rule else None,
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


#: Builtins whose only effect is talking to the operator; they never ask first.
_NEVER_ASK_TOOLS = frozenset({"question"})


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
        self._force_ask_tools: frozenset[str] = frozenset()
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

    def require_confirmation_for(self, tools: Iterable[str]) -> None:
        """Force matching tools to ask even when an allow rule would match."""
        self._force_ask_tools = frozenset(
            canonical_tool_name(name) for name in tools if isinstance(name, str)
        )

    # -- evaluation --------------------------------------------------------

    def _first_match(
        self,
        rules: Sequence[Rule],
        tool: str,
        key: str | None,
        bundle: str | None,
        *,
        input_data: Mapping[str, Any] | None = None,
    ) -> Rule | None:
        for rule in rules:
            if rule.matches(tool, key, bundle, input_data=input_data):
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

        public_name = canonical_tool_name(call.name)
        denied = self._first_match(
            self._deny, public_name, key, bundle, input_data=call.input
        )
        if denied is not None:
            return self._verdict(
                call, spec, key, Outcome.DENY, denied, "deny", f"Denied by rule {denied.raw!r}", suggestions
            )

        if public_name in self._force_ask_tools:
            return self._ask_verdict(
                call, spec, key, None, attended, suggestions
            )

        grant_effect, grant = self._first_grant(
            grants, public_name, key, bundle, input_data=call.input
        )
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

        allowed = self._first_match(
            self._allow, public_name, key, bundle, input_data=call.input
        )
        if allowed is not None:
            return self._verdict(
                call, spec, key, Outcome.ALLOW, allowed, "allow",
                f"Allowed by rule {allowed.raw!r}", suggestions,
            )

        # Asking the operator a question is itself the approval surface, so it
        # never opens an approval of its own; deny rules and deny mode still win.
        if public_name in _NEVER_ASK_TOOLS and self.mode != "deny":
            return self._verdict(
                call, spec, key, Outcome.ALLOW, None, "user_interaction",
                "Questions go to the operator directly", suggestions,
            )

        ask = self._first_match(
            self._ask, public_name, key, bundle, input_data=call.input
        )
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
                self._evaluate_multi_target(
                    call, specs[call.name], grant_list, attended
                )
                if (
                    call.name in specs
                    and specs[call.name].multi_path_targets is not None
                )
                else self.evaluate(
                    call,
                    specs.get(call.name),
                    grants=grant_list,
                    attended=attended,
                )
                for call in calls
            )
        )

    def _evaluate_multi_target(
        self,
        call: ToolCall,
        spec: ToolSpec,
        grants: tuple[Grant, ...],
        attended: bool,
    ) -> Evaluation:
        """Plan a multi-path call by evaluating each target independently.

        Multi-target calls never use their scalar permission key. Every target
        must first pass the hard path boundaries, then uses the usual ordered
        deny/grant/allow/ask/mode policy against its own canonical path.
        """
        # A scalar-key suggestion would invite a grant unrelated to the targets.
        suggestions: tuple[str, ...] = ()
        try:
            targets = spec.resolve_multi_path_targets(call.input)
            if not targets:
                raise ValueError("multi-path resolver returned no targets")
            if len(targets) > MAX_PERMISSION_TARGETS:
                return Evaluation(
                    call=call,
                    spec=spec,
                    key=None,
                    outcome=Outcome.DENY,
                    decision=Decision.DENY_ONCE,
                    code="multi_target_request_limit",
                    reason=(
                        f"multi-path call has {len(targets)} targets; the "
                        f"approval request limit is {MAX_PERMISSION_TARGETS}"
                    ),
                    suggestions=suggestions,
                )
            target_evaluations: list[PathTargetEvaluation] = []
            for target in targets:
                try:
                    if self.path_guard is None:
                        raise PathSecurityError(
                            "Multi-path policy requires a path guard",
                            code="path_guard",
                        )
                    resolved = self.path_guard.resolve(
                        target.path, for_write=True
                    )
                    canonical_path = str(resolved.absolute)
                except PathSecurityError as exc:
                    target_evaluations.append(
                        PathTargetEvaluation(
                            target.role,
                            target.path,
                            Outcome.DENY,
                            Decision.DENY_ONCE,
                            exc.code,
                            str(exc),
                        )
                    )
                    continue
                canonical = PathTarget(
                    target.role, canonical_path, target.path
                )
                target_evaluations.append(
                    self._evaluate_target(call, spec, canonical, grants, attended)
                )
        except PathSecurityError as exc:
            return Evaluation(
                call=call,
                spec=spec,
                key=None,
                outcome=Outcome.DENY,
                decision=Decision.DENY_ONCE,
                code=exc.code,
                reason=str(exc),
                suggestions=suggestions,
            )
        except Exception as exc:  # noqa: BLE001 - invalid resolver output fails closed
            return Evaluation(
                call=call,
                spec=spec,
                key=None,
                outcome=Outcome.DENY,
                decision=Decision.DENY_ONCE,
                code="multi_path_target_error",
                reason=f"multi-path target resolution failed: {exc}",
                suggestions=suggestions,
            )

        target_evaluations = tuple(target_evaluations)
        pending_targets = tuple(
            item for item in target_evaluations if item.outcome is Outcome.ASK
        )
        estimated_request_size = sum(
            len(item.role)
            + len(item.key)
            + min(len(item.reason), 512)
            + len(
                exact_rule(canonical_tool_name(call.name), item.key)
                if _representable_exact_key(item.key)
                else ""
            )
            + 64
            for item in pending_targets
        )
        if estimated_request_size * 6 > MAX_PERMISSION_TARGET_REQUEST_CHARS:
            return Evaluation(
                call=call,
                spec=spec,
                key=None,
                outcome=Outcome.DENY,
                decision=Decision.DENY_ONCE,
                code="multi_target_request_limit",
                reason="multi-path approval details exceed the bounded request size",
                suggestions=suggestions,
                target_evaluations=target_evaluations,
            )
        request_details = [
            {
                "role": item.role,
                "path": item.key,
                "reason": item.reason[:512],
                "suggested_rule": (
                    exact_rule(canonical_tool_name(call.name), item.key)
                    if _representable_exact_key(item.key)
                    else ""
                ),
            }
            for item in pending_targets
        ]
        request_preview = "\n".join(
            f"{item['role']}: {item['path']} — {item['reason']}"
            for item in request_details
        )
        request_size = len(
            json.dumps(
                {"targets": request_details, "preview": request_preview},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if request_size > MAX_PERMISSION_TARGET_REQUEST_CHARS:
            return Evaluation(
                call=call,
                spec=spec,
                key=None,
                outcome=Outcome.DENY,
                decision=Decision.DENY_ONCE,
                code="multi_target_request_limit",
                reason="multi-path approval details exceed the bounded request size",
                suggestions=suggestions,
                target_evaluations=target_evaluations,
            )
        if any(item.outcome is Outcome.DENY for item in target_evaluations):
            outcome = Outcome.DENY
            code = "multi_target_deny"
            reason = "Denied because at least one path target was denied"
            decision = Decision.DENY_ONCE
        elif any(item.outcome is Outcome.FAIL_TURN for item in target_evaluations):
            outcome = Outcome.FAIL_TURN
            code = "multi_target_fail_turn"
            reason = "Failed because at least one path target requires a failed turn"
            decision = None
        elif any(item.outcome is Outcome.ASK for item in target_evaluations):
            outcome = Outcome.ASK
            code = "multi_target_ask"
            reason = "Approval is required for at least one path target"
            decision = None
        else:
            outcome = Outcome.ALLOW
            code = "multi_target_allow"
            reason = "Allowed because every path target was allowed"
            decision = Decision.ALLOW_ONCE
        return Evaluation(
            call=call,
            spec=spec,
            key=None,
            outcome=outcome,
            decision=decision,
            code=code,
            reason=reason,
            suggestions=suggestions,
            target_evaluations=target_evaluations,
        )

    def _evaluate_target(
        self,
        call: ToolCall,
        spec: ToolSpec,
        target: PathTarget,
        grants: tuple[Grant, ...],
        attended: bool,
    ) -> PathTargetEvaluation:
        key = target.path
        # Multi-target references are path security boundaries even when their
        # owning tool belongs to a non-filesystem bundle.
        if self.path_guard is not None:
            try:
                self.path_guard.recheck(key, for_write=True)
            except PathSecurityError as exc:
                return PathTargetEvaluation(
                    target.role,
                    key,
                    Outcome.DENY,
                    Decision.DENY_ONCE,
                    exc.code,
                    str(exc),
                )

        tool = canonical_tool_name(call.name)
        denied = self._first_match(
            self._deny, tool, key, spec.bundle, input_data=call.input
        )
        if denied is not None:
            return PathTargetEvaluation(
                target.role,
                key,
                Outcome.DENY,
                Decision.DENY_ONCE,
                "deny",
                f"Denied by rule {denied.raw!r}",
                denied,
            )

        if tool in self._force_ask_tools:
            verdict = self._ask_verdict(call, spec, key, None, attended, ())
            return PathTargetEvaluation(
                target.role,
                key,
                verdict.outcome,
                verdict.decision,
                verdict.code,
                verdict.reason,
                verdict.rule,
            )
        effect, grant = self._first_grant(
            grants, tool, key, spec.bundle, input_data=call.input
        )
        if effect == "deny":
            return PathTargetEvaluation(
                target.role,
                key,
                Outcome.DENY,
                Decision.DENY_ONCE,
                "session_grant_deny",
                f"Denied by session grant {grant.rule!r}",
            )
        if effect == "allow":
            return PathTargetEvaluation(
                target.role,
                key,
                Outcome.ALLOW,
                Decision.ALLOW_ONCE,
                "session_grant",
                f"Allowed by session grant {grant.rule!r}",
            )
        allowed = self._first_match(
            self._allow, tool, key, spec.bundle, input_data=call.input
        )
        if allowed is not None:
            return PathTargetEvaluation(
                target.role,
                key,
                Outcome.ALLOW,
                Decision.ALLOW_ONCE,
                "allow",
                f"Allowed by rule {allowed.raw!r}",
                allowed,
            )
        ask = self._first_match(
            self._ask, tool, key, spec.bundle, input_data=call.input
        )
        if ask is not None:
            verdict = self._ask_verdict(call, spec, key, ask, attended, ())
        elif self.mode == "deny":
            return PathTargetEvaluation(
                target.role,
                key,
                Outcome.DENY,
                Decision.DENY_ONCE,
                "mode_deny",
                "Denied by default mode",
            )
        elif self.mode == "allow":
            return PathTargetEvaluation(
                target.role,
                key,
                Outcome.ALLOW,
                Decision.ALLOW_ONCE,
                "mode_allow",
                "Allowed by default mode",
            )
        else:
            verdict = self._ask_verdict(call, spec, key, None, attended, ())
        return PathTargetEvaluation(
            target.role,
            key,
            verdict.outcome,
            verdict.decision,
            verdict.code,
            verdict.reason,
            verdict.rule,
        )

    def request_for(self, evaluation: Evaluation) -> PermissionRequest:
        key = evaluation.key
        preview = key[:200] if key else None
        targets: tuple[PermissionTargetRequest, ...] = ()
        # A keyed request is never given a bare whole-tool default: it uses the
        # most specific exact-action rule when the key is representable, and no
        # persistable rule at all otherwise (so ``*_ALWAYS`` degrades to
        # ``*_ONCE``). Only a keyless tool may default to its whole-tool rule.
        if evaluation.target_evaluations:
            unresolved = tuple(
                item
                for item in evaluation.target_evaluations
                if item.outcome is Outcome.ASK
            )
            targets = tuple(
                PermissionTargetRequest(
                    role=item.role,
                    path=item.key,
                    reason=item.reason[:512],
                    suggested_rule=(
                        exact_rule(canonical_tool_name(evaluation.call.name), item.key)
                        if _representable_exact_key(item.key)
                        else ""
                    ),
                )
                for item in unresolved
            )
            # Existing approval UIs render only ``preview``. Keep every pending
            # path visible there as well as in the structured target details.
            preview = "\n".join(
                f"{target.role}: {target.path} — {target.reason}"
                for target in targets
            )
            default_rule = ""
            persistence_available = bool(targets) and all(
                bool(target.suggested_rule) for target in targets
            )
        elif key is None:
            default_rule = canonical_tool_name(evaluation.call.name)
            persistence_available = True
        elif _representable_exact_key(key):
            default_rule = exact_rule(canonical_tool_name(evaluation.call.name), key)
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
            suggestions=(
                tuple(
                    dict.fromkeys(
                        target.suggested_rule
                        for target in targets
                        if target.suggested_rule
                    )
                )
                if targets
                else evaluation.suggestions
            ),
            default_rule=default_rule,
            persistence_available=persistence_available,
            targets=targets,
        )

    # -- internals ---------------------------------------------------------

    def _check_boundaries(
        self, spec: ToolSpec, key: str | None
    ) -> tuple[str, str] | None:
        if self.path_guard is None or key is None:
            return None
        if not permission_bundle_matches("fs", spec.bundle) and not spec.path_mode:
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
        *,
        input_data: Mapping[str, Any] | None = None,
    ) -> tuple[Effect | None, Grant | None]:
        for grant in grants:
            if grant.matches(tool, key, bundle, input_data=input_data):
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
                suggestions.append(exact_rule(canonical_tool_name(call.name), key))
        else:
            # Keyless tools may legitimately be granted as a whole. Keep the
            # original display spelling for historical approval UI records.
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
        target_grants: list[tuple[PermissionTargetRequest, Grant]] = []
        if resolved.persists:
            if request.targets:
                if (
                    request.persistence_available
                    and len(request.targets) <= MAX_PERMISSION_TARGETS
                    and all(
                        target.path
                        and _representable_exact_key(target.path)
                        and rule_is_exact_for(
                            target.suggested_rule, request.tool, target.path
                        )
                        for target in request.targets
                    )
                ):
                    try:
                        target_grants = [
                            (
                                target,
                                grant_for_decision(
                                    resolved,
                                    rule=target.suggested_rule,
                                    scope=self._scope,
                                ),
                            )
                            for target in request.targets
                        ]
                    except PermissionRuleError:
                        target_grants = []
                    if any(item is None for _, item in target_grants):
                        target_grants = []
                if len(target_grants) != len(request.targets):
                    target_grants = []
            elif chosen_rule:
                try:
                    grant = grant_for_decision(
                        resolved, rule=chosen_rule, scope=self._scope
                    )
                except PermissionRuleError:
                    grant = None
            if grant is None and not target_grants:
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
        if request.targets:
            record["targets"] = [target.to_dict() for target in request.targets]
            if target_grants:
                record["target_grants"] = [
                    {
                        "role": target.role,
                        "path": target.path,
                        "grant": target_grant.to_dict(),
                    }
                    for target, target_grant in target_grants
                ]
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
