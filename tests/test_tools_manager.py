"""Tests for :mod:`nexus.tools.manager` (plan section 5.3).

Covers catalog/profile selection, schema validation, the prepare/gate/dispatch
protocol, concurrency planning, timeouts, cancellation, exception conversion,
result capping, event emission, and service ownership.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, PermissionsSection, ToolsSection
from nexus.core.cancel import CancelToken
from nexus.errors import OperationCancelled
from nexus.model.message import Text
from nexus.tools.builtin._jobs import JobRegistry
from nexus.tools.builtin.todo import TodoStore
from nexus.tools.bundles import UnknownProfileError
from nexus.tools.manager import (
    MAX_DISPLAY_CHARS,
    DuplicateToolError,
    PreparedCall,
    ToolInputError,
    ToolManager,
    ToolManagerError,
    ToolSelectionError,
    validate_tool_input,
)
from nexus.tools.permissions import Decision, PermissionEngine
from nexus.tools.spec import (
    RegisteredTool,
    ToolCall,
    ToolContext,
    ToolExecutionResult,
    ToolSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EMPTY_SCHEMA: dict = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

ALL_BUILTINS = (
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "Glob",
    "Grep",
    "LS",
    "Bash",
    "BashOutput",
    "KillShell",
    "TodoWrite",
    "ReloadExtensions",
    "ListExtensions",
    "WriteTool",
    "Skill",
)


def cfg(**tools_kw) -> Config:
    return Config(
        v2=ConfigV2(
            permissions=PermissionsSection(mode="allow"),
            tools=ToolsSection(**tools_kw),
        )
    )


def make_spec(
    name: str,
    *,
    bundle: str = "task",
    mutates: bool = False,
    concurrency: str = "parallel",
    timeout_s: float | None = None,
    permission_key=None,
    max_result_tokens: int = 25_000,
    input_schema: dict | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} test tool",
        input_schema=dict(input_schema) if input_schema is not None else dict(EMPTY_SCHEMA),
        bundle=bundle,
        mutates=mutates,
        concurrency=concurrency,
        timeout_s=timeout_s,
        permission_key=permission_key,
        max_result_tokens=max_result_tokens,
    )


async def _noop(args, ctx):
    return ToolExecutionResult.text("ok")


def make_tool(name: str, run, **spec_kw) -> RegisteredTool:
    return RegisteredTool(spec=make_spec(name, **spec_kw), run=run, origin="builtin")


class Probe:
    """Records concurrent entry counts so overlap is observable."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.starts: dict[str, int] = {}
        self.calls: list[str] = []

    def enter(self, name: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.starts.setdefault(name, self.active)
        self.calls.append(name)

    def leave(self) -> None:
        self.active -= 1


def sleeper(probe: Probe, name: str, delay: float = 0.05, text: str | None = None):
    async def run(args, ctx):
        probe.enter(name)
        try:
            await asyncio.sleep(delay)
            return ToolExecutionResult.text(text if text is not None else f"{name}:ok")
        finally:
            probe.leave()

    return run


def ctx_factory(workspace: Path, *, session_id: str = "s1"):
    def make(call, spec_):
        return ToolContext(
            workspace=workspace,
            session_id=session_id,
            turn_id="t1",
            config=cfg(),
        )

    return make


def gate(batch, decision: Decision = Decision.ALLOW_ONCE):
    return batch.with_decisions(
        {entry.call.id: decision for entry in batch.entries if entry.executable}
    )


def manager(workspace: Path, tools, names, **kwargs) -> ToolManager:
    return ToolManager(
        cfg(max_parallel=kwargs.pop("max_parallel", 8)),
        workspace=workspace,
        tools=tools,
        tool_names=names,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Catalog and profile selection
# ---------------------------------------------------------------------------


def test_default_catalog_is_builtins_in_bundle_order(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    assert m.names == ALL_BUILTINS
    assert m.profile == "coding"
    assert all(tool.origin == "builtin" for tool in m.tools)
    assert [s.name for s in m.schemas()] == list(ALL_BUILTINS)
    assert m.schemas() == m.schemas()  # deterministic


def test_research_profile_is_read_only(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path, profile="research")
    assert m.names == ("Read", "Glob", "Grep", "LS")


def test_chat_profile_has_no_tools(tmp_path: Path):
    assert ToolManager(cfg(), workspace=tmp_path, profile="chat").names == ()


def test_ops_profile_is_shell_only(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path, profile="ops")
    assert m.names == ("Bash", "BashOutput", "KillShell")


def test_unknown_profile_fails_closed(tmp_path: Path):
    with pytest.raises(UnknownProfileError):
        ToolManager(cfg(), workspace=tmp_path, profile="does-not-exist")


def test_duplicate_catalog_names_fail(tmp_path: Path):
    spec = make_spec("Dup")
    tools = [
        RegisteredTool(spec=spec, run=_noop, origin="builtin"),
        RegisteredTool(spec=spec, run=_noop, origin="builtin"),
    ]
    with pytest.raises(DuplicateToolError):
        ToolManager(cfg(), workspace=tmp_path, tools=tools)


def test_explicit_unknown_tool_name_fails(tmp_path: Path):
    with pytest.raises(ToolSelectionError):
        ToolManager(cfg(), workspace=tmp_path, tool_names=["Missing"])


def test_duplicate_requested_names_fail(tmp_path: Path):
    with pytest.raises(DuplicateToolError):
        ToolManager(cfg(), workspace=tmp_path, tool_names=["Read", "Read"])


def test_mapping_and_lookup(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    assert "Read" in m
    assert "Nope" not in m
    assert len(m) == len(ALL_BUILTINS)
    assert m.get("Read").name == "Read"
    assert m.get("Nope") is None
    assert m.get(5) is None
    assert m.require("Read").spec.name == "Read"
    with pytest.raises(ToolSelectionError):
        m.require("Nope")
    assert list(m.as_mapping()) == list(ALL_BUILTINS)
    assert [tool.name for tool in m] == list(ALL_BUILTINS)
    assert [spec.name for spec in m.specs] == list(ALL_BUILTINS)


# ---------------------------------------------------------------------------
# JSON Schema validation
# ---------------------------------------------------------------------------

ADVERSARIAL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "s": {"type": "string", "minLength": 2, "maxLength": 4},
        "n": {"type": "integer", "minimum": 1, "maximum": 10},
        "f": {"type": "number"},
        "b": {"type": "boolean"},
        "e": {"type": "string", "enum": ["a", "b"]},
        "arr": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 1,
            "maxItems": 3,
        },
        "obj": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
            "additionalProperties": False,
        },
    },
    "required": ["s"],
    "additionalProperties": False,
}

