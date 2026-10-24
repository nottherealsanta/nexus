from __future__ import annotations

import pytest

from nexus.config import Config
from nexus.config.layers import build_v2
from nexus.config.schema import ConfigV2, WorktreesSection
from nexus.errors import ConfigError


def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_worktree_defaults_and_project_configuration_load(tmp_path):
    assert WorktreesSection() == ConfigV2().worktrees
    assert WorktreesSection().base == "current"
    assert WorktreesSection().auto_remove_clean is False
    assert WorktreesSection().max_worktrees == 16
    assert WorktreesSection().commit_max_files == 2000
    assert WorktreesSection().commit_max_file_mb == 50

    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n[worktrees]\nbase = "release/next"\n'
        'setup = ["uv", "sync"]\nsubmodules = "recursive"\n',
        encoding="utf-8",
    )
    loaded = Config.load(tmp_path, home=_home(tmp_path), environ={})
    assert loaded.v2.worktrees.base == "release/next"
    assert loaded.v2.worktrees.setup == ["uv", "sync"]
    assert loaded.v2.worktrees.submodules == "recursive"
    assert loaded.v2.worktrees.auto_remove_clean is False


@pytest.mark.parametrize("key", ["sessions", "checkpoints"])
def test_removed_worktree_keys_are_rejected(key):
    with pytest.raises(ConfigError):
        build_v2({"worktrees": {key: True}})


@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"auto_remove_clean": 1}, "auto_remove_clean"),
        ({"max_worktrees": 0}, "max_worktrees"),
        ({"max_worktrees": True}, "max_worktrees"),
        ({"commit_max_files": 0}, "commit_max_files"),
        ({"commit_max_file_mb": 2049}, "commit_max_file_mb"),
        ({"setup_timeout": 1801}, "setup_timeout"),
        ({"copy_max_mb": -1}, "copy_max_mb"),
        ({"base": "../main"}, "base"),
        ({"base": "branch with spaces"}, "base"),
        ({"setup": "npm ci"}, "setup"),
        ({"setup": ["uv", ""]}, "setup"),
        ({"copy": ["../outside"]}, "copy"),
        ({"share_dirs": ["/outside"]}, "share_dirs"),
    ],
)
def test_worktree_config_values_are_bounded(kwargs, field):
    with pytest.raises((ValueError, TypeError), match=field):
        WorktreesSection(**kwargs)


def test_setup_shell_requires_string_and_enables_shell_form():
    configured = WorktreesSection(setup="npm ci", setup_shell=True)
    assert configured.setup == "npm ci" and configured.setup_shell
    with pytest.raises(ValueError, match="requires worktrees.setup"):
        WorktreesSection(setup_shell=True)


@pytest.mark.parametrize(
    "values,field",
    [
        ({"auto_remove_clean": "true"}, "auto_remove_clean"),
        ({"submodules": "all"}, "submodules"),
        ({"setup_on_fail": "continue"}, "setup_on_fail"),
        ({"setup_for_subagents": "inherit"}, "setup_for_subagents"),
    ],
)
def test_worktree_enum_values_are_validated_during_config_load(values, field):
    with pytest.raises(ConfigError, match=field):
        build_v2({"worktrees": values})


def test_project_layer_cannot_set_user_only_worktree_limit(tmp_path):
    home = _home(tmp_path)
    (tmp_path / "nexus.toml").write_text(
        "config_version = 2\n[worktrees]\nmax_worktrees = 2\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="worktrees.max_worktrees.*user-level"):
        Config.load(tmp_path, home=home, environ={})


def test_user_layer_can_set_worktree_limit(tmp_path):
    home = _home(tmp_path)
    config_dir = home / ".nexus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "config_version = 2\n[worktrees]\nmax_worktrees = 32\n", encoding="utf-8"
    )
    assert Config.load(tmp_path, home=home, environ={}).v2.worktrees.max_worktrees == 32


@pytest.mark.parametrize("value", ["true", "false"])
@pytest.mark.parametrize("path", ["nexus.toml", ".agents/nexus.toml", ".nexus/nexus.toml"])
def test_project_layers_cannot_set_auto_remove_clean(tmp_path, path, value):
    home = _home(tmp_path)
    config = tmp_path / path
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"config_version = 2\n[worktrees]\nauto_remove_clean = {value}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="worktrees.auto_remove_clean.*user-level"):
        Config.load(tmp_path, home=home, environ={})


def test_user_layer_can_enable_auto_remove_clean_with_project_worktree_settings(tmp_path):
    home = _home(tmp_path)
    config_dir = home / ".nexus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "config_version = 2\n[worktrees]\nauto_remove_clean = true\n", encoding="utf-8"
    )
    (tmp_path / "nexus.toml").write_text(
        'config_version = 2\n[worktrees]\nbase = "main"\n', encoding="utf-8"
    )
    loaded = Config.load(tmp_path, home=home, environ={})
    assert loaded.v2.worktrees.auto_remove_clean is True
    assert loaded.v2.worktrees.base == "main"
