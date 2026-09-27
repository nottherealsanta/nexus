"""ToolManager: selected registry, validation, scheduling, and dispatch.

Plan section 5.3. The manager is the L3 owner of:

* an **immutable, ordered catalog** selected from the built-in tools (``fs`` +
  ``shell`` + ``task``) for one frozen profile/config snapshot;
* one shell :class:`~nexus.tools.builtin._jobs.JobRegistry` (owned or injected)
  and one :class:`~nexus.tools.builtin.todo.TodoStore`, both injected through the
  explicit :class:`~nexus.tools.spec.ToolContext` seams;
* **strict JSON Schema validation** of every call before permission or dispatch;
* **preparation** (:meth:`ToolManager.prepare`) that turns unknown tools and
  schema-invalid inputs into ordered, model-visible error results and
  canonicalizes filesystem permission keys through its :class:`PathGuard`;
* **dispatch** (:meth:`ToolManager.dispatch`) with bounded parallelism, exclusive
  tools run alone, per-tool timeouts, cooperative cancellation, exception
  conversion, and result capping.

Permission gating happens *outside* the manager and *before* dispatch. The
manager never prompts: it consumes allow/deny decisions (typically through
:meth:`PreparedBatch.apply_plan`) and refuses to start an executable call that
has no decision. Nothing in this module imports the session, runtime, context, or
core-loop concrete classes; it reaches the event stream only through
:class:`nexus.events.Event` (L0) and the model/tool contracts.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import math
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import msgspec

from ..config import Config
from ..errors import OperationCancelled, ToolError
from ..events import Event
from ..model.message import DUPLICATE_TOOL_CALL_KEY, Image, Text
from ..model.request import ToolSchema
from .bundles import DEFAULT_PROFILE, get_bundle, get_profile
from .names import canonical_tool_name, permission_bundle_matches
from .permissions import (
    BatchPlan,
    Decision,
    Outcome,
    PathGuard,
    PathSecurityError,
)
from .spec import (
    CancelTokenView,
    PathTarget,
    RegisteredTool,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
    ToolSpecError,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "DuplicateToolError",
    "PreparedBatch",
    "PreparedCall",
    "ToolInputError",
    "ToolManager",
    "ToolManagerError",
    "ToolPreview",
    "ToolSelectionError",
    "validate_tool_input",
]

#: The same 4 chars/token heuristic the context builder is calibrated against.
CHARS_PER_TOKEN = 4
#: A single image is charged this many tokens when budgeting a result.
IMAGE_TOKEN_ESTIMATE = 1600
#: Hard cap on a rendered ``display`` string before persistence.
MAX_DISPLAY_CHARS = 2000
#: Hard cap on any single metric string value.
MAX_METRIC_CHARS = 200
#: Extra grace before the manager's backstop timeout cancels a ``Bash`` call; the
#: tool owns its configured timeout and should be allowed to report it first.
BASH_TIMEOUT_GRACE_S = 5.0


class ToolManagerError(ToolError):
    """A manager configuration or dispatch-contract violation."""


class DuplicateToolError(ToolManagerError):
    """Two registered tools share a name."""


class ToolSelectionError(ToolManagerError):
    """An explicitly requested tool name is not in the catalog."""


class ToolInputError(ToolError):
    """A call's input does not satisfy its declared JSON schema."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__("; ".join(self.errors))


# ---------------------------------------------------------------------------
# JSON Schema validation (the Phase 2 subset used by the built-ins)
# ---------------------------------------------------------------------------


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "null":
        return value is None
    # Unknown type keyword: the spec validator already rejected non-JSON types,
    # so an unrecognized keyword is treated as unconstrained (forward-compatible).
    return True


