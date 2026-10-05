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


async def main(count):
    with tempfile.TemporaryDirectory(prefix="nexus-live-latency-") as temp:
        root=Path(temp)
        os.environ.update(XDG_CONFIG_HOME=str(root/"config"),XDG_STATE_HOME=str(root/"state"))
        runtime,provider,gate=build_runtime(root/"workspace")
        gate.release.set()
        async def tick(_request):
            await asyncio.sleep(.02)
            return TextDelta(" token")
        provider._scripts=[[MessageStart(), *[tick for _ in range(60)], MessageStop("end_turn")]]
        facade=HostFacade(runtime)
        handle=runtime.session(SESSION_ID)
        source=[event for event in _seed_events("reference") if event.turn=="reference-2"]
        for index in range(count):
            for original in source:
                data=dict(original.data)
                for key in ("queued_id","call_id"):
                    if key in data: data[key]=f"{index}-{data[key]}"
                handle.append_event(Event(type=original.type,data=data,turn=f"history-{index}"))
        async def start():
            await asyncio.sleep(float(os.environ.get("NEXUS_LIVE_SETTLE", "1")))
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
    asyncio.run(main(parser.parse_args().history))
