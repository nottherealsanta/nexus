"""Shared Rust build folder per repository (plan 36)."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from nexus import cargo_env, doctor
from nexus.tools.base import ToolContext
from nexus.tools.bash import run as bash_run


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS10_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo"))
    monkeypatch.delenv("CARGO_BUILD_BUILD_DIR", raising=False)
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    cargo_env.git_common_dir.cache_clear()
    return tmp_path


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x", cwd=path)
    return path


def test_worktrees_share_one_folder_and_repos_differ(env):
    main = repo(env / "a")
    git("worktree", "add", "-q", str(env / "a-wt"), "-b", "wt", cwd=main)
    (main / "sub").mkdir()
    other = repo(env / "b")
    first = cargo_env.extra_env(main / "sub")[cargo_env.VAR]
    assert first == cargo_env.extra_env(env / "a-wt")[cargo_env.VAR]
    assert first.startswith(str(env / "cargo" / "build" / "nexus-"))
    assert cargo_env.extra_env(other)[cargo_env.VAR] != first


def test_outside_git_adds_nothing(env):
    (env / "plain").mkdir()
    assert cargo_env.extra_env(env / "plain") == {}
    assert cargo_env.process_env(env / "plain") is None


@pytest.mark.parametrize("var", ["CARGO_BUILD_BUILD_DIR", "CARGO_TARGET_DIR"])
def test_user_environment_wins(env, monkeypatch, var):
    main = repo(env / "a")
    monkeypatch.setenv(var, "/mine")
    assert cargo_env.extra_env(main) == {}


def test_cargo_config_build_dir_wins(env):
    main = repo(env / "a")
    (main / "crate").mkdir()
    (main / ".cargo").mkdir()
    (main / ".cargo" / "config.toml").write_text('[build]\nbuild-dir = "elsewhere"\n')
    assert cargo_env.extra_env(main / "crate") == {}


def test_setting_turns_it_off(env):
    main = repo(env / "a")
    (env / "home").mkdir()
    (env / "home" / "config.toml").write_text("[tools]\nshared_rust_builds = false\n")
    assert cargo_env.extra_env(main) == {}


def test_bash_tool_sees_the_variable(env):
    main = repo(env / "a")
    out = asyncio.run(bash_run({"command": "echo $CARGO_BUILD_BUILD_DIR"}, ToolContext(workspace=main)))
    assert cargo_env.extra_env(main)[cargo_env.VAR] in out


def test_doctor_reports_size(env):
    folder = env / "cargo" / "build" / "nexus-abc"
    folder.mkdir(parents=True)
    (folder / "x").write_bytes(b"0" * 2048)
    [row] = doctor.check_rust_builds()
    assert row["name"] == "rust builds" and "1 shared build folder" in row["detail"] and "rm -rf" in row["detail"]
