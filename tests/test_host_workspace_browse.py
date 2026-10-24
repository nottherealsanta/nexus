"""Read-only workspace directory listing and Git status for the sidebar, through the host protocol."""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from nexus.host import protocol as p
from nexus.host.facade import HostFacade
from nexus.host_support import workspace_browse as wb


def _facade(workspace):
    return HostFacade(SimpleNamespace(workspace=workspace))


def _git(workspace, *args):
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


async def test_directory_listing_orders_labels_and_hides(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print()\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "Zed.txt").write_text("zz")
    (tmp_path / "apple.txt").write_text("a")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "link").symlink_to(tmp_path / "src")

    result = await _facade(tmp_path).handle(p.DirectoryList())
    assert isinstance(result, p.DirectoryListResult)
    assert result.path == "" and not result.truncated
    assert [(row["name"], row["kind"]) for row in result.entries] == [
        ("docs", "dir"), ("src", "dir"), ("apple.txt", "file"), ("link", "link"), ("Zed.txt", "file"),
    ]
    sizes = {row["name"]: row["size"] for row in result.entries}
    assert sizes["apple.txt"] == 1 and sizes["src"] is None and sizes["link"] is None

    child = await _facade(tmp_path).handle(p.DirectoryList(path="src"))
    assert isinstance(child, p.DirectoryListResult)
    assert child.path == "src"
    assert child.entries == [{"name": "main.py", "path": "src/main.py", "kind": "file", "size": 8}]


@pytest.mark.parametrize(
    "path",
    ["..", "../x", "/etc", "a/../b", ".git", "src/.hidden", "a\\b", "a\x00b", "a\nb", "a//b", "x" * 2000, "link"],
)
async def test_directory_listing_refuses_unsafe_paths(tmp_path, path):
    (tmp_path / "src").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "src")
    result = await _facade(tmp_path).handle(p.DirectoryList(path=path))
    assert isinstance(result, p.ErrorResult)


async def test_directory_listing_does_not_cross_a_nested_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    workspace = tmp_path / "ws"
    (workspace / "a").mkdir(parents=True)
    (workspace / "a" / "escape").symlink_to(outside)
    result = await _facade(workspace).handle(p.DirectoryList(path="a/escape"))
    assert isinstance(result, p.ErrorResult)
    listed = await _facade(workspace).handle(p.DirectoryList(path="a"))
    assert [(row["name"], row["kind"]) for row in listed.entries] == [("escape", "link")]


async def test_directory_listing_is_bounded(tmp_path):
    for index in range(12):
        (tmp_path / f"f{index:02d}.txt").write_text("x")
    result = await _facade(tmp_path).handle(p.DirectoryList(limit=5))
    assert isinstance(result, p.DirectoryListResult)
    assert len(result.entries) == 5 and result.truncated
    assert isinstance(await _facade(tmp_path).handle(p.DirectoryList(limit=0)), p.ErrorResult)
    capped = await _facade(tmp_path).handle(p.DirectoryList(limit=10_000))
    assert len(capped.entries) == 12 and not capped.truncated


async def test_directory_listing_honours_read_deny_roots(tmp_path, monkeypatch):
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "key.txt").write_text("k")
    (tmp_path / "open").mkdir()
    denied = ((tmp_path / "secret").resolve(),)
    monkeypatch.setattr(wb, "_file_search_denied_roots", lambda runtime, **_kwargs: denied)

    root = await _facade(tmp_path).handle(p.DirectoryList())
    assert [row["name"] for row in root.entries] == ["open"]
    assert isinstance(await _facade(tmp_path).handle(p.DirectoryList(path="secret")), p.ErrorResult)


