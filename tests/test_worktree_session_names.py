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


@pytest.mark.parametrize("bad", ["-lead", "_lead", "has space", "a/b", "dot.name", "x" * 61, "ünï"])
async def test_invalid_names_are_refused(project, tmp_path, bad):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        handle = rt.session("v")
        with pytest.raises(ValueError, match="(Worktree name|name must be)"):
            await rt.promote_session(handle.id, reason="r", name=bad)
        assert rt.session_root(handle.id).kind == "main"
    finally:
        await rt.aclose()


async def test_existing_branch_or_folder_refused_and_good_name_accepted(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        git(project, "branch", "nexus/wt/taken")
        handle = rt.session("v")
        with pytest.raises(ValueError, match="already exists"):
            await rt.promote_session(handle.id, reason="r", name="taken")
        (rt._worktree_root / "worktrees" / "folder").mkdir(parents=True)
        with pytest.raises(ValueError, match="folder"):
            await rt.promote_session(handle.id, reason="r", name="folder")
        good = "A-good_name1" + "z" * 48
        await rt.promote_session(handle.id, reason="r", name=good)
        assert rt.session_root(handle.id).path.name == good
    finally:
        await rt.aclose()


async def test_named_worktree_creates_exact_folder_branch_and_environment(project, tmp_path):  # noqa: F811
    provider = ScriptedProvider(text_response("one"), text_response("two"))
    rt = runtime(project, tmp_path, provider)
    host = HostFacade(rt)
    try:
        handle = rt.session("named")
        result = await host.handle(p.SessionPromote(session=handle.id, reason="login fix"))
        assert not isinstance(result, p.ErrorResult), result
        assert result.state["kind"] == "worktree" and result.state["main_path_display"] == str(project)
        root = rt.session_root(handle.id)
        await rt.detach_session_worktree(handle.id)
        await rt.promote_session(handle.id, reason="again", name="login-fix")
        root = rt.session_root(handle.id)
        assert root.kind == "worktree" and root.path.name == "login-fix"
        assert root.path == (rt._worktree_root / "worktrees" / "login-fix")
        assert root.branch == "nexus/wt/login-fix"
        assert git(project, "branch", "--list", "nexus/wt/login-fix")
        state = rt.session_worktree_status(handle.id)
        assert state["worktree_name"] == "login-fix" and state["branch"] == "nexus/wt/login-fix"
        await drain(handle, "hello")
        await drain(handle, "again")
        assert rt.sessions.summary(handle.id).title == "hello"
        sha = git(project, "rev-parse", "HEAD")
        for index in (0, 1):
            text = _system(provider, index)
            assert "<session_worktree>" in text
            for fact in (f"Worktree directory: {root.path}", f"Main checkout directory: {project}",
                         "Worktree branch: nexus/wt/login-fix", "Base branch: main", f"Base commit: {sha}"):
                assert fact in text, fact
            for step in ("merge --ff-only", "git merge main", "branch nexus/wt/login-fix", "/merge"):
                assert step in text, step
    finally:
        await rt.aclose()


async def test_worktree_and_root_survive_restart_and_replay(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    handle = rt.session("durable")
    sid = handle.id
    await rt.promote_session(sid, reason="keep", name="keep-me")
    await drain(handle, "go")
    await rt.aclose()
    again = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        state = again.session_worktree_status(sid)
        assert state["worktree_name"] == "keep-me" and state["branch"] == "nexus/wt/keep-me"
        assert again.sessions.summary(sid).title == "go"
        assert again.session_root(sid).kind == "worktree"
        listing = again.list_session_worktrees(sid)
        assert listing["current"] == "keep-me"
        assert [row["name"] for row in listing["worktrees"] if row["kind"] == "worktree"] == ["keep-me"]
    finally:
        await again.aclose()


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
    assert record.branch == "nexus/wt/" + record.path.name
    assert record.child_id == "wt-" + record.path.name
    assert record.path.is_dir() and record.lifecycle == "pending"


def test_default_and_subagent_style_allocation_keeps_slug_token(service_parent):
    parent, root, service = service_parent
    record = service.create_session(parent, "plain", "calm otter", root=root)
    assert re.fullmatch(r"calm-otter-[0-9a-f]{6}", record.path.name)


def test_explicit_name_wins_over_random_words(service_parent):
    parent, root, service = service_parent
    record = service.create_session(parent, "exact", "t", root=root, exact_name="my-name", random_words=True)
    assert record.path.name == "my-name" and record.branch == "nexus/wt/my-name"
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
    assert second.branch == "nexus/wt/" + second.path.name


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
    git(parent, "branch", "nexus/wt/zzadj-zzanimal")  # branch only: no folder
    by_branch = service.create_session(parent, "branch", "t", root=root, random_words=True)
    assert by_branch.path.name.startswith("zzadj-zzanimal-")


def test_exhaustion_after_32_pairs_and_8_suffixed_pairs(service_parent, monkeypatch):
    parent, root, service = service_parent
    service.create_session(parent, "warm", "t", root=root, random_words=True)
    monkeypatch.setattr(names, "ADJECTIVES", ("zzadj",))
    monkeypatch.setattr(names, "ANIMALS", ("zzanimal",))
    git(parent, "branch", "nexus/wt/zzadj-zzanimal")
    git(parent, "branch", "nexus/wt/zzadj-zzanimal-abcdef")
    calls = []

    def fixed(nbytes=3):
        calls.append(nbytes)
        return "abcdef"

    monkeypatch.setattr(secrets, "token_hex", fixed)
    with pytest.raises(WorktreeError, match="could not allocate"):
        service.create_session(parent, "full", "t", root=root, random_words=True)
    assert len(calls) == 8
    # Failed allocation publishes nothing: the same session can still be created later.
    git(parent, "branch", "-D", "nexus/wt/zzadj-zzanimal")
    assert service.create_session(parent, "full", "t", root=root, random_words=True).path.name == "zzadj-zzanimal"


async def test_unnamed_create_gets_word_pair_without_becoming_its_title(project, tmp_path):  # noqa: F811
    provider = ScriptedProvider(text_response("x"))
    rt = runtime(project, tmp_path, provider)
    try:
        h = rt.session("auto")
        await rt.promote_session(h.id, reason="isolate")
        await drain(h, "hi")
        root = rt.session_root(h.id)
        assert root.kind == "worktree" and _is_word_pair(root.path.name)
        assert root.branch == "nexus/wt/" + root.path.name
        assert rt.session_worktree_status(h.id)["worktree_name"] == root.path.name
        assert rt.sessions.title_source(h.id) != "user"
        assert rt.sessions.summary(h.id).title != root.path.name
    finally:
        await rt.aclose()


async def test_explicit_name_keeps_first_message_title_and_exact_folder(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("x")))
    try:
        h = rt.session("named2")
        await rt.promote_session(h.id, reason="r", name="exact-one")
        await drain(h, "hi")
        assert rt.session_root(h.id).path.name == "exact-one"
        assert rt.sessions.summary(h.id).title == "hi"
        assert rt.sessions.title_source(h.id) == "first_message"
    finally:
        await rt.aclose()


async def test_every_git_session_gets_the_block_and_a_non_git_workspace_does_not(project, tmp_path):  # noqa: F811
    plain = ScriptedProvider(text_response("x"))
    rt = runtime(project, tmp_path, plain)
    try:
        h = rt.session("plain")
        await drain(h, "hi")
        text = _system(plain)
        assert "<session_worktree>" in text and "Current root: main checkout" in text and "worktree_create" in text
    finally:
        await rt.aclose()
    folder = tmp_path / "not-git"
    folder.mkdir()
    bare = ScriptedProvider(text_response("x"))
    rt = runtime(folder, tmp_path / "other", bare)
    try:
        h = rt.session("nogit")
        await drain(h, "hi")
        assert "<session_worktree>" not in _system(bare)
        assert not {name for name in {t.name for t in bare.requests[0].tools} if name.startswith("worktree_")}
    finally:
        await rt.aclose()


async def test_worktree_move_preserves_explicit_user_rename(project, tmp_path):  # noqa: F811
    rt = runtime(project, tmp_path, ScriptedProvider(text_response("one"), text_response("two")))
    host = HostFacade(rt)
    try:
        handle = rt.session("renamed")
        await drain(handle, "first instruction")
        result = await host.handle(p.SessionRename(session_id=handle.id, title="My chosen title"))
        assert isinstance(result, p.SessionRenameResult)
        await rt.promote_session(handle.id, reason="isolate", name="folder-only")
        await drain(handle, "second instruction")
        assert rt.session_root(handle.id).path.name == "folder-only"
        assert rt.sessions.summary(handle.id).title == "My chosen title"
        assert rt.sessions.title_source(handle.id) == "user"
    finally:
        await rt.aclose()
