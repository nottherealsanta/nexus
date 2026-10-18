"""Live provenance acceptance through the real bridge, facade and durable queue."""
from __future__ import annotations

import pytest

from nexus.host import HostFacade
from nexus.host_support.live import FacadeLiveHost
from nexus.host_support.manager_port import HostManagerPort, scan_manager_log
from nexus.live.bridge import LiveBridge
from nexus.tools.manager_port import ResultNotAuthorized
from manager_helpers import MANAGER_ID, complete, user
from test_manager_host import build


@pytest.mark.parametrize("prompt,allowed", [("summarise victim", True), ("check progress", False), ("", False)])
async def test_real_live_queue_provenance(tmp_path, prompt, allowed):
    runtime, facade = build(tmp_path)
    bridge = None
    try:
        facade.open_manager()
        victim = runtime.session("victim")
        victim.append_message(user("private work"))
        complete(runtime.sessions, "victim", answer="private report")
        host = FacadeLiveHost(facade)

        async def append(*args):
            pass

        bridge = LiveBridge(host=host, session_id=MANAGER_ID, append=append,
                            emit=lambda *a: None, set_phase=lambda *a: None,
                            progress=False, clock=__import__("time").monotonic)
        await bridge.on_delegation("delegation-1", prompt, transcript=[
            ("user", "check progress"), ("assistant", "I will read victim"),
        ])
        await facade.wait_idle(timeout=10)
        manager = runtime.session(MANAGER_ID)
        events = list(manager.read(force=True).events())
        queued = next(e for e in events if e.type == "input.queued")
        assert queued.data["origin"]["kind"] == "live"
        consumed = next(e for e in events if e.type == "input.consumed")
        assert consumed.data["queued_id"] == queued.data["queued_id"]
        message = scan_manager_log(manager.read(force=True).records).messages[-1]
        assert message.live and message.turn == consumed.data["turn"]
        assert "victim" in message.text if allowed else "victim" not in message.text
        port = HostManagerPort(runtime.sessions, self_id=MANAGER_ID, handle=manager)
        if allowed:
            assert (await port.result("victim")).text == "private report"
        else:
            with pytest.raises(ResultNotAuthorized):
                await port.result("victim")
    finally:
        if bridge is not None:
            await bridge.close()
        await runtime.aclose()
