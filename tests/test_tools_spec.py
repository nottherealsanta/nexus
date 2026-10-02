"""Contract tests for :mod:`nexus.tools.spec` (plan section 3.4)."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import msgspec
import pytest

from nexus.config import Config
from nexus.model.message import Text, ToolUse
from nexus.model.request import ToolSchema
from nexus.tools.spec import (
    RegisteredTool,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
    ToolSpecError,
    validate_input_schema,
    validate_tool_name,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

OBJECT_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


def make_spec(**overrides) -> ToolSpec:
    payload = {
        "name": "Read",
        "description": "Read a file",
        "input_schema": dict(OBJECT_SCHEMA),
        "bundle": "fs",
    }
    payload.update(overrides)
    return ToolSpec(**payload)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Read", "Bash", "mcp__server__tool", "A1_b", "x" * 64])
def test_valid_tool_names(name):
    assert validate_tool_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "_leading", "1leading", "has-dash", "has space", "sla/sh", "x" * 65, None, 5],
)
def test_invalid_tool_names(name):
    with pytest.raises(ToolSpecError):
        validate_tool_name(name)


# ---------------------------------------------------------------------------
# ToolSpec validation
# ---------------------------------------------------------------------------


def test_spec_accepts_full_valid_declaration():
    spec = ToolSpec(
        name="Write",
        description="Write a file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        bundle="fs",
        mutates=True,
        concurrency="exclusive",
        timeout_s=30,
        max_result_tokens=1000,
        permission_key=lambda data: str(data["path"]),
        version="1",
    )
    assert spec.mutates is True
    assert spec.concurrency == "exclusive"
    assert spec.timeout_s == 30
    assert spec.max_result_tokens == 1000
    assert spec.resolve_permission_key({"path": "/tmp/x"}) == "/tmp/x"


def test_spec_is_frozen():
    spec = make_spec()
    with pytest.raises((AttributeError, TypeError)):
        spec.name = "Other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "1bad"},
        {"description": ""},
        {"description": 123},
        {"description": "a\x00b"},
        {"input_schema": {"type": "array"}},
        {"input_schema": {"properties": {}}},
        {"input_schema": "not-a-dict"},
        {"bundle": "nope"},
        {"bundle": ""},
        {"mutates": 1},
        {"concurrency": "sometimes"},
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": float("nan")},
        {"timeout_s": float("inf")},
        {"timeout_s": "10"},
        {"permission_key": "not-callable"},
        {"max_result_tokens": 0},
        {"max_result_tokens": -5},
        {"max_result_tokens": True},
        {"version": ""},
        {"version": 1},
    ],
)
def test_spec_rejects_invalid_harness_fields(overrides):
    with pytest.raises(ToolSpecError):
        make_spec(**overrides)


@pytest.mark.parametrize("bundle", ["fs", "shell", "task"])
def test_spec_accepts_known_bundles(bundle):
    assert make_spec(bundle=bundle).bundle == bundle


# ---------------------------------------------------------------------------
# input_schema subset validation
# ---------------------------------------------------------------------------


def test_object_schema_with_nested_properties_is_valid():
    schema = {
        "type": "object",
        "properties": {
            "options": {
                "type": "object",
                "properties": {"recursive": {"type": "boolean"}},
                "additionalProperties": {"type": "string"},
            },
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "anyOf": [{"type": "object"}, {"type": "object"}],
    }
    assert validate_input_schema(schema) is schema


@pytest.mark.parametrize(
    "schema",
    [
        None,
        [],
        {"type": "string"},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"x": "not-a-schema"}},
        {"type": "object", "required": "path"},
        {"type": "object", "required": [1]},
        {"type": "object", "properties": {"x": {"type": "nonsense"}}},
        {"type": "object", "properties": {"x": {"type": "object", "required": [None]}}},
        {"type": "object", "additionalProperties": "yes"},
        {"type": "object", "items": 5},
    ],
)
def test_invalid_schemas_rejected(schema):
    with pytest.raises(ToolSpecError):
        validate_input_schema(schema)


def test_schema_must_be_json_serializable():
    with pytest.raises(ToolSpecError):
        validate_input_schema({"type": "object", "default": (1, 2)})
    with pytest.raises(ToolSpecError):
        validate_input_schema({"type": "object", "properties": {1: {"type": "string"}}})


def test_schema_to_schema_and_serialization_round_trip():
    spec = make_spec()
    schema = spec.to_schema()
    assert isinstance(schema, ToolSchema)
    encoded = msgspec.json.encode(msgspec.structs.asdict(schema))
    assert json.loads(encoded)["name"] == "Read"
    assert json.loads(encoded)["input_schema"] == OBJECT_SCHEMA


def test_permission_key_resolution_is_strict():
    spec = make_spec(permission_key=lambda data: "")
    with pytest.raises(ToolSpecError):
        spec.resolve_permission_key({})
    spec = make_spec(permission_key=lambda data: "a\x00b")
    with pytest.raises(ToolSpecError):
        spec.resolve_permission_key({})
    assert make_spec().resolve_permission_key({}) is None


# ---------------------------------------------------------------------------
# ToolCall
# ---------------------------------------------------------------------------


def test_tool_call_round_trips_with_ir_tool_use():
    block = ToolUse(id="call-1", name="Read", input={"path": "/tmp/x"})
    call = ToolCall.from_tool_use(block)
    assert call.id == "call-1"
    assert call.input == {"path": "/tmp/x"}
    assert call.to_tool_use() == block


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": "", "name": "Read", "input": {}},
        {"id": "x", "name": "1bad", "input": {}},
        {"id": "x", "name": "Read", "input": []},
    ],
)
def test_tool_call_rejects_invalid(kwargs):
    with pytest.raises(ToolSpecError):
        ToolCall(**kwargs)


# ---------------------------------------------------------------------------
# ToolExecutionResult
# ---------------------------------------------------------------------------


def test_execution_result_text_and_ir_conversion():
    result = ToolExecutionResult.text("hello", is_error=True)
    assert result.is_error is True
    assert result.display == "hello"
    block = result.to_tool_result("call-1")
    assert block.tool_use_id == "call-1"
    assert block.is_error is True
    assert block.content == [Text(text="hello")]


def test_execution_result_serializes():
    result = ToolExecutionResult(
        content=[Text(text="ok")],
        display="ok",
        metrics={"ms": 3},
        context_note="note",
    )
    decoded = json.loads(msgspec.json.encode(msgspec.structs.asdict(result)))
    assert decoded["content"] == [{"type": "text", "text": "ok"}]
    assert decoded["context_note"] == "note"


# ---------------------------------------------------------------------------
# ToolContext
# ---------------------------------------------------------------------------


def test_tool_context_never_exposes_runtime():
    context = ToolContext(
        workspace=Path("/tmp/ws"),
        session_id="s",
        turn_id="t",
        config=Config(),
    )
    assert not hasattr(context, "runtime")
    assert "runtime" not in ToolContext.__dataclass_fields__
    annotation = ToolContext.__annotations__
    assert all("Runtime" not in str(value) for value in annotation.values())


def test_tool_context_is_frozen():
    context = ToolContext(
        workspace=Path("/tmp"), session_id="s", turn_id="t", config=Config()
    )
    with pytest.raises(AttributeError):
        context.session_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RegisteredTool
# ---------------------------------------------------------------------------


async def _noop_run(data, context):  # pragma: no cover - trivial
    return ToolExecutionResult.text("ok")


def test_registered_tool_validates_provenance():
    tool = RegisteredTool(spec=make_spec(), run=_noop_run, origin="builtin", generation=3)
    assert tool.name == "Read"
    assert tool.bundle == "fs"
    assert tool.to_schema().name == "Read"
    with pytest.raises(ToolSpecError):
        RegisteredTool(spec=make_spec(), run=_noop_run, origin="bogus")
    with pytest.raises(ToolSpecError):
        RegisteredTool(spec=make_spec(), run="not-callable", origin="builtin")


# ---------------------------------------------------------------------------
# Layering: tools must not reach up to runtime/ui/cli
# ---------------------------------------------------------------------------

FORBIDDEN = ("nexus.runtime", "nexus.ui", "nexus.cli", "nexus.agent")


def _resolve_from(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    is_init = path.name == "__init__.py"
    package = parts if is_init else parts[:-1]
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(_resolve_from(path, node))
    return modules


@pytest.mark.parametrize(
    "path", sorted((REPO_ROOT / "nexus" / "tools").rglob("*.py")), ids=lambda p: p.name
)
def test_tools_layer_does_not_import_upward(path):
    violations = sorted(m for m in _imports(path) if m.startswith(FORBIDDEN))
    assert not violations, f"{path.name} imports {violations}"


def test_permission_key_callback_failure_becomes_a_tool_error():
    def broken(data):
        raise ValueError("model must be a non-empty reference without whitespace")
    with pytest.raises(ToolSpecError, match="model must"):
        make_spec(permission_key=broken).resolve_permission_key({"model": "bad model"})
