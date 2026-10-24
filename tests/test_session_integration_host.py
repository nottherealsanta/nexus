"""User-facing integration preview/confirmation against a real Runtime."""
from nexus.host import HostFacade, protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response, tool_response
from test_worktree_sessions_commits import repository, git
from test_worktree_sessions_runtime import runtime, drain


async def prepared(tmp_path):
    main = repository(tmp_path / "integration-project")
    (main / ".gitignore").write_text(".agents/\n")
    git(main, "add", ".gitignore")
    git(main, "commit", "-qm", "ignore project extensions")
    provider = ScriptedProvider(tool_response(("write", "write", {"path": "saved.txt", "content": "saved\n"})), text_response("done"))
    rt = runtime(main, tmp_path, provider)
    handle = rt.session("integrate-me")
    await rt.promote_session(handle.id, reason="isolate the work")
    events = await drain(handle, "write")
    assert events[-1].type == "turn.completed", events[-1]
    return rt, HostFacade(rt), handle.id, main


async def test_host_integration_requires_fresh_single_use_confirmation(tmp_path):
    rt, facade, session, main = await prepared(tmp_path)
    try:
        before = git(main, "rev-parse", "HEAD")
        preview = await facade.handle(p.SessionIntegrate(session=session))
        assert isinstance(preview, p.SessionWorktreeResult), preview
        assert preview.state["token"]
        assert git(main, "rev-parse", "HEAD") == before
        assert not (main / "saved.txt").exists()
        token = preview.state["token"]
        result = await facade.handle(p.SessionIntegrate(session=session, confirm_token=token))
        assert isinstance(result, p.SessionWorktreeResult), result
        assert (main / "saved.txt").read_text() == "saved\n"
        assert git(main, "status", "--porcelain") == ""
        replay = await facade.handle(p.SessionIntegrate(session=session, confirm_token=token))
        assert isinstance(replay, p.ErrorResult)
        assert "used" in replay.message
    finally:
        await rt.aclose()


async def test_host_integration_stale_session_preview_does_not_change_main(tmp_path):
    rt, facade, session, main = await prepared(tmp_path)
    try:
        preview = await facade.handle(p.SessionIntegrate(session=session))
        assert isinstance(preview, p.SessionWorktreeResult), preview
        before = git(main, "rev-parse", "HEAD")
        worktree = rt.session_root(session).path
        (worktree / "later.txt").write_text("later\n")
        git(worktree, "add", "later.txt")
        git(worktree, "commit", "-qm", "later change")
        result = await facade.handle(p.SessionIntegrate(session=session, confirm_token=preview.state["token"]))
        assert isinstance(result, p.ErrorResult)
        assert "changed" in result.message
        assert git(main, "rev-parse", "HEAD") == before
    finally:
        await rt.aclose()
