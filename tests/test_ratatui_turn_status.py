"""Running-turn status line projection (GROK_TUI_PLAN WS-D)."""
from types import SimpleNamespace as NS

from nexus.ui.ratatui.turn_status import turn_status


def _view(phase="running", tools=(), blocks=(), done=False, tokens=0):
    message = NS(role="assistant", done=done, blocks=list(blocks))
    turn = NS(started_ts=100.0, updated_ts=105.0, tools=list(tools), messages=[message],
              usage=NS(output_tokens=tokens))
    return NS(phase=phase, active_turn=turn)


def test_idle_is_empty():
    assert turn_status(_view(phase="idle")) is None
    assert turn_status(NS(phase="running", active_turn=None)) is None


def test_waiting_then_thinking_then_tool_then_writing():
    waiting = turn_status(_view(tokens=121000))
    assert waiting == {"activity": "Waiting for response", "activity_since": 105.0,
                       "turn_since": 100.0, "output_tokens": 121000, "output_estimated": False}
    think = NS(kind="thinking", finalized=False, started_ts=107.0, text="x")
    assert turn_status(_view(blocks=[think]))["activity"] == "Thinking"
    assert turn_status(_view(blocks=[think]))["activity_since"] == 107.0
    tool = NS(status="running", name="Bash", started_ts=110.0, requested_ts=109.0)
    got = turn_status(_view(tools=[tool]))
    assert (got["activity"], got["activity_since"]) == ("Running Bash", 110.0)
    text = NS(kind="text", finalized=False, started_ts=None, text="hello")
    assert turn_status(_view(blocks=[text]))["activity"] == "Writing"


def test_permission_wait_is_named():
    assert turn_status(_view(phase="awaiting_permission"))["activity"] == "Waiting for permission"


def test_streamed_output_is_estimated_until_usage_catches_up():
    text = NS(kind="text", finalized=False, started_ts=None, text="x" * 400)
    think = NS(kind="thinking", finalized=True, started_ts=1.0, text="y" * 200)
    live = turn_status(_view(blocks=[think, text], tokens=50))
    assert (live["output_tokens"], live["output_estimated"]) == (150, True)
    exact = turn_status(_view(blocks=[think, text], tokens=300))
    assert (exact["output_tokens"], exact["output_estimated"]) == (300, False)
    assert turn_status(_view(tokens=7))["output_estimated"] is False


async def test_provider_summary_is_cached_and_bounded():
    from nexus.ui.ratatui.settings_pages import provider_summary

    calls = []

    class Client:
        async def providers_status(self):
            calls.append(1)
            return NS(providers=[{"connected": True}, {"connected": False}, {"connected": True}])

    wf = NS(client=Client())
    assert await provider_summary(wf) == "2 of 3 connected"
    assert await provider_summary(wf) == "2 of 3 connected"
    assert len(calls) == 1

    class Broken:
        async def providers_status(self):
            raise RuntimeError("down")

    assert await provider_summary(NS(client=Broken())) == ""
