"""Only the root session spawns: children get no subagent service, catalog entry, or forged spawn."""
from __future__ import annotations

from types import SimpleNamespace

from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.runtime import Runtime
from nexus.tools.builtin.task import build_task_tool
from nexus.tools.spec import RegisteredTool, ToolExecutionResult, ToolSpec
from test_agent_selection import _config


def _write_probe_tool(workspace, log) -> None:
    """An extension tool that records whether its context carries a subagent service."""
    tools = workspace / ".agents" / "tools"
    tools.mkdir(parents=True)
    (tools / "probe_ctx.py").write_text(
        "from pathlib import Path\n"
        "from nexus.tools.spec import ToolExecutionResult, ToolSpec\n"
        "SPEC = ToolSpec(name='ProbeCtx', description='probe', "
        "input_schema={'type':'object','properties':{},'additionalProperties':False}, "
        "bundle='fs')\n"
        "async def run(args, ctx):\n"
        f"    with open({str(log)!r}, 'a') as handle:\n"
        "        handle.write(f'{ctx.session_id}|{ctx.subagents is None}\\n')\n"
        "    return ToolExecutionResult.text('probed')\n",
        encoding="utf-8",
    )


async def test_extension_child_context_has_no_subagents_and_forged_spawn_makes_no_child(tmp_path):
    log = tmp_path / "probe.log"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_probe_tool(workspace, log)
    provider = ScriptedProvider(
        tool_response(("root-probe", "ProbeCtx", {})),
        tool_response(("spawn", "subagent", {"prompt": "delegate", "subagent_type": "general", "wait": True})),
        tool_response(
            ("child-probe", "ProbeCtx", {}),
            ("forged-subagent", "subagent", {"prompt": "nested", "subagent_type": "general"}),
            ("forged-task", "Task", {"prompt": "nested", "subagent_type": "general"}),
        ),
        text_response("child done"),
        text_response("root done"),
    )
    runtime = Runtime(workspace, config=_config(), providers={"scripted": provider})
    [event async for event in runtime.session("sec-tree").send("go")]
    events = list(runtime.session("sec-tree").events)

    # The root context carries the service; the child's never does.
    rows = [line.split("|") for line in log.read_text(encoding="utf-8").splitlines()]
    assert [flag for _, flag in rows] == ["False", "True"]
    assert rows[0][0] != rows[1][0]

    # The forged nested spawns were refused without running and made no child.
    assert [e.data["depth"] for e in events if e.type == "agent.spawned"] == [1]
    failed = {
        e.data.get("call_id") for e in events
        if e.type == "tool.failed" and e.data.get("executed") is False
    }
    assert {"forged-subagent", "forged-task"} <= failed
    await runtime.aclose()


async def test_injected_extension_catalog_subagent_and_task_never_reach_a_child(tmp_path, monkeypatch):
    runtime = Runtime(
        tmp_path, config=_config(),
        providers={"scripted": ScriptedProvider(text_response("unused"))},
    )

    async def run(args, ctx):
        return ToolExecutionResult.text("spawned")

    task_alias = RegisteredTool(
        ToolSpec(
            name="Task", description="forged alias",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            bundle="task",
        ),
        run,
    )
    injected = {
        **dict(runtime.manifest.tools),
        "subagent": build_task_tool(None),
        "Task": task_alias,
    }
    fake = SimpleNamespace(tools=injected)
    monkeypatch.setattr(Runtime, "manifest", property(lambda self: fake))

    manager = runtime._build_child_tool_manager(
        SimpleNamespace(
            session_id="s/sub/1", workspace=tmp_path,
            tools=("read", "subagent", "Task"), permissions=None,
        ),
        _config(), None,
    )
    assert "read" in manager.names
    assert not {"subagent", "Task"} & set(manager.names)
    await runtime.aclose()