def _json_equal(left: object, right: object) -> bool:
    """Equality that never confuses ``True``/``1`` or ``False``/``0``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    return left == right


def _validate_node(
    value: object, schema: Mapping[str, Any], path: str, errors: list[str]
) -> None:
    expected = schema.get("type")
    if isinstance(expected, str) and not _matches_type(value, expected):
        errors.append(
            f"{path}: expected {expected}, got {_type_name(value)}"
        )
        return

    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(value, item) for item in enum):
        rendered = ", ".join(repr(item) for item in enum)
        errors.append(f"{path}: must be one of [{rendered}] (got {value!r})")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path}: must be at least {min_length} character(s)")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path}: must be at most {max_length} character(s)")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    errors.append(f"{path}: must match pattern {pattern!r}")
            except re.error:
                pass

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: must be >= {minimum} (got {value})")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: must be <= {maximum} (got {value})")

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: must contain at least {min_items} item(s)")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: must contain at most {max_items} item(s)")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_node(item, items, f"{path}[{index}]", errors)

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in value:
                    errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties")
        known = set(properties) if isinstance(properties, Mapping) else set()
        if isinstance(properties, Mapping):
            for name, sub_schema in properties.items():
                if name in value and isinstance(sub_schema, Mapping):
                    _validate_node(
                        value[name], sub_schema, f"{path}.{name}", errors
                    )
        additional = schema.get("additionalProperties")
        if additional is False:
            for name in value:
                if name not in known:
                    errors.append(f"{path}: unexpected property {name!r}")
        elif isinstance(additional, Mapping):
            for name in value:
                if name not in known:
                    _validate_node(
                        value[name], additional, f"{path}.{name}", errors
                    )


def validate_tool_input(spec: ToolSpec, tool_input: Mapping[str, Any]) -> None:
    """Raise :class:`ToolInputError` unless ``tool_input`` satisfies the schema.

    Implements the subset the built-ins declare: object/required/
    additionalProperties, scalar and array/object property types (``integer`` is
    distinct from ``boolean``), finite ``number``, ``array``/``items``, ``enum``,
    ``minLength``/``maxLength``, ``minimum``/``maximum``, and
    ``minItems``/``maxItems``.
    """
    if not isinstance(tool_input, Mapping):
        raise ToolInputError(["input: must be an object"])
    errors: list[str] = []
    _validate_node(dict(tool_input), spec.input_schema, "input", errors)
    if errors:
        raise ToolInputError(errors)


# ---------------------------------------------------------------------------
# Prepared calls and batches
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPreview:
    """A non-executing preview of one call's permission shape.

    Used to hand a ``PreToolUse``/``PostToolUse`` hook the canonical permission
    key and bundle without executing anything. Filesystem and path-mode keys are
    canonicalized through the manager's :class:`PathGuard`, exactly as
    :meth:`ToolManager.prepare` would, so a hook matcher such as
    ``Write(**/*.py)`` sees the same absolute key the permission engine will.
    """

    name: str
    bundle: str | None = None
    key: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PreparedCall:
    """One batch entry after validation and key canonicalization.

    Exactly one of ``error`` (a model-visible pre-execution error) or ``spec``
    (an executable call awaiting a permission decision) is meaningful. ``key`` is
    the canonical permission key the gate should match against.
    """

    call: ToolCall
    spec: ToolSpec | None
    key: str | None = None
    error: ToolExecutionResult | None = None
    code: str | None = None
    decision: Decision | None = None
    path_targets: tuple[PathTarget, ...] = ()
    _multi_target_authorization: object | None = dataclasses.field(
        default=None, repr=False, compare=False
    )

    @property
    def executable(self) -> bool:
        return self.spec is not None and self.error is None

    @property
    def permitted(self) -> bool:
        return self.executable and self.decision is not None and self.decision.allows


@dataclass(frozen=True)
class PreparedBatch:
    """An ordered, gated batch ready for :meth:`ToolManager.dispatch`."""

    entries: tuple[PreparedCall, ...] = ()

    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def for_call(self, call_id: str) -> PreparedCall | None:
        for entry in self.entries:
            if entry.call.id == call_id:
                return entry
        return None

    @property
    def executable(self) -> tuple[PreparedCall, ...]:
        return tuple(entry for entry in self.entries if entry.executable)

    @property
    def errors(self) -> tuple[PreparedCall, ...]:
        return tuple(entry for entry in self.entries if entry.error is not None)

    @property
    def unresolved(self) -> tuple[PreparedCall, ...]:
        """Executable calls the gate has not yet decided (never dispatched)."""
        return tuple(
            entry for entry in self.executable if entry.decision is None
        )

    def calls(self) -> tuple[ToolCall, ...]:
        """The executable calls, in order, for the permission engine."""
        return tuple(entry.call for entry in self.executable)

    def spec_map(self) -> dict[str, ToolSpec]:
        return {
            entry.spec.name: entry.spec
            for entry in self.executable
            if entry.spec is not None
        }

    def apply_plan(self, plan: BatchPlan) -> PreparedBatch:
        """Fold a permission :class:`BatchPlan` into decisions/errors.

        ``ALLOW`` becomes a decision; ``DENY``/``FAIL_TURN`` becomes a
        model-visible error. ``ASK`` is left undecided for the caller to resolve
        with :meth:`with_decisions` after the approval round-trip.

        Evaluations are matched to entries **positionally**, consuming each
        evaluation at most once, so duplicate ids cannot let a later evaluation
        overwrite an earlier one (the plan is built from this batch's executable
        calls in order).
        """
        remaining = list(plan.evaluations)
        updated: list[PreparedCall] = []
        for entry in self.entries:
            if entry.error is not None or entry.spec is None:
                updated.append(entry)
                continue
            evaluation = None
            for index, candidate in enumerate(remaining):
                if candidate.call.id == entry.call.id:
                    evaluation = remaining.pop(index)
                    break
            if evaluation is None:
                updated.append(entry)
                continue
            if evaluation.outcome is Outcome.ALLOW:
                updated.append(
                    dataclasses.replace(
                        entry,
                        decision=evaluation.decision or Decision.ALLOW_ONCE,
                    )
                )
            elif evaluation.outcome in (Outcome.DENY, Outcome.FAIL_TURN):
                updated.append(
                    dataclasses.replace(
                        entry,
                        decision=Decision.DENY_ONCE,
                        code=evaluation.code,
                        error=ToolExecutionResult.text(
                            f"{entry.call.name}: {evaluation.reason}",
                            is_error=True,
                        ),
                    )
                )
            else:  # ASK: await an explicit decision
                updated.append(entry)
        return PreparedBatch(tuple(updated))

    def with_decisions(
        self,
        decisions: Mapping[str, Decision | str]
        | Sequence[tuple[str, Decision | str]],
    ) -> PreparedBatch:
        """Attach resolved decisions (e.g. after an approval round-trip).

        Accepts a mapping or an ordered sequence of ``(call_id, decision)``
        pairs. Each decision is consumed at most once, so the same id supplied
        twice applies to two distinct entries rather than overwriting one.
        """
        if isinstance(decisions, Mapping):
            remaining: list[tuple[str, Decision | str]] = list(decisions.items())
        else:
            remaining = [(call_id, decision) for call_id, decision in decisions]
        updated: list[PreparedCall] = []
        for entry in self.entries:
            if not entry.executable:
                updated.append(entry)
                continue
            raw: Decision | str | None = None
            for index, (call_id, decision) in enumerate(remaining):
                if call_id == entry.call.id:
                    raw = decision
                    remaining.pop(index)
                    break
            if raw is None:
                updated.append(entry)
                continue
            decision = Decision.from_value(raw)
            if decision.denies:
                updated.append(
                    dataclasses.replace(
                        entry,
                        decision=decision,
                        code="permission_denied",
                        error=ToolExecutionResult.text(
                            f"{entry.call.name}: denied by permission decision",
                            is_error=True,
                        ),
                    )
                )
            else:
                updated.append(dataclasses.replace(entry, decision=decision))
        return PreparedBatch(tuple(updated))

    def to_ir_results(
        self, results: Sequence[ToolExecutionResult]
    ) -> tuple[Any, ...]:
        """Convert ordered execution results into ordered IR ``ToolResult`` blocks."""
        if len(results) != len(self.entries):
            raise ToolManagerError("results must align one-to-one with entries")
        return tuple(
            result.to_tool_result(entry.call.id)
            for entry, result in zip(self.entries, results)
        )


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


def _safe_message(value: object, *, limit: int = 500) -> str:
    text = str(value).replace("\x00", "")
    return text if len(text) <= limit else text[:limit] + "…"


def _constant_permission_key(key: str) -> Callable[[dict[str, Any]], str]:
    """A permission-key function that always returns a fixed canonical path.

    Used by the explicit manager path mode: after the manager resolves a
    tool's declared relative key through the :class:`PathGuard`, it hands the
    permission engine a spec whose key function returns that absolute key, so
    rule matching and persisted grants use the canonical form.
    """

    def _key(_data: dict[str, Any]) -> str:
        return key

    return _key


def _first_text(result: ToolExecutionResult) -> str:
    for block in result.content:
        if isinstance(block, Text):
            return _safe_message(block.text)
    return ""


def _tool_call_digest(call: ToolCall) -> str:
    """Stable identity for the complete prepared call, including its inputs."""
    payload = json.dumps(
        {"id": call.id, "name": call.name, "input": call.input},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate_lines(text: str, max_chars: int) -> str:
    """UTF-8 safe prefix of ``text`` cut at a line boundary when possible."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    newline = head.rfind("\n")
    if newline > max_chars // 2:
        return head[:newline]
    return head


def _permissions_section(config: Config):
    from ..config.schema import PermissionsSection

    v2 = getattr(config, "v2", None)
    section = getattr(v2, "permissions", None)
    return section if isinstance(section, PermissionsSection) else PermissionsSection()


def _tools_section(config: Config):
    from ..config.schema import ToolsSection

    v2 = getattr(config, "v2", None)
    section = getattr(v2, "tools", None)
    return section if isinstance(section, ToolsSection) else ToolsSection()


class ToolManager:
    """Owns a frozen tool catalog plus the services and scheduling for calls."""

    def __init__(
        self,
        config: Config,
        *,
        workspace: str | Path,
        profile: str | None = None,
        tools: Sequence[RegisteredTool] | None = None,
        tool_names: Iterable[str] | None = None,
        restrict: Iterable[str] | None = None,
        job_registry: Any | None = None,
        todo_store: Any | None = None,
        path_guard: PathGuard | None = None,
        default_timeout_s: float | None = None,
        max_parallel: int | None = None,
    ) -> None:
        self._config = config
        self._workspace = Path(workspace).resolve()

        v2 = getattr(config, "v2", None)
        agent = getattr(v2, "agent", None)
        self._profile_name = profile or getattr(agent, "profile", None) or DEFAULT_PROFILE
        self._profile = get_profile(self._profile_name)  # raises UnknownProfileError

        catalog = tuple(tools) if tools is not None else self._builtin_catalog()
        self._catalog = catalog
        self._by_name = self._index(catalog)
        selected = self._select(catalog, tool_names)
        if restrict is not None:
            # An activation overlay narrows the selected catalog to a subset;
            # unknown names are ignored (they were never available). Order is
            # preserved so schemas stay deterministic.
            allowed = {
                name for name in restrict if isinstance(name, str)
            }
            selected = tuple(tool for tool in selected if tool.name in allowed)
        self._tools = selected
        # ``prepare`` must resolve against the *selected* catalog, not the full
        # one: a profile that excludes a tool (e.g. ``research`` excludes
        # ``Write``) has to reject a call to it as an unknown tool. ``_by_name``
        # remains the full-catalog index used only for explicit selection.
        self._selected = self._index(self._tools)

        tools_section = _tools_section(config)
        self._max_result_tokens = tools_section.max_result_tokens
        self._max_parallel = (
            max_parallel if max_parallel is not None else tools_section.max_parallel
        )
        if (
            isinstance(self._max_parallel, bool)
            or not isinstance(self._max_parallel, int)
            or self._max_parallel < 1
        ):
            raise ToolManagerError("max_parallel must be a positive integer")
        self._default_timeout_s = default_timeout_s
        self._bash_timeout_s = float(tools_section.bash_timeout_s)

        permissions = _permissions_section(config)
        self._path_guard = path_guard or PathGuard(
            self._workspace,
            write_roots=tuple(permissions.write_roots) or ("./",),
            read_denyroots=tuple(permissions.read_denyroots),
        )

        self._owns_job_registry = job_registry is None
        self._job_registry = (
            self._new_job_registry() if job_registry is None else job_registry
        )
        self._owns_todo_store = todo_store is None
        self._todo_store = self._new_todo_store() if todo_store is None else todo_store
        self._closed = False
        self._multi_target_authorizations: dict[object, tuple[Any, ...]] = {}
        self._multi_target_authority = object()

    # -- construction helpers ---------------------------------------------

    @staticmethod
    def _builtin_catalog() -> tuple[RegisteredTool, ...]:
        from .builtin import BUILTIN_CATALOG

        return BUILTIN_CATALOG

    @staticmethod
    def _new_job_registry():
        from .builtin._jobs import JobRegistry

        return JobRegistry()

    @staticmethod
    def _new_todo_store():
        from .builtin.todo import TodoStore

        return TodoStore()

    @staticmethod
    def _index(catalog: Sequence[RegisteredTool]) -> Mapping[str, RegisteredTool]:
        indexed: dict[str, RegisteredTool] = {}
        for tool in catalog:
            if not isinstance(tool, RegisteredTool):
                raise ToolManagerError("tools must be RegisteredTool instances")
            if tool.name in indexed:
                raise DuplicateToolError(f"duplicate tool name {tool.name!r}")
            indexed[tool.name] = tool
        return MappingProxyType(indexed)

    def _select(
        self,
        catalog: Sequence[RegisteredTool],
        tool_names: Iterable[str] | None,
    ) -> tuple[RegisteredTool, ...]:
        if tool_names is not None:
            selected: list[RegisteredTool] = []
            seen: set[str] = set()
            for name in tool_names:
                if not isinstance(name, str):
                    raise ToolSelectionError("tool names must be strings")
                canonical = canonical_tool_name(name)
                from .names import LEGACY_TOOL_NAMES

                if canonical in self._by_name and name in LEGACY_TOOL_NAMES:
                    name = canonical
                if name in seen:
                    raise DuplicateToolError(f"duplicate requested tool {name!r}")
                tool = self._by_name.get(name)
                if tool is None:
                    raise ToolSelectionError(
                        f"unknown tool {name!r}; available: "
                        f"{', '.join(sorted(self._by_name))}"
                    )
                seen.add(name)
                selected.append(tool)
            return tuple(selected)
        return self._select_profile(catalog)

    def _select_profile(
        self, catalog: Sequence[RegisteredTool]
    ) -> tuple[RegisteredTool, ...]:
        """Ordered profile selection intersected with the registered catalog.

        Order is the profile's bundle order (then ``include``), so ``schemas()``
        is deterministic. A declared-but-unregistered bundle tool is simply
        absent, not an error; an explicit ``tool_names`` request for one *is*.

        A registered tool that declares a bundle (an external, hot-loaded tool)
        joins that bundle's ordered list after the built-in names, sorted by
        ``(casefold name, name)``. This is how a manifest's external tools become
        selectable through the same profile machinery without the manager
        importing ``nexus.ext``.
        """
        profile_bundles = self._profile.bundles
        by_bundle: dict[str, list[str]] = {}
        available: set[str] = set()
        for tool in catalog:
            available.add(tool.name)
            bundle = getattr(tool.spec, "bundle", None)
            if isinstance(bundle, str):
                by_bundle.setdefault(bundle, []).append(tool.name)
        order: list[str] = []
        for bundle_name in profile_bundles:
            extra = sorted(
                (
                    name
                    for name in by_bundle.get(bundle_name, ())
                    if name not in get_bundle(bundle_name).tools
                ),
                key=lambda name: (name.casefold(), name),
            )
            for name in (*get_bundle(bundle_name).tools, *extra):
                if name in available and name not in order:
                    order.append(name)
        for name in self._profile.include:
            if name in available and name not in order:
                order.append(name)
        excluded = set(self._profile.exclude)
        selected: list[RegisteredTool] = []
        for name in order:
            if name in excluded:
                continue
            tool = self._by_name[name]
            # A read-only profile (for example ``research``) never enables a
            # mutating tool, including dynamically named MCP tools that no
            # static ``exclude`` list can name.
            if self._profile.read_only and getattr(tool.spec, "mutates", False):
                continue
            selected.append(tool)
        return tuple(selected)

    # -- introspection -----------------------------------------------------

    @property
    def profile(self) -> str:
        return self._profile_name

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def path_guard(self) -> PathGuard:
        return self._path_guard

    @property
    def job_registry(self) -> Any:
        return self._job_registry

    @property
    def todo_store(self) -> Any:
        return self._todo_store

    @property
    def max_parallel(self) -> int:
        return self._max_parallel

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def tools(self) -> tuple[RegisteredTool, ...]:
        return self._tools

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(tool.spec for tool in self._tools)

    def schemas(self) -> tuple[ToolSchema, ...]:
        """Model-facing schemas in profile order (deterministic)."""
        return tuple(tool.to_schema() for tool in self._tools)

    def as_mapping(self) -> Mapping[str, RegisteredTool]:
        return MappingProxyType({tool.name: tool for tool in self._tools})

    def get(self, name: object) -> RegisteredTool | None:
        if not isinstance(name, str):
            return None
        return self._selected.get(name)

    def require(self, name: str) -> RegisteredTool:
        tool = self.get(name)
        if tool is None:
            raise ToolSelectionError(
                f"unknown tool {name!r}; available: {', '.join(self.names)}"
            )
        return tool

    def __contains__(self, name: object) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self):
        return iter(self._tools)

    # -- preparation -------------------------------------------------------

    def prepare(self, calls: Sequence[ToolCall | Any]) -> PreparedBatch:
        """Validate and canonicalize a batch; execute nothing and prompt nothing.

        Unknown tools and schema-invalid inputs become ordered error results.
        Filesystem calls have their ``path`` rewritten to the canonical absolute
        path resolved through the manager's :class:`PathGuard`, so the permission
        engine matches deny/allow rules against a canonical key. The tool still
        re-checks the path immediately before touching the filesystem.

        Duplicate call ids are rejected deterministically here — before any
        permission decision or dispatch. Because two entries with one id cannot
        be told apart when results are matched, the whole duplicated-id group is
        refused: each offending entry becomes an ordered, model-visible error and
        none of the ambiguous calls can execute.
        """
        # The execution-time re-check uses the same guard preparation used, so a
        # caller-supplied guard would be a lie; there is deliberately no seam.
        guard = self._path_guard
        normalized = [self._coerce_call(call) for call in calls]
        counts: dict[str, int] = {}
        for call in normalized:
            counts[call.id] = counts.get(call.id, 0) + 1
        duplicates = {call_id for call_id, count in counts.items() if count > 1}
        entries: list[PreparedCall] = []
        for call in normalized:
            if call.id in duplicates:
                # Defense in depth: the loop normalizes ids before persisting,
                # but a directly-constructed batch with duplicate ids is still
                # refused as a whole group.
                entries.append(self._duplicate_entry(call))
            elif DUPLICATE_TOOL_CALL_KEY in call.input:
                # The loop re-identified a later duplicate: it carries a unique
                # synthetic id and a marker naming the id it duplicated. Reject
                # it without consulting the schema so it can never execute.
                entries.append(self._marked_duplicate_entry(call))
            else:
                entries.append(self._prepare_one(call, guard))
        return PreparedBatch(tuple(entries))

    def preview(self, calls: Sequence[ToolCall | Any]) -> tuple[ToolPreview, ...]:
        """Canonicalize each call's ``(bundle, key)`` without executing anything.

        This is the seam a ``PreToolUse``/``PostToolUse`` hook uses to match on
        the canonical permission key (an absolute path for fs/path-mode tools)
        and bundle. It reuses :meth:`_prepare_one`, so the key it reports is
        exactly the key the permission engine plans against; an unknown tool or
        an invalid input reports ``key=None`` plus a sanitized ``error``.
        """
        previews: list[ToolPreview] = []
        for call in calls:
            normalized = self._coerce_call(call)
            prepared = self._prepare_one(normalized, self._path_guard)
            spec = prepared.spec
            bundle = getattr(spec, "bundle", None)
            previews.append(
                ToolPreview(
                    name=normalized.name,
                    bundle=bundle if isinstance(bundle, str) else None,
                    key=prepared.key,
                    error=(
                        _first_text(prepared.error)
                        if prepared.error is not None
                        else None
                    ),
                )
            )
        return tuple(previews)

    @staticmethod
    def _duplicate_entry(call: ToolCall) -> PreparedCall:
        return PreparedCall(
            call=call,
            spec=None,
            code="duplicate_tool_call_id",
            error=ToolExecutionResult.text(
                f"{call.name}: duplicate tool-call id {call.id!r}; the whole "
                "duplicated-id group was rejected and none was executed",
                is_error=True,
            ),
        )

    @staticmethod
    def _marked_duplicate_entry(call: ToolCall) -> PreparedCall:
        original = call.input.get(DUPLICATE_TOOL_CALL_KEY)
        return PreparedCall(
            call=call,
            spec=None,
            code="duplicate_tool_call_id",
            error=ToolExecutionResult.text(
                f"{call.name}: duplicate tool-call id {str(original)!r}; the "
                "duplicated call was not executed",
                is_error=True,
            ),
        )

    def _prepare_one(self, call: ToolCall | Any, guard: PathGuard) -> PreparedCall:
        normalized = self._coerce_call(call)
        tool = self.get(normalized.name)
        if tool is None:
            available = ", ".join(self.names) or "(none)"
            return PreparedCall(
                call=normalized,
                spec=None,
                code="unknown_tool",
                error=ToolExecutionResult.text(
                    f"Unknown tool {normalized.name!r}; available tools: {available}",
                    is_error=True,
                ),
            )
        spec = tool.spec
        try:
            validate_tool_input(spec, normalized.input)
        except ToolInputError as exc:
            return PreparedCall(
                call=normalized,
                spec=spec,
                code="schema_invalid",
                error=ToolExecutionResult.text(
                    f"Invalid input for {normalized.name}: {exc}",
                    is_error=True,
                ),
            )
        try:
            raw_targets = spec.resolve_multi_path_targets(normalized.input)
        except Exception as exc:  # noqa: BLE001 - resolver failures fail closed
            return PreparedCall(
                call=normalized,
                spec=spec,
                code="multi_path_target_error",
                error=ToolExecutionResult.text(
                    f"{normalized.name}: {exc}", is_error=True
                ),
            )
        if spec.multi_path_targets is not None and not raw_targets:
            return PreparedCall(
                call=normalized,
                spec=spec,
                code="multi_path_target_error",
                error=ToolExecutionResult.text(
                    f"{normalized.name}: multi-path resolver returned no targets",
                    is_error=True,
                ),
            )
        if raw_targets:
            targets: list[PathTarget] = []
            try:
                for target in raw_targets:
                    resolved = guard.resolve(target.path, for_write=True)
                    targets.append(
                        PathTarget(
                            target.role,
                            str(resolved.absolute),
                            raw_path=target.path,
                        )
                    )
            except PathSecurityError as exc:
                return PreparedCall(
                    call=normalized,
                    spec=spec,
                    code=exc.code,
                    error=ToolExecutionResult.text(
                        f"{normalized.name}: {exc}", is_error=True
                    ),
                )
            canonical_targets = tuple(targets)
            try:
                key = spec.resolve_permission_key(normalized.input)
            except ToolSpecError as exc:
                return PreparedCall(
                    call=normalized,
                    spec=spec,
                    path_targets=canonical_targets,
                    code="permission_key_error",
                    error=ToolExecutionResult.text(
                        f"{normalized.name}: {exc}", is_error=True
                    ),
                )
            return PreparedCall(
                call=normalized,
                spec=spec,
                key=key,
                path_targets=canonical_targets,
            )
        if permission_bundle_matches("fs", spec.bundle) and "path" in normalized.input:
            return self._prepare_fs(normalized, spec, guard)
        if spec.path_mode:
            return self._prepare_path_mode(normalized, spec, guard)
        try:
            key = spec.resolve_permission_key(normalized.input)
        except ToolSpecError as exc:
            return PreparedCall(
                call=normalized,
                spec=spec,
                code="permission_key_error",
                error=ToolExecutionResult.text(
                    f"{normalized.name}: {exc}", is_error=True
                ),
            )
        return PreparedCall(call=normalized, spec=spec, key=key)

    def _prepare_path_mode(
        self, call: ToolCall, spec: ToolSpec, guard: PathGuard
    ) -> PreparedCall:
        """Canonicalize an explicit path-mode key into an absolute permission key.

        The declared ``permission_key`` yields a workspace-relative path (for
        example ``.nexus/tools/x.py``). It is resolved through the manager's
        :class:`PathGuard`, which enforces the write-root/read-deny boundary and
        fails closed on a symlink or ``..`` escape. The spec handed to the
        permission engine is rebuilt with a constant key function returning that
        canonical absolute path, so rules and persisted grants match the same
        form the ``fs`` bundle uses. The call's input is left untouched: the tool
        still receives its own ``filename``/``path`` field.
        """
        try:
            raw_key = spec.resolve_permission_key(call.input)
        except ToolSpecError as exc:
            return PreparedCall(
                call=call,
                spec=spec,
                code="permission_key_error",
                error=ToolExecutionResult.text(f"{call.name}: {exc}", is_error=True),
            )
        if not raw_key:
            return PreparedCall(
                call=call,
                spec=spec,
                code="permission_key_error",
                error=ToolExecutionResult.text(
                    f"{call.name}: permission key is empty", is_error=True
                ),
            )
        try:
            resolved = guard.resolve(raw_key, for_write=spec.mutates)
        except PathSecurityError as exc:
            return PreparedCall(
                call=call,
                spec=spec,
                code=exc.code,
                error=ToolExecutionResult.text(f"{call.name}: {exc}", is_error=True),
            )
        canonical = str(resolved.absolute)
        bound = msgspec.structs.replace(
            spec, permission_key=_constant_permission_key(canonical)
        )
        return PreparedCall(call=call, spec=bound, key=canonical)

    def _prepare_fs(
        self, call: ToolCall, spec: ToolSpec, guard: PathGuard
    ) -> PreparedCall:
        raw = call.input.get("path")
        if raw is None:
            raw = "."  # directory tools treat an omitted path as the workspace
        try:
            resolved = guard.resolve(raw, for_write=spec.mutates)
        except PathSecurityError as exc:
            return PreparedCall(
                call=call,
                spec=spec,
                code=exc.code,
                error=ToolExecutionResult.text(
                    f"{call.name}: {exc}", is_error=True
                ),
            )
        canonical_input = dict(call.input)
        canonical_input["path"] = str(resolved.absolute)
        canonical = ToolCall(id=call.id, name=call.name, input=canonical_input)
        try:
            key = spec.resolve_permission_key(canonical_input)
        except ToolSpecError as exc:
            return PreparedCall(
                call=canonical,
                spec=spec,
                code="permission_key_error",
                error=ToolExecutionResult.text(f"{call.name}: {exc}", is_error=True),
            )
        return PreparedCall(call=canonical, spec=spec, key=key)

    def _coerce_call(self, call: ToolCall | Any) -> ToolCall:
        if isinstance(call, ToolCall):
            canonical = canonical_tool_name(call.name)
            if canonical not in self._selected:
                canonical = call.name
            return call if canonical == call.name else ToolCall(
                id=call.id, name=canonical, input=dict(call.input)
            )
        from_tool_use = getattr(call, "from_tool_use", None)
        if from_tool_use is not None:
            parsed = ToolCall.from_tool_use(call)
            canonical = canonical_tool_name(parsed.name)
            if canonical not in self._selected:
                canonical = parsed.name
            return parsed if canonical == parsed.name else ToolCall(
                id=parsed.id, name=canonical, input=dict(parsed.input)
            )
        raise ToolManagerError("calls must be ToolCall or ToolUse instances")

    # -- dispatch ----------------------------------------------------------

    async def dispatch(
        self,
        prepared: PreparedBatch,
        ctx_factory: Callable[[ToolCall, ToolSpec], ToolContext] | None = None,
        *,
        parallel_allowed: bool = True,
        max_parallel: int | None = None,
        emit: Callable[[Event], object] | None = None,
        cancel: CancelTokenView | None = None,
    ) -> tuple[ToolExecutionResult, ...]:
        """Execute a fully-gated batch, preserving original result order.

        ``parallel_allowed=False`` (a provider without parallel tool calls)
        serializes everything. Read-only calls run up to ``max_parallel`` at
        once; ``exclusive`` and mutating tools run alone in declaration order.
        Undecided or denied calls never start. ``emit`` receives
        ``tool.started``/``tool.progress``/``tool.completed``/``tool.failed``
        :class:`Event` objects.
        """
        if not isinstance(prepared, PreparedBatch):
            raise ToolManagerError("dispatch expects a PreparedBatch")
        entries = prepared.entries
        results: list[ToolExecutionResult | None] = [None] * len(entries)
        limit = self._max_parallel if max_parallel is None else max_parallel
        if not parallel_allowed:
            limit = 1
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ToolManagerError("max_parallel must be a positive integer")

        runnable: list[int] = []
        for index, entry in enumerate(entries):
            if entry.error is not None:
                results[index] = entry.error
                await self._emit(
                    emit,
                    "tool.failed",
                    {
                        "call_id": entry.call.id,
                        "tool": entry.call.name,
                        "code": entry.code or "invalid",
                        "error": _first_text(entry.error),
                        "executed": False,
                    },
                )
                continue
            if entry.path_targets or (
                entry.spec is not None
                and entry.spec.multi_path_targets is not None
            ):
                authorized = self._consume_multi_target_authorization(entry)
                target_recheck = (
                    self._recheck_path_targets(entry) if entry.path_targets else None
                )
                if target_recheck is not None:
                    code, target_error = target_recheck
                    results[index] = target_error
                    await self._emit(
                        emit,
                        "tool.failed",
                        {
                            "call_id": entry.call.id,
                            "tool": entry.call.name,
                            "code": code,
                            "error": _first_text(target_error),
                            "executed": False,
                        },
                    )
                    continue
                if not authorized:
                    result = ToolExecutionResult.text(
                        f"{entry.call.name}: no valid multi-target approval "
                        "evidence was provided; call was refused",
                        is_error=True,
                    )
                    results[index] = result
                    await self._emit(
                        emit,
                        "tool.failed",
                        {
                            "call_id": entry.call.id,
                            "tool": entry.call.name,
                            "code": "multi_target_unauthorized",
                            "error": _first_text(result),
                            "executed": False,
                        },
                    )
                    continue
            if entry.spec is None:
                results[index] = ToolExecutionResult.text(
                    f"Unknown tool {entry.call.name!r}", is_error=True
                )
                await self._emit(
                    emit,
                    "tool.failed",
                    {
                        "call_id": entry.call.id,
                        "tool": entry.call.name,
                        "code": "unknown_tool",
                        "executed": False,
                    },
                )
                continue
            if entry.decision is None or entry.decision.denies:
                code = "ungated" if entry.decision is None else "permission_denied"
                message = (
                    "no permission decision was provided"
                    if entry.decision is None
                    else "denied by permission decision"
                )
                results[index] = ToolExecutionResult.text(
                    f"{entry.call.name}: {message}", is_error=True
                )
                await self._emit(
                    emit,
                    "tool.failed",
                    {
                        "call_id": entry.call.id,
                        "tool": entry.call.name,
                        "code": code,
                        "executed": False,
                    },
                )
                continue
            runnable.append(index)

        pending: list[int] = []

        async def flush() -> None:
            if pending:
                batch = list(pending)
                pending.clear()
                await self._run_group(
                    batch, entries, results, ctx_factory, emit, cancel
                )

        for index in runnable:
            entry = entries[index]
            if self._is_exclusive(entry):
                await flush()
                await self._run_group(
                    [index], entries, results, ctx_factory, emit, cancel
                )
            else:
                pending.append(index)
                if len(pending) >= limit:
                    await flush()
        await flush()

        return tuple(
            result
            if result is not None
            else ToolExecutionResult.text("tool did not run", is_error=True)
            for result in results
        )

    @staticmethod
    def _is_exclusive(entry: PreparedCall) -> bool:
        assert entry.spec is not None
        return entry.spec.mutates or entry.spec.concurrency == "exclusive"

    def _recheck_path_targets(
        self, entry: PreparedCall
    ) -> tuple[str, ToolExecutionResult] | None:
        """Recheck every prepared multi-path target immediately before dispatch."""
        first_error: tuple[str, ToolExecutionResult] | None = None
        for target in entry.path_targets:
            try:
                raw_path = target.raw_path or target.path
                resolved = self._path_guard.recheck(raw_path, for_write=True)
            except PathSecurityError as exc:
                candidate = (
                    exc.code,
                    ToolExecutionResult.text(f"{entry.call.name}: {exc}", is_error=True),
                )
            else:
                candidate = (
                    "path_changed",
                    ToolExecutionResult.text(
                        f"{entry.call.name}: {target.role} path changed after "
                        "preparation; the call was refused",
                        is_error=True,
                    ),
                ) if resolved.key != target.path else None
            if first_error is None and candidate is not None:
                first_error = candidate
        return first_error

    def _authorize_multi_target(
        self,
        prepared: PreparedBatch,
        evaluation: Any,
        decision: Decision,
        *,
        authority: object,
        scope: object | None = None,
    ) -> PreparedBatch:
        """Record runtime-gate evidence for one fully evaluated multi-path call.

        The returned batch only carries an opaque handle. Authorization is
        verified against this manager's private table at dispatch, so callers
        cannot manufacture approval by setting a scalar decision.
        """
        if authority is not self._multi_target_authority:
            return prepared
        if not isinstance(prepared, PreparedBatch):
            raise ToolManagerError("multi-target authorization needs a PreparedBatch")
        decision = Decision.from_value(decision)
        if not decision.allows or getattr(evaluation.outcome, "value", None) not in {
            "allow",
            "ask",
        }:
            return prepared
        call = evaluation.call
        entry = next(
            (
                item
                for item in prepared.entries
                if item.call.id == call.id and item.call.name == call.name
            ),
            None,
        )
        if (
            entry is None
            or entry.error is not None
            or entry.spec is None
            or entry.spec.multi_path_targets is None
            or self._selected.get(entry.call.name) is None
            or self._selected[entry.call.name].spec is not entry.spec
            or not entry.path_targets
        ):
            return prepared
        target_evaluations = tuple(getattr(evaluation, "target_evaluations", ()))
        targets = tuple((item.role, item.path) for item in entry.path_targets)
        evaluated = tuple(
            (item.role, item.key) for item in target_evaluations
        )
        if (
            not target_evaluations
            or evaluated != targets
            or any(
                getattr(item.outcome, "value", None) not in {"allow", "ask"}
                for item in target_evaluations
            )
        ):
            return prepared
        call_digest = _tool_call_digest(call)
        binding = (
            call.id,
            call.name,
            call_digest,
            id(entry.spec),
            targets,
            getattr(evaluation.outcome, "value", None),
            evaluation.code,
            tuple(
                (
                    item.role,
                    item.key,
                    getattr(item.outcome, "value", None),
                    item.code,
                )
                for item in target_evaluations
            ),
            decision.value,
        )
        token = object()
        self._multi_target_authorizations[token] = (scope, *binding)
        return PreparedBatch(
            tuple(
                dataclasses.replace(item, _multi_target_authorization=token)
                if item is entry
                else item
                for item in prepared.entries
            )
        )

    def _revoke_multi_target_authorizations(self, scope: object) -> None:
        """Revoke outstanding runtime evidence minted by one permission gate."""
        for token, binding in tuple(self._multi_target_authorizations.items()):
            if binding[0] is scope:
                self._multi_target_authorizations.pop(token, None)

    def _consume_multi_target_authorization(self, entry: PreparedCall) -> bool:
        token = entry._multi_target_authorization
        if token is None:
            return False
        binding = self._multi_target_authorizations.pop(token, None)
        if binding is None:
            return False
        _, *binding = binding
        binding = tuple(binding)
        # Evidence is single-use even if validation fails.
        if entry.spec is None or self._selected.get(entry.call.name) is None:
            return False
        if self._selected[entry.call.name].spec is not entry.spec:
            return False
        expected_targets = tuple((item.role, item.path) for item in entry.path_targets)
        expected = (
            entry.call.id,
            entry.call.name,
            _tool_call_digest(entry.call),
            id(entry.spec),
            expected_targets,
            *binding[5:],
        )
        return (
            entry.decision is not None
            and entry.decision.allows
            and entry.decision.value == binding[8]
            and binding == expected
        )

    def _recheck_fs_entry(
        self, entry: PreparedCall
    ) -> tuple[str, ToolExecutionResult] | None:
        """Execution-time fs authorization check, or ``None`` when unchanged.

        Only calls whose canonical permission key is an absolute path are
        re-checked: that is exactly the set whose key was produced by
        ``_prepare_fs`` or ``_prepare_path_mode`` (directory tools without a
        path keep a relative ``.`` key and have no target to swap). A key that
        is no longer canonical — a symlink retarget, a swapped parent, or a
        now-forbidden boundary — is refused fail-closed.
        """
        spec = entry.spec
        if spec is None or entry.key is None:
            return None
        if not permission_bundle_matches("fs", spec.bundle) and not spec.path_mode:
            return None
        if not os.path.isabs(entry.key):
            return None
        try:
            resolved = self._path_guard.recheck(entry.key, for_write=spec.mutates)
        except PathSecurityError as exc:
            return exc.code, ToolExecutionResult.text(
                f"{entry.call.name}: {exc}", is_error=True
            )
        if resolved.key != entry.key:
            return "path_changed", ToolExecutionResult.text(
                f"{entry.call.name}: path changed after permission was granted "
                f"(planned {entry.key!r}, now {resolved.key!r}); the call was "
                "refused and nothing was written",
                is_error=True,
            )
        return None

    async def _run_group(
        self,
        indices: Sequence[int],
        entries: Sequence[PreparedCall],
        results: list[ToolExecutionResult | None],
        ctx_factory: Callable[[ToolCall, ToolSpec], ToolContext] | None,
        emit: Callable[[Event], object] | None,
        cancel: CancelTokenView | None,
    ) -> None:
        tasks = [
            asyncio.ensure_future(
                self._execute(entries[index], ctx_factory, emit, cancel)
            )
            for index in indices
        ]
        try:
            group = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for index, result in zip(indices, group):
            results[index] = result

    async def _execute(
        self,
        entry: PreparedCall,
        ctx_factory: Callable[[ToolCall, ToolSpec], ToolContext] | None,
        emit: Callable[[Event], object] | None,
        cancel: CancelTokenView | None,
    ) -> ToolExecutionResult:
        assert entry.spec is not None
        spec = entry.spec
        tool = self._selected[spec.name]
        call = entry.call

        # Execution-time authorization: re-canonicalize the fs path through the
        # manager's PathGuard immediately before the tool runs and compare it to
        # the canonical key the permission engine planned against. A symlink or
        # path swap between planning and execution changes the key, so the call
        # is refused without touching the target. The tool's own recheck remains
        # as defense in depth.
        recheck = self._recheck_fs_entry(entry)
        if recheck is not None:
            code, guard_error = recheck
            await self._emit(
                emit,
                "tool.failed",
                {
                    "call_id": call.id,
                    "tool": spec.name,
                    "code": code,
                    "error": _first_text(guard_error),
                    "executed": False,
                },
            )
            return guard_error

        await self._emit(
            emit,
            "tool.started",
            {"call_id": call.id, "tool": spec.name, "bundle": spec.bundle},
        )
        ctx = self._make_context(ctx_factory, call, spec, emit, cancel)
        if cancel is not None:
            cancel.raise_if_cancelled()

        timeout = self._timeout_for(spec)
        started = time.monotonic()
        failed = False
        try:
            coro = tool.run(dict(call.input), ctx)
            if timeout is not None:
                result = await asyncio.wait_for(coro, timeout)
            else:
                result = await coro
        except (OperationCancelled, asyncio.CancelledError):
            raise
        except TimeoutError:
            failed = True
            result = ToolExecutionResult.text(
                f"{spec.name}: timed out after {timeout:g}s; the call was stopped",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 - tool failures are model-visible
            failed = True
            result = ToolExecutionResult.text(
                f"{spec.name} failed: {type(exc).__name__}: {_safe_message(exc)}",
                is_error=True,
            )
        if not isinstance(result, ToolExecutionResult):
            failed = True
            result = ToolExecutionResult.text(
                f"{spec.name}: tool returned an invalid result", is_error=True
            )
        result = self.cap_result(entry, result)
        duration_ms = round((time.monotonic() - started) * 1000, 3)
        if failed:
            await self._emit(
                emit,
                "tool.failed",
                {
                    "call_id": call.id,
                    "tool": spec.name,
                    "error": _first_text(result),
                    "duration_ms": duration_ms,
                    "executed": True,
                },
            )
        else:
            await self._emit(
                emit,
                "tool.completed",
                {
                    "call_id": call.id,
                    "tool": spec.name,
                    "is_error": result.is_error,
                    "duration_ms": duration_ms,
                    "executed": True,
                },
            )
        return result

    def _make_context(
        self,
        ctx_factory: Callable[[ToolCall, ToolSpec], ToolContext] | None,
        call: ToolCall,
        spec: ToolSpec,
        emit: Callable[[Event], object] | None,
        cancel: CancelTokenView | None,
    ) -> ToolContext:
        if ctx_factory is not None:
            base = ctx_factory(call, spec)
            if not isinstance(base, ToolContext):
                raise ToolManagerError("ctx_factory must return a ToolContext")
        else:
            base = ToolContext(
                workspace=self._workspace,
                session_id="",
                turn_id="",
                config=self._config,
            )
        return dataclasses.replace(
            base,
            call_id=call.id,
            cancel_token=cancel if cancel is not None else base.cancel_token,
            emit=self._ctx_emitter(emit, call, spec),
            job_registry=self._job_registry,
            todo_store=self._todo_store,
        )

    def _ctx_emitter(
        self,
        emit: Callable[[Event], object] | None,
        call: ToolCall,
        spec: ToolSpec,
    ) -> Callable[[str, dict[str, Any] | None], Any]:
        async def _report(event_type: str, data: dict[str, Any] | None = None) -> None:
            payload = {
                "call_id": call.id,
                "tool": spec.name,
                "bundle": spec.bundle,
                **dict(data or {}),
            }
            await self._emit(emit, event_type, payload)

        return _report

    @staticmethod
    async def _emit(
        emit: Callable[[Event], object] | None,
        event_type: str,
        data: Mapping[str, Any],
    ) -> None:
        if emit is None:
            return
        event = Event(type=event_type, data=dict(data))
        outcome = emit(event)
        if inspect.isawaitable(outcome):
            await outcome

    # -- result capping ----------------------------------------------------

    def _timeout_for(self, spec: ToolSpec) -> float | None:
        if spec.timeout_s is not None:
            return spec.timeout_s
        if spec.name == "bash":
            return self._bash_timeout_s + BASH_TIMEOUT_GRACE_S
        return self._default_timeout_s

    def cap_result(
        self, entry: PreparedCall, result: ToolExecutionResult
    ) -> ToolExecutionResult:
        """Cap content/display/metrics before persistence, deterministically."""
        spec = entry.spec
        display = self._cap_display(result.display)
        metrics = self._safe_metrics(result.metrics)
        if spec is None:
            return msgspec.structs.replace(
                result, display=display, metrics=metrics
            )

        budget_tokens = min(spec.max_result_tokens, self._max_result_tokens)
        char_budget = max(1, budget_tokens * CHARS_PER_TOKEN)
        text_blocks = [b for b in result.content if isinstance(b, Text)]
        image_count = sum(1 for b in result.content if isinstance(b, Image))
        total_chars = sum(len(b.text) for b in text_blocks)
        estimated = total_chars // CHARS_PER_TOKEN + image_count * IMAGE_TOKEN_ESTIMATE
        if estimated <= budget_tokens:
            return msgspec.structs.replace(
                result, display=display, metrics=metrics
            )

        remaining = char_budget
        shown_chars = 0
        new_content: list[Any] = []
        for block in result.content:
            if isinstance(block, Text):
                if remaining <= 0:
                    continue
                text = block.text
                if len(text) > remaining:
                    text = _truncate_lines(text, remaining)
                new_content.append(Text(text=text))
                shown_chars += len(text)
                remaining -= len(text)
            elif isinstance(block, Image):
                if remaining >= IMAGE_TOKEN_ESTIMATE * CHARS_PER_TOKEN:
                    new_content.append(block)
                    remaining -= IMAGE_TOKEN_ESTIMATE * CHARS_PER_TOKEN
                else:
                    new_content.append(
                        Text(text="[image omitted: result exceeded the token budget]")
                    )
            else:
                new_content.append(block)

        marker = (
            f"[{spec.name}: result truncated, showing {shown_chars} of "
            f"{total_chars} chars; re-run with a narrower query or smaller range]"
        )
        new_content.append(Text(text=marker))
        context_note = result.context_note or marker
        merged_metrics = dict(metrics or {})
        merged_metrics.update(
            {"truncated": True, "shown_chars": shown_chars, "total_chars": total_chars}
        )
        return ToolExecutionResult(
            content=new_content,
            is_error=result.is_error,
            display=display,
            metrics=self._safe_metrics(merged_metrics),
            context_note=context_note,
            diff=result.diff,
        )

    @staticmethod
    def _cap_display(display: str | None) -> str | None:
        if display is None:
            return None
        if len(display) <= MAX_DISPLAY_CHARS:
            return display
        return display[:MAX_DISPLAY_CHARS] + "… [display truncated]"

    @staticmethod
    def _safe_metrics(metrics: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if metrics is None:
            return None
        safe: dict[str, Any] = {}
        for key, value in list(metrics.items())[:32]:
            name = str(key)
            if value is None or isinstance(value, (bool, int)):
                safe[name] = value
            elif isinstance(value, float):
                safe[name] = value if math.isfinite(value) else str(value)
            elif isinstance(value, str):
                safe[name] = (
                    value
                    if len(value) <= MAX_METRIC_CHARS
                    else value[:MAX_METRIC_CHARS] + "…"
                )
            elif isinstance(value, (list, tuple)) and all(
                isinstance(item, (bool, int, float, str)) for item in value
            ):
                safe[name] = list(value)[:64]
            else:
                safe[name] = f"<{type(value).__name__}>"
        return safe

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        """Terminate owned jobs and release resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._owns_job_registry:
            aclose = getattr(self._job_registry, "aclose", None)
            if aclose is not None:
                await aclose()