VALID_INPUT = {
    "s": "ab",
    "n": 1,
    "f": 1.5,
    "b": True,
    "e": "a",
    "arr": [1, 2],
    "obj": {"x": "y"},
}


def _input_spec() -> ToolSpec:
    return make_spec("Validate", input_schema=ADVERSARIAL_SCHEMA)


def test_validate_accepts_valid_input():
    validate_tool_input(_input_spec(), VALID_INPUT)


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing required
        {"s": "a"},  # minLength
        {"s": "abcde"},  # maxLength
        {"s": 5},  # wrong type
        {"s": "ab", "n": 0},  # minimum
        {"s": "ab", "n": 11},  # maximum
        {"s": "ab", "n": True},  # bool is not integer
        {"s": "ab", "n": 1.0},  # float is not integer
        {"s": "ab", "f": float("nan")},  # non-finite number
        {"s": "ab", "f": float("inf")},
        {"s": "ab", "b": 1},  # 1 is not boolean
        {"s": "ab", "e": "c"},  # enum
        {"s": "ab", "e": True},  # bool must not match string enum
        {"s": "ab", "arr": []},  # minItems
        {"s": "ab", "arr": [1, 2, 3, 4]},  # maxItems
        {"s": "ab", "arr": ["x"]},  # items type
        {"s": "ab", "obj": {}},  # nested required
        {"s": "ab", "obj": {"x": "y", "z": 1}},  # nested additionalProperties
        {"s": "ab", "extra": 1},  # top-level additionalProperties
    ],
)
def test_validate_rejects_adversarial_input(payload):
    with pytest.raises(ToolInputError):
        validate_tool_input(_input_spec(), payload)


