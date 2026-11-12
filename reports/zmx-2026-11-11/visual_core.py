"""Isolated Core for the ZMX paired captures: seeds one ZMX child per shared scenario
(interface/scenarios/zmx-*.json), then serves it. No provider and no CLI is started.

Run with a scratch NEXUS10_HOME and NEXUS_PORT:  NEXUS10_HOME=/tmp/zv NEXUS_PORT=49021 python visual_core.py
"""
import asyncio
import json
import os
import time
from pathlib import Path

import uvicorn

from nexus.codec import message_from_json
from nexus.config import ConfigFile
from nexus.daemon import _write_info
from nexus.llmhub.types import Message, Text, ToolCall, ToolResult
from nexus.server import build_app, make_runtime
from nexus.store import Store

SCENARIOS = Path(__file__).parents[2] / "interface/scenarios"


async def main():
    root = Path(os.environ["NEXUS10_HOME"])
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    config = ConfigFile()
    config.patch(config.read()["revision"], {"zmx.claude_backend": "zmx", "zmx.antigravity": True}, [])
    rt = make_runtime(Store(root / "core.db"))
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title="Orchestrator", model="", permission="allow")
    rt.store.append_message(parent.id, Message("user", (Text("Delegate the checks to the CLI subagents."),)))
    ids = {}
    for name in ("zmx-parallel-tools", "zmx-denial", "zmx-needs-attention", "zmx-interrupt", "zmx-recovered"):
        scenario = json.loads((SCENARIOS / f"{name}.json").read_text())
        info = scenario["session"]
        child = rt.create_session(project, title=info["title"], model=info["model"], permission=info["permission"],
                                  agent="scout", parent=parent, parent_call_id=f"call_{name}")
        turn = rt.store.start_turn(child.id, data={"origin": "prompt"})
        for item in scenario["messages"]:
            message = message_from_json({"role": item["role"], "parts": item["parts"]})
            if item["role"] == "assistant":
                rt.store.save_assistant_message(child.id, message, turn_id=turn["id"])
            elif item["role"] == "tool":
                rt.store.save_tool_message(child.id, message, turn_id=turn["id"])
            else:
                rt.store.append_message(child.id, message)
        rt.store.finish_turn(turn["id"], "completed", steps=1)
        rt._save_worker(child.id, info["worker"])
        ids[name] = child.id
    # The parent's `session` calls, so both clients show a child card per ZMX child to open it from.
    calls = [ToolCall(f"call_{name}", "session", json.dumps({"agent": "scout", "title": rt.session(cid).title,
                                                              "prompt": "Run the check."}))
             for name, cid in ids.items()]
    rt.store.append_message(parent.id, Message("assistant", tuple(calls)))
    rt.store.append_message(parent.id, Message("tool", tuple(
        ToolResult(c.id, json.dumps({"session": ids[c.id.removeprefix("call_")], "status": "done"})) for c in calls)))
    port = int(os.environ["NEXUS_PORT"])
    url, token = f"http://127.0.0.1:{port}", "zmx-visual-check"
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    (root / "ids.json").write_text(json.dumps({"parent": parent.id, "children": ids, "project": project.id}))
    try:
        await uvicorn.Server(uvicorn.Config(build_app(rt, token=token, url=url), host="127.0.0.1", port=port, log_level="warning")).serve()
    finally:
        await rt.shutdown()


asyncio.run(main())
