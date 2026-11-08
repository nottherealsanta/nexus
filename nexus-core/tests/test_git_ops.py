"""Offline Git control-plane primitives against disposable repositories."""
import subprocess

import pytest

from nexus import git_ops
from nexus.worktrees import WorktreeError


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Tester")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "tracked").write_text("initial\n")
    git(root, "add", "tracked")
    git(root, "commit", "-qm", "initial")
    return root


async def test_status_stage_unstage_literal_names_and_registered_checkout(repo, tmp_path):
    (repo / "tracked").write_text("changed\n")
    (repo / "[literal]").write_text("new\n")
    (repo / "l").write_text("other\n")
    before = await git_ops.status(repo, repo)
    assert {c["path"] for c in before["changes"]} == {"tracked", "[literal]", "l"}
    staged = await git_ops.index(repo, repo, "stage", ["[literal]", "tracked"])
    by_path = {c["path"]: c for c in staged["changes"]}
    assert by_path["[literal]"]["staged"] and by_path["tracked"]["staged"]
    assert not by_path["l"]["staged"]
    unstaged = await git_ops.index(repo, repo, "unstage", ["tracked"])
    by_path = {c["path"]: c for c in unstaged["changes"]}
    assert not by_path["tracked"]["staged"] and by_path["tracked"]["unstaged"]
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    with pytest.raises(WorktreeError, match="registered"):
        await git_ops.index(repo, other, "stage", ["tracked"])
    for path in ("../other", "/tmp/file", ".git/config", ""):
        with pytest.raises(WorktreeError):
            await git_ops.index(repo, repo, "stage", [path])


async def test_linked_worktree_is_accepted_but_subdirectory_is_not(repo, tmp_path):
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "branch", str(linked))
    (linked / "file").write_text("worktree\n")
    result = await git_ops.index(repo, linked, "stage", ["file"])
    assert result["changes"][0]["staged"]
    with pytest.raises(WorktreeError):
        await git_ops.action(repo, repo, "branch_delete_force", value="branch")
    (linked / "subdir").mkdir()
    with pytest.raises(WorktreeError, match="registered"):
        await git_ops.status(repo, linked / "subdir")


async def test_index_refuses_in_progress_merge(repo):
    (repo / "tracked").write_text("changed\n")
    (repo / ".git" / "MERGE_HEAD").write_text(git(repo, "rev-parse", "HEAD"))
    with pytest.raises(WorktreeError, match="in progress"):
        await git_ops.index(repo, repo, "stage", ["tracked"])
    (repo / ".git" / "MERGE_HEAD").unlink()
    assert (await git_ops.index(repo, repo, "stage", ["tracked"]))["changes"][0]["staged"]


async def test_commit_branch_switch_and_safe_delete(repo):
    (repo / "tracked").write_text("changed\n")
    await git_ops.index(repo, repo, "stage", ["tracked"])
    await git_ops.action(repo, repo, "commit", value="record work")
    assert git(repo, "log", "-1", "--format=%s").strip() == "record work"
    main = git(repo, "branch", "--show-current").strip()
    await git_ops.action(repo, repo, "branch_create", value="feature")
    await git_ops.action(repo, repo, "switch", value="feature")
    (repo / "tracked").write_text("feature\n")
    await git_ops.index(repo, repo, "stage", ["tracked"])
    await git_ops.action(repo, repo, "commit", value="feature work")
    await git_ops.action(repo, repo, "switch", value=main)
    with pytest.raises(WorktreeError, match="not fully merged"):
        await git_ops.action(repo, repo, "branch_delete", value="feature")
    for bad in ("--detach", "@{-1}", "bad..branch", "refs/heads/../escape"):
        with pytest.raises(WorktreeError):
            await git_ops.action(repo, repo, "switch", value=bad)


