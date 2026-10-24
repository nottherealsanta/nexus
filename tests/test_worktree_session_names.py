"""Session worktree names: user-chosen validation, durable name, folder/branch, environment block,
and the automatic random ``<adjective>-<animal>`` names."""
from __future__ import annotations

import re
import secrets

import pytest

from nexus.agents.worktrees import WorktreeError, WorktreeService
from nexus.host import HostFacade, protocol as p
from nexus.model.providers.scripted import ScriptedProvider, text_response
from nexus.session import names
from test_worktree_sessions_create import repository
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


def _is_word_pair(slug):
    adjective, _, animal = slug.partition("-")
    return adjective in names.ADJECTIVES and animal in names.ANIMALS


@pytest.fixture
def service_parent(tmp_path):
    parent = repository(tmp_path / "parent")
    return parent, tmp_path / "daemon" / "worktree-service", WorktreeService()


def test_random_words_names_folder_and_branch_adjective_animal(service_parent):
    parent, root, service = service_parent
    record = service.create_session(parent, "words", "any title", root=root, random_words=True)
    assert _is_word_pair(record.path.name)
    assert record.branch == "nexus/session/" + record.path.name
    assert record.path.is_dir() and record.lifecycle == "pending"


def test_default_and_subagent_style_allocation_keeps_slug_token(service_parent):
    parent, root, service = service_parent
    record = service.create_session(parent, "plain", "calm otter", root=root)
    assert re.fullmatch(r"calm-otter-[0-9a-f]{6}", record.path.name)


def test_explicit_name_wins_over_random_words(service_parent):
    parent, root, service = service_parent
    record = service.create_session(parent, "exact", "t", root=root, exact_name="my-name", random_words=True)
    assert record.path.name == "my-name" and record.branch == "nexus/session/my-name"
    with pytest.raises(ValueError, match="taken"):
        service.create_session(parent, "exact2", "t", root=root, exact_name="my-name", random_words=True)


def test_pair_collision_retries_then_falls_back_to_suffixed_pair(service_parent, monkeypatch):
    parent, root, service = service_parent
    service.create_session(parent, "warm", "t", root=root, random_words=True)  # creates the key first
    monkeypatch.setattr(names, "ADJECTIVES", ("zzadj",))
    monkeypatch.setattr(names, "ANIMALS", ("zzanimal",))
    first = service.create_session(parent, "one", "t", root=root, random_words=True)
    assert first.path.name == "zzadj-zzanimal"
    # Branch-only and folder-only collisions both count; every plain pair is taken now.
    second = service.create_session(parent, "two", "t", root=root, random_words=True)
    assert re.fullmatch(r"zzadj-zzanimal-[0-9a-f]{6}", second.path.name)
    assert second.branch == "nexus/session/" + second.path.name


def test_branch_only_and_folder_only_collisions_are_skipped(service_parent, monkeypatch):
    parent, root, service = service_parent
    service.create_session(parent, "warm", "t", root=root, random_words=True)
    monkeypatch.setattr(names, "ADJECTIVES", ("zzadj",))
    monkeypatch.setattr(names, "ANIMALS", ("zzanimal",))
    folder = root / "worktrees" / "zzadj-zzanimal"
    folder.mkdir()  # folder only: no branch of that name
    by_folder = service.create_session(parent, "folder", "t", root=root, random_words=True)
    assert by_folder.path.name.startswith("zzadj-zzanimal-")
    folder.rmdir()
    git(parent, "branch", "nexus/session/zzadj-zzanimal")  # branch only: no folder
    by_branch = service.create_session(parent, "branch", "t", root=root, random_words=True)
    assert by_branch.path.name.startswith("zzadj-zzanimal-")


def test_exhaustion_after_32_pairs_and_8_suffixed_pairs(service_parent, monkeypatch):
    parent, root, service = service_parent
    service.create_session(parent, "warm", "t", root=root, random_words=True)
    monkeypatch.setattr(names, "ADJECTIVES", ("zzadj",))
    monkeypatch.setattr(names, "ANIMALS", ("zzanimal",))
    git(parent, "branch", "nexus/session/zzadj-zzanimal")
    git(parent, "branch", "nexus/session/zzadj-zzanimal-abcdef")
    calls = []

    def fixed(nbytes=3):
        calls.append(nbytes)
        return "abcdef"

    monkeypatch.setattr(secrets, "token_hex", fixed)
    with pytest.raises(WorktreeError, match="could not allocate"):
        service.create_session(parent, "full", "t", root=root, random_words=True)
    assert len(calls) == 8
    # Failed allocation publishes nothing: the same session can still be created later.
    git(parent, "branch", "-D", "nexus/session/zzadj-zzanimal")
    assert service.create_session(parent, "full", "t", root=root, random_words=True).path.name == "zzadj-zzanimal"


async def test_root_session_gets_word_pair_without_becoming_its_title(project, tmp_path):  # noqa: F811
    provider = ScriptedProvider(text_response("x"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        h = rt.session("auto")
        await drain(h, "hi")
        root = rt.session_root(h.id)
        assert root.kind == "worktree" and _is_word_pair(root.path.name)
        assert root.branch == "nexus/session/" + root.path.name
        assert rt.session_worktree_status(h.id)["worktree_name"] == root.path.name
        assert rt.sessions.title_source(h.id) != "user"
        assert rt.sessions.summary(h.id).title != root.path.name
    finally:
        await rt.aclose()


async def test_forks_get_their_own_word_pair(project, tmp_path):  # noqa: F811
    provider = ScriptedProvider(text_response("parent"), text_response("fork"))
    rt = runtime(project, tmp_path, provider, sessions=True)
    try:
        parent = rt.session("parent")
        await drain(parent, "hello")
        parent_root = rt.session_root(parent.id)
        fork = rt.sessions.fork(parent.id, new_id="fork")
        await drain(fork, "diverge")
        fork_root = rt.session_root(fork.id)
        assert _is_word_pair(parent_root.path.name) and _is_word_pair(fork_root.path.name)
        assert fork_root.path != parent_root.path
        assert rt.sessions.title_source(fork.id) != "user"
    finally:
        await rt.aclose()


async def test_ask_style_set_without_name_creates_word_pair_and_no_user_title(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        h = rt.session("ask")
        state = rt.set_session_worktree(h.id, True)
        assert state["selected"] and state["pending_name"] == ""
        await drain(h, "hi")
        root = rt.session_root(h.id)
        assert root.kind == "worktree" and _is_word_pair(root.path.name)
        assert rt.sessions.title_source(h.id) != "user"
    finally:
        await rt.aclose()


async def test_explicit_name_still_sets_user_title_and_exact_folder(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        h = rt.session("named2")
        rt.set_session_worktree(h.id, True, name="exact-one")
        await drain(h, "hi")
        assert rt.session_root(h.id).path.name == "exact-one"
        assert rt.sessions.summary(h.id).title == "exact-one"
        assert rt.sessions.title_source(h.id) == "user"
    finally:
        await rt.aclose()


async def test_unnamed_worktree_has_no_block_outside_worktrees(project, tmp_path):  # noqa: F811
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
        assert root.kind == "worktree" and _is_word_pair(root.path.name)
        assert rt.session_worktree_status(h.id)["worktree_name"] == root.path.name
        assert "<session_worktree>" in _system(provider)
    finally:
        await rt.aclose()
