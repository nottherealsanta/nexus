"""Read-only bounded Git diff projection through the host protocol."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from nexus.host import protocol as p
from nexus.host.facade import HostFacade


def _git(workspace, *args):
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


async def test_git_diff_is_scoped_and_bounded(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "nexus@example.test")
    _git(tmp_path, "config", "user.name", "Nexus Test")
    path = tmp_path / "note.txt"
    path.write_text("before\n")
    _git(tmp_path, "add", "note.txt")
    _git(tmp_path, "commit", "-qm", "start")
    path.write_text("after\n")
    facade = HostFacade(SimpleNamespace(workspace=tmp_path))

    result = await facade.handle(p.GitDiff())
    assert isinstance(result, p.GitDiffResult)
    assert "+after" in result.patch and not result.truncated
    assert isinstance(await facade.handle(p.GitDiff(ref="--output=/tmp/leak")), p.ErrorResult)

    _git(tmp_path, "add", "note.txt")
    staged = await facade.handle(p.GitDiff(staged=True))
    assert isinstance(staged, p.GitDiffResult) and "+after" in staged.patch

    path.write_text("x" * 300_000)
    large = await facade.handle(p.GitDiff())
    assert isinstance(large, p.GitDiffResult)
    assert large.truncated and len(large.patch.encode()) <= 256 * 1024
