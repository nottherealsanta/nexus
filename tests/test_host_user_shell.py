"""Composer ``!`` shell mode: run with bash, add output to context, never start a turn."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.config import Config
from nexus.host import HostFacade
from nexus.host import protocol as p
from nexus.model.message import Text
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.runtime import Runtime
from nexus.view import fold


@pytest.fixture
async def facade(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_OUTPUT_DIR", str(tmp_path / "spill"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = Config.load(workspace, home=tmp_path / "home", environ={})
    runtime = Runtime(workspace, config=config,
                      providers={"scripted": ScriptedProvider(text_response("unused"))})
    host = HostFacade(runtime)
    try:
        yield host
    finally:
        await host.shutdown()
        await runtime.aclose()


async def _completed(handle, timeout=10.0):
    async def _wait():
        while True:
            done = [e for e in handle.events if e.type == "shell.completed"]
            if done:
                return done[-1]
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_wait(), timeout)


def _user_texts(handle) -> list[str]:
    return [
        "".join(b.text for b in m.content if isinstance(b, Text))
        for m in handle.messages if m.role == "user"
    ]


async def test_idle_shell_adds_output_to_context_without_a_turn(facade):
    facade.open_session("s1")
    result = await facade.handle(p.SessionShell(session="s1", command="echo hello-shell; exit 3"))
    assert isinstance(result, p.SessionShellResult)
    handle = facade.runtime.sessions.open("s1")
    event = await _completed(handle)
    assert event.data["exit_code"] == 3 and event.data["context"] == "added"
    assert "hello-shell" in event.data["output"]
    assert not any(e.type == "turn.started" for e in handle.events)
    [text] = _user_texts(handle)
    assert "$ echo hello-shell; exit 3" in text and "hello-shell" in text
    view = fold(handle.events)
    turn = view.turns[-1]
    assert turn.kind == "shell" and turn.messages[0].text == "!echo hello-shell; exit 3"
    [tool] = turn.tools
    assert tool.name == "bash" and tool.status == "failed" and tool.input["command"].startswith("echo")
    assert "hello-shell" in tool.result[0]["text"]


async def test_large_shell_output_is_limited_and_saved(facade, tmp_path):
    facade.open_session("s2")
    await facade.handle(p.SessionShell(session="s2", command="seq 1 5000"))
    handle = facade.runtime.sessions.open("s2")
    event = await _completed(handle)
    path = Path(event.data["output_path"])
    assert path.parent == tmp_path / "spill" and "\n4321\n" in path.read_text()
    assert "[output too large: 5001 lines" in event.data["output"]
    assert str(path) in _user_texts(handle)[0]


async def test_stop_kills_a_running_shell_command(facade):
    facade.open_session("s3")
    await facade.handle(p.SessionShell(session="s3", command="sleep 30"))
    await asyncio.sleep(0.2)
    result = await facade.handle(p.SessionCancel(session="s3"))
    assert result.cancelled is True
    handle = facade.runtime.sessions.open("s3")
    event = await _completed(handle, timeout=8.0)
    assert event.data["status"] == "cancelled"
    assert fold(handle.events).turns[-1].phase == "cancelled"


async def test_empty_shell_command_is_rejected(facade):
    facade.open_session("s4")
    result = await facade.handle(p.SessionShell(session="s4", command="   "))
    assert isinstance(result, p.ErrorResult)


async def test_shell_context_waits_for_a_safe_boundary_during_a_turn(facade):
    facade.open_session("s5")
    handle = facade.runtime.sessions.open("s5")
    handle._active = SimpleNamespace(turn_id="t1")  # a turn owns the session
    try:
        assert handle.add_context("shell output") is False
        assert _user_texts(handle) == []
        # Shell context is appended at the boundary but is not steering.
        assert await handle.consume_steering() is False
        assert _user_texts(handle) == ["shell output"]
    finally:
        handle._active = None


async def test_stop_right_after_submit_still_closes_the_run(facade):
    facade.open_session("s6")
    for _ in range(2):
        await facade.handle(p.SessionShell(session="s6", command="sleep 30"))
    # No yield: the tasks are cancelled before they begin running.
    result = await facade.handle(p.SessionCancel(session="s6"))
    assert result.cancelled is True
    handle = facade.runtime.sessions.open("s6")

    async def _both():
        while len([e for e in handle.events if e.type == "shell.completed"]) < 2:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_both(), 8.0)
    done = [e for e in handle.events if e.type == "shell.completed"]
    assert {e.data["status"] for e in done} == {"cancelled"}
    assert all(turn.phase == "cancelled" for turn in fold(handle.events).turns)
