"""Adversarial path-security tests for :class:`nexus.tools.permissions.PathGuard`."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.tools.permissions import PathGuard, PathSecurityError


def make_guard(workspace: Path, **kwargs) -> PathGuard:
    return PathGuard(workspace, **kwargs)


def test_relative_path_resolves_under_workspace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace)
    resolved = guard.resolve("sub/file.txt", for_write=True)
    assert resolved.absolute == workspace / "sub" / "file.txt"
    assert resolved.key == str(workspace / "sub" / "file.txt")
    assert resolved.display == "sub/file.txt"
    assert resolved.inside_workspace is True


def test_absolute_path_inside_workspace_is_normalized(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "a" / "b.txt"
    guard = make_guard(workspace)
    resolved = guard.resolve(str(target), for_write=True)
    assert resolved.key == str(target)
    assert resolved.display == "a/b.txt"


def test_dotdot_read_leaves_workspace_but_resolves(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("x")
    guard = make_guard(workspace)
    resolved = guard.resolve("../outside.txt", for_write=False)
    assert resolved.absolute == tmp_path / "outside.txt"
    assert resolved.inside_workspace is False
    assert resolved.display == str(tmp_path / "outside.txt")


@pytest.mark.parametrize("raw", ["../outside.txt", "/etc/passwd", "../../etc/hosts"])
def test_workspace_write_root_rejects_escapes(tmp_path, raw):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace)
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve(raw, for_write=True)
    assert excinfo.value.code in {"write_root", "read_deny"}


def test_explicit_write_root_outside_workspace_is_allowed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    guard = make_guard(workspace, write_roots=["./", str(shared)])
    resolved = guard.resolve(str(shared / "out.txt"), for_write=True)
    assert resolved.absolute == shared / "out.txt"


def test_symlink_write_escape_fails_closed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "escape"
    link.symlink_to(outside, target_is_directory=True)
    guard = make_guard(workspace)
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve("escape/new.txt", for_write=True)
    assert excinfo.value.code == "write_root"


def test_missing_target_under_symlink_parent_fails_closed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    guard = make_guard(workspace)
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve("link/does-not-exist/yet.txt", for_write=True)
    assert excinfo.value.code == "write_root"


def test_prospective_write_resolves_existing_parent(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace)
    resolved = guard.resolve("newdir/newfile.txt", for_write=True)
    assert resolved.key == str(workspace / "newdir" / "newfile.txt")


def test_symlink_into_read_denyroot_fails_closed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "id_rsa").write_text("key")
    (workspace / "leak").symlink_to(secret, target_is_directory=True)
    guard = make_guard(workspace, read_denyroots=[str(secret)])
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve("leak/id_rsa", for_write=False)
    assert excinfo.value.code == "read_deny"


def test_read_denyroot_also_blocks_writes(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = workspace / "secret"
    secret.mkdir()
    guard = make_guard(workspace, read_denyroots=[str(secret)])
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve("secret/file", for_write=True)
    assert excinfo.value.code == "read_deny"


def test_read_denyroot_tilde_is_expanded(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "id_rsa"
    key.write_text("key")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace, read_denyroots=["~/.ssh"], home=home)
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve(str(key), for_write=False)
    assert excinfo.value.code == "read_deny"


@pytest.mark.parametrize(
    "raw,code",
    [
        ("bad\x00path", "nul_byte"),
        ("", "empty_path"),
        ("   ", "empty_path"),
        ("~/secrets", "home_expansion"),
        ("~root/.ssh", "home_expansion"),
        ("$HOME/.ssh", "env_expansion"),
        ("${HOME}/.ssh", "env_expansion"),
    ],
)
def test_rejected_tool_paths(tmp_path, raw, code):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace)
    with pytest.raises(PathSecurityError) as excinfo:
        guard.resolve(raw, for_write=True)
    assert excinfo.value.code == code


def test_non_string_path_rejected(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(PathSecurityError) as excinfo:
        make_guard(workspace).resolve(None, for_write=True)
    assert excinfo.value.code == "not_a_string"


def test_unicode_paths_resolve(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace)
    resolved = guard.resolve("données/naïve.txt", for_write=True)
    assert resolved.absolute == workspace / "données" / "naïve.txt"
    assert resolved.display == "données/naïve.txt"


def test_unicode_lookalikes_are_literal_names(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    guard = make_guard(workspace)
    # U+2024 ONE DOT LEADER is not a path separator or parent reference.
    resolved = guard.resolve("\u2024\u2024/etc/passwd", for_write=False)
    assert resolved.inside_workspace is True


def test_recheck_detects_symlink_swap(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    safe = workspace / "safe"
    safe.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "link"
    link.symlink_to(safe, target_is_directory=True)
    guard = make_guard(workspace)

    first = guard.recheck("link/file.txt", for_write=True)
    assert first.absolute == safe / "file.txt"

    link.unlink()
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathSecurityError) as excinfo:
        guard.recheck("link/file.txt", for_write=True)
    assert excinfo.value.code == "write_root"


def test_guard_rejects_bad_root_configuration(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(PathSecurityError):
        make_guard(workspace, write_roots=[""])
    with pytest.raises(PathSecurityError):
        make_guard(workspace, read_denyroots=["bad\x00root"])


def test_workspace_root_is_canonicalized(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    guard = make_guard(alias)
    resolved = guard.resolve("file.txt", for_write=True)
    assert resolved.absolute == real / "file.txt"
    assert resolved.display == "file.txt"