def test_validate_input_error_is_actionable():
    with pytest.raises(ToolInputError) as info:
        validate_tool_input(_input_spec(), {"s": "ab", "n": 0, "extra": 1})
    message = str(info.value)
    assert "input.n" in message
    assert ">= 1" in message
    assert "unexpected property 'extra'" in message


def test_validate_input_rejects_non_object():
    with pytest.raises(ToolInputError):
        validate_tool_input(_input_spec(), [1, 2])


# ---------------------------------------------------------------------------
# prepare(): unknown / schema errors never execute
# ---------------------------------------------------------------------------


async def test_unknown_and_invalid_calls_never_execute(tmp_path: Path):
    ran: list[str] = []

    async def run(args, ctx):
        ran.append("Echo")
        return ToolExecutionResult.text("ran")

    m = manager(tmp_path, [make_tool("Echo", run)], ["Echo"])
    batch = m.prepare(
        [
            ToolCall(id="c1", name="Nope", input={}),
            ToolCall(id="c2", name="Echo", input={"bad": 1}),
        ]
    )
    assert batch.for_call("c1").code == "unknown_tool"
    assert batch.for_call("c2").code == "schema_invalid"
    results = await m.dispatch(batch, ctx_factory(tmp_path))
    assert ran == []
    assert all(result.is_error for result in results)
    assert "Unknown tool" in results[0].content[0].text
    assert "unexpected property 'bad'" in results[1].content[0].text


