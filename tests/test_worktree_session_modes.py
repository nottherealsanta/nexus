"""Remember three native session modes without changing independent child policy."""
from nexus.host import HostFacade, protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from test_worktree_sessions_runtime import project, runtime, drain  # noqa: F401


async def test_three_modes_persist_and_ask_choice_is_per_session(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")))
    host = HostFacade(rt)
    try:
        handle = rt.session("first")
        state = (await host.handle(p.SessionWorktreeChoice(session=handle.id))).state
        assert state["mode"] == "ask" and not state["chosen"] and not state["enabled"]
        result = await host.handle(p.SessionWorktreeChoice(session=handle.id, mode="always"))
        assert result.state["chosen"] and result.state["enabled"]
        assert not rt._worktree_root.exists()
        other = rt.session("other")
        assert rt.session_worktree_status(other.id)["enabled"]
        result = await host.handle(p.SessionWorktreeChoice(session=handle.id, mode="ask"))
        assert not result.state["chosen"] and not result.state["enabled"]
        await host.handle(p.SessionWorktreeSet(session=handle.id, enabled=False))
        await drain(handle, "hello")
        assert rt.session_worktree_status(handle.id)["chosen"]
        fresh = rt.session("fresh")
        assert not rt.session_worktree_status(fresh.id)["chosen"]
        # Changing the project default never changes an already-started root.
        result = await host.handle(p.SessionWorktreeChoice(session=handle.id, mode="always"))
        assert result.state["kind"] == "main" and not result.state["enabled"]
        assert rt.session_worktree_status(fresh.id)["enabled"]
    finally:
        await rt.aclose()
    reopened = runtime(project, tmp_path, ScriptedProvider(text_response("done")))
    try:
        assert reopened._session_worktrees.mode() == "always"
        assert reopened.session_worktree_status("first")["kind"] == "main"
    finally:
        await reopened.aclose()


async def test_none_overrides_legacy_always_and_invalid_mode_is_refused(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")), sessions=True)
    host = HostFacade(rt)
    try:
        handle = rt.session("mode")
        assert rt.session_worktree_status(handle.id)["mode"] == "always"
        result = await host.handle(p.SessionWorktreeChoice(session=handle.id, mode="none"))
        assert result.state["chosen"] and not result.state["enabled"]
        invalid = await host.handle(p.SessionWorktreeChoice(session=handle.id, mode="sometimes"))
        assert isinstance(invalid, p.ErrorResult)
        assert rt.session_worktree_status(handle.id)["mode"] == "none"
    finally:
        await rt.aclose()


async def test_shell_cannot_edit_main_before_selected_worktree_is_prepared(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("done")), sessions=True)
    try:
        handle = rt.session("shell")
        result = await HostFacade(rt).handle(p.SessionShell(session=handle.id, command="touch accidental.txt"))
        assert isinstance(result, p.ErrorResult)
        assert "first prompt" in result.message
        assert not (project / "accidental.txt").exists()
    finally:
        await rt.aclose()
