"""Isolated Core for the NSH look audit (plan 05 section 5.3): several offline mock sessions, no provider calls.

Run with NEXUS10_DEV=1, a scratch NEXUS10_HOME and NEXUS_PORT."""
import asyncio
import json
import os
import time
from pathlib import Path

import uvicorn

from nexus.daemon import _write_info, info_path
from nexus.server import build_app, make_runtime
from nexus.store import Store


async def main():
    root = Path(os.environ["NEXUS10_HOME"])
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "README.md").write_text("A tiny workspace for the look audit.\n")
    (workspace / "main.py").write_text("print('hello')\n")
    rt = make_runtime(Store(root / "core.db"))
    project = rt.open_project(str(workspace))
    url = f"http://127.0.0.1:{os.environ['NEXUS_PORT']}"
    token = "isolated-visual-check"
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    server = uvicorn.Server(uvicorn.Config(build_app(rt, token=token, url=url), host="127.0.0.1",
                                           port=int(os.environ["NEXUS_PORT"]), log_level="warning"))
    task = asyncio.create_task(server.serve())
    await asyncio.sleep(0.5)
    ids = {}
    for key, title, model, perm, prompt in [
        ("rich", "Streaming and tools", "mock/tool_marathon", "allow", "Work through the marathon."),
        ("text", "Rich text reply", "mock/streaming_rich", "allow", "Show rich text."),
        ("question", "Waiting question", "mock/question", "ask", "Ask me something."),
        ("approval", "Waiting approval", "mock/bash_wait", "ask", "Run the command."),
        ("hello", "Hello", "mock/hello", "allow", "Say hello."),
    ]:
        s = rt.create_session(project, title=title, model=model, permission=perm)
        ids[key] = s.id
        rt.prompt(s.id, prompt)
        await asyncio.sleep(0.3)
    await asyncio.sleep(8)
    (root / "ids.json").write_text(json.dumps({**ids, "project": project.id}))
    try:
        await task
    finally:
        await rt.shutdown()
        rt.store.close()
        info_path().unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