def test_unknown_tool_error_lists_available_names(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    entry = m.prepare([ToolCall(id="c", name="Nope", input={})]).for_call("c")
    assert entry.error is not None
    assert "Read" in entry.error.content[0].text


# ---------------------------------------------------------------------------
# Ordering and concurrency
# ---------------------------------------------------------------------------


async def test_results_preserve_declaration_order(tmp_path: Path):
    probe = Probe()
    tools = [
        make_tool("T0", sleeper(probe, "T0", delay=0.2)),
        make_tool("T1", sleeper(probe, "T1", delay=0.1)),
        make_tool("T2", sleeper(probe, "T2", delay=0.0)),
    ]
    m = manager(tmp_path, tools, ["T0", "T1", "T2"], max_parallel=3)
    calls = [ToolCall(id=f"c{i}", name=f"T{i}", input={}) for i in range(3)]
    results = await m.dispatch(gate(m.prepare(calls)), ctx_factory(tmp_path))
    assert [r.content[0].text for r in results] == ["T0:ok", "T1:ok", "T2:ok"]


async def test_read_only_calls_parallelize(tmp_path: Path):
    probe = Probe()
    tools = [make_tool(f"R{i}", sleeper(probe, f"R{i}", delay=0.1)) for i in range(4)]
    m = manager(tmp_path, tools, [f"R{i}" for i in range(4)], max_parallel=4)
    calls = [ToolCall(id=f"c{i}", name=f"R{i}", input={}) for i in range(4)]
    await m.dispatch(gate(m.prepare(calls)), ctx_factory(tmp_path))
    assert probe.max_active == 4


async def test_max_parallel_bounds_concurrency(tmp_path: Path):
    probe = Probe()
    tools = [make_tool(f"R{i}", sleeper(probe, f"R{i}", delay=0.1)) for i in range(4)]
    m = manager(tmp_path, tools, [f"R{i}" for i in range(4)], max_parallel=2)
    calls = [ToolCall(id=f"c{i}", name=f"R{i}", input={}) for i in range(4)]
    await m.dispatch(gate(m.prepare(calls)), ctx_factory(tmp_path))
    assert probe.max_active == 2


async def test_exclusive_tools_run_alone(tmp_path: Path):
    probe = Probe()
    tools = [
        make_tool("Read1", sleeper(probe, "Read1")),
        make_tool("Excl", sleeper(probe, "Excl"), concurrency="exclusive"),
        make_tool("Read2", sleeper(probe, "Read2")),
    ]
    m = manager(tmp_path, tools, ["Read1", "Excl", "Read2"], max_parallel=8)
    calls = [
        ToolCall(id="c1", name="Read1", input={}),
        ToolCall(id="c2", name="Excl", input={}),
        ToolCall(id="c3", name="Read2", input={}),
    ]
    await m.dispatch(gate(m.prepare(calls)), ctx_factory(tmp_path))
    assert probe.max_active == 1
    assert probe.starts["Excl"] == 1


async def test_mutating_parallel_tools_serialize(tmp_path: Path):
    probe = Probe()
    tools = [
        make_tool(
            "M1",
            sleeper(probe, "M1"),
            mutates=True,
            concurrency="parallel",
            permission_key=lambda data: "m1",
        ),
        make_tool(
            "M2",
            sleeper(probe, "M2"),
            mutates=True,
            concurrency="parallel",
            permission_key=lambda data: "m2",
        ),
    ]
    m = manager(tmp_path, tools, ["M1", "M2"], max_parallel=8)
    calls = [
        ToolCall(id="c1", name="M1", input={}),
        ToolCall(id="c2", name="M2", input={}),
    ]
    await m.dispatch(gate(m.prepare(calls)), ctx_factory(tmp_path))
    assert probe.max_active == 1


async def test_same_path_writes_serialize(tmp_path: Path):
    probe = Probe()
    path_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    tools = [
        make_tool(
            "W1",
            sleeper(probe, "W1"),
            bundle="fs",
            mutates=True,
            concurrency="parallel",
            input_schema=path_schema,
            permission_key=lambda data: data["path"],
        ),
        make_tool(
            "W2",
            sleeper(probe, "W2"),
            bundle="fs",
            mutates=True,
            concurrency="parallel",
            input_schema=path_schema,
            permission_key=lambda data: data["path"],
        ),
    ]
    m = manager(tmp_path, tools, ["W1", "W2"], max_parallel=8)
    batch = m.prepare(
        [
            ToolCall(id="c1", name="W1", input={"path": "out.txt"}),
            ToolCall(id="c2", name="W2", input={"path": "out.txt"}),
        ]
    )
    assert batch.for_call("c1").key == batch.for_call("c2").key
    await m.dispatch(gate(batch), ctx_factory(tmp_path))
    assert probe.max_active == 1


async def test_provider_without_parallel_tool_calls_serializes(tmp_path: Path):
    probe = Probe()
    tools = [make_tool(f"R{i}", sleeper(probe, f"R{i}", delay=0.05)) for i in range(3)]
    m = manager(tmp_path, tools, [f"R{i}" for i in range(3)], max_parallel=8)
    calls = [ToolCall(id=f"c{i}", name=f"R{i}", input={}) for i in range(3)]
    await m.dispatch(
        gate(m.prepare(calls)), ctx_factory(tmp_path), parallel_allowed=False
    )
    assert probe.max_active == 1


# ---------------------------------------------------------------------------
# Timeouts, cancellation, exception conversion
# ---------------------------------------------------------------------------


async def test_timeout_becomes_safe_model_error(tmp_path: Path):
    async def slow(args, ctx):
        await asyncio.sleep(5)
        return ToolExecutionResult.text("late")

    m = manager(
        tmp_path,
        [make_tool("Slow", slow, timeout_s=0.05)],
        ["Slow"],
    )
    events: list = []
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Slow", input={})])),
        ctx_factory(tmp_path),
        emit=events.append,
    )
    text = results[0].content[0].text
    assert results[0].is_error is True
    assert "timed out" in text
    assert "Traceback" not in text
    assert events[-1].type == "tool.failed"


