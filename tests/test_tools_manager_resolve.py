"""Per-call authority, validation and dispatch for proxy tools."""
import asyncio

import pytest

from nexus.config import Config
from nexus.tools.manager import ToolManager
from nexus.tools.permissions import Decision, PermissionEngine
from nexus.tools.spec import RegisteredTool, ResolvedTarget, ToolCall, ToolExecutionResult, ToolSpec


def proxy(*, mutates=False, concurrency="parallel", timeout_s=1):
    target = ToolSpec(name="Target", description="Target", bundle="task", mutates=mutates,
        concurrency=concurrency, timeout_s=timeout_s,
        permission_key=lambda data: "target_key", input_schema={"type": "object",
            "properties": {"value": {"type": "string"}}, "required": ["value"]})

    async def run(data, ctx):
        await asyncio.sleep(0)
        return ToolExecutionResult.text(data["value"])

    spec = ToolSpec(name="Proxy", description="Proxy", bundle="task", mutates=False,
        input_schema={"type": "object"})
    return RegisteredTool(spec, run, resolve=lambda data: ResolvedTarget(target, run, data))


@pytest.mark.parametrize("mutates", [False, True])
async def test_authority_and_execution_follow_target(tmp_path, mutates):
    manager = ToolManager(Config(), workspace=tmp_path, tools=[proxy(mutates=mutates)])
    batch = manager.prepare([ToolCall("1", "Proxy", {"value": "ok"})])
    entry = batch.entries[0]
    assert entry.call.name == "Proxy"
    assert entry.spec.name == "Target"
    assert entry.spec.mutates is mutates
    assert entry.key == "target_key"
    engine = PermissionEngine(mode="allow", deny=["Target(target_key)"])
    assert engine.plan(batch.calls(), batch.spec_map()).evaluations[0].outcome.value == "deny"
    result = await manager.dispatch(batch.with_decisions({"1": Decision.ALLOW_ONCE}))
    assert result[0].content[0].text == "ok"
    await manager.aclose()


async def test_research_refusal_and_schema(tmp_path):
    manager = ToolManager(Config(), workspace=tmp_path, profile="research", tools=[proxy(mutates=True)])
    entry = manager.prepare([ToolCall("1", "Proxy", {"value": "ok"})]).entries[0]
    assert "research" in entry.error.content[0].text
    entry = manager.prepare([ToolCall("1", "Proxy", {})]).entries[0]
    assert "Input schema" in entry.error.content[0].text
    await manager.aclose()


async def test_duplicates_and_exclusive_target(tmp_path):
    manager = ToolManager(Config(), workspace=tmp_path, tools=[proxy(concurrency="exclusive")])
    call = ToolCall("1", "Proxy", {"value": "ok"})
    assert all(entry.error for entry in manager.prepare([call, call]).entries)
    assert manager.prepare([call]).entries[0].spec.concurrency == "exclusive"
    await manager.aclose()
