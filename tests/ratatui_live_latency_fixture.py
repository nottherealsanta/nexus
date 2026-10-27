"""Offline real-host frontend latency fixture (responsiveness plan §0).

Seed durable history, then use ScriptedProvider at 50 deltas/s through the actual
host subscription, Python bridge coalescer and native terminal. No external model.
"""
from __future__ import annotations
import argparse
import asyncio
import os
from pathlib import Path
import sys
import tempfile

from mock_llm_fixture import build_runtime, SESSION_ID
from ratatui_fixture import _seed_events
from test_ratatui_integration import FacadeTransport
from nexus.client.protocol import Client
from nexus.events import Event
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.stream import MessageStart, MessageStop, TextDelta
from nexus.ui.ratatui.prototype import run


class Transport(FacadeTransport):
    async def request(self, command):
        if isinstance(command,p.SetupStatus):
            return p.SetupStatusResult(required=False)
        return await super().request(command)


async def main(count, scenario="stream"):
    with tempfile.TemporaryDirectory(prefix="nexus-live-latency-") as temp:
        root=Path(temp)
        os.environ.update(XDG_CONFIG_HOME=str(root/"config"),XDG_STATE_HOME=str(root/"state"),NEXUS_HOME=str(root/"home"))
        runtime,provider,gate=build_runtime(root/"workspace")
        gate.release.set()
        async def tick(_request):
            await asyncio.sleep(.02)
            return TextDelta(" token")
        provider._scripts=[[MessageStart(), *[tick for _ in range(60)], MessageStop("end_turn")]]
        facade=HostFacade(runtime)
        handle=runtime.session(SESSION_ID)
        if scenario == "slow-start":
            original_start = handle.start_turn

            async def delayed_start(*args, **kwargs):
                await asyncio.sleep(1)
                return await original_start(*args, **kwargs)

            handle.start_turn = delayed_start
        if scenario != "stream":
            from nexus.model.providers.scripted import Wait, text_response, tool_response
            if scenario == "shell":
                provider._scripts = [tool_response(("slow", "shell", {"command": "sleep 10"})), text_response("CANCELLED_DONE")]
            elif scenario == "interrupt":
                provider._scripts = [[MessageStart(), TextDelta("RUNNING_MARKER"), Wait()], text_response("CANCELLED_DONE")]
            else:
                provider._scripts = [text_response("MESSAGE_DONE")]
            if scenario == "worktree":
                import subprocess
                for args in [("init", "-b", "main"), ("config", "user.name", "Fixture"),
                             ("config", "user.email", "fixture@example.invalid"),
                             ("config", "commit.gpgsign", "false"), ("add", "."),
                             ("commit", "-m", "fixture")]:
                    subprocess.run(["git", *args], cwd=root/"workspace", check=True, capture_output=True)
                # Exercise the actual first-message tool path, not a reentrant
                # promote call inside start_turn's already-held session lock.
                provider._scripts = [tool_response(("create", "worktree_create", {"reason": "latency fixture", "name": "first"})), text_response("MESSAGE_DONE")]
        source=[event for event in _seed_events("reference") if event.turn=="reference-2"]
        for index in range(count):
            for original in source:
                data=dict(original.data)
                for key in ("queued_id","call_id"):
                    if key in data: data[key]=f"{index}-{data[key]}"
                handle.append_event(Event(type=original.type,data=data,turn=f"history-{index}"))
        async def start():
            await asyncio.sleep(float(os.environ.get("NEXUS_LIVE_SETTLE", "1")))
            if scenario in {"idle", "worktree", "slow-start"}:
                print("READY_FOR_ENTER", file=sys.stderr, flush=True)
                return
            if scenario in {"interrupt", "shell"}:
                await facade.start_turn(SESSION_ID, "Run cancellation fixture")
                await asyncio.sleep(.3)
                queued, _ = await facade.enqueue(SESSION_ID, "PROMOTION_MARKER", mode="steer")
                await asyncio.sleep(.2)
                import time
                began = time.perf_counter()
                await facade.set_queued_mode(SESSION_ID, queued, "interrupt")
                print(f"PROMOTION_REPLY_MS {(time.perf_counter()-began)*1000:.3f}", file=sys.stderr, flush=True)
                await facade.wait_idle(timeout=15)
                print(f"CANCELLATION_IDLE_MS {(time.perf_counter()-began)*1000:.3f}", file=sys.stderr, flush=True)
                return
            await facade.start_turn(SESSION_ID,"Stream the latency fixture")
            await facade.wait_idle(timeout=15)
            print("LIVE STREAM DONE",file=sys.stderr,flush=True)
        task=asyncio.create_task(start())
        try:
            await run(root/"workspace",SESSION_ID,Path("rust/tui/target/debug/nexus-ratatui").resolve(),client=Client(Transport(facade)))
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await runtime.aclose()


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--history",type=int,default=10)
    parser.add_argument("--scenario", choices=["stream", "idle", "worktree", "slow-start", "interrupt", "shell"], default="stream")
    args = parser.parse_args()
    asyncio.run(main(args.history, args.scenario))
