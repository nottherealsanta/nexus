from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.config.paths import project_state_dir
from nexus.core.cancel import CancelToken
from nexus.host_support.user_shell import UserShells
from nexus.model.message import Text
from nexus.tools.permissions import Decision
from nexus.tools.permissions import PathGuard
from nexus.tools.manager import ToolManager
from nexus.tools.spec import ToolCall

from test_session_root_runtime import _project, _runtime


def _result_text(result) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, Text))


async def _call(turn, call_id: str, name: str, args: dict):
    batch = turn.manager.prepare([ToolCall(id=call_id, name=name, input=args)])
    gated = batch.with_decisions({call_id: Decision.ALLOW_ONCE})
    results = await turn.dispatcher.dispatch(
        gated, emit=lambda *_: None, cancel=CancelToken()
    )
    return results[0]


@pytest.mark.asyncio
async def test_real_tools_and_composer_shell_use_session_root(tmp_path: Path) -> None:
    project, linked_a, _ = _project(tmp_path)
    (linked_a / "nested").mkdir()
    (linked_a / "search.txt").write_text("root marker\n", encoding="utf-8")
    runtime = _runtime(project, tmp_path / "home")
    composer = UserShells(runtime)
    try:
        session = runtime.session("root-tools")
        root = runtime.set_session_root("root-tools", linked_a, branch="feature-a")
        turn = runtime._make_tool_turn(
            config=runtime._load_config(), session=session, turn_id="turn-root", attended=True
        )
        assert turn is not None
        assert turn.manager.path_guard.workspace == root.path
        context = turn.dispatcher._ctx_factory(ToolCall("ctx", "read", {"path": "search.txt"}), None)
        assert context.workspace == root.path
        assert context.project_root == project.resolve()
        assert context.state_home == runtime._home

        written = await _call(
            turn, "write-root", "write",
            {"path": "created.txt", "content": "initial\n"},
        )
        assert not written.is_error, _result_text(written)
        assert (linked_a / "created.txt").read_text(encoding="utf-8") == "initial\n"

        edited = await _call(
            turn, "edit-root", "edit",
            {"path": "created.txt", "old_string": "initial", "new_string": "edited"},
        )
        assert not edited.is_error, _result_text(edited)
        assert (linked_a / "created.txt").read_text(encoding="utf-8") == "edited\n"

        found = await _call(
            turn, "grep-root", "grep", {"pattern": "root marker", "path": "."}
        )
        assert not found.is_error, _result_text(found)
        assert "search.txt" in _result_text(found)
        assert str(project) not in _result_text(found)

        denied = await _call(
            turn, "write-project", "write",
            {"path": str(project / "forbidden.txt"), "content": "must not land"},
        )
        assert denied.is_error
        assert not (project / "forbidden.txt").exists()

        moved = await _call(turn, "shell-cd", "shell", {"action": "cd", "path": "nested"})
        assert not moved.is_error, _result_text(moved)
        cwd_file = project_state_dir(project, runtime._home) / "shell" / "root-tools" / "cwd"
        assert cwd_file.exists()
        assert str(linked_a / "nested") in cwd_file.read_text(encoding="utf-8")
        shell_pwd = await _call(turn, "shell-pwd", "shell", {"command": "pwd"})
        assert not shell_pwd.is_error, _result_text(shell_pwd)
        assert str(linked_a / "nested") in _result_text(shell_pwd)
        back_at_root = await _call(
            turn, "shell-back", "shell", {"action": "cd", "path": ".."}
        )
        assert not back_at_root.is_error
        escaped = await _call(
            turn, "shell-escape", "shell", {"action": "cd", "path": ".."}
        )
        assert escaped.is_error
        assert str(linked_a) in cwd_file.read_text(encoding="utf-8")

        # Composer ! always starts at the selected root, independent of shell cd.
        shell_id = composer.start(session, "pwd")
        for _ in range(200):
            completed = next(
                (event for event in reversed(session.events)
                 if event.type == "shell.completed" and event.data.get("shell_id") == shell_id),
                None,
            )
            if completed is not None:
                break
            await asyncio.sleep(0.01)
        assert completed is not None, "composer shell did not finish"
        started = next(
            event for event in session.events
            if event.type == "shell.started" and event.data.get("shell_id") == shell_id
        )
        assert started.data["cwd"] == str(linked_a.resolve())
        assert str(linked_a) in completed.data["output"]
        assert str(project) not in completed.data["output"]
    finally:
        await composer.aclose()
        await runtime.aclose()


@pytest.mark.asyncio
async def test_injected_tool_manager_narrow_write_root_survives_root_selection(
    tmp_path: Path,
) -> None:
    project, linked_a, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")
    try:
        runtime.session("narrow-manager")
        runtime.set_session_root("narrow-manager", linked_a, branch="feature-a")
        config = runtime._load_config()
        injected_guard = PathGuard(project, write_roots=("restricted",))
        runtime._tools = ToolManager(config, workspace=project, path_guard=injected_guard)

        turn = runtime._make_tool_turn(
            config=config,
            session=runtime.session("narrow-manager"),
            turn_id="turn-narrow",
            attended=True,
        )

        assert turn is not None
        assert turn.manager.path_guard.workspace == linked_a.resolve()
        assert turn.manager.path_guard.write_roots == ((linked_a / "restricted").resolve(),)
        assert turn.engine.path_guard.write_roots == turn.manager.path_guard.write_roots
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_settings_agent_guard_cannot_be_replaced_by_session_root_guard(
    tmp_path: Path,
) -> None:
    project, linked_a, _ = _project(tmp_path)
    runtime = _runtime(project, tmp_path / "home")

    class AgentSelector:
        @staticmethod
        def select_tools(_definition, *, available, **_kwargs):
            return SimpleNamespace(selected=frozenset(available))

    try:
        runtime.session("settings-guard")
        runtime.set_session_root("settings-guard", linked_a, branch="feature-a")
        session = runtime.session("settings-guard")
        session._turn_agent_definition = SimpleNamespace(
            profile=None, write_roots=("project",)
        )
        runtime._agents = AgentSelector()
        runtime._extensions = None

        turn = runtime._make_tool_turn(
            config=runtime._load_config(),
            session=session,
            turn_id="turn-settings-guard",
            attended=True,
        )

        assert turn is not None
        assert turn.manager.path_guard.workspace == linked_a.resolve()
        assert turn.manager.path_guard.write_roots == ()
        assert turn.engine.path_guard is turn.manager.path_guard
    finally:
        await runtime.aclose()