async def test_exception_is_converted_to_safe_error(tmp_path: Path):
    async def boom(args, ctx):
        raise ValueError("kaboom")

    m = manager(tmp_path, [make_tool("Boom", boom)], ["Boom"])
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Boom", input={})])),
        ctx_factory(tmp_path),
    )
    text = results[0].content[0].text
    assert results[0].is_error is True
    assert "ValueError" in text and "kaboom" in text
    assert "Traceback" not in text


async def test_invalid_tool_return_is_converted(tmp_path: Path):
    async def bad(args, ctx):
        return "not-a-result"

    m = manager(tmp_path, [make_tool("Bad", bad)], ["Bad"])
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Bad", input={})])),
        ctx_factory(tmp_path),
    )
    assert results[0].is_error is True
    assert "invalid result" in results[0].content[0].text


async def test_cooperative_cancellation_propagates(tmp_path: Path):
    token = CancelToken()

    async def wait_cancel(args, ctx):
        while not ctx.cancel_token.cancelled:
            await asyncio.sleep(0.01)
        ctx.cancel_token.raise_if_cancelled()

    m = manager(tmp_path, [make_tool("Wait", wait_cancel)], ["Wait"])
    batch = gate(m.prepare([ToolCall(id="c", name="Wait", input={})]))
    task = asyncio.create_task(
        m.dispatch(batch, ctx_factory(tmp_path), cancel=token)
    )
    await asyncio.sleep(0.05)
    token.cancel("stop")
    with pytest.raises(OperationCancelled):
        await task


async def test_task_cancellation_propagates(tmp_path: Path):
    async def slow(args, ctx):
        await asyncio.sleep(5)

    m = manager(tmp_path, [make_tool("Slow", slow)], ["Slow"])
    batch = gate(m.prepare([ToolCall(id="c", name="Slow", input={})]))
    task = asyncio.create_task(m.dispatch(batch, ctx_factory(tmp_path)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Result capping
# ---------------------------------------------------------------------------


def result_text(result: ToolExecutionResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, Text))


async def test_cap_truncates_at_line_boundary_with_marker(tmp_path: Path):
    body = "\n".join(f"line-{i:04d}" for i in range(500))

    async def big(args, ctx):
        return ToolExecutionResult.text(body)

    m = manager(
        tmp_path,
        [make_tool("Big", big, max_result_tokens=100)],
        ["Big"],
    )
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Big", input={})])),
        ctx_factory(tmp_path),
    )
    text = result_text(results[0])
    assert "line-0000" in text
    assert text.count("line-") < 500
    assert "[Big: result truncated" in text
    assert "re-run" in text
    assert results[0].metrics["truncated"] is True
    assert results[0].context_note is not None


async def test_cap_uses_min_of_spec_and_config(tmp_path: Path):
    async def big(args, ctx):
        return ToolExecutionResult.text("x" * 5000)

    m = ToolManager(
        cfg(max_result_tokens=5),
        workspace=tmp_path,
        tools=[make_tool("Big", big, max_result_tokens=25_000)],
        tool_names=["Big"],
    )
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Big", input={})])),
        ctx_factory(tmp_path),
    )
    assert results[0].metrics["truncated"] is True


async def test_cap_is_utf8_safe(tmp_path: Path):
    body = "é" * 2000

    async def big(args, ctx):
        return ToolExecutionResult.text(body)

    m = manager(tmp_path, [make_tool("Big", big, max_result_tokens=10)], ["Big"])
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Big", input={})])),
        ctx_factory(tmp_path),
    )
    shown = result_text(results[0]).split("\n[")[0]
    assert shown
    assert body.startswith(shown)
    assert shown.encode("utf-8")  # never raises on a split code point


async def test_cap_preserves_context_note(tmp_path: Path):
    async def big(args, ctx):
        return ToolExecutionResult.text("x" * 5000, context_note="keep me")

    m = manager(tmp_path, [make_tool("Big", big, max_result_tokens=10)], ["Big"])
    results = await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Big", input={})])),
        ctx_factory(tmp_path),
    )
    assert results[0].context_note == "keep me"


