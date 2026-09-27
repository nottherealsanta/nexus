"""Direct host starts persist drawable user input and replay it exactly once."""

from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.schema import (
    AgentSection,
    ConfigV2,
    ModelSection,
    PermissionsSection,
    ToolsSection,
)
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime


def _config() -> Config:
    return Config(
        model="scripted/direct-chat",
        version=2,
        v2=ConfigV2(
            model=ModelSection(default="scripted/direct-chat"),
            agent=AgentSection(profile="coding"),
            permissions=PermissionsSection(mode="allow", on_unattended="allow"),
            tools=ToolsSection(),
        ),
    )


@pytest.mark.asyncio
async def test_direct_session_start_user_message_is_durable_and_replayable(tmp_path):
    runtime = Runtime(
        tmp_path,
        config=_config(),
        providers={
            "scripted": ScriptedProvider(
                text_response("assistant reply"), text_response("queued reply")
            )
        },
    )
    facade = HostFacade(runtime)
    facade.open_session("direct-chat")

    try:
        result = await facade.handle(
            p.SessionStart(session="direct-chat", content="visible prompt")
        )
        assert isinstance(result, p.SessionStartResult)
        await facade.wait_idle(timeout=5.0)

        handle = runtime.session("direct-chat")
        input_events = [event for event in handle.events if event.type.startswith("input.")]
        assert [event.type for event in input_events] == ["input.started"]
        started = input_events[0]
        assert started.turn == result.turn_id
        assert started.data["content"][0]["text"] == "visible prompt"

        view, seq = facade.state("direct-chat")
        assert seq == handle.events[-1].seq
        assert len(view.turns) == 1
        assert [message.role for message in view.turns[0].messages] == ["user", "assistant"]
        assert [message.text for message in view.turns[0].messages] == [
            "visible prompt",
            "assistant reply",
        ]

        enqueued = await facade.handle(
            p.SessionEnqueue(session="direct-chat", content="queued prompt")
        )
        assert isinstance(enqueued, p.SessionEnqueueResult)
        await facade.wait_idle(timeout=5.0)

        view, _seq = facade.state("direct-chat")
        assert len(view.turns) == 2
        assert [message.text for message in view.turns[1].messages] == [
            "queued prompt",
            "queued reply",
        ]
        assert sum(
            message.role == "user"
            for turn in view.turns
            for message in turn.messages
        ) == 2

        await runtime.aclose()

        # A new runtime folds the persisted log as a reconnecting host would.
        reopened = Runtime(
            tmp_path, config=_config(), providers={"scripted": ScriptedProvider()}
        )
        try:
            replayed, _seq = HostFacade(reopened).state("direct-chat")
            assert len(replayed.turns) == 2
            assert [
                [message.text for message in turn.messages]
                for turn in replayed.turns
            ] == [
                ["visible prompt", "assistant reply"],
                ["queued prompt", "queued reply"],
            ]
            assert (
                sum(
                    message.role == "user"
                    for turn in replayed.turns
                    for message in turn.messages
                )
                == 2
            )
        finally:
            await reopened.aclose()
    finally:
        if not runtime.closed:
            await runtime.aclose()
