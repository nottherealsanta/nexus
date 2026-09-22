"""Hook contracts (plan section 5.7).

Deterministic behaviour the model cannot skip -- the thing prompts cannot
guarantee. A hook is attached to one lifecycle :class:`HookEvent` and, when the
event fires, returns a :class:`HookDecision`:

* ``allow``  -- no change;
* ``warn``   -- record an advisory and continue;
* ``block``  -- stop the action and surface ``reason`` to the caller (for a
  tool that becomes an error ``ToolResult`` the model sees);
* ``modify`` -- replace the event's input mapping. The chain is applied in
  order and the caller **must re-validate the schema and re-run the permission
  gate** on the new input; a hook is policy, not a permission grant.

Two hook kinds share this model:

* ``command`` -- an argv executed with no shell by default (plan section 5.7);
* ``python``  -- an in-process callable from ``.nexus/hooks/*.py``, loaded
  through the same quarantine/loader seam as tools (plan section 6.1).

This module is manager-layer (L3) and deliberately imports nothing from
``core``/``runtime``: a hook reaches a narrow, frozen :class:`HookInvocation`
and a :class:`HookContext`, never the loop. Matcher evaluation structurally
reuses the permission rule grammar (``nexus.tools.permissions``) so a matcher
is exactly a tool rule there.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..errors import NexusError

__all__ = [
    "HOOK_EVENTS",
    "MODIFIABLE_EVENTS",
    "TRUSTED_CODE_WARNING",
    "HookAction",
    "HookContext",
    "HookDecision",
    "HookError",
    "HookEvent",
    "HookFailure",
    "HookInvocation",
    "HookOnNonzero",
    "HookOutcome",
    "HookSpec",
]


class HookError(NexusError, ValueError):
    """A hook declaration, matcher, or decision is invalid."""


#: The warning surfaced whenever in-process Python hooks are loaded. Extension
#: files are trusted code, exactly as ``nexus.toml`` and ``SOUL.md`` are: the
#: permission engine gates *calls*, not *loading* (plan section 11).
TRUSTED_CODE_WARNING = (
    "in-process hooks are trusted code: loading a .py hook executes it with the "
    "harness's privileges. Only enable hooks you have reviewed."
)


class HookEvent(StrEnum):
    """The lifecycle events a hook may attach to (plan section 5.7)."""

    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    CONTEXT_ASSEMBLED = "ContextAssembled"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    PRE_COMPACT = "PreCompact"
    TURN_END = "TurnEnd"
    SESSION_END = "SessionEnd"
    EXTENSION_LOADED = "ExtensionLoaded"

    @classmethod
    def coerce(cls, value: object) -> HookEvent:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError as exc:
                raise HookError(f"unknown hook event: {value!r}") from exc
        raise HookError(f"hook event must be a string, got {type(value).__name__}")

    @classmethod
    def is_valid(cls, value: object) -> bool:
        try:
            cls.coerce(value)
        except HookError:
            return False
        return True


#: Every event name, in declaration order.
HOOK_EVENTS: tuple[str, ...] = tuple(event.value for event in HookEvent)

#: Events whose ``modify`` decision changes the input the caller will act on.
#: A ``modify`` returned for any other event is downgraded to a warning: there is
#: nothing to revalidate, so silently rewriting data would be a trap.
#:
#: ``PreCompact`` is modifiable only through typed compaction options
#: (``strategy``/``keep``/``keep_recent``/``min_content_chars``); the context
#: layer rejects an unrecognized option rather than acting on it.
MODIFIABLE_EVENTS = frozenset(
    {
        HookEvent.PRE_TOOL_USE.value,
        HookEvent.USER_PROMPT_SUBMIT.value,
        HookEvent.PRE_COMPACT.value,
    }
)


class HookAction(StrEnum):
    """What a hook decided."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    MODIFY = "modify"

    @classmethod
    def coerce(cls, value: object) -> HookAction:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            aliases = {"deny": "block", "approve": "allow", "ask": "warn"}
            text = aliases.get(text, text)
            try:
                return cls(text)
            except ValueError as exc:
                raise HookError(f"unknown hook action: {value!r}") from exc
        raise HookError(f"hook action must be a string, got {type(value).__name__}")


