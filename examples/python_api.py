"""Drive the host facade in-process with an offline provider.

The facade (`nexus.host.HostFacade`) is the transport-neutral surface every UI
sits on. This example uses it directly, with no daemon and no credentials: the
`ScriptedProvider` replays a tool call and a text reply deterministically.

Run from the project root:

    python3 examples/python_api.py

It creates a throwaway workspace, runs one turn, approves the gated Write, and
prints the resulting conversation view.
"""
from __future__ import annotations

import asyncio
import tempfile
from contextlib import aclosing
from pathlib import Path

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.model.providers.scripted import (
    ScriptedProvider,
    text_response,
    tool_response,
)
from nexus.runtime import Runtime
from nexus.tools.permissions import Decision

TERMINAL = {"turn.completed", "turn.failed", "turn.cancelled"}


def build_runtime(workspace: Path) -> Runtime:
    """A runtime whose only provider is a deterministic offline script."""
    provider = ScriptedProvider(
        tool_response(
            ("call-1", "Write", {"path": "notes.txt", "content": "hello from Nexus"})
        ),
        text_response("Wrote notes.txt."),
    )
    config = Config(
        model="scripted/m",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/m"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="ask", on_unattended="deny"),
            tools=ToolsSection(),
        ),
    )
    return Runtime(workspace, config=config, providers={"scripted": provider})


async def main() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="nexus-example-"))
    runtime = build_runtime(workspace)
    facade = HostFacade(runtime, owns_runtime=True)
    try:
        facade.open_session("demo")

        # Subscribe *before* the turn: opening the stream registers this view,
        # which makes the run attended, so an `ask` rule prompts instead of
        # falling through to the unattended policy.
        async with aclosing(
            facade.subscribe("demo", from_seq=0, follow=True, client_id="example")
        ) as events:
            first = await events.__anext__()  # attaching registers the view
            print(f"  {first.seq:>3} {first.type}")

            turn_id = await facade.start_turn("demo", "Write a short note.")
            print(f"started {turn_id}")

            async for event in events:
                print(f"  {event.seq:>3} {event.type}")
                if event.type == "permission.requested":
                    # Any view may answer; the first responder wins.
                    resolved = facade.resolve_permission(
                        "demo", event.data["id"], Decision.ALLOW_ONCE
                    )
                    print(f"      approved={resolved}")
                if event.type in TERMINAL:
                    break

        view, seq = facade.state("demo")
        print(f"\nview at seq {seq}:")
        for message in view.messages:
            print(f"  {message.role}: {message.text}")
        print(f"\nwrote: {(workspace / 'notes.txt').read_text(encoding='utf-8')!r}")
    finally:
        await runtime.aclose()
        print(f"workspace: {workspace}")


if __name__ == "__main__":
    asyncio.run(main())
