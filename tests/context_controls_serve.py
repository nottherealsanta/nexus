"""Real host/runtime TUI fixture with bounded browser-test geometry telemetry."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from nexus.client.protocol import Client
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.ui.tui.app import NexusTextualApp
from textual.widgets import Button, Static
from test_agent_selection import _config
from test_context_extension_selection import skill, server


class Transport:
    def __init__(self, facade):
        self.facade = facade
        self.calls = []

    async def request(self, command):
        result = await self.facade.handle(command)
        if isinstance(command, (p.ContextExtensionSelect, p.AgentSelect, p.AgentReset)):
            self.calls.append({"command": type(command).__name__, "category": getattr(command, "category", ""), "name": getattr(command, "name", ""), "enabled": getattr(command, "enabled", None), "error": isinstance(result, p.ErrorResult)})
        return result

    def events(self, session, from_seq=0, **kwargs):
        return self.facade.subscribe(session, from_seq, **kwargs)

    async def aclose(self):
        await self.facade.shutdown()


class App(NexusTextualApp):
    async def on_mount(self):
        await super().on_mount()
        self.set_interval(0.1, self.telemetry)

    def telemetry(self):
        widgets = {}
        for widget in self.screen.query("*"):
            if widget.id and (widget.id.startswith("extension-") or widget.id.startswith("context-") or widget.id in {"chat-editor", "root-agent-name"}):
                region = widget.region
                widgets[widget.id] = {"x": region.x, "y": region.y, "width": region.width, "height": region.height, "disabled": widget.disabled}
                if isinstance(widget, Button):
                    widgets[widget.id]["text"] = str(widget.label)
                elif isinstance(widget, Static):
                    widgets[widget.id]["text"] = str(widget.render())
        session = self.controller.session
        handle = self.controller.client.transport.facade.runtime.session(session)
        data = {"widgets": widgets, "session": session, "locked": handle.context_locked, "calls": self.controller.client.transport.calls, "disabled": {key: sorted(value) for key, value in handle.disabled_extensions.items()}, "turns": len(self.controller.view.turns)}
        target = Path(os.environ["NEXUS_CONTEXT_TELEMETRY"])
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.replace(target)


def main():
    with tempfile.TemporaryDirectory(prefix="nx-context-") as directory:
        root = Path(directory)
        workspace, home = root / "project", root / "home"
        workspace.mkdir()
        skill(home / ".nexus", "global-skill")
        skill(workspace / ".agents", "project-skill")
        (home / ".nexus/mcp.json").write_text(json.dumps({"servers": {"global-server": server()}}))
        (workspace / ".agents/mcp.json").write_text(json.dumps({"servers": {"project-server": server()}}))
        runtime = Runtime(workspace, home=home, config=_config(), providers={"scripted": ScriptedProvider(text_response("Done."))})
        App(Client(Transport(HostFacade(runtime))), session="controls").run()


if __name__ == "__main__":
    main()