class HookOnNonzero(StrEnum):
    """How a command hook's non-zero exit / timeout is interpreted."""

    BLOCK = "block"
    WARN = "warn"
    IGNORE = "ignore"

    @classmethod
    def coerce(cls, value: object) -> HookOnNonzero:
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError as exc:
                raise HookError(f"unknown on_nonzero policy: {value!r}") from exc
        raise HookError(
            f"on_nonzero must be a string, got {type(value).__name__}"
        )


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HookError("hook input must be a mapping")
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise HookError("hook input keys must be strings")
        frozen[key] = item
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class HookDecision:
    """The verdict one hook returns (plan section 5.7)."""

    action: HookAction = HookAction.ALLOW
    reason: str = ""
    new_input: Mapping[str, Any] | None = None
    hook: str = ""
    event: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", HookAction.coerce(self.action))
        if not isinstance(self.reason, str):
            object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(self, "new_input", _freeze_mapping(self.new_input))
        if self.action is HookAction.MODIFY and self.new_input is None:
            raise HookError("a modify decision requires new_input")

    # -- constructors ------------------------------------------------------

    @classmethod
    def allow(cls, *, hook: str = "", event: str = "") -> HookDecision:
        return cls(action=HookAction.ALLOW, hook=hook, event=event)

    @classmethod
    def warn(
        cls, reason: str = "", *, hook: str = "", event: str = ""
    ) -> HookDecision:
        return cls(action=HookAction.WARN, reason=reason, hook=hook, event=event)

    @classmethod
    def block(
        cls, reason: str = "", *, hook: str = "", event: str = ""
    ) -> HookDecision:
        return cls(action=HookAction.BLOCK, reason=reason, hook=hook, event=event)

    @classmethod
    def modify(
        cls,
        new_input: Mapping[str, Any],
        reason: str = "",
        *,
        hook: str = "",
        event: str = "",
    ) -> HookDecision:
        return cls(
            action=HookAction.MODIFY,
            reason=reason,
            new_input=new_input,
            hook=hook,
            event=event,
        )

    # -- introspection -----------------------------------------------------

    @property
    def allows(self) -> bool:
        return self.action is HookAction.ALLOW

    @property
    def blocks(self) -> bool:
        return self.action is HookAction.BLOCK

    @property
    def modifies(self) -> bool:
        return self.action is HookAction.MODIFY

    @property
    def warned(self) -> bool:
        return self.action is HookAction.WARN

    def with_source(self, *, hook: str, event: str) -> HookDecision:
        """A copy tagged with the hook/event that produced it."""
        return HookDecision(
            action=self.action,
            reason=self.reason,
            new_input=self.new_input,
            hook=hook,
            event=event,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "new_input": dict(self.new_input) if self.new_input is not None else None,
            "hook": self.hook,
            "event": self.event,
        }