async def test_pull_is_fast_forward_only_with_a_local_bare_remote(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    branch = git(repo, "branch", "--show-current").strip()
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-qu", "origin", branch)
    peer = tmp_path / "peer"
    git(tmp_path, "clone", "-q", "-b", branch, str(remote), str(peer))
    git(peer, "config", "user.name", "Tester")
    git(peer, "config", "user.email", "test@example.invalid")
    (peer / "peer").write_text("remote\n")
    git(peer, "add", "peer")
    git(peer, "commit", "-qm", "remote")
    git(peer, "push", "-q")
    await git_ops.action(repo, repo, "pull")
    assert (repo / "peer").read_text() == "remote\n"
    (repo / "local").write_text("local\n")
    git(repo, "add", "local")
    git(repo, "commit", "-qm", "local")
    (peer / "peer2").write_text("remote again\n")
    git(peer, "add", "peer2")
    git(peer, "commit", "-qm", "remote again")
    git(peer, "push", "-q")
    with pytest.raises(WorktreeError, match="fast-forward"):
        await git_ops.action(repo, repo, "pull")
    assert not (repo / "peer2").exists()
    with pytest.raises(WorktreeError):
        await git_ops.action(repo, repo, "push")
    assert git(peer, "rev-parse", "HEAD").strip() == git(repo, "rev-parse", "origin/" + branch).strip()


async def test_unborn_checkout_can_stage_and_unstage(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "new").write_text("first\n")
    staged = await git_ops.index(tmp_path, tmp_path, "stage", ["new"])
    assert staged["changes"][0]["staged"]
    unstaged = await git_ops.index(tmp_path, tmp_path, "unstage", ["new"])
    assert unstaged["changes"][0]["status"] == "??"


async def test_status_preserves_rename_paths_with_newlines(repo):
    old = repo / "tracked"
    new = repo / "new\nname"
    old.rename(new)
    git(repo, "add", "-A")
    changes = (await git_ops.status(repo, repo))["changes"]
    assert changes == [{"path": "new\nname", "old_path": "tracked", "status": "R ",
                        "staged": True, "unstaged": False}]


async def test_default_worktree_accepts_project_subdirectory(repo):
    subdir = repo / "nested"
    subdir.mkdir()
    assert await git_ops.default_worktree(subdir) == repo.resolve()


async def test_discard_restores_tracked_deletes_only_regular_untracked(repo, tmp_path):
    (repo / "tracked").write_text("changed\n")
    (repo / "new").write_text("untracked\n")
    result = await git_ops.discard(repo, repo, ["tracked", "new"])
    assert result["changes"] == []
    assert (repo / "tracked").read_text() == "initial\n" and not (repo / "new").exists()
    outside = tmp_path / "outside"
    outside.write_text("safe\n")
    (repo / "link").symlink_to(outside)
    with pytest.raises(WorktreeError, match="regular"):
        await git_ops.discard(repo, repo, ["link"])
    assert outside.read_text() == "safe\n" and (repo / "link").is_symlink()


async def test_upstream_proposal_requires_explicit_confirmation(repo, tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    proposal = (await git_ops.status(repo, repo))["push_upstream"]
    assert proposal == {"remote": "origin", "branch": git(repo, "branch", "--show-current").strip()}
    with pytest.raises(WorktreeError, match="explicit confirmation"):
        await git_ops.action(repo, repo, "push")
    await git_ops.action(repo, repo, "push", value="origin")
    assert (await git_ops.status(repo, repo))["push_upstream"] is None


async def test_git_output_limits_are_explicit(repo, tmp_path, monkeypatch):
    from nexus import worktrees

    fake = tmp_path / "bin"
    fake.mkdir()
    executable = fake / "git"
    executable.write_text("#!/bin/sh\nexec /usr/bin/yes x\n")
    executable.chmod(0o755)
    monkeypatch.setitem(worktrees.ENV, "PATH", str(fake))
    with pytest.raises(WorktreeError, match="stdout exceeded"):
        await worktrees._git("anything", cwd=repo)
    executable.write_text("#!/bin/sh\nexec /usr/bin/yes x 1>&2\n")
    with pytest.raises(WorktreeError, match="stderr exceeded"):
        await worktrees._git("anything", cwd=repo)
    executable.write_text("#!/bin/sh\nexec /bin/sleep 5\n")
    with pytest.raises(WorktreeError, match="timed out"):
        await worktrees._git("anything", cwd=repo, timeout=0.05)


async def test_status_announces_omitted_rows(repo, monkeypatch):
    from nexus import worktrees

    original = worktrees._git

    async def many(*args, **kwargs):
        if args[:1] == ("status",):
            return "## main\0" + "".join(f"?? file-{index}\0" for index in range(10002))
        return await original(*args, **kwargs)

    monkeypatch.setattr(worktrees, "_git", many)
    result = await git_ops.status(repo, repo)
    assert len(result["changes"]) == 10000 and result["changes_omitted"] == 2