async def test_file_preview_reads_text_through_the_host_and_redacts(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('héllo')\ntoken = abcdef123456\n")
    result = await _facade(tmp_path).handle(p.FilePreview(path="src/a.py"))
    assert isinstance(result, p.FilePreviewResult)
    assert (result.path, result.status, result.truncated) == ("src/a.py", "text", False)
    assert "héllo" in result.text and "abcdef123456" not in result.text
    assert result.size == result.shown == len("print('héllo')\ntoken = abcdef123456\n".encode())


async def test_file_preview_clips_and_never_splits_a_character(tmp_path):
    (tmp_path / "big.txt").write_text("é" * 100)  # two bytes each
    result = await _facade(tmp_path).handle(p.FilePreview(path="big.txt", max_bytes=51))
    assert result.status == "text" and result.truncated
    assert result.text == "é" * 25 and result.shown == 50 and result.size == 200
    capped = await _facade(tmp_path).handle(p.FilePreview(path="big.txt", max_bytes=10**9))
    assert capped.shown == 200 and not capped.truncated
    for bad in (0, -1):
        assert isinstance(await _facade(tmp_path).handle(p.FilePreview(path="big.txt", max_bytes=bad)), p.ErrorResult)
    (tmp_path / "huge.txt").write_bytes(b"a" * (wb.MAX_PREVIEW_BYTES + 5))
    huge = await _facade(tmp_path).handle(p.FilePreview(path="huge.txt", max_bytes=10**9))
    assert huge.shown == wb.MAX_PREVIEW_BYTES and huge.truncated and huge.size == wb.MAX_PREVIEW_BYTES + 5


async def test_file_preview_hides_binary_and_empty_files(tmp_path):
    (tmp_path / "nul.bin").write_bytes(b"ab\x00cd")
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9 au lait")
    (tmp_path / "empty.txt").write_bytes(b"")
    for name in ("nul.bin", "latin.txt"):
        result = await _facade(tmp_path).handle(p.FilePreview(path=name))
        assert result.status == "binary" and result.text == "" and result.shown == 0 and result.message
    empty = await _facade(tmp_path).handle(p.FilePreview(path="empty.txt"))
    assert (empty.status, empty.text, empty.size) == ("empty", "", 0)


@pytest.mark.parametrize(
    "path",
    ["", "..", "../x", "/etc/passwd", "a/../b", ".env", "src/.hidden", "a\\b", "a\x00b", "src", "link.txt", "dirlink/f.txt",
     "missing.txt", "x" * 2000, ".git/config"],
)
async def test_file_preview_refuses_unsafe_paths(tmp_path, path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("secret")
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / ".hidden").write_text("h")
    (workspace / ".env").write_text("SECRET=1")
    (workspace / "link.txt").symlink_to(outside / "f.txt")
    (workspace / "dirlink").symlink_to(outside)
    result = await _facade(workspace).handle(p.FilePreview(path=path))
    assert isinstance(result, p.ErrorResult)
    assert "secret" not in str(result)


async def test_file_preview_refuses_credential_names_special_files_and_deny_roots(tmp_path, monkeypatch):
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "key.txt").write_text("k")
    (tmp_path / "sk-abcdef123456.txt").write_text("k")
    monkeypatch.setattr(
        wb, "_file_search_denied_roots",
        lambda runtime, **_kwargs: ((tmp_path / "secret").resolve(),),
    )
    for path in ("secret/key.txt", "sk-abcdef123456.txt"):
        assert isinstance(await _facade(tmp_path).handle(p.FilePreview(path=path)), p.ErrorResult)
    if hasattr(os, "mkfifo"):
        os.mkfifo(tmp_path / "pipe")
        assert isinstance(await _facade(tmp_path).handle(p.FilePreview(path="pipe")), p.ErrorResult)


def test_normalize_relative_accepts_plain_paths():
    assert wb.normalize_relative("") == ""
    assert wb.normalize_relative(".") == ""
    assert wb.normalize_relative("src/pkg/") == "src/pkg"
    with pytest.raises(ValueError):
        wb.normalize_relative(None)


