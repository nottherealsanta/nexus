#!/usr/bin/env python3
"""A real Nexus Core ASGI app (nexus.server.build_app, real store, bus, HTTP API, SSE) whose provider
is scripted, for visual and interaction tests without provider calls. Same trick as
nexus-core/tests/test_server.py (`Scripted`); nexus-core is imported, never edited.

  cd nexus-core && NEXUS10_HOME=<mode-700 dir> NEXUS_PORT=4812 uv run python ../nexus-nsh/e2e/scripted_core.py

It writes <home>/daemon.json like `nexus daemon start`, so the NSH host discovers it the same way.
Prompt text picks the script: "tool" asks for a bash call (waits for approval), "slow" streams text
slowly, "ask" calls the `ask` tool, anything else answers with Markdown.
"""
import asyncio
import json
import os
import secrets
import signal
import sys
import time

from nexus import runtime as runtime_module
from nexus.daemon import _write_info, info_path
from nexus.llmhub.types import Done, PartDone, Reasoning, Text, TextDelta, ToolCall, Usage
from nexus.server import build_app, make_runtime
from nexus.store import Store
from nexus.paths import home

MD = """Here is what I found.

- The loader reads `config.toml` once
- Retries are bounded (3 attempts)

```python
def load(path):
    return tomllib.loads(path.read_text())
```

Let me know if you want the same change in `cli.py`."""


class Scripted:
    name = "scripted"

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "tools": [t.name for t in req.tools]}

    async def stream(self, req):
        last = req.messages[-1]
        if last.role == "tool":  # after a tool result: finish
            yield TextDelta("Done. The command finished; output is above.")
            yield PartDone(Text("Done. The command finished; output is above."))
            yield Usage(120, 0, 14, 0)
            yield Done("end_turn")
            return
        prompt = "".join(getattr(p, "text", "") for p in last.parts).lower()
        if "tool" in prompt:
            yield PartDone(Reasoning("The user wants a directory listing; I will run ls."))
            yield PartDone(ToolCall("call_1", "bash", json.dumps({"command": "ls -la src/ && echo done", "timeout": 20})))
            yield Done("tool_calls")
        elif "ask" in prompt:
            yield PartDone(ToolCall("call_q", "ask", json.dumps({"questions": [{"id": "db", "question": "Which database should the migration target?", "options": [{"id": "pg", "label": "PostgreSQL"}, {"id": "sq", "label": "SQLite"}], "multiple": False, "allow_free_text": False}]})))
            yield Done("tool_calls")
        elif "slow" in prompt:
            for word in ("Streaming ", "a ", "long ", "reply ", "word ", "by ", "word… ") * 6:
                yield TextDelta(word)
                await asyncio.sleep(0.6)
            yield PartDone(Text("Streaming a long reply word by word…"))
            yield Done("end_turn")
        else:
            yield PartDone(Reasoning("Check the loader module, then summarise."))
            for chunk in MD.split("\n"):
                yield TextDelta(chunk + "\n")
            yield PartDone(Text(MD))
            yield Usage(80, 0, 60, 0)
            yield Done("end_turn")


def main() -> None:
    import uvicorn

    port = int(os.environ.get("NEXUS_PORT", "4812"))
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(h, 0o700)
    provider = Scripted()
    runtime_module.provider_for = lambda ref: (provider, ref.partition("/")[2] or "m")
    rt = make_runtime("scripted/m", Store(h / "nexus.db"))
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{port}"
    app = build_app(rt, token=token, url=url)
    _write_info({"url": url, "pid": os.getpid(), "token": token, "started": time.time()})
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=2)
    finally:
        try:
            if json.loads(info_path().read_text()).get("pid") == os.getpid():
                info_path().unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
