#!/usr/bin/env python3
"""M0 spike (b): the real model through the real Manager loop and tools (docs/manager-design.md section 16).

    cd nexus-core
    uv run python scripts/spikes/luna_tools.py --copy-auth-from ~/.nexus --yes

It builds a scratch Nexus (never your home) seeded like `scripts/scripted_manager_core.py`: two projects and
sessions in every state, a pending approval and a question. It starts a Manager call in-process on `--model`
(default `copilot/gpt-6-luna`) and says a fixed list of requests to it, one per episode state, at each effort in
`--efforts`. For each request it records which tools the model chose against the expected ones, the sentences it
spoke, whether any id, hash or path was read aloud, and the time to the first sentence and to the end of the turn.

The point is the plan's questions: does it pick the right tool, how does it phrase replies for speech, does it read
ids aloud, and what effort keeps it fast. Acting requests (start, send, answer) are safe: they only touch the
scratch sessions, whose model is the offline mock.

Spends: `len(efforts) x 12` Manager turns, each a few model requests of roughly 3k to 6k input tokens, on the
chosen provider's account. Sends session titles and answers of the *scratch* world only. By running with `--yes` you
acknowledge the Manager's privacy notice for that provider (it is printed first and recorded in the scratch home).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
import _common

# (what the user says, tools the model should call at least one of)
REQUESTS = [
    ("What's going on?", {"overview", "show"}),
    ("What needs me?", {"overview", "session_status", "show"}),
    ("Show me the flaky test question.", {"show", "read_session", "session_status"}),
    ("Find the session about the sidebar.", {"find_sessions"}),
    ("What did the sidebar session say?", {"find_sessions", "read_session"}),
    ("Open the auth refactor.", {"navigate", "find_sessions"}),
    ("Go back.", {"navigate"}),
    ("What am I looking at?", {"current_view"}),
    ("Draw how the client talks to the core.", {"show"}),
    ("Start a session in nexus to fix the dictation cue.", {"start_session"}),
    ("Tell the checkout bug session to add a regression test.", {"send_to_session", "find_sessions"}),
    ("Yes.", {"decide_approval", "session_status", "overview"}),
]
LEAK = re.compile(r"\b(?:ses|per|qst|prj|msg)_[0-9a-f]{6,}\b|\b[0-9a-f]{12,}\b|(?:/[\w.\-]+){3,}")


class FakeViews:
    """The server's per-stream view store, reduced to what the Manager reads."""

    def has_stream(self, stream_id): return True
    def view_for(self, stream_id): return None
    def on_detach(self, listener): pass


async def run(args: argparse.Namespace) -> list[dict]:
    from scripted_manager_core import live, seed

    from nexus.config import ConfigFile
    from nexus.llmhub.types import ToolCall
    from nexus.paths import home
    from nexus.server import make_runtime
    from nexus.store import Store

    config = home() / "config.toml"
    config.write_text(f'[manager]\nenabled = true\nmodel = "{args.model}"\nproactive = "off"\n')
    store = Store(home() / "nexus.db")
    store.set_preference("model.last", "mock/hello")
    rt = make_runtime(store)
    sessions = seed(rt, home() / "projects")
    live(rt, sessions)
    manager = rt.manager
    manager.attach_views(FakeViews())
    print(manager.consent.set(args.model, True)["notice"])
    assert ConfigFile().read()["values"]["manager"]["model"] == args.model
    rows = []
    for effort in args.efforts.split(","):
        await manager.start(f"spike-{effort}", "spike")
        events = rt.bus.open()
        for request, expected in REQUESTS:
            before = len(store.messages(manager.current_episode().id)) if manager.current_episode() else 0
            if manager.current_episode():
                store.update_session(manager.current_episode().id, effort="" if effort == "none" else effort)
            spoken, first, started = [], None, time.perf_counter()
            manager.say(f"spike-{effort}", request, "typed")
            while manager.turn is not None or not manager._queue.empty() or not spoken and time.perf_counter() - started < 1:
                await asyncio.sleep(0.05)
                while True:
                    try:
                        event = await asyncio.wait_for(events.get(), 0.01)
                    except TimeoutError:
                        break
                    if event and event["type"] == "manager.speech":
                        first = first or time.perf_counter() - started
                        spoken.append(event["data"]["text"])
                    if event and event["type"] == "manager.navigate":
                        manager.navigate_result(f"spike-{effort}", event["data"]["request_id"], True, None)
                if time.perf_counter() - started > args.timeout:
                    break
            episode = manager.current_episode()
            calls = [p.name for m in store.messages(episode.id)[before:] for p in m.message.parts if isinstance(p, ToolCall)]
            text = " ".join(spoken)
            row = {"effort": effort, "request": request, "tools": calls, "expected_any": sorted(expected),
                   "tool_ok": bool(expected & set(calls)), "spoken": text, "leaks": LEAK.findall(text),
                   "first_sentence_ms": round((first or 0) * 1000), "total_ms": round((time.perf_counter() - started) * 1000)}
            rows.append(row)
            print(json.dumps(row))
        events.close()
        await manager.stop(f"spike-{effort}")
    await rt.shutdown()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    _common.add_arguments(parser)
    parser.add_argument("--model", default="copilot/gpt-6-luna")
    parser.add_argument("--efforts", default="none,low,medium")
    parser.add_argument("--timeout", type=float, default=90, help="seconds to wait for one turn")
    parser.add_argument("--out", help="write the rows here as JSON")
    args = parser.parse_args()
    efforts = len(args.efforts.split(","))
    _common.prepare(args, spends=f"{efforts * len(REQUESTS)} Manager turns on {args.model} (several requests of 3k-6k "
                                 "input tokens each); the sessions sent are the scratch world's, not yours")
    rows = asyncio.run(run(args))
    for effort in args.efforts.split(","):
        mine = [r for r in rows if r["effort"] == effort]
        ok = sum(r["tool_ok"] for r in mine)
        leaks = sum(bool(r["leaks"]) for r in mine)
        mean = sum(r["first_sentence_ms"] for r in mine) / max(1, len(mine))
        print(f"effort {effort:6}: right tool {ok}/{len(mine)}, ids or paths read aloud in {leaks}, "
              f"mean time to first sentence {mean:.0f} ms")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
