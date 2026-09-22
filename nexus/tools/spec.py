"""Tool contracts (plan section 3.4).

A tool is described by a :class:`ToolSpec`: the model-facing declaration
(``name``/``description``/``input_schema``) plus harness behaviour that is
**never sent to a model** (bundle, concurrency, timeout, permission key). The
implementation is a coroutine taking ``(input, ToolContext)`` and returning a
:class:`ToolExecutionResult`.

Naming note
-----------
The plan calls the execution result ``ToolResult``, but the message IR already
owns that name (:class:`nexus.model.message.ToolResult`, a content block). To
keep the two contracts distinct, the harness-side type here is
:class:`ToolExecutionResult`; :meth:`ToolExecutionResult.to_tool_result` converts
it into the IR block the loop persists.

Boundary note
-------------
:class:`ToolContext` deliberately does **not** expose a ``Runtime``. A tool can
reach exactly the workspace, its session/turn identity, config, a cancellation
token, a progress emitter, and (in later packets) narrowly-typed ``spawn_agent``
and ``invoke_tool`` callables. That boundary is what lets a hot-loaded tool be
reviewed rather than trusted with the whole stack.
"""
from __future__ import annotations

import inspect
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import msgspec

from ..config import Config
from ..errors import ToolError
from ..model.message import ContentBlock, Text, ToolResult, ToolUse
from ..model.request import ToolSchema

#: Tool names: one leading letter, then up to 63 letters/digits/underscores.
NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")

#: JSON Schema type keywords accepted at the top level or nested.
JSON_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

Concurrency = Literal["parallel", "exclusive"]


class ToolSpecError(ToolError, ValueError):
    """A tool declaration is invalid: bad name, schema, or harness field."""


# ---------------------------------------------------------------------------
# Minimal structural views (keeps this module free of core/manager imports)
# ---------------------------------------------------------------------------


