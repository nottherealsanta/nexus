from pathlib import Path

import pytest

from nexus import files
from nexus.files import count_entries, mutate_path


def test_mutations_and_confined_paths(tmp_path: Path):
    mutate_path(tmp_path, "create_dir", "folder")
    mutate_path(tmp_path, "create_file", "folder/a")
    mutate_path(tmp_path, "rename", "folder/a", "folder/b")
    assert (tmp_path / "folder/b").is_file()
    with pytest.raises(ValueError):
        mutate_path(tmp_path, "create_file", "folder/b")
    mutate_path(tmp_path, "create_file", "folder/existing")
    (tmp_path / "folder" / "outside").symlink_to(tmp_path.parent, target_is_directory=True)
    assert count_entries(tmp_path, "folder") == 3
    with pytest.raises(ValueError):
        mutate_path(tmp_path, "rename", "folder/b", "folder/existing")
    assert (tmp_path / "folder/b").exists()
    with pytest.raises(ValueError):
        mutate_path(tmp_path, "rename", "folder/b", "../outside")
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        mutate_path(tmp_path, "create_file", "link/escape")
    with pytest.raises(ValueError):
        mutate_path(tmp_path, "delete", "link")
    mutate_path(tmp_path, "delete", "folder")
    assert not (tmp_path / "folder").exists()


def test_count_budget_fails_without_reporting_a_partial_count(tmp_path: Path, monkeypatch):
    (tmp_path / "a").touch()
    times = iter([0.0, 4.0])
    monkeypatch.setattr(files.time, "monotonic", lambda: next(times))
    with pytest.raises(ValueError, match="too large to count"):
        count_entries(tmp_path, "")


def test_count_rejects_directory_replaced_by_symlink(tmp_path: Path, monkeypatch):
    folder = tmp_path / "child"
    folder.mkdir()
    real_open = files.os.open

    def swap(path, flags, *args, **kwargs):
        if path == "child" and kwargs.get("dir_fd") is not None:
            folder.rmdir()
            folder.symlink_to(tmp_path.parent, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(files.os, "open", swap)
    with pytest.raises(ValueError):
        count_entries(tmp_path, "")