def test_cap_display_and_metrics_are_bounded(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    tool = make_tool("Big", _noop)
    entry = PreparedCall(
        call=ToolCall(id="c", name="Big", input={}), spec=tool.spec
    )
    result = ToolExecutionResult(
        content=[Text(text="ok")],
        display="d" * (MAX_DISPLAY_CHARS * 3),
        metrics={"big": "m" * 1000, "n": 1, "obj": object()},
    )
    capped = m.cap_result(entry, result)
    assert len(capped.display) <= MAX_DISPLAY_CHARS + 32
    assert capped.display.endswith("… [display truncated]")
    assert capped.metrics["big"].endswith("…")
    assert capped.metrics["n"] == 1
    assert capped.metrics["obj"] == "<object>"


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_event_order_and_safe_payload(tmp_path: Path):
    async def run(args, ctx):
        await ctx.report("working", {"step": 1})
        return ToolExecutionResult.text("done")

    m = manager(tmp_path, [make_tool("Work", run)], ["Work"])
    events: list = []
    await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Work", input={})])),
        ctx_factory(tmp_path),
        emit=events.append,
    )
    assert [event.type for event in events] == [
        "tool.started",
        "tool.progress",
        "tool.completed",
    ]
    assert events[0].data == {"call_id": "c", "tool": "Work", "bundle": "task"}
    assert events[1].data["step"] == 1
    assert events[2].data["is_error"] is False
    for event in events:
        assert "input" not in event.data
        assert "env" not in event.data
        json.dumps(event.data)


async def test_async_emit_callback_is_awaited(tmp_path: Path):
    seen: list = []

    async def sink(event):
        seen.append(event)

    m = ToolManager(cfg(), workspace=tmp_path)
    await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Read", input={"path": "x.txt"})])),
        ctx_factory(tmp_path),
        emit=sink,
    )
    assert [event.type for event in seen] == ["tool.started", "tool.completed"]


# ---------------------------------------------------------------------------
# Gating protocol
# ---------------------------------------------------------------------------


async def test_denied_calls_never_execute(tmp_path: Path):
    ran: list[str] = []

    async def run(args, ctx):
        ran.append("Echo")
        return ToolExecutionResult.text("ran")

    m = manager(tmp_path, [make_tool("Echo", run)], ["Echo"])
    batch = m.prepare([ToolCall(id="c", name="Echo", input={})]).with_decisions(
        {"c": Decision.DENY_ONCE}
    )
    events: list = []
    results = await m.dispatch(batch, ctx_factory(tmp_path), emit=events.append)
    assert ran == []
    assert results[0].is_error is True
    assert events[0].type == "tool.failed"
    assert events[0].data["code"] == "permission_denied"


async def test_ungated_executable_call_never_executes(tmp_path: Path):
    ran: list[str] = []

    async def run(args, ctx):
        ran.append("Echo")
        return ToolExecutionResult.text("ran")

    m = manager(tmp_path, [make_tool("Echo", run)], ["Echo"])
    batch = m.prepare([ToolCall(id="c", name="Echo", input={})])
    events: list = []
    results = await m.dispatch(batch, ctx_factory(tmp_path), emit=events.append)
    assert ran == []
    assert results[0].is_error is True
    assert "no permission decision" in results[0].content[0].text
    assert events[0].data["code"] == "ungated"


