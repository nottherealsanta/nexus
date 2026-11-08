"""Real-provider fork cache check. Scratch NEXUS10_HOME; credentials read from the real ~/.nexus/auth.json."""
import asyncio, json, os, sys, tempfile
from pathlib import Path

scratch = Path(tempfile.mkdtemp(prefix="forkcache-"))
os.environ["NEXUS10_HOME"] = str(scratch / "home")
(scratch / "home").mkdir(mode=0o700)

from nexus.llmhub.auth import store as auth_store  # noqa: E402
auth_store.home = lambda: Path.home() / ".nexus"

from nexus.server import make_runtime  # noqa: E402
from nexus.store import Store  # noqa: E402

TASK = ('Call the `session` tool exactly once with agent "fork", wait true, title "pong" and prompt '
        '"Reply with exactly the word pong and nothing else. Do not call any tool." '
        'After it returns, reply with exactly the word done.')


async def main(model, effort):
    ws = scratch / "proj"; ws.mkdir()
    (ws / "README.md").write_text("fork cache check\n")
    store = Store(scratch / "nexus.db"); store.set_preference("model.last", model)
    rt = make_runtime(store)
    project = rt.open_project(str(ws))
    parent = rt.create_session(project, title="fork cache", model=model, permission="allow", effort=effort,
                               agent="orchestrate", disabled_tools=os.environ.get("DISABLE","").split(",") if os.environ.get("DISABLE") else None)
    rt.prompt(parent.id, TASK)
    for _ in range(1800):
        await asyncio.sleep(0.1)
        if not rt.turns:
            break
    out = {"model": model, "scratch": str(scratch), "sessions": []}
    for s in [parent, *rt.store.children(parent.id)]:
        evs = rt.store.events(s.id, limit=5000)
        types = sorted({e.get("type") for e in evs})
        rows = [e for e in evs if "usage" in json.dumps(e.get("data", {}))[:100000] and e.get("type") not in ("text_delta",)]
        out["sessions"].append({"id": s.id, "fork": getattr(s, "fork", None), "types": types,
                                "usage_events": [{"type": e["type"], "data": {k: v for k, v in e["data"].items() if k in ("usage", "model", "error")}} for e in rows][:12],
                                "errors": [e["data"] for e in evs if e.get("type") in ("session.error", "error")][:3]})
    print(json.dumps(out, indent=1, default=str))

asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
