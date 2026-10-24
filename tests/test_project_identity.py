"""Project resolution uses bounded filesystem metadata, preserving full paths."""
import subprocess

import pytest

from nexus.config.paths import project_key
from nexus.host_support.project_identity import resolve_project


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True,
                          env={'PATH': '/usr/bin:/bin', 'HOME': str(root),
                               'GIT_CONFIG_NOSYSTEM': '1'})


def test_checkout_subdirectory_and_linked_worktree_share_identity(tmp_path):
    main = tmp_path / 'main'
    main.mkdir()
    git(main, 'init')
    git(main, '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'seed')
    sub = main / 'sub'
    sub.mkdir()
    linked = tmp_path / 'linked'
    git(main, 'worktree', 'add', str(linked), '-b', 'linked')
    root = resolve_project(main)
    assert root.key == project_key(main)
    assert resolve_project(sub).root == main
    identity = resolve_project(linked)
    assert identity.root == main and identity.key == root.key
    assert identity.launch_worktree == str(linked)
    assert resolve_project(sub).launch_root == sub


def test_submodule_pointer_is_an_independent_repo(tmp_path):
    main = tmp_path / 'main'
    modules = main / '.git/modules/pkg'
    modules.mkdir(parents=True)
    (main / '.git/HEAD').write_text('ref: refs/heads/main\n')
    (modules / 'HEAD').write_text('ref: refs/heads/main\n')
    sub = main / 'pkg'
    sub.mkdir()
    (sub / '.git').write_text('gitdir: ../.git/modules/pkg\n')
    assert resolve_project(sub).root == sub
    assert resolve_project(sub).kind == 'git'


def test_plain_missing_target_and_missing_root(tmp_path):
    assert resolve_project(tmp_path).kind == 'plain'
    (tmp_path / '.git').write_text('gitdir: missing\n')
    assert resolve_project(tmp_path).root == tmp_path
    assert resolve_project(tmp_path).kind == 'plain'
    with pytest.raises(ValueError, match='does not exist'):
        resolve_project(tmp_path / 'missing')


def test_walk_is_bounded_and_paths_are_not_display_truncated(tmp_path):
    (tmp_path / '.git').mkdir()
    (tmp_path / '.git/HEAD').write_text('ref: refs/heads/main\n')
    path = tmp_path
    for _ in range(65):
        path /= 'level'
    path.mkdir(parents=True)
    identity = resolve_project(path)
    assert identity.kind == 'plain' and identity.root == path
    assert len(str(identity.root)) > 200


@pytest.mark.parametrize('path', ['', 'relative/path', '/tmp/with\nnewline', '/tmp/with\x00null', '/' + 'x' * 4096])
def test_project_identity_rejects_invalid_paths(path):
    with pytest.raises(ValueError):
        resolve_project(path)


def test_project_identity_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    project = tmp_path / 'project'
    project.mkdir()
    assert resolve_project('~/project').root == project.resolve()
