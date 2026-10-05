"""Real scripted harness through the native host adapter, without native terminal."""
import asyncio

import pytest

from nexus.config import Config
from nexus.config.schema import ConfigV2, ModelSection, AgentSection, PermissionsSection, ToolsSection
from nexus.host import HostFacade
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from nexus.runtime import Runtime
from nexus.client.protocol import Client
from nexus.ui.ratatui.controller import NativeController
from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui.prototype import project


class FacadeTransport:
    def __init__(self, facade): self.facade = facade
    async def request(self, command): return await self.facade.handle(command)
    def events(self, session, from_seq=0, *, follow=True, client_id=None):
        return self.facade.subscribe(session, from_seq, follow=follow, client_id=client_id)
    async def aclose(self): pass


@pytest.mark.asyncio
async def test_live_replay_and_tool_details_match_real_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "note.txt").write_text("complete tool output")
    config = Config(model="scripted/native", version=2, v2=ConfigV2(
        model=ModelSection(default="scripted/native"), agent=AgentSection(profile="coding"),
        permissions=PermissionsSection(mode="allow", on_unattended="allow"), tools=ToolsSection()))
    runtime = Runtime(tmp_path, config=config, providers={"scripted": ScriptedProvider(
        tool_response(("read", "Read", {"path": "note.txt"})), text_response("Finished"))})
    facade = HostFacade(runtime)
    controller = NativeController(Client(FacadeTransport(facade)), "native")
    shell = ShellActions(controller)
    received = asyncio.Event()
    async def update(event):
        controller.ingest(event)
        if event.type == "turn.completed": received.set()
    try:
        await controller.bootstrap()
        controller.resume(update)
        await shell.submit("Read the note")
        await asyncio.wait_for(received.wait(), 5)
        live = project(controller, 1, shell=shell)
        assert "complete tool output" in "\n".join(live["lines"])
        assert "Finished" in "\n".join(live["lines"])
        await controller.switch_session("native")
        await controller.bootstrap()
        replay = project(controller, 2, shell=shell)
        assert replay["lines"] == live["lines"]
    finally:
        await controller.close()
        await runtime.aclose()