class CancelTokenView(Protocol):
    """The subset of :class:`nexus.core.cancel.CancelToken` a tool may use."""

    @property
    def cancelled(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    def raise_if_cancelled(self) -> None: ...

    async def wait(self) -> None: ...


class ProgressEmitter(Protocol):
    """A UI-agnostic progress sink: ``emit(event_type, data)``."""

    def __call__(
        self, event_type: str, data: dict[str, Any] | None = ...
    ) -> object | Awaitable[object]: ...


class JobRegistryView(Protocol):
    """The shell-job registry seam injected through :class:`ToolContext`.

    Structural only: it names the methods the shell built-ins use so the tools
    layer can type the seam without importing the concrete
    :class:`nexus.tools.builtin._jobs.JobRegistry` (and therefore without a
    cycle). ``None`` until the tool manager packet supplies one.
    """

    def job(self, job_id: object) -> object | None: ...

    async def kill(self, job_id: object) -> object: ...

    async def spawn(
        self,
        command: str,
        *,
        cwd: object,
        env: object = ...,
        output_limit: int | None = ...,
    ) -> object: ...

    async def aclose(self) -> None: ...


class TodoStoreView(Protocol):
    """The session-scoped todo-state seam injected through :class:`ToolContext`.

    Implemented structurally by :class:`nexus.tools.builtin.todo.TodoStore`; the
    manager owns one and injects it so ``TodoWrite`` never writes a workspace
    file and the state survives across calls in a session.
    """

    def get(self, session_id: str) -> tuple[object, ...]: ...

    def replace(self, session_id: str, items: object) -> tuple[object, ...]: ...

    def clear(self, session_id: str) -> None: ...


#: How a tool declares what a permission rule matches against.
PermissionKeyFn = Callable[[dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_tool_name(name: object) -> str:
    """Return ``name`` when it matches the tool-name grammar, else raise."""
    if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
        raise ToolSpecError(
            "Tool name must start with a letter and contain only letters, "
            "digits, or underscores (max 64 chars)"
        )
    return name


def _validate_json_value(value: object, path: str) -> None:
    """Reject anything that cannot be encoded as JSON (JSON Schema must be)."""
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if "\x00" in value:
            raise ToolSpecError(f"{path} contains a NUL byte")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolSpecError(f"{path} has a non-string object key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ToolSpecError(
        f"{path} is not JSON-serializable: {type(value).__name__}"
    )


def _validate_schema_node(node: object, path: str, *, root: bool = False) -> None:
    if not isinstance(node, dict):
        raise ToolSpecError(f"{path} must be a JSON Schema object")
    _validate_json_value(node, path)
    node_type = node.get("type")
    if root:
        if node_type != "object":
            raise ToolSpecError(
                "input_schema must declare \"type\": \"object\" at the top level"
            )
    elif node_type is not None and (
        not isinstance(node_type, str) or node_type not in JSON_SCHEMA_TYPES
    ):
        raise ToolSpecError(f"{path}.type is not a JSON Schema type: {node_type!r}")
    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ToolSpecError(f"{path}.properties must be an object")
        for name, sub_schema in properties.items():
            if not isinstance(name, str):
                raise ToolSpecError(f"{path}.properties has a non-string key")
            _validate_schema_node(sub_schema, f"{path}.properties.{name}")
    required = node.get("required")
    if required is not None and (
        not isinstance(required, list)
        or not all(isinstance(item, str) for item in required)
    ):
        raise ToolSpecError(f"{path}.required must be a list of strings")
    if "items" in node:
        _validate_schema_node(node["items"], f"{path}.items")
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in node:
            branches = node[combinator]
            if not isinstance(branches, list):
                raise ToolSpecError(f"{path}.{combinator} must be a list")
            for index, branch in enumerate(branches):
                _validate_schema_node(branch, f"{path}.{combinator}[{index}]")
    if "additionalProperties" in node:
        extra = node["additionalProperties"]
        if not isinstance(extra, bool):
            _validate_schema_node(extra, f"{path}.additionalProperties")


def validate_input_schema(schema: object) -> dict[str, Any]:
    """Validate the Phase 2 subset of JSON Schema and return the mapping.

    The top level must be an object schema; nested property/items/combinator
    schemas are validated recursively; the whole document must be JSON-safe.
    Unknown keywords are permitted (they are forwarded to the model untouched).
    """
    if not isinstance(schema, dict):
        raise ToolSpecError("input_schema must be a JSON object")
    _validate_schema_node(schema, "input_schema", root=True)
    return schema


# ---------------------------------------------------------------------------
# The spec
# ---------------------------------------------------------------------------


class ToolSpec(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A validated, immutable tool declaration (plan section 3.4)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    bundle: str
    mutates: bool = False
    concurrency: Concurrency = "parallel"
    timeout_s: float | None = None
    permission_key: PermissionKeyFn | None = None
    max_result_tokens: int = 25_000
    version: str = "1"

    def __post_init__(self) -> None:
        from .bundles import BUNDLE_NAMES

        validate_tool_name(self.name)
        if not isinstance(self.description, str) or not self.description.strip():
            raise ToolSpecError("description must be a non-empty string")
        if "\x00" in self.description:
            raise ToolSpecError("description must not contain a NUL byte")
        validate_input_schema(self.input_schema)
        if not isinstance(self.bundle, str) or self.bundle not in BUNDLE_NAMES:
            raise ToolSpecError(
                f"bundle must be one of {', '.join(sorted(BUNDLE_NAMES))}"
            )
        if not isinstance(self.mutates, bool):
            raise ToolSpecError("mutates must be a bool")
        if self.concurrency not in ("parallel", "exclusive"):
            raise ToolSpecError("concurrency must be 'parallel' or 'exclusive'")
        if self.timeout_s is not None and (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ToolSpecError("timeout_s must be a positive finite number")
        if self.permission_key is not None and not callable(self.permission_key):
            raise ToolSpecError("permission_key must be callable or None")
        if (
            isinstance(self.max_result_tokens, bool)
            or not isinstance(self.max_result_tokens, int)
            or self.max_result_tokens <= 0
        ):
            raise ToolSpecError("max_result_tokens must be a positive integer")
        if not isinstance(self.version, str) or not self.version:
            raise ToolSpecError("version must be a non-empty string")

    def to_schema(self) -> ToolSchema:
        """The model-facing declaration (no harness behaviour)."""
        return ToolSchema(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
        )

    def resolve_permission_key(self, tool_input: dict[str, Any]) -> str | None:
        """Invoke ``permission_key`` safely; ``None`` when the tool declares none.

        A malformed return value raises :class:`ToolSpecError`; the permission
        engine catches it and denies rather than trusting a bad key.
        """
        if self.permission_key is None:
            return None
        value = self.permission_key(dict(tool_input))
        if not isinstance(value, str) or not value:
            raise ToolSpecError(
                f"{self.name}.permission_key must return a non-empty string"
            )
        if "\x00" in value:
            raise ToolSpecError(f"{self.name}.permission_key returned a NUL byte")
        return value


# ---------------------------------------------------------------------------
# Calls and results
# ---------------------------------------------------------------------------


class ToolCall(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A parsed, validated tool invocation (the tools-layer view of a ToolUse)."""

    id: str
    name: str
    input: dict[str, Any] = msgspec.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ToolSpecError("ToolCall.id must be a non-empty string")
        validate_tool_name(self.name)
        if not isinstance(self.input, dict):
            raise ToolSpecError("ToolCall.input must be an object")

    @classmethod
    def from_tool_use(cls, block: ToolUse) -> ToolCall:
        return cls(id=block.id, name=block.name, input=dict(block.input))

    def to_tool_use(self) -> ToolUse:
        return ToolUse(id=self.id, name=self.name, input=dict(self.input))


class ToolExecutionResult(msgspec.Struct, forbid_unknown_fields=True):
    """What a tool returns to the harness (the plan's ``ToolResult``)."""

    content: list[ContentBlock]
    is_error: bool = False
    display: str | None = None
    metrics: dict[str, Any] | None = None
    context_note: str | None = None

    @classmethod
    def text(
        cls,
        text: str,
        *,
        is_error: bool = False,
        display: str | None = None,
        context_note: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> ToolExecutionResult:
        """Convenience constructor for the common single-text-block result."""
        body = str(text)
        return cls(
            content=[Text(text=body)],
            is_error=is_error,
            display=body if display is None else display,
            context_note=context_note,
            metrics=metrics,
        )

    def to_tool_result(self, tool_use_id: str) -> ToolResult:
        """Convert to the IR content block the loop persists.

        ``context_note`` is carried into the persisted block so later context
        compaction can evict the (large) content in an assembled copy without
        losing the durable, re-runnable note.
        """
        return ToolResult(
            tool_use_id=tool_use_id,
            content=list(self.content),
            is_error=self.is_error,
            context_note=self.context_note,
        )


# ---------------------------------------------------------------------------
# Execution context and registration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """Everything a tool is allowed to reach. Never exposes a ``Runtime``."""

    workspace: Path
    session_id: str
    turn_id: str
    config: Config
    cancel_token: CancelTokenView | None = None
    call_id: str = ""
    emit: ProgressEmitter | None = None
    #: Supplied by the agents packet; ``None`` until then.
    spawn_agent: Callable[..., Awaitable[Any]] | None = None
    #: Supplied by the tool manager packet; ``None`` until then.
    invoke_tool: Callable[..., Awaitable[Any]] | None = None
    #: Explicit service seams injected by the tool manager (never a Runtime).
    job_registry: JobRegistryView | None = None
    todo_store: TodoStoreView | None = None

    async def report(self, text: str, data: dict[str, Any] | None = None) -> None:
        """Emit a ``tool.progress`` event if a sink is attached."""
        if self.emit is None:
            return
        payload = {"text": str(text), **dict(data or {})}
        outcome = self.emit("tool.progress", payload)
        if inspect.isawaitable(outcome):
            await outcome


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[ToolExecutionResult]]

REGISTRATION_ORIGINS = ("builtin", "mcp", "ext", "skill")


@dataclass(frozen=True)
class RegisteredTool:
    """A spec paired with its implementation and provenance."""

    spec: ToolSpec
    run: ToolFn
    origin: str = "builtin"
    source: str | None = None
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.spec, ToolSpec):
            raise ToolSpecError("RegisteredTool.spec must be a ToolSpec")
        if not callable(self.run):
            raise ToolSpecError("RegisteredTool.run must be callable")
        if self.origin not in REGISTRATION_ORIGINS:
            raise ToolSpecError(
                f"origin must be one of {', '.join(REGISTRATION_ORIGINS)}"
            )
        if not isinstance(self.generation, int) or self.generation < 0:
            raise ToolSpecError("generation must be a non-negative integer")

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def bundle(self) -> str:
        return self.spec.bundle

    @property
    def mutates(self) -> bool:
        return self.spec.mutates

    def to_schema(self) -> ToolSchema:
        return self.spec.to_schema()


__all__ = [
    "JSON_SCHEMA_TYPES",
    "NAME_PATTERN",
    "REGISTRATION_ORIGINS",
    "CancelTokenView",
    "Concurrency",
    "JobRegistryView",
    "PermissionKeyFn",
    "ProgressEmitter",
    "RegisteredTool",
    "TodoStoreView",
    "ToolCall",
    "ToolContext",
    "ToolExecutionResult",
    "ToolFn",
    "ToolSpec",
    "ToolSpecError",
    "validate_input_schema",
    "validate_tool_name",
]
