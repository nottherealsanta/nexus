"""Real Monty workers: persistence, caller isolation, cancellation and limits."""
import asyncio

import pytest

from nexus.kernels import Kernels
from nexus.llmhub.types import ToolSpec
from nexus.tools import DEFAULT_TOOLS, Tool, ToolContext, ToolError


@pytest.fixture
async def kernels():
    registry = Kernels()
    yield registry
    await registry.shutdown()


async def allow(call, args):
    return True


async def run(kernels, tmp_path, code, *, caller="parent", tools=None, approve=allow, events=None, **args):
    return await kernels.run({"code": code, **args}, root="root", caller=caller,
                             ctx=ToolContext(tmp_path, "outer"),
                             tools=tools if tools is not None else list(DEFAULT_TOOLS[:-1]), approve=approve,
                             publish=lambda kind, data: events.append((kind, data)) if events is not None else None)


async def test_persistence_shared_tree_and_inspection(kernels, tmp_path):
    first = await run(kernels, tmp_path, "x = 41\nprint('hello')\nx")
    assert "hello" in first and "Out [1]: 41" in first
    assert "Out [2]: 82" in await run(kernels, tmp_path, "x * 2", caller="child")
    k = next(iter(kernels.items.values()))
    assert k.json()["users"] == ["child", "parent"]
    assert {v["name"]: v["repr"] for v in k.variables}["x"] == "41"
    await run(kernels, tmp_path, "x += 1", kernel_id=k.id)
    await kernels.delete(k.id)
    assert not kernels.items and not kernels.main
    with pytest.raises(ToolError, match="no kernel"):
        await run(kernels, tmp_path, "x", kernel_id=k.id)


async def test_gather_approval_events_and_caught_failure(kernels, tmp_path):
    (tmp_path / "a").write_text("abc")
    events, approvals = [], []

    async def approve(call, args):
        approvals.append(call.name)
        return call.name == "read"

    result = await run(kernels, tmp_path, '''import asyncio
r = await asyncio.gather(read(path="a"), read(path="."))
try:
    await write(path="bad", content="denied")
except RuntimeError as e:
    print(str(e))
len(r)''', approve=approve, events=events)
    assert "Out [1]: 2" in result and "declined" in result
    assert approvals.count("read") == 2 and "write" in approvals
    calls = [d for kind, d in events if kind == "code.call"]
    assert len(calls) == 3 and calls[-1]["error"]
    assert all(d["parent_call_id"] == "outer" for d in calls)
    assert not (tmp_path / "bad").exists()