@dataclass(frozen=True)
class HookSpec:
    """One declarative hook (command from ``hooks.toml`` or a loaded function).

    The concrete type satisfies the manifest's structural ``HookSpec`` protocol
    (``.name``) without ``nexus.ext`` importing this package.
    """

    event: str
    name: str
    kind: str = "command"
    matcher: str | None = None
    command: tuple[str, ...] | None = None
    shell_command: str | None = None
    shell: bool = False
    on_nonzero: HookOnNonzero = HookOnNonzero.WARN
    timeout_s: float = 10.0
    disabled: bool = False
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    source: str = ""
    source_sha256: str = ""
    index: int = 0
    order: int = 0
    generation: int = 0
    fn: Callable[..., Any] | None = None

    def __post_init__(self) -> None:
        if not HookEvent.is_valid(self.event):
            raise HookError(f"HookSpec.event is not a known event: {self.event!r}")
        if not isinstance(self.name, str) or not self.name:
            raise HookError("HookSpec.name must be a non-empty string")
        if self.kind not in ("command", "python"):
            raise HookError(f"unknown hook kind: {self.kind!r}")
        if self.timeout_s is None:
            object.__setattr__(self, "timeout_s", 10.0)
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or self.timeout_s <= 0
        ):
            raise HookError("HookSpec.timeout_s must be a positive number")
        if not isinstance(self.disabled, bool):
            raise HookError("HookSpec.disabled must be a bool")
        if self.matcher is not None and not isinstance(self.matcher, str):
            raise HookError("HookSpec.matcher must be a string or None")
        if self.kind == "command":
            if self.shell:
                if not isinstance(self.shell_command, str) or not self.shell_command:
                    raise HookError("a shell command hook needs a command string")
            else:
                command = self.command
                if not command or not all(
                    isinstance(part, str) and part for part in command
                ):
                    raise HookError("a command hook needs a non-empty argv")
                object.__setattr__(self, "command", tuple(command))
        else:
            if not callable(self.fn):
                raise HookError("a python hook needs a callable fn")
        object.__setattr__(self, "env", _freeze_mapping(dict(self.env)) or MappingProxyType({}))

    # -- introspection -----------------------------------------------------

    @property
    def is_command(self) -> bool:
        return self.kind == "command"

    @property
    def is_python(self) -> bool:
        return self.kind == "python"

    @property
    def argv(self) -> tuple[str, ...]:
        """The argv to execute, or a single shell invocation."""
        if self.shell:
            return ("/bin/sh", "-c", self.shell_command or "")
        return tuple(self.command or ())

    def matches(
        self,
        tool: str | None,
        key: str | None = None,
        bundle: str | None = None,
    ) -> bool:
        """Whether this hook's matcher fires for a tool call.

        Matchers are permission rules, so ``Write(**/*.py)``, ``Read``,
        ``Bundle:fs`` and ``mcp__*`` all mean exactly what they mean in
        ``nexus.tools.permissions``. A missing matcher (or ``*``) matches every
        call; a matcher on a non-tool event can never match.
        """
        matcher = self.matcher
        if matcher is None or matcher == "" or matcher == "*":
            return True
        if tool is None:
            return False
        from ..tools.permissions import PermissionRuleError, parse_rule

        try:
            rule = parse_rule(matcher)
        except PermissionRuleError:
            return False
        return rule.matches(tool, key, bundle)

    def fingerprint(self) -> str:
        """A stable content digest that ignores the generation and ``fn``."""
        material: dict[str, Any] = {
            "event": self.event,
            "name": self.name,
            "kind": self.kind,
            "matcher": self.matcher,
            "on_nonzero": str(self.on_nonzero),
            "timeout_s": self.timeout_s,
            "disabled": self.disabled,
            "env": dict(sorted(self.env.items())),
            "cwd": self.cwd,
            "source": self.source,
            "source_sha256": self.source_sha256,
            "shell": self.shell,
            "command": list(self.command) if self.command is not None else None,
            "shell_command": self.shell_command,
        }
        encoded = json.dumps(material, sort_keys=True, default=repr)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """A sanitized, JSON-safe view for diagnostics/events."""
        from ..ext.quarantine import sanitize_text

        program: str | None = None
        argv: list[str] | None = None
        if self.kind == "command":
            if self.shell:
                program = "/bin/sh -c"
            else:
                parts = self.command or ()
                program = parts[0] if parts else None
                argv = list(parts)
        payload: dict[str, Any] = {
            "event": self.event,
            "name": self.name,
            "kind": self.kind,
            "matcher": self.matcher,
            "program": program,
            "on_nonzero": str(self.on_nonzero),
            "timeout_s": self.timeout_s,
            "disabled": self.disabled,
            "shell": self.shell,
            "source": sanitize_text(self.source, limit=200),
            "sha256": self.source_sha256,
            "generation": self.generation,
        }
        if argv is not None:
            payload["argv"] = [sanitize_text(part, limit=200) for part in argv]
        return payload