async def test_apply_plan_maps_allow_and_deny(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    engine = PermissionEngine(
        mode="allow",
        deny=["Read(**/.env)"],
        path_guard=m.path_guard,
    )
    batch = m.prepare(
        [
            ToolCall(id="c1", name="Read", input={"path": ".env"}),
            ToolCall(id="c2", name="Read", input={"path": "ok.txt"}),
        ]
    )
    plan = engine.plan(batch.calls(), batch.spec_map())
    gated = batch.apply_plan(plan)
    assert gated.for_call("c1").error is not None
    assert gated.for_call("c1").code == "deny"
    assert gated.for_call("c2").decision is not None
    assert gated.for_call("c2").decision.allows


# ---------------------------------------------------------------------------
# Filesystem permission-key canonicalization
# ---------------------------------------------------------------------------


def test_fs_permission_key_is_canonicalized(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    batch = m.prepare(
        [ToolCall(id="c", name="Read", input={"path": "./sub/../a.txt"})]
    )
    entry = batch.for_call("c")
    expected = str((tmp_path / "a.txt").resolve())
    assert entry.key == expected
    assert entry.call.input["path"] == expected


def test_fs_write_outside_write_root_becomes_error(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    batch = m.prepare(
        [
            ToolCall(
                id="c", name="Write", input={"path": "../escape.txt", "content": "x"}
            )
        ]
    )
    entry = batch.for_call("c")
    assert entry.error is not None
    assert entry.code == "write_root"


def test_directory_tool_without_path_is_left_alone(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    entry = m.prepare([ToolCall(id="c", name="LS", input={})]).for_call("c")
    assert entry.error is None
    assert entry.key == "."


# ---------------------------------------------------------------------------
# Service ownership and context injection
# ---------------------------------------------------------------------------


async def test_owned_job_registry_is_closed_and_idempotent(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    registry = m.job_registry
    assert isinstance(registry, JobRegistry)
    await m.aclose()
    assert registry.closed is True
    await m.aclose()  # idempotent, no error


async def test_injected_job_registry_is_not_closed(tmp_path: Path):
    registry = JobRegistry()
    m = ToolManager(cfg(), workspace=tmp_path, job_registry=registry)
    await m.aclose()
    assert registry.closed is False
    await registry.aclose()


async def test_services_are_injected_into_tool_context(tmp_path: Path):
    registry = JobRegistry()
    store = TodoStore()
    captured: dict = {}

    async def run(args, ctx):
        captured["registry"] = ctx.job_registry
        captured["store"] = ctx.todo_store
        captured["call_id"] = ctx.call_id
        return ToolExecutionResult.text("ok")

    m = ToolManager(
        cfg(),
        workspace=tmp_path,
        tools=[make_tool("Probe", run)],
        tool_names=["Probe"],
        job_registry=registry,
        todo_store=store,
    )
    await m.dispatch(
        gate(m.prepare([ToolCall(id="c", name="Probe", input={})])),
        ctx_factory(tmp_path),
    )
    assert captured["registry"] is registry
    assert captured["store"] is store
    assert captured["call_id"] == "c"


async def test_todo_state_persists_across_calls_through_manager(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path, profile="coding")
    await m.dispatch(
        gate(
            m.prepare(
                [
                    ToolCall(
                        id="c1",
                        name="TodoWrite",
                        input={
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "ship it",
                                    "status": "pending",
                                    "priority": "high",
                                }
                            ]
                        },
                    )
                ]
            )
        ),
        ctx_factory(tmp_path, session_id="s1"),
    )
    assert m.todo_store.get("s1")[0].content == "ship it"
    # A second call replaces the list, still session-scoped.
    await m.dispatch(
        gate(
            m.prepare(
                [ToolCall(id="c2", name="TodoWrite", input={"todos": []})]
            )
        ),
        ctx_factory(tmp_path, session_id="s1"),
    )
    assert m.todo_store.get("s1") == ()


# ---------------------------------------------------------------------------
# IR conversion
# ---------------------------------------------------------------------------


async def test_to_ir_results_is_ordered_and_typed(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)
    batch = m.prepare(
        [ToolCall(id="c1", name="Read", input={"path": "missing.txt"})]
    )
    results = await m.dispatch(gate(batch), ctx_factory(tmp_path))
    ir = batch.to_ir_results(results)
    assert ir[0].tool_use_id == "c1"
    assert isinstance(ir[0].content[0], Text)


async def test_dispatch_rejects_non_batch(tmp_path: Path):
    m = ToolManager(cfg(), workspace=tmp_path)

    with pytest.raises(ToolManagerError):
        await m.dispatch([], ctx_factory(tmp_path))
