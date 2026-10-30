"""grep, glob, ls: confinement, .gitignore, clipping notices, read observations."""
from __future__ import annotations

import contextlib
import shutil
import subprocess

import pytest

from nexus.tools import DEFAULT_TOOLS, ToolContext, ToolError
from nexus.tools.glob import run as glob
from nexus.tools.grep import run as grep
from nexus.tools.ls import run as ls
from nexus.tools.walk import glob_match


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("import os\nneedle = 1\n")
    (tmp_path / "src" / "deep" / "b.py").write_text("NEEDLE = 2\n")
    (tmp_path / "notes.md").write_text("a needle in notes\n")
    (tmp_path / ".hidden").write_text("needle hidden\n")
    (tmp_path / "bin.dat").write_bytes(b"needle\x00\x01")
    return ToolContext(tmp_path)


@pytest.fixture(params=["rg", "python"])
def engine(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    elif not shutil.which("rg"):
        pytest.skip("rg is not installed")
    return request.param


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})


def test_registered_with_the_other_task_tools():
    names = [tool.spec.name for tool in DEFAULT_TOOLS]
    assert {"grep", "glob", "ls", "patch"} <= set(names)
    assert names[-1] == "code"


def test_glob_match_semantics():
    assert glob_match("src/deep/b.py", "**/*.py") and glob_match("a.py", "**/*.py")
    assert glob_match("src/a.py", "src/*.py") and not glob_match("src/deep/b.py", "src/*.py")
    assert not glob_match("src/a.pyc", "**/*.py")


async def test_grep_finds_lines_sorted_and_skips_hidden_and_binary(ctx, engine):
    out = await grep({"pattern": "needle", "case_insensitive": True}, ctx)
    assert out.splitlines() == ["notes.md:1:a needle in notes", "src/a.py:2:needle = 1", "src/deep/b.py:1:NEEDLE = 2"]
    assert (await grep({"pattern": "needle"}, ctx)).count("\n") == 1  # case sensitive: two matches
    assert ".hidden:1:needle hidden" in await grep({"pattern": "needle", "include_hidden": True}, ctx)


async def test_grep_path_glob_literal_and_no_match(ctx, engine):
    assert (await grep({"pattern": "needle", "path": "src", "glob": "*.py"}, ctx)).splitlines() == ["src/a.py:2:needle = 1"]
    assert await grep({"pattern": "a.b", "regex": False}, ctx) == "no matches"
    assert await grep({"pattern": "nothing-here"}, ctx) == "no matches"


async def test_grep_searches_a_single_file(ctx, engine):
    out = await grep({"pattern": "needle", "path": "src/a.py"}, ctx)
    assert out.splitlines() == ["src/a.py:2:needle = 1"]
    assert await grep({"pattern": "needle", "path": "src/a.py", "regex": False}, ctx) == "src/a.py:2:needle = 1"
    assert await grep({"pattern": "absent", "path": "notes.md"}, ctx) == "no matches"


async def test_grep_announces_clipping(ctx, engine):
    (ctx.workspace / "many.txt").write_text("hit\n" * 10)
    out = await grep({"pattern": "hit", "max_matches": 3}, ctx)
    assert out.count("many.txt:") == 3 and "there are more" in out
    (ctx.workspace / "wide.txt").write_text("w" + "x" * 5000 + "\n")
    out = await grep({"pattern": "^wx"}, ctx)
    assert "line clipped" in out or "omitted end of long line" in out


async def test_grep_refuses_bad_input(ctx, engine):
    with pytest.raises(ToolError, match="outside the workspace"):
        await grep({"pattern": "x", "path": ".."}, ctx)
    with pytest.raises(ToolError, match="does not exist"):
        await grep({"pattern": "x", "path": "nope.md"}, ctx)
    with pytest.raises(ToolError):
        await grep({"pattern": "("}, ctx)
    with pytest.raises(ToolError, match="non-empty"):
        await grep({"pattern": ""}, ctx)


