"""Host workspace-file completion is bounded and path-only."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.host import HostFacade
from nexus.host import facade as host_facade
from nexus.host import protocol as p
from nexus.ui.cli.client import Client


def _facade(workspace: Path, *, denied: tuple[str, ...] = ()) -> HostFacade:
    permissions = SimpleNamespace(read_denyroots=list(denied))
    config = SimpleNamespace(v2=SimpleNamespace(permissions=permissions))
    runtime = SimpleNamespace(
        workspace=workspace,
        context=SimpleNamespace(effective_config=lambda: config),
    )
    return HostFacade(runtime)


def test_file_search_lists_normal_nested_files_in_stable_order(tmp_path):
    (tmp_path / "zeta.txt").write_text("z", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "alpha.py").write_text("a", encoding="utf-8")
    (tmp_path / "nested" / "other.txt").write_text("o", encoding="utf-8")
    facade = _facade(tmp_path)

    assert facade.search_files("") == [
        "zeta.txt",
        "nested/alpha.py",
        "nested/other.txt",
    ]
    assert facade.search_files("ALPHA") == ["nested/alpha.py"]


def test_file_search_ranks_name_matches_and_shallow_paths_first(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").touch()
    (tmp_path / "deep" / "er").mkdir(parents=True)
    (tmp_path / "deep" / "er" / "app.py").touch()
    (tmp_path / "myapp.txt").touch()
    (tmp_path / "application.md").touch()

    assert _facade(tmp_path).search_files("app") == [
        "application.md",
        "deep/er/app.py",
        "myapp.txt",
        "app/main.py",
    ]


def test_file_search_skips_git_ignored_paths(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").touch()
    (tmp_path / "debug.log").touch()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").touch()
    (tmp_path / "notes.md").touch()

    assert _facade(tmp_path).search_files("") == ["notes.md", "src/main.py"]


def test_file_search_skips_hidden_entries(tmp_path):
    (tmp_path / ".secret").write_text("hidden", encoding="utf-8")
    hidden_dir = tmp_path / ".private"
    hidden_dir.mkdir()
    (hidden_dir / "inside.txt").write_text("hidden", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")

    assert _facade(tmp_path).search_files("") == ["visible.txt"]


def test_file_search_skips_symlinks_and_symlink_escape(tmp_path):
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    (external / "outside.txt").write_text("outside", encoding="utf-8")
    (tmp_path / "inside.txt").write_text("inside", encoding="utf-8")
    (tmp_path / "escape").symlink_to(external, target_is_directory=True)
    (tmp_path / "linked.txt").symlink_to(external / "outside.txt")

    assert _facade(tmp_path).search_files("") == ["inside.txt"]


def test_file_search_prunes_read_denied_roots(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "public.txt").write_text("public", encoding="utf-8")

    assert _facade(tmp_path, denied=("private",)).search_files("") == [
        "public.txt"
    ]


def test_file_search_rejects_traversal_and_bounds_query(tmp_path):
    facade = _facade(tmp_path)
    for query in ("../outside", "sub/../../outside", "/etc/passwd", "a\\..\\b"):
        with pytest.raises(ValueError):
            facade.search_files(query)
    with pytest.raises(ValueError):
        facade.search_files("x" * 257)


def test_file_search_caps_limit_and_result_count(tmp_path):
    for index in range(125):
        (tmp_path / f"file-{index:03}.txt").touch()
    facade = _facade(tmp_path)

    assert facade.search_files("", limit=3) == [
        "file-000.txt",
        "file-001.txt",
        "file-002.txt",
    ]
    assert len(facade.search_files("", limit=10_000)) == 100
    assert facade.search_files("", limit=0) == []


def test_file_search_scandir_consumption_is_bounded(tmp_path, monkeypatch):
    consumed = 0

    class Entry:
        def __init__(self, name: str):
            self.name = name
            self.path = str(tmp_path / name)

        def is_symlink(self):
            return False

        def is_dir(self, *, follow_symlinks):
            return False

        def is_file(self, *, follow_symlinks):
            return True

    class Scan:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            nonlocal consumed
            for name in ("z.txt", "a.txt", "b.txt", "ignored.txt"):
                consumed += 1
                yield Entry(name)

    monkeypatch.setattr(host_facade, "_MAX_FILE_SEARCH_ENTRIES", 3)
    monkeypatch.setattr(host_facade.os, "scandir", lambda _path: Scan())

    assert _facade(tmp_path).search_files("") == ["a.txt", "b.txt", "z.txt"]
    assert consumed == 3


async def test_file_search_protocol_dispatch_and_client_api(tmp_path):
    (tmp_path / "alpha.txt").touch()
    (tmp_path / "beta.txt").touch()
    facade = _facade(tmp_path)

    command = p.FileSearch(query="alpha", limit=5)
    assert p.decode_command(p.encode_command(command)) == command
    result = await facade.handle(command)
    assert result == p.FileSearchResult(paths=["alpha.txt"])
    assert p.decode_result(p.encode_result(result)) == result

    class Transport:
        async def request(self, command):
            return await facade.handle(command)

        async def aclose(self):
            pass

        def events(self, *args, **kwargs):
            raise AssertionError("file search does not use event streams")

    client = Client(Transport())
    assert await client.search_files("beta") == ["beta.txt"]


async def test_file_search_dispatch_returns_error_for_traversal(tmp_path):
    result = await _facade(tmp_path).handle(p.FileSearch(query="../secret"))

    assert isinstance(result, p.ErrorResult)