async def test_parallel_read_and_bash(kernels, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n")
    events = []
    result = await run(kernels, tmp_path, '''import asyncio
r, b = await asyncio.gather(read(path="a.txt"), bash(command="printf 'beta\\\\n'"))
print(r.splitlines()[0])
b''', events=events)
    assert "alpha" in result and "beta" in result and "2 calls" in result
    calls = [d for kind, d in events if kind == "code.call"]
    assert [c["tool"] for c in calls] == ["read", "bash"]
    assert all(c["error"] is None for c in calls)
    assert {c["cell"] for c in calls} == {1}


async def test_saved_tool_handles_use_current_permissions_and_directory(kernels, tmp_path):
    (tmp_path / "a").write_text("parent")
    child = tmp_path / "child"
    child.mkdir()
    (child / "a").write_text("child")
    await run(kernels, tmp_path, "saved = read\nsaved_write = write\nawait saved(path='a')")
    assert "child" in await run(kernels, child, "await saved(path='a')", caller="child", tools=[DEFAULT_TOOLS[0]])
    with pytest.raises(ToolError, match="unavailable|not defined"):
        await run(kernels, child, "await saved_write(path='bad', content='oops')", tools=[DEFAULT_TOOLS[0]])
    assert not (child / "bad").exists()
    with pytest.raises(ToolError):
        await run(kernels, tmp_path, "await code(code='1')")
    with pytest.raises(ToolError):
        await run(kernels, tmp_path, "await session(agent='build', prompt='hi')")


async def test_errors_timeout_and_cancellation_release_kernel(kernels, tmp_path):
    await run(kernels, tmp_path, "x = 41")
    with pytest.raises(ToolError, match="ZeroDivisionError"):
        await run(kernels, tmp_path, "1 / 0")
    assert "Out [3]: 41" in await run(kernels, tmp_path, "x")  # ordinary errors keep state
    with pytest.raises(ToolError, match="timed out"):
        await run(kernels, tmp_path, "await bash(command='sleep 5')", timeout_s=0.1)
    assert not kernels.items
    await run(kernels, tmp_path, "x = 41")
    task = asyncio.create_task(run(kernels, tmp_path, "await bash(command='sleep 5')"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not kernels.items
    assert "42" in await run(kernels, tmp_path, "42")


async def test_timeout_stops_nested_command_and_closes_its_call(kernels, tmp_path):
    events = []
    with pytest.raises(ToolError, match="timed out"):
        await run(kernels, tmp_path, "await bash(command='sleep 2; touch late')", timeout_s=0.2, events=events)
    calls = [d for kind, d in events if kind == "code.call"]
    assert len(calls) == 1 and calls[0]["tool"] == "bash" and calls[0]["error"]
    kinds = [kind for kind, _ in events]
    assert kinds.index("code.call") < kinds.index("code.cell") < kinds.index("kernel.deleted")
    await asyncio.sleep(2.2)  # the command would have finished by now if cancellation had not killed it
    assert not (tmp_path / "late").exists()
    assert len([d for kind, d in events if kind == "code.call"]) == 1


async def test_timeout_cancels_a_pending_nested_approval(kernels, tmp_path):
    events, approvals = [], []
    answered = asyncio.Event()

    async def approve(call, args):
        approvals.append(call.name)
        await answered.wait()
        return True

    async def sentinel(args, ctx):
        (ctx.workspace / "ran").write_text("yes")
        return "ran"

    tool = Tool(ToolSpec("sentinel", "test tool", {"type": "object", "properties": {}}), sentinel)
    with pytest.raises(ToolError, match="timed out"):
        await run(kernels, tmp_path, "await sentinel()", tools=[tool], approve=approve, timeout_s=0.1, events=events)
    [call] = [d for kind, d in events if kind == "code.call"]
    assert call["tool"] == "sentinel" and call["error"] == "interrupted or timed out"
    assert approvals == ["sentinel"]
    answered.set()  # a late allow must not run the tool after the cell is gone
    await asyncio.sleep(0.1)
    assert not (tmp_path / "ran").exists()


async def test_shutdown_closes_a_busy_kernel_and_its_pool(kernels, tmp_path):
    task = asyncio.create_task(run(kernels, tmp_path, "await bash(command='sleep 2; touch late')"))
    while not kernels.items or not next(iter(kernels.items.values())).counter:
        await asyncio.sleep(0.005)
    await kernels.shutdown()
    assert not kernels.items and not kernels.main and kernels.pool is None
    with pytest.raises((asyncio.CancelledError, ToolError)):
        await task
    await asyncio.sleep(2.2)
    assert not (tmp_path / "late").exists()


async def test_sandbox_and_clipping(kernels, tmp_path):
    for code in ["open('/etc/hosts').read()", "import os\nos.listdir('/')", "import subprocess"]:
        with pytest.raises(ToolError):
            await run(kernels, tmp_path, code)
    result = await run(kernels, tmp_path, "print('a' * 100000)\n42")
    assert "clipped" in result and "42" in result
    for timeout in [0, -1, True, float('inf'), 601]:
        with pytest.raises(ToolError, match="timeout_s"):
            await run(kernels, tmp_path, "1", timeout_s=timeout)


async def test_serialization_and_cancelled_waiter_keeps_active_kernel(kernels, tmp_path):
    active = asyncio.create_task(run(kernels, tmp_path, "x = 7\nawait bash(command='sleep 0.2')"))
    while not kernels.items or not next(iter(kernels.items.values())).counter:
        await asyncio.sleep(0.005)
    with pytest.raises(ToolError, match="timed out"):
        await run(kernels, tmp_path, "x = 99", timeout_s=0.03)
    await active
    assert "Out [2]: 7" in await run(kernels, tmp_path, "x")


async def test_memory_limit_and_live_kernel_cap(kernels, tmp_path, monkeypatch):
    from nexus import kernels as module

    monkeypatch.setattr(module, "MAX_KERNELS", 2)
    await kernels.select("a", "parent", None)
    await kernels.select("b", "child", None)
    with pytest.raises(ToolError, match="limit"):
        await kernels.select("c", "third", None)
    k = next(iter(kernels.items.values()))
    with pytest.raises(ToolError, match="MemoryError|memory"):
        await run(kernels, tmp_path, "x = 'a' * 100000000", kernel_id=k.id)
    assert k.id not in kernels.items
