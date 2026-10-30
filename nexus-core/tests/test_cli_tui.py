"""Plain `nexus` and `nexus tui` open the terminal client (an installed `nexus` is the Core CLI; the TUI is a separate binary)."""
from __future__ import annotations

import os
import stat

import pytest

from nexus import cli


@pytest.fixture
def fake_tui(tmp_path, monkeypatch):
    tui = tmp_path / "nexus-tui"
    tui.write_text("#!/bin/sh\n")
    tui.chmod(tui.stat().st_mode | stat.S_IEXEC)
    calls = []
    monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in ("NEXUS_TUI_LAUNCH_DIR", "NEXUS_TUI_CORE_CMD", "PREFIX"):
        monkeypatch.delenv(name, raising=False)
    return tui, calls


def test_plain_nexus_opens_the_tui_in_the_current_directory(fake_tui, tmp_path, monkeypatch):
    tui, calls = fake_tui
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 0
    assert calls == [(str(tui), [str(tui)])]
    assert os.environ["NEXUS_TUI_LAUNCH_DIR"] == str(tmp_path.resolve()) or os.environ["NEXUS_TUI_LAUNCH_DIR"] == str(tmp_path)


def test_tui_subcommand_passes_flags_through(fake_tui):
    tui, calls = fake_tui
    cli.main(["tui", "--light", "--256"])
    assert calls == [(str(tui), [str(tui), "--light", "--256"])]


def test_other_commands_are_not_intercepted(fake_tui, capsys):
    _, calls = fake_tui
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert not calls
    assert "tui" in capsys.readouterr().out


def test_without_the_tui_binary_it_says_how_to_install_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PREFIX", str(tmp_path))
    assert cli.main([]) == 1
    assert "nexus-tui" in capsys.readouterr().err