async def test_grep_does_not_follow_symlinks_out(ctx, tmp_path_factory, engine):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("needle outside\n")
    (ctx.workspace / "link").symlink_to(outside)
    assert "outside" not in await grep({"pattern": "needle", "case_insensitive": True}, ctx)


async def test_grep_and_glob_respect_gitignore_and_see_new_files(ctx, engine):
    git(ctx.workspace, "init", "-q")
    (ctx.workspace / ".gitignore").write_text("ignored/\n")
    (ctx.workspace / "ignored").mkdir()
    (ctx.workspace / "ignored" / "x.py").write_text("needle ignored\n")
    out = await grep({"pattern": "needle"}, ctx)
    assert "ignored" not in out and "src/a.py" in out
    assert "ignored" not in await glob({"pattern": "**/*.py"}, ctx)
    (ctx.workspace / "fresh.py").write_text("x\n")  # untracked, written a moment ago
    assert "fresh.py" in await glob({"pattern": "*.py"}, ctx)


async def test_glob_matches_files_and_directories(ctx):
    assert (await glob({"pattern": "**/*.py"}, ctx)).splitlines() == ["src/a.py", "src/deep/b.py"]
    assert (await glob({"pattern": "*/", "path": "."}, ctx)).splitlines() == ["src/"]
    assert (await glob({"pattern": "*.py", "path": "src"}, ctx)).splitlines() == ["src/a.py"]
    assert await glob({"pattern": "*.rs"}, ctx) == "no matches"
    assert ".hidden" in await glob({"pattern": ".*", "include_hidden": True}, ctx)
    assert ".hidden" not in await glob({"pattern": "*"}, ctx)


async def test_glob_announces_clipping_and_refuses_bad_patterns(ctx):
    for i in range(250):
        (ctx.workspace / f"f{i:03}.txt").write_text("x")
    out = await glob({"pattern": "*.txt"}, ctx)
    assert "showing 200 of 250 matches" in out
    with pytest.raises(ToolError, match="relative"):
        await glob({"pattern": "/etc/*"}, ctx)
    with pytest.raises(ToolError, match="too many"):
        await glob({"pattern": "/".join(["**"] * 6)}, ctx)
    with pytest.raises(ToolError, match="outside the workspace"):
        await glob({"pattern": "*", "path": "../"}, ctx)


async def test_ls_lists_one_directory(ctx, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (ctx.workspace / "out").symlink_to(outside)
    (ctx.workspace / "in").symlink_to(ctx.workspace / "src")
    lines = (await ls({}, ctx)).splitlines()
    assert lines == ["bin.dat", "in@ -> src", "notes.md", "out@", "src/"]
    assert ".hidden" in await ls({"include_hidden": True}, ctx)
    assert (await ls({"path": "src"}, ctx)).splitlines() == ["a.py", "deep/"]
    (ctx.workspace / "empty").mkdir()
    assert await ls({"path": "empty"}, ctx) == "(empty directory)"
    with pytest.raises(ToolError):
        await ls({"path": "missing"}, ctx)
    with pytest.raises(ToolError, match="outside"):
        await ls({"path": "/"}, ctx)


async def test_ls_announces_clipping(ctx):
    for i in range(1005):
        (ctx.workspace / "src" / "deep" / f"{i}.t").write_text("")
    assert "showing 1000 of 1006 entries" in await ls({"path": "src/deep"}, ctx)


class Guard:
    """Records what a file tool asks of the session's read observations."""

    def __init__(self, error=None):
        self.error, self.required, self.forgotten = error, [], []

    @contextlib.asynccontextmanager
    async def mutation(self):
        yield

    def require(self, display, path, *, complete=False, affected=()):
        if self.error:
            raise ToolError(self.error)
        self.required.append((display, complete))

    def forget(self, paths):
        self.forgotten.extend(paths)
