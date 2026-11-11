"""Isolated, deterministic Core for the section 21–23 paired visual checks; no provider calls."""
import asyncio
import json
import os
import time
from pathlib import Path

import uvicorn

from nexus import runtime as runtime_module
from nexus.codec import message_to_json
from nexus.daemon import _write_info, info_path
from nexus.llmhub.types import Done, Message, PartDone, Text, TextDelta, ToolCall
from nexus.server import build_app, make_runtime
from nexus.store import Store


class VisualProvider:
    name = "visual"

    def body(self, req):
        return {"model": req.model, "instructions": req.instructions,
                "tools": [{"name": t.name, "description": t.description, "parameters": t.parameters} for t in req.tools],
                "messages": [message_to_json(m) for m in req.messages]}

    async def stream(self, req):
        if req.messages[-1].role != "tool":
            yield PartDone(ToolCall("read1", "read", json.dumps({"path": "README.md"})))
            yield Done("tool_calls")
        else:
            answer = "The Core listens on **port 4747**, as the README confirms."
            for word in answer.split(" "):
                yield TextDelta(word + " ")
                await asyncio.sleep(0.01)
            yield PartDone(Text(answer))
            yield Done("end_turn")


async def main():
    root = Path(os.environ["NEXUS10_HOME"])
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "README.md").write_text("Nexus Core listens on port 4747.\n")
    provider = VisualProvider()
    runtime_module.provider_for = lambda ref: (provider, ref.partition("/")[2])
    rt = make_runtime(Store(root / "core.db"))
    project = rt.open_project(str(workspace))
    parent = rt.create_session(project, title="Status and side questions", model="mock/visual", permission="allow")
    turn = rt.store.start_turn(parent.id, data={"origin": "prompt"})
    rt.store.append_message(parent.id, Message("user", (Text("Check the session"),)))
    rt.store.finish_turn(turn["id"], "interrupted", error={"kind": "interrupted", "message": "interrupted by the user"})
    url = f"http://127.0.0.1:{os.environ['NEXUS_PORT']}"
    token = "isolated-visual-check"
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    (root / "ids.json").write_text(json.dumps({"parent": parent.id, "project": project.id}))
    try:
        await uvicorn.Server(uvicorn.Config(build_app(rt, token=token, url=url), host="127.0.0.1", port=int(os.environ["NEXUS_PORT"]), log_level="warning")).serve()
    finally:
        await rt.shutdown()
        rt.store.close()
        info_path().unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
