#!/usr/bin/env python3
"""A real Nexus Core ASGI app (nexus.server.build_app, real store, bus, HTTP API, SSE) whose provider
is scripted, for visual and interaction tests without provider calls. Same trick as
nexus-core/tests/test_server.py (`Scripted`); nexus-core is imported, never edited.

  cd nexus-core && NEXUS10_HOME=<mode-700 dir> NEXUS_PORT=4812 uv run python ../nexus-nsh/e2e/scripted_core.py

It writes <home>/daemon.json like `nexus daemon start`, so both clients discover it the same way.
Prompt text picks the script: "scenario" runs a multi-step review with thoughts, tools, a patch, a
failure, a child and an image; "think" streams reasoning so the live group and footer can be seen;
"tool" asks for a bash call (waits for approval), "slow" streams text slowly, "fail" reads a
missing file, "ask" calls the `ask` tool, anything else answers with Markdown.
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
from nexus.llmhub.types import Done, PartDone, Reasoning, ReasoningDelta, Text, TextDelta, ToolCall, Usage
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

SCENARIO = """## Timeline review

The footer is only part of a **finished** turn: while the model is still working, the live thought
belongs in the activity group and the turn has no footer yet.

```python
def build_rows(messages, turns):
    return group_activities(rows)
```

- [x] thoughts and tools share one group
- [x] failed tools stay visible
- [ ] footer waits for the turn to finish

The child session and image are below; open either to inspect it."""

PATCH = """*** Begin Patch
*** Update File: README.md
@@
-# Nexus demo
+# Nexus demo workspace
*** End Patch"""


def prompt_of(req) -> str:
    for message in reversed(req.messages):
        if message.role == "user":
            return "".join(getattr(part, "text", "") for part in message.parts).lower()
    return ""


def assistant_steps(req) -> int:
    return sum(1 for message in req.messages if message.role == "assistant")


class Scripted:
    name = "scripted"

    def body(self, req):
        return {"model": req.model, "messages": len(req.messages), "tools": [t.name for t in req.tools]}

    async def stream(self, req):
        prompt = prompt_of(req)
        last = req.messages[-1]
        if "scenario" in prompt:
            step = assistant_steps(req)
            if step == 0:
                yield PartDone(Reasoning("Map the timeline renderer before touching the footer."))
                yield PartDone(Reasoning("Check how activity groups collect thoughts and tools."))
                yield PartDone(ToolCall("s_bash", "bash", json.dumps({"command": "rg -n 'footer|Thinking' nexus-nsh/src/ui/timeline.tsx", "timeout": 20})))
                yield PartDone(ToolCall("s_read", "read", json.dumps({"path": "nexus-nsh/src/ui/timeline.tsx"})))
                yield PartDone(ToolCall("s_grep", "grep", json.dumps({"pattern": "thinking", "path": "nexus-nsh/src"})))
                yield Usage(5_200, 3_000, 60, 40)
                yield Done("tool_calls")
                return
            if step == 1:
                yield PartDone(Reasoning("The footer is pushed for the last message even while the turn runs."))
                yield PartDone(Reasoning("Move the live thought into the running group and hide the footer."))
                yield PartDone(ToolCall("s_patch", "patch", json.dumps({"patch": PATCH})))
                yield PartDone(ToolCall("s_fail", "read", json.dumps({"path": "missing-notes.txt"})))
                yield Usage(5_900, 3_400, 140, 30)
                yield Done("tool_calls")
                return
            if step == 2:
                yield PartDone(Reasoning("Show a child session beside the live turn."))
                yield PartDone(ToolCall("s_child", "session", json.dumps({"agent": "scout", "prompt": "List the files touched by the timeline fix and return one line.", "wait": False})))
                yield PartDone(ToolCall("s_image", "read", json.dumps({"path": "clipboard.png"})))
                yield Usage(6_100, 3_500, 90, 20)
                yield Done("tool_calls")
                return
            yield PartDone(Reasoning("Finished the review; write the result."))
            for chunk in SCENARIO.split("\n"):
                yield TextDelta(chunk + "\n")
            yield PartDone(Text(SCENARIO))
            yield Usage(7_200, 4_200, 220, 0)
            yield Done("end_turn")
            return
        if "think" in prompt:
            if assistant_steps(req) == 0:
                yield PartDone(Reasoning("Check the live renderer before the long think."))
                yield PartDone(ToolCall("t_read", "read", json.dumps({"path": "nexus-nsh/src/ui/timeline.tsx"})))
                yield Usage(480, 0, 12, 0)
                yield Done("tool_calls")
                return
            text = ("Reading the timeline renderer. The footer is painted before the live thought; "
                    "move the thought into the activity group and keep the footer for the end. ") * 4
            for _ in range(300):
                yield ReasoningDelta(text)
                await asyncio.sleep(0.25)
            yield PartDone(Reasoning(text))
            yield Usage(900, 0, 30, 12)
            yield Done("end_turn")
            return
        if "fail" in prompt:
            if assistant_steps(req) == 0:
                yield PartDone(Reasoning("Check the missing notes file before changing anything."))
                yield PartDone(ToolCall("f_read", "read", json.dumps({"path": "missing-notes.txt"})))
                yield Done("tool_calls")
            else:
                yield PartDone(Reasoning("The read failed; report it plainly."))
                yield TextDelta("The notes file is missing, so I stopped before changing anything.")
                yield PartDone(Text("The notes file is missing, so I stopped before changing anything."))
                yield Usage(480, 0, 18, 0)
                yield Done("end_turn")
            return
        if last.role == "tool":  # after an ordinary tool result: finish
            yield TextDelta("Done. The command finished; output is above.")
            yield PartDone(Text("Done. The command finished; output is above."))
            yield Usage(120, 0, 14, 0)
            yield Done("end_turn")
            return
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
    agents = h / "agents"
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    (agents / "scenario.md").write_text(
        "---\n"
        "description: Multi-step visual scenario with child sessions\n"
        "mode: all\n"
        "control: [session]\n"
        "---\n"
        "You run the visual timeline scenario.\n"
    )
    store = Store(h / "nexus.db")
    store.set_preference("model.last", "scripted/m")
    rt = make_runtime(store)
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
