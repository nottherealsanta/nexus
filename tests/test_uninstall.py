"""``nexus uninstall``: show everything, ask, stop daemons, remove it all, then the program."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus import cli
from nexus.host_support import install
from nexus.host_support import uninstall as un


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake machine: Nexus home with sessions and models, HF speech caches, TUI prefs."""
    home = tmp_path / "user" / ".nexus"
    (home / "models" / "voice" / "parakeet").mkdir(parents=True)
    (home / "models" / "speech" / "python" / "en_core_web_sm").mkdir(parents=True)
    (home / "nexus.db").write_bytes(b"x" * 2048)
    (home / "credentials.json").write_text("{}")
    hub = tmp_path / "hub"
    (hub / "models--sahilmahendrakar--Paradee-8M-v1.0" / "snapshots").mkdir(parents=True)
    (hub / "models--hexgrad--Kokoro-82M" / "snapshots").mkdir(parents=True)
    (hub / "models--someone--other").mkdir(parents=True)
    xdg = tmp_path / "xdg"
    (xdg / "nexus").mkdir(parents=True)
    (xdg / "nexus" / "tui.json").write_text("{}")
    monkeypatch.setenv("NEXUS_HOME", str(home))
    monkeypatch.setenv("HF_HUB_CACHE", str(hub))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user"))
    stopped = []

    async def stop_all(home=None):
        stopped.append(True)
        return 1

    ran: list[list[str]] = []
    monkeypatch.setattr(install, "stop_all_daemons", stop_all)
    monkeypatch.setattr(install, "install_method", lambda: "uv-tool")
    monkeypatch.setattr(install, "find_uv", lambda environ=None: "/u/uv")
    monkeypatch.setattr(subprocess, "run", lambda cmd, check=False: ran.append(cmd) or SimpleNamespace(returncode=0))
    return SimpleNamespace(home=home, hub=hub, xdg=xdg, stopped=stopped, ran=ran)


def _answer(monkeypatch, text, tty=True):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr("builtins.input", lambda *a: text)


def test_plan_lists_home_models_and_preferences_but_not_other_caches(machine):
    paths = {t.path for t in un.uninstall_plan()}
    assert machine.home in paths
    assert machine.xdg / "nexus" / "tui.json" in paths
    assert machine.hub / "models--sahilmahendrakar--Paradee-8M-v1.0" in paths
    assert machine.hub / "models--hexgrad--Kokoro-82M" in paths
    assert machine.hub / "models--someone--other" not in paths
    home = next(t for t in un.uninstall_plan() if t.path == machine.home)
    assert home.size >= 2048 and "sessions" in home.label and "dictation model" in home.label


def test_declining_removes_nothing(machine, monkeypatch, capsys):
    _answer(monkeypatch, "n")
    assert cli.main(["uninstall"]) == 1
    out = capsys.readouterr().out
    assert str(machine.home) in out and "uv tool uninstall nexus-harness" in out
    assert "Cancelled" in out and machine.home.exists() and machine.stopped == [] and machine.ran == []


def test_confirming_removes_everything_then_the_program(machine, monkeypatch, capsys):
    _answer(monkeypatch, "y")
    assert cli.main(["uninstall"]) == 0
    assert not machine.home.exists() and not (machine.xdg / "nexus").exists()
    assert not (machine.hub / "models--sahilmahendrakar--Paradee-8M-v1.0").exists()
    assert not (machine.hub / "models--hexgrad--Kokoro-82M").exists()
    assert (machine.hub / "models--someone--other").exists()
    assert machine.stopped == [True]
    assert machine.ran == [["/u/uv", "tool", "uninstall", "nexus-harness"]]
    assert "Nexus is uninstalled." in capsys.readouterr().out


def test_without_a_terminal_it_needs_yes(machine, monkeypatch, capsys):
    _answer(monkeypatch, "y", tty=False)
    assert cli.main(["uninstall"]) == 1
    assert "pass --yes" in capsys.readouterr().err and machine.home.exists()
    assert cli.main(["uninstall", "--yes"]) == 0 and not machine.home.exists()


def test_editable_installs_keep_the_program(machine, monkeypatch, capsys):
    monkeypatch.setattr(install, "install_method", lambda: "editable")
    assert cli.main(["uninstall", "--yes"]) == 0
    assert machine.ran == [] and "not removed (editable install" in capsys.readouterr().out


@pytest.mark.parametrize("where", ["user-home", "no-db"])
def test_a_home_that_is_not_a_nexus_home_is_refused(machine, monkeypatch, capsys, tmp_path, where):
    target = tmp_path / "user" if where == "user-home" else tmp_path / "projects"
    target.mkdir(exist_ok=True)
    (target / "keep.txt").write_text("mine")
    monkeypatch.setenv("NEXUS_HOME", str(target))
    assert cli.main(["uninstall", "--yes"]) == 1
    assert "Nothing was removed" in capsys.readouterr().err
    assert (target / "keep.txt").exists() and machine.stopped == []


def test_a_symlinked_home_removes_the_link_not_its_target(machine, monkeypatch, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "nexus.db").write_text("db")
    link = tmp_path / "user" / "linked" / ".nexus"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    monkeypatch.setenv("NEXUS_HOME", str(link))
    assert un.remove(un.Target(link, "home", 0)) is None
    assert not link.is_symlink() and (real / "nexus.db").exists()


def test_human_sizes():
    assert un.human_size(512) == "512 B"
    assert un.human_size(2_500_000) == "2.5 MB"
    assert un.human_size(1_318_912_000) == "1.3 GB"
