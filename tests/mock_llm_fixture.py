"""Deterministic real-runtime mock LLM fixture, without a UI toolkit."""
from __future__ import annotations

from __future__ import annotations

import asyncio

from types import SimpleNamespace

from pathlib import Path

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

from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)

from nexus.model.stream import MessageStop, ThinkingDelta, Usage

from nexus.runtime import Runtime

SESSION_ID = "mock-llm-e2e"

CHILD_AGENT_ID = f"{SESSION_ID}/sub/1"

FIXTURE_NAME = "mock-notes.txt"

FIXTURE_CONTENT = "alpha\nbeta\n"

CHILD_TRANSCRIPT = "Child report: the fixture was read successfully."

FINAL_RESPONSE = "Done: I read and updated the isolated fixture, and checked it in a child task."

USER_PROMPT = "Read mock-notes.txt, replace beta with nexus,\nand verify the result with a child task."

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
