"""git_head: subprocess-free branch/worktree projection."""

from __future__ import annotations

from pathlib import Path

from nexus.host_support.git_head import git_head

SHA = "0123456789abcdef0123456789abcdef01234567"


def _repo(root: Path, head: str = "ref: refs/heads/main\n") -> Path:
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(head)
    return root


def test_normal_repo_branch(tmp_path: Path) -> None:
    _repo(tmp_path)
    info = git_head(tmp_path)
    assert info == {
        "root": str(tmp_path.resolve()),
        "branch": "main",
        "detached": False,
        "worktree": False,
        "worktree_name": "",
    }


def test_nested_workspace(tmp_path: Path) -> None:
    _repo(tmp_path, "ref: refs/heads/feat/x\n")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    info = git_head(str(sub))
    assert info["branch"] == "feat/x"
    assert info["root"] == str(tmp_path.resolve())


def test_detached_head(tmp_path: Path) -> None:
    _repo(tmp_path, SHA + "\n")
    info = git_head(tmp_path)
    assert info["branch"] == SHA[:7]
    assert info["detached"] is True


def test_linked_worktree(tmp_path: Path) -> None:
    main = _repo(tmp_path / "main")
    gitdir = main / ".git" / "worktrees" / "feat"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/feat\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n")
    info = git_head(wt)
    assert info["branch"] == "feat"
    assert info["worktree"] is True
    assert info["worktree_name"] == "feat"
    assert info["root"] == str(wt.resolve())


def test_not_a_repo(tmp_path: Path) -> None:
    ws = tmp_path / "plain"
    ws.mkdir()
    # tmp_path may sit inside a repo on some machines; only assert no raise there.
    assert isinstance(git_head(ws), dict)
    assert git_head(None) == {}
    assert git_head(tmp_path / "missing" / "deeper") in ({}, git_head(tmp_path))


def test_malformed_head(tmp_path: Path) -> None:
    _repo(tmp_path, "\x00\x01\x02")
    assert isinstance(git_head(tmp_path), dict)
    (tmp_path / ".git" / "HEAD").write_bytes(b"\xff\xfe" * 10)
    assert isinstance(git_head(tmp_path), dict)
    (tmp_path / ".git" / "HEAD").unlink()
    assert git_head(tmp_path) == {}
    bad = tmp_path / "x"
    bad.mkdir()
    (bad / ".git").write_text("garbage")
    assert git_head(bad) == {}
