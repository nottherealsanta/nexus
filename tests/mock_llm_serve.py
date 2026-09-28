"""Run the real Textual Nexus app against a deterministic local model script.

The scripted provider enters the normal Runtime, HostFacade, Client, reducer and
Textual widget pipeline. The child response is gated by an asyncio event so a
browser can inspect genuine pending Task/Read activity before completion.
Everything is local to this test process and its disposable workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from textual.widgets import Button, Static

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    AgentsSection,
    ConfigV2,
    ExtSection,
    HooksSection,
    MCPSection,
    ModelParams,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.model.stream import MessageStop, ThinkingDelta, Usage
from nexus.runtime import Runtime
from nexus.ui.cli.client import Client
from nexus.ui.tui.app import NexusTextualApp
from nexus.ui.tui.timeline import (
    TaskActivityWidget,
    ToolActivityWidget,
    TurnWidget,
    UserMessage,
)
from nexus.util import new_id

SESSION_ID = "mock-llm-e2e"
CHILD_AGENT_ID = f"{SESSION_ID}/sub/1"
FIXTURE_NAME = "mock-notes.txt"
FIXTURE_CONTENT = "alpha\nbeta\n"
CHILD_TRANSCRIPT = "Child report: the fixture was read successfully."
FINAL_RESPONSE = "Done: I read and updated the isolated fixture, and checked it in a child task."
USER_PROMPT = "Read mock-notes.txt, replace beta with nexus,\nand verify the result with a child task."


def _style_color_hex(style: Any) -> str | None:
    """Normalize Rich/Textual foreground values across supported versions."""
    if style is None:
        return None
    foreground = getattr(style, "foreground", style)
    value = getattr(foreground, "hex", foreground)
    if isinstance(value, str):
        match = re.search(r"#[0-9a-fA-F]{6}", value)
        return match.group(0) if match else value
    return str(value)


class MockGate:
    def __init__(self) -> None:
        self.pending = asyncio.Event()
        self.release = asyncio.Event()


def _config() -> Config:
    return Config(
        model="scripted/nexus-e2e-model",
        version=2,
        v2=ConfigV2(
            model=ModelSection(
                default="scripted/nexus-e2e-model",
                params=ModelParams(thinking_budget=2048),
            ),
            agent=AgentSection(profile="coding"),
            agents=AgentsSection(enabled=True, max_depth=2, max_concurrent=1),
            permissions=PermissionsSection(mode="allow", on_unattended="allow", write_roots=["./"]),
            tools=ToolsSection(),
            ext=ExtSection(enabled=False),
            hooks=HooksSection(enabled=False),
            mcp=MCPSection(enabled=False),
        ),
    )


def build_runtime(workspace: Path, gate: MockGate | None = None) -> tuple[Runtime, ScriptedProvider, MockGate]:
    gate = gate or MockGate()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / FIXTURE_NAME).write_text(FIXTURE_CONTENT, encoding="utf-8")
    agent_dir = workspace / ".nexus" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "general.md").write_text(
        "---\n"
        "name: general\n"
        "description: Mock E2E root agent.\n"
        "contexts: [root, subagent]\n"
        "model: scripted/nexus-e2e-model\n"
        "reasoning_effort: medium\n"
        "color: #4F8EF7\n"
        "---\n"
        "Use the available tools to complete the request.\n",
        encoding="utf-8",
    )

    async def child_final(_request):
        gate.pending.set()
        await asyncio.wait_for(gate.release.wait(), timeout=120)
        return [
            ThinkingDelta(text="I will inspect the fixture before changing it."),
            *text_response(CHILD_TRANSCRIPT, usage=Usage(input=17, output=9, reasoning=4)),
        ]

    provider = ScriptedProvider(
        [
            *tool_response(
                ("root-read", "Read", {"path": FIXTURE_NAME}),
                usage=Usage(input=31, output=7, reasoning=5),
            )[:-1],
            MessageStop(stop_reason="tool_use"),
        ],
        tool_response(("root-edit", "Edit", {
            "path": FIXTURE_NAME, "old_string": "beta", "new_string": "nexus"
        })),
        tool_response(("root-read-error", "Read", {"path": "missing-from-fixture.txt"})),
        tool_response(("root-task", "Task", {
            "prompt": "Read mock-notes.txt and report its contents.",
            "subagent_type": "general",
            "tools": ["Read"],
            "description": "Verify the edited fixture",
        })),
        tool_response(("child-read", "Read", {"path": FIXTURE_NAME})),
        [child_final],
        text_response(FINAL_RESPONSE, usage=Usage(input=48, output=16)),
        text_response(FINAL_RESPONSE, usage=Usage(input=52, output=18)),
    )
    runtime = Runtime(workspace, config=_config(), providers={"scripted": provider})
    runtime._registry = SimpleNamespace(
        get=lambda _reference: SimpleNamespace(reasoning_efforts=("medium",))
    )
    runtime._assembler._registry = runtime._registry
    return runtime, provider, gate


class FacadeTransport:
    def __init__(self, facade: HostFacade) -> None:
        self.facade = facade

    async def request(self, command: p.Command) -> p.Result:
        return await self.facade.handle(command)

    def events(self, session: str, from_seq: int = 0, *, follow: bool = True, client_id: str | None = None) -> AsyncIterator:
        return self.facade.subscribe(session, from_seq, follow=follow, client_id=client_id)

    async def aclose(self) -> None:
        return None


class MockTextualApp(NexusTextualApp):
    def __init__(self, facade: HostFacade, provider: ScriptedProvider, gate: MockGate, control_port: int) -> None:
        super().__init__(Client(FacadeTransport(facade)), session=SESSION_ID)
        self.mock_facade = facade
        self.mock_provider = provider
        self.mock_gate = gate
        self.control_port = control_port
        self._control_server: asyncio.Server | None = None
        # Keep the checked-in nested-runtime path executable without changing production.
        import nexus.runtime as runtime_module
        self._old_runtime_new_id = getattr(runtime_module, "new_id", None)
        runtime_module.new_id = new_id

    async def on_mount(self) -> None:
        await super().on_mount()
        self._control_server = await asyncio.start_server(self._control_request, "127.0.0.1", self.control_port)

    async def on_unmount(self) -> None:
        if self._control_server is not None:
            self._control_server.close()
            await self._control_server.wait_closed()
        import nexus.runtime as runtime_module
        if self._old_runtime_new_id is None:
            del runtime_module.new_id
        else:
            runtime_module.new_id = self._old_runtime_new_id
        await super().on_unmount()

    def _snapshot(self) -> dict[str, Any]:
        base = self.screen_stack[0]
        conversation = self.controller.view
        turns = list(base.query(TurnWidget))
        rows = []
        for turn in turns:
            for widget in turn._items.values():
                row = {
                    "type": type(widget).__name__,
                    "call_id": getattr(widget, "call_id", None),
                    "name": getattr(getattr(widget, "tool", None), "name", None),
                    "header": "", "detail": "", "expanded_text": "",
                    "expanded": getattr(widget, "expanded", False),
                    "child_metrics": "", "region": None, "child_link_region": None,
                }
                if widget.region is not None:
                    region = widget.region
                    row["region"] = [region.x, region.y, region.width, region.height]
                if isinstance(widget, ToolActivityWidget):
                    row["header"] = str(widget.query_one("#tool-header").render())
                    row["detail"] = ""
                    row["expanded_text"] = widget._details_text()
                    if isinstance(widget, TaskActivityWidget) and widget.tool.child_agent_ids:
                        row["child_metrics"] = widget._details_text()
                rows.append(row)
        users = list(base.query(UserMessage))
        root_agent = base.query_one("#root-agent")
        context_usage = base.query_one("#context-usage")
        completion = base.query_one("#completion-popup")
        editor = base.query_one("#chat-editor")
        composer = base.query_one("#chat-input")
        rendered_agent = root_agent.summary()
        child = self.controller.find_agent(CHILD_AGENT_ID)
        child_view = None
        if child is not None:
            child_view = child.to_dict()
            child_view["body"] = child.body.to_dict()
        return {
            "running": self.controller.running,
            "gate_pending": self.mock_gate.pending.is_set(),
            "provider_calls": self.mock_provider.calls,
            "view": conversation.to_dict(),
            "controller_view": conversation.to_dict(),
            "messages": [message.to_dict() for message in conversation.messages],
            "child_transcript": child_view,
            "metadata": self.mock_facade.current_agent_metadata(SESSION_ID),
            "fixture": (self.mock_facade.runtime.workspace / FIXTURE_NAME).read_text(encoding="utf-8"),
            "ui": {
                "status": str(base.query_one("#connection-status").render()),
                "root_agent": root_agent.summary().plain,
                "root_agent_spans": [
                    {
                        "text": rendered_agent.plain[span.start:span.end],
                        "color": _style_color_hex(span.style),
                    }
                    for span in rendered_agent.spans
                ],
                "root_agent_color": _style_color_hex(rendered_agent.spans[0].style),
                "root_agent_region": [root_agent.region.x, root_agent.region.y, root_agent.region.width, root_agent.region.height],
                "context_usage": context_usage.render().plain if isinstance(context_usage.render(), Static) else str(context_usage.render()),
                "context_region": [context_usage.region.x, context_usage.region.y, context_usage.region.width, context_usage.region.height],
                "completion_visible": completion.display,
                "completion_text": completion.render().plain if isinstance(completion.render(), Static) else str(completion.render()),
                "completion_region": [completion.region.x, completion.region.y, completion.region.width, completion.region.height] if completion.region is not None else None,
                "editor_text": editor.text,
                "editor_region": [editor.region.x, editor.region.y, editor.region.width, editor.region.height],
                "composer_border_left": repr(composer.styles.border_left),
                "composer_border_color": composer.styles.border_left[1].hex,
                "composer_region": [composer.region.x, composer.region.y, composer.region.width, composer.region.height],
                "user_padding": [users[0].styles.padding.top, users[0].styles.padding.right, users[0].styles.padding.bottom, users[0].styles.padding.left] if users else [],
                "user_region": [users[0].region.x, users[0].region.y, users[0].region.width, users[0].region.height] if users else [],
                "app_title_count": len(base.query("#app-title")),
                "screen": type(self.screen).__name__,
                "screen_grid": [self.size.width, self.size.height],
                "editor_border": repr(base.query_one("#chat-editor").styles.border),
                "user_background": repr(users[0].styles.background) if users else "",
                "turn_renderings": [turn.render() for turn in turns],
                "button_labels": [str(button.label) for button in base.query("Button")],
                "turn_items": rows,
                "cards": rows,
                "reasoning": [block.text for message in conversation.messages for block in message.blocks if block.kind == "thinking"],
            },
        }

    async def _control_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        path = ""
        try:
            request_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            method, path, _version = request_head.split(b"\r\n", 1)[0].decode("ascii").split()
            if method == "GET" and path == "/health":
                status, body = 200, {"ok": True}
            elif method == "GET" and path == "/gate":
                base = self.screen_stack[0]
                task = next((item for item in base.query(TaskActivityWidget) if item.call_id == "root-task"), None)
                child = self.controller.find_agent(CHILD_AGENT_ID)
                status, body = 200, {
                    "running": self.controller.running,
                    "pending": self.mock_gate.pending.is_set(),
                    "provider_calls": self.mock_provider.calls,
                    "task_header": str(task.query_one("#tool-header").render()) if task else "",
                    "task_status": task.tool.status if task else "",
                    "child_status": child.status if child else "",
                }
            elif method == "GET" and path == "/state":
                status, body = 200, self._snapshot()
            elif method == "GET" and path == "/screen":
                status, body = 200, {"screen": type(self.screen).__name__}
            elif method == "GET" and path == "/inspector":
                screen = self.screen_stack[-1]
                if type(screen).__name__ != "AgentTranscriptScreen":
                    status, body = 409, {"screen": type(screen).__name__}
                else:
                    timeline = screen.query_one("#agent-timeline")
                    await timeline.set_view(screen.agent.body)
                    parts = [getattr(w, "_markdown", "") for w in timeline.query("Markdown")]
                    parts += [str(w.render()) for w in timeline.query("Static")]
                    status, body = 200, {
                        "screen": type(screen).__name__,
                        "heading": str(screen.query_one("#agent-inspector-heading").render()),
                        "transcript": "\n".join(part for part in parts if part),
                    }
            elif method == "POST" and path == "/release":
                self.mock_gate.release.set()
                status, body = 200, {"released": True}
            elif method == "POST" and path.startswith("/ui/tool-detail/"):
                call_id = path.rsplit("/", 1)[-1]
                widget = next(item for item in self.screen_stack[0].query(ToolActivityWidget) if item.call_id == call_id)
                await widget.open_details()
                status, body = 200, {"opened": type(self.screen).__name__ == "ToolDetailsScreen"}
            elif method == "POST" and path.startswith("/ui/tool-agent/"):
                call_id = path.rsplit("/", 1)[-1]
                widget = next(item for item in self.screen_stack[0].query(ToolActivityWidget) if item.call_id == call_id)
                if isinstance(widget, TaskActivityWidget):
                    child = next(iter(widget._children()), None)
                    if child is not None:
                        from nexus.ui.tui.messages import AgentOpenRequested

                        self.screen.dismiss(None)
                        await self._agent_open_requested(AgentOpenRequested(child.id))
                status, body = 200, {"opened": type(self.screen).__name__ == "AgentTranscriptScreen"}
            elif method == "POST" and path == "/ui/focus-editor":
                self.screen_stack[0].query_one("#chat-editor").focus()
                status, body = 200, {"focused": True}
            elif method == "POST" and path == "/ui/agent-transcript":
                await self.action_open_agent(CHILD_AGENT_ID)
                status, body = 200, {"opened": type(self.screen).__name__ == "AgentTranscriptScreen"}
            elif method == "POST" and path.startswith("/ui/tool-agent/"):
                widget = next(item for item in self.screen_stack[0].query(ToolActivityWidget) if item.call_id == path.rsplit("/", 1)[-1])
                await widget.open_details()
                self.screen.dismiss(None)
                await self.action_open_agent(CHILD_AGENT_ID)
                status, body = 200, {"opened": type(self.screen).__name__ == "AgentTranscriptScreen"}
            elif method == "POST" and path.startswith("/ui/tool-agent/"):
                call_id = path.rsplit("/", 1)[-1]
                widget = next(item for item in self.screen_stack[0].query(ToolActivityWidget) if item.call_id == call_id)
                await widget.open_details()
                self.screen.dismiss(None)
                await self.action_open_agent(CHILD_AGENT_ID)
                status, body = 200, {"opened": type(self.screen).__name__ == "AgentTranscriptScreen"}
            elif method == "POST" and path == "/ui/agent-transcript/click":
                link = next(iter(self.screen_stack[0].query(TaskActivityWidget)[0].query(Button)))
                link.press()
                status, body = 200, {"pressed": True}
            elif method == "POST" and path == "/ui/back":
                self.action_back_from_agent()
                status, body = 200, {"screen": type(self.screen).__name__}
            else:
                status, body = 404, {"error": "not found"}
        except Exception as exc:  # noqa: BLE001 - test control reports failures as responses
            status, body = 500, {"error": f"{type(exc).__name__}: {exc}", "path": path}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reason = "OK" if status == 200 else "Internal Server Error" if status == 500 else "Not Found"
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n".encode("ascii") + payload
        )
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


def create_app(workspace: Path, control_port: int) -> tuple[MockTextualApp, Runtime, ScriptedProvider, MockGate]:
    runtime, provider, gate = build_runtime(workspace)
    facade = HostFacade(runtime)
    return MockTextualApp(facade, provider, gate, control_port), runtime, provider, gate


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", action="store_true", help="run the Textual Web child app")
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--control-port", type=int, default=None)
    args = parser.parse_args(argv)
    if not args.app:
        parser.error("--app is required when launching the Textual app")
    workspace = args.workspace or Path(os.environ.get("NEXUS_MOCK_WORKSPACE", "/tmp/nexus-mock-llm"))
    control_port = args.control_port or int(os.environ.get("NEXUS_MOCK_CONTROL_PORT", "8765"))
    app, _runtime, _provider, _gate = create_app(workspace, control_port)
    try:
        app.run()
    except BaseException:
        (workspace / ".mock-textual-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
