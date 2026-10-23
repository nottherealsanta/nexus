"""User-named session worktrees: validation, durable name, folder/branch, environment block."""
from __future__ import annotations

import re

import pytest

from nexus.host import HostFacade, protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from test_worktree_sessions_runtime import project, runtime, drain, git  # noqa: F401


def _system(provider, index=0):
    system = provider.requests[index].system
    return system if isinstance(system, str) else "\n".join(str(getattr(b, "text", b)) for b in system)


@pytest.mark.parametrize("bad", ["", "-lead", "_lead", "has space", "a/b", "dot.name", "x" * 61, "ünï"])
async def test_invalid_names_are_refused(project, tmp_path, bad):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        handle = rt.session("v")
        if bad == "":
            rt.set_session_worktree(handle.id, True, name=bad)  # empty means unnamed, accepted
            return
        with pytest.raises(ValueError, match="Worktree name"):
            rt.set_session_worktree(handle.id, True, name=bad)
        assert rt.session_worktree_status(handle.id)["pending_name"] == ""
    finally:
        await rt.aclose()


async def test_existing_branch_or_folder_refused_and_good_name_accepted(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        git(project, "branch", "nexus/session/taken")
        handle = rt.session("v")
        with pytest.raises(ValueError, match="already exists"):
            rt.set_session_worktree(handle.id, True, name="taken")
        (rt._worktree_root / "worktrees" / "folder").mkdir(parents=True)
        with pytest.raises(ValueError, match="folder"):
            rt.set_session_worktree(handle.id, True, name="folder")
        state = rt.set_session_worktree(handle.id, True, name="A-good_name1" + "z" * 48)
        assert state["pending_name"] == "A-good_name1" + "z" * 48 and state["selected"]
    finally:
        await rt.aclose()


async def test_named_first_turn_creates_exact_folder_branch_title_and_environment(project, tmp_path):  # noqa: F811
    provider = ScriptedProvider(text_response("one"), text_response("two"))
    rt = runtime(project, tmp_path, provider)
    host = HostFacade(rt)
    try:
        handle = rt.session("named")
        result = await host.handle(p.SessionWorktreeSet(session=handle.id, enabled=True, name="login-fix"))
        assert not isinstance(result, p.ErrorResult), result
        assert result.state["pending_name"] == "login-fix" and result.state["worktree_name"] == ""
        assert result.state["selected"] and result.state["kind"] == "main"
        assert result.state["main_path_display"] == str(project) and result.state["base_branch"] == "main"
        assert not rt._worktree_root.exists()
        await drain(handle, "hello")
        root = rt.session_root(handle.id)
        assert root.kind == "worktree" and root.path.name == "login-fix"
        assert root.path == (rt._worktree_root / "worktrees" / "login-fix")
        assert root.branch == "nexus/session/login-fix"
        assert git(project, "branch", "--list", "nexus/session/login-fix")
        state = rt.session_worktree_status(handle.id)
        assert state["worktree_name"] == "login-fix" and state["pending_name"] == ""
        assert state["branch"] == "nexus/session/login-fix" and state["kind"] == "worktree"
        assert rt.sessions.summary(handle.id).title == "login-fix"
        await drain(handle, "again")
        sha = git(project, "rev-parse", "HEAD")
        for index in (0, 1):
            text = _system(provider, index)
            assert "<session_worktree>" in text
            for fact in (f"Worktree directory: {root.path}", f"Main checkout directory: {project}",
                         "Worktree branch: nexus/session/login-fix", "Base branch: main", f"Base commit: {sha}"):
                assert fact in text, fact
            for step in ("Finishing and merging", 'merge --ff-only', "git merge main",
                         "branch nexus/session/login-fix", "/merge"):
                assert step in text, step
    finally:
        await rt.aclose()


async def test_name_and_worktree_survive_restart_and_replay_keeps_pending_name(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    handle = rt.session("durable")
    sid = handle.id
    rt.set_session_worktree(sid, True, name="keep-me")
    # The selection event carries the name, so replay of the log reproduces it.
    assert rt._session_worktrees.selection(handle)["name"] == "keep-me"
    await drain(handle, "go")
    await rt.aclose()
    again = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        state = again.session_worktree_status(sid)
        assert state["worktree_name"] == "keep-me" and state["branch"] == "nexus/session/keep-me"
        assert again.sessions.summary(sid).title == "keep-me"
        assert again._session_worktrees.selection(again.session(sid))["name"] == "keep-me"
    finally:
        await again.aclose()


async def test_name_refused_after_start_and_ignored_when_disabled(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        handle = rt.session("late")
        state = rt.set_session_worktree(handle.id, False, name="ignored")
        assert state["pending_name"] == "" and not state["selected"]
        await drain(handle, "hi")
        with pytest.raises(ValueError, match="locked"):
            rt.set_session_worktree(handle.id, True, name="too-late")
    finally:
        await rt.aclose()


async def test_unnamed_worktree_keeps_token_suffix_and_has_no_block_outside_worktrees(project, tmp_path):  # noqa: F811
    plain = ScriptedProvider(text_response("x"))
    rt = runtime(project, tmp_path, plain)
    try:
        h = rt.session("plain")
        rt.set_session_worktree(h.id, False)
        await drain(h, "hi")
        assert "<session_worktree>" not in _system(plain) and "/worktrees" not in _system(plain)
    finally:
        await rt.aclose()
    provider = ScriptedProvider(text_response("x"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        h = rt.session("auto")
        await drain(h, "hi")
        root = rt.session_root(h.id)
        assert root.kind == "worktree" and re.fullmatch(r"nexus/session/.+-[0-9a-f]{6}", root.branch)
        assert rt.session_worktree_status(h.id)["worktree_name"] == root.path.name
        assert "<session_worktree>" in _system(provider)
    finally:
        await rt.aclose()