@dataclass(frozen=True)
class HookInvocation:
    """The frozen input a hook runs against. Contains no behavior or manager."""

    event: str
    tool: str | None = None
    key: str | None = None
    bundle: str | None = None
    tool_input: Mapping[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    turn_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not HookEvent.is_valid(self.event):
            raise HookError(f"unknown hook event: {self.event!r}")
        object.__setattr__(self, "tool_input", _freeze_mapping(self.tool_input) or MappingProxyType({}))
        object.__setattr__(self, "data", _freeze_mapping(self.data) or MappingProxyType({}))

    @property
    def tool_path(self) -> str | None:
        """The permission key when it is path-shaped, for ``$NEXUS_TOOL_PATH``."""
        if not isinstance(self.key, str):
            return None
        return self.key

    def with_tool_input(self, tool_input: Mapping[str, Any]) -> HookInvocation:
        return HookInvocation(
            event=self.event,
            tool=self.tool,
            key=self.key,
            bundle=self.bundle,
            tool_input=tool_input,
            session_id=self.session_id,
            turn_id=self.turn_id,
            data=self.data,
        )

    def env(self) -> dict[str, str]:
        """The bounded ``NEXUS_*`` context exported to a command hook.

        Only these explicit values cross the process boundary; the host
        environment is never inherited wholesale.
        """
        env: dict[str, str] = {"NEXUS_HOOK_EVENT": self.event}
        if self.tool is not None:
            env["NEXUS_TOOL_NAME"] = self.tool
        if self.key is not None:
            env["NEXUS_TOOL_KEY"] = self.key
        if self.tool_path is not None:
            env["NEXUS_TOOL_PATH"] = self.tool_path
        if self.bundle is not None:
            env["NEXUS_TOOL_BUNDLE"] = self.bundle
        if self.session_id is not None:
            env["NEXUS_SESSION_ID"] = self.session_id
        if self.turn_id is not None:
            env["NEXUS_TURN_ID"] = self.turn_id
        return env

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "tool": self.tool,
            "key": self.key,
            "bundle": self.bundle,
            "tool_input": dict(self.tool_input),
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "data": dict(self.data),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, default=repr)


@dataclass(frozen=True)
class HookContext:
    """The narrow surface an in-process Python hook receives."""

    workspace: Any
    event: str
    session_id: str | None = None
    turn_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "event": self.event,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
        }


@dataclass(frozen=True)
class HookFailure:
    """One sanitized hook discovery/load failure (never a source body)."""

    kind: str
    name: str
    error: str = ""
    error_type: str = ""
    path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise HookError("HookFailure.kind must be a non-empty string")
        if not isinstance(self.name, str) or not self.name:
            raise HookError("HookFailure.name must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "error": self.error,
            "error_type": self.error_type,
            "path": self.path,
        }


@dataclass(frozen=True)
class HookOutcome:
    """The aggregate result of running every hook attached to one event.

    ``modified_input`` is set only when at least one ``modify`` ran; whenever it
    is set :attr:`requires_revalidation` is ``True`` and the caller **must**
    re-validate the new input against the tool schema and re-evaluate the
    permission gate before acting on it. A hook is policy, not a grant.
    """

    event: str
    decision: HookAction = HookAction.ALLOW
    reason: str = ""
    original_input: Mapping[str, Any] | None = None
    modified_input: Mapping[str, Any] | None = None
    decisions: tuple[HookDecision, ...] = ()
    warnings: tuple[str, ...] = ()
    fired: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", HookAction.coerce(self.decision))
        object.__setattr__(self, "original_input", _freeze_mapping(self.original_input))
        object.__setattr__(self, "modified_input", _freeze_mapping(self.modified_input))

    @classmethod
    def allow(cls, event: str) -> HookOutcome:
        return cls(event=event)

    @property
    def allowed(self) -> bool:
        return self.decision in (HookAction.ALLOW, HookAction.MODIFY)

    @property
    def blocked(self) -> bool:
        return self.decision is HookAction.BLOCK

    @property
    def modified(self) -> bool:
        return self.modified_input is not None

    @property
    def requires_revalidation(self) -> bool:
        """True when ``modified_input`` must be revalidated by the caller."""
        return self.modified_input is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "decision": self.decision.value,
            "reason": self.reason,
            "modified": self.modified,
            "requires_revalidation": self.requires_revalidation,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "warnings": list(self.warnings),
            "fired": list(self.fired),
            "failures": list(self.failures),
            "modified_input": (
                dict(self.modified_input) if self.modified_input is not None else None
            ),
        }