def test_parse_status_labels_every_group():
    data = (
        b"# branch.oid abc\0# branch.head main\0# branch.upstream origin/main\0# branch.ab +2 -1\0"
        b"1 M. N... 100644 100644 100644 aaa bbb staged.py\0"
        b"1 .M N... 100644 100644 100644 aaa bbb dirty.py\0"
        b"1 AM N... 100644 100644 100644 aaa bbb both.py\0"
        b"u UU N... 100644 100644 100644 100644 a b c clash.py\0"
        b"? new file.txt\0"
    )
    parsed = wb.parse_status(data)
    assert (parsed["branch"], parsed["upstream"], parsed["ahead"], parsed["behind"]) == ("main", "origin/main", 2, 1)
    assert [(row["group"], row["label"], row["path"]) for row in parsed["rows"]] == [
        ("staged", "modified", "staged.py"),
        ("unstaged", "modified", "dirty.py"),
        ("staged", "added", "both.py"),
        ("unstaged", "modified", "both.py"),
        ("conflicts", "unmerged", "clash.py"),
        ("untracked", "untracked", "new file.txt"),
    ]


async def test_git_status_reports_labelled_changes(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "nexus@example.test")
    _git(tmp_path, "config", "user.name", "Nexus Test")
    (tmp_path / "tracked.txt").write_text("before\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "start")
    (tmp_path / "tracked.txt").write_text("after\n")
    (tmp_path / "staged.txt").write_text("new\n")
    _git(tmp_path, "add", "staged.txt")
    (tmp_path / "loose.txt").write_text("loose\n")

    result = await _facade(tmp_path).handle(p.GitStatus())
    assert isinstance(result, p.GitStatusResult) and result.available
    rows = {(row["group"], row["path"]): row["label"] for row in result.rows}
    assert rows == {
        ("unstaged", "tracked.txt"): "modified",
        ("staged", "staged.txt"): "added",
        ("untracked", "loose.txt"): "untracked",
    }
    assert result.counts == {"unstaged": 1, "staged": 1, "untracked": 1}
    assert not result.truncated
    # Reading the status leaves the repository untouched.
    assert (tmp_path / "tracked.txt").read_text() == "after\n"


async def test_git_status_outside_a_repository_is_unavailable(tmp_path):
    result = await _facade(tmp_path).handle(p.GitStatus())
    assert isinstance(result, p.GitStatusResult)
    assert not result.available and result.message and result.rows == []


async def test_git_status_is_bounded(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    for index in range(8):
        (tmp_path / f"u{index}.txt").write_text("x")
    monkeypatch.setattr(wb, "MAX_STATUS_ENTRIES", 3)
    result = await _facade(tmp_path).handle(p.GitStatus())
    assert isinstance(result, p.GitStatusResult)
    assert len(result.rows) == 3 and result.truncated


async def test_project_directory_listing_outside_session_is_readonly_and_validated(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "src").mkdir()
    (outside / "readme.txt").write_text("x")
    result = await _facade(workspace).handle(p.ProjectDirectoryList(path=str(outside)))
    assert isinstance(result, p.DirectoryListResult)
    assert result.path == str(outside.resolve())
    assert [(r["name"], r["kind"]) for r in result.entries] == [("src", "dir"), ("readme.txt", "file")]
    assert all(r["path"].startswith(str(outside.resolve())) for r in result.entries)
    for path in ("relative", str(outside / "readme.txt"), str(outside / "missing"), "/bad\npath"):
        assert isinstance(await _facade(workspace).handle(p.ProjectDirectoryList(path=path)), p.ErrorResult)
    limited = await _facade(workspace).handle(p.ProjectDirectoryList(path=str(outside), limit=1))
    assert len(limited.entries) == 1 and limited.truncated
    # The original workspace-scoped contract is unchanged.
    assert isinstance(await _facade(workspace).handle(p.DirectoryList(path=str(outside))), p.ErrorResult)


async def test_project_directory_listing_honours_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "_file_search_denied_roots", lambda runtime, **kwargs: (tmp_path.resolve(),))
    assert isinstance(await _facade(tmp_path).handle(p.ProjectDirectoryList(path=str(tmp_path))), p.ErrorResult)
