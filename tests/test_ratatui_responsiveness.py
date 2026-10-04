"""Native incremental wire, scheduling and stale reply contracts."""
import asyncio
from types import SimpleNamespace

import pytest

from nexus.ui.ratatui.background import BackgroundActions
from nexus.ui.ratatui.preferences import Preferences
from nexus.ui.ratatui.stream_projection import StreamProjection
from nexus.ui.ratatui.wire import TerminalWire
from nexus.ui_support.native_schedule import UpdateCoalescer


def snapshot(**values):
    return {"revision":1, "generation":1, "blocks":[{"id":"a", "rev":"1", "text":"hello"}],
            "history":["prompt"], "sessions":[{"id":"s"}], "restore":"", "insert":"", **values}


def test_wire_omits_static_sections_appends_history_and_resets():
    wire = TerminalWire()
    first = wire.encode(snapshot())
    assert first["reset"] and first["schema"] == 3
    assert wire.encode(snapshot(revision=2)) is None
    delta = wire.encode(snapshot(revision=3, blocks=[{"id":"a","rev":"2","text":"hello world"}]))
    assert "sessions" not in delta and "history" not in delta
    assert delta["blocks_from"] == 0
    history = wire.encode(snapshot(revision=4, history=["prompt","new"]))
    assert history["history_append"] == ["new"] and "history" not in history
    reset = wire.encode(snapshot(generation=2))
    assert reset["reset"] and "sessions" in reset


def test_wire_preserves_repeated_one_shots_and_clear():
    wire = TerminalWire()
    assert wire.encode(snapshot(insert="marker"))["insert"] == "marker"
    assert wire.encode(snapshot(revision=2, insert="marker"))["insert"] == "marker"
    assert wire.encode(snapshot(revision=3))["insert"] == ""


@pytest.mark.asyncio
async def test_terminal_coalescing_preserves_insert_and_latest_state():
    state = snapshot()
    emitted = []
    wire = TerminalWire()
    async def emit():
        delta = wire.encode(state)
        if delta:
            emitted.append(delta)
        state["insert"] = ""
    coalescer = UpdateCoalescer(emit, delay=10)
    coalescer.schedule()
    state["insert"] = "marker"
    state["revision"] = 2
    coalescer.schedule()
    await coalescer.flush()
    assert len(emitted) == 1 and emitted[0]["insert"] == "marker"
    state["insert"] = "marker"
    coalescer.schedule()
    await coalescer.flush()
    assert emitted[-1]["insert"] == "marker"


@pytest.mark.asyncio
async def test_preferences_write_off_loop_and_flush_latest(tmp_path):
    preferences = Preferences(tmp_path / "tui.json")
    preferences.set("details_tab", "Files")
    preferences.set("details_tab", "Logs")
    assert not preferences.path.exists()
    await preferences.flush()
    assert Preferences(preferences.path).values["details_tab"] == "Logs"


@pytest.mark.asyncio
async def test_completion_is_latest_request_wins(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    async def complete(_client, prefix, _query, **_kwargs):
        if prefix == "old":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()  # simulate an uncancellable late transport reply
        return [prefix]
    monkeypatch.setattr("nexus.ui.ratatui.background.complete", complete)
    shell = SimpleNamespace(generation=1, client=object(), controller=SimpleNamespace(session="s",supported_levels=[]))
    updates = []
    async def update(): updates.append(shell.completion_query)
    background = BackgroundActions(shell, update)
    background.start({"type":"complete","text":"old"})
    await entered.wait()
    background.start({"type":"complete","text":"new"})
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(*list(background.tasks))
    assert shell.completions == ["new"] and updates == ["new"]
    await background.close()


def test_stream_projection_reuses_prefix_and_matches_full_projection(tmp_path, monkeypatch):
    from dataclasses import replace
    from nexus.view.model import BlockView
    from test_ratatui_projection import _turn
    from nexus.ui.ratatui.prototype import project, _project_turn
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.view import initial_state
    monkeypatch.setenv("XDG_CONFIG_HOME",str(tmp_path))
    view = initial_state("s")
    view.turns = [_turn(), replace(_turn(), id="tail", index=1)]
    controller = SimpleNamespace(view=view, session="s")
    shell = ShellActions(controller)
    shell.local_transcript = True
    shell.preferences.values["context_preview"] = False
    first = project(controller,1,shell=shell,literal=False)
    mirror = StreamProjection()
    mirror.remember(first,controller,shell)
    prefix = first["blocks"][0]
    view.turns[-1] = replace(view.turns[-1], messages=[*view.turns[-1].messages[:-1], replace(view.turns[-1].messages[-1],blocks=[BlockView(text="More text")])])
    streamed = mirror.project(controller,shell,2,"tail",_project_turn)
    assert streamed["blocks"][0] is prefix
    full = project(controller,2,shell=shell,literal=False)
    assert streamed["blocks"] == full["blocks"]
    assert mirror.project(controller,shell,3,"other",_project_turn) is None


@pytest.mark.asyncio
async def test_real_mock_fixture_replays_complete_hidden_tool_content(tmp_path, monkeypatch):
    from mock_llm_fixture import build_runtime, SESSION_ID, USER_PROMPT, CHILD_AGENT_ID
    from nexus.host import HostFacade
    from nexus.ui.ratatui.actions import ShellActions
    from nexus.ui.ratatui.prototype import project
    monkeypatch.setenv("XDG_CONFIG_HOME",str(tmp_path/"config"))
    monkeypatch.setenv("XDG_STATE_HOME",str(tmp_path/"state"))
    runtime, _, gate = build_runtime(tmp_path/"workspace")
    facade = HostFacade(runtime)
    facade.open_session(SESSION_ID)
    try:
        gate.release.set()
        await facade.start_turn(SESSION_ID,USER_PROMPT)
        await facade.wait_idle(timeout=15)
        view, _ = facade.state(SESSION_ID)
        controller = SimpleNamespace(view=view,session=SESSION_ID)
        shell = ShellActions(controller)
        shell.local_transcript = True
        shell.preferences.values["context_preview"] = False
        snap = project(controller,1,shell=shell,literal=False)
        members = [member for group in snap["blocks"] if group["kind"]=="tool_group" for member in group["members"]]
        assert any("alpha" in member["local_detail"] for member in members)
        assert any("not found" in member["local_detail"].lower() for member in members)
        assert any(member["members"] for member in members)  # edit diff remains available
        assert any(block.get("operation",{}).get("id")==CHILD_AGENT_ID for block in snap["blocks"] if block.get("operation"))
    finally:
        gate.release.set()
        await runtime.aclose()
