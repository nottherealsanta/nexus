from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from nexus.host import HostFacade
from nexus.host import protocol as p


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    _git(main, "config", "user.name", "Nexus Test")
    _git(main, "config", "user.email", "nexus@example.test")
    (main / "shared.txt").write_text("project copy\n")
    _git(main, "add", "shared.txt")
    _git(main, "commit", "-qm", "initial")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-qb", "feature/session", str(linked))
    (linked / "shared.txt").write_text("session copy\n")
    (linked / "session-only.txt").write_text("only here\n")
    return main, linked


def _facade(main: Path, linked: Path) -> HostFacade:
    guard = SimpleNamespace(read_denyroots=())
    runtime = SimpleNamespace(
        workspace=main,
        session_root=lambda session: SimpleNamespace(path=linked if session == "feature" else main),
        session_path_guard=lambda session: guard,
    )
    facade = object.__new__(HostFacade)
    facade.runtime = runtime
    return facade


def test_workspace_git_and_file_commands_use_session_root(tmp_path):
    main, linked = _repo(tmp_path)
    facade = _facade(main, linked)

    status = asyncio.run(facade._dispatch(p.GitStatus(session="feature")))
    assert status.available
    assert status.branch == "feature/session"
    assert {row["path"] for row in status.rows} == {"session-only.txt", "shared.txt"}

    preview = asyncio.run(facade._dispatch(p.FilePreview("shared.txt", session="feature")))
    assert preview.text == "session copy\n"

    listing = asyncio.run(facade._dispatch(p.DirectoryList(session="feature")))
    assert {entry["name"] for entry in listing.entries} == {"shared.txt", "session-only.txt"}

    diff = asyncio.run(facade._dispatch(p.GitDiff(session="feature")))
    assert "session copy" in diff.patch


def test_empty_session_selects_project_root(tmp_path):
    main, linked = _repo(tmp_path)
    facade = _facade(main, linked)

    status = asyncio.run(facade._dispatch(p.GitStatus()))
    assert status.available
    assert status.branch

    preview = asyncio.run(facade._dispatch(p.FilePreview("shared.txt")))
    assert preview.text == "project copy\n"
