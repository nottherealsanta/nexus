"""Desktop launch is lazy, host-only, and works without an interactive terminal."""
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from nexus.ui.desktop.run import binary_path


def test_desktop_binary_override_is_explicit(tmp_path, monkeypatch):
    binary = tmp_path / "nexus-desktop"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setenv("NEXUS_DESKTOP_BINARY", str(binary))
    assert binary_path() == binary.resolve()
    binary.chmod(0o600)
    with pytest.raises(RuntimeError, match="Desktop executable is missing"):
        binary_path()


def test_desktop_import_is_lazy():
    result = subprocess.run([sys.executable, "-c", "import sys; import nexus.ui.desktop.run; assert 'textual' not in sys.modules; assert 'nexus.runtime' not in sys.modules"], capture_output=True)
    assert result.returncode == 0, result.stderr


def test_desktop_cli_does_not_need_a_tty(tmp_path, monkeypatch):
    from nexus import cli
    import nexus.ui.desktop.run

    launch = AsyncMock(return_value=0)
    monkeypatch.setattr(nexus.ui.desktop.run, "run", launch)
    assert cli.main(["--workspace", str(tmp_path), "desktop", "--session", "saved"]) == 0
    assert launch.await_args.args == (tmp_path,)
    assert launch.await_args.kwargs == {"session": "saved"}


@pytest.mark.asyncio
async def test_desktop_passes_existing_client_to_shared_bridge(tmp_path, monkeypatch):
    import nexus.ui.desktop.run as desktop
    import nexus.ui.ratatui.prototype as bridge

    binary = tmp_path / "desktop"
    monkeypatch.setattr(desktop, "binary_path", lambda: binary)
    launch = AsyncMock(return_value=0)
    monkeypatch.setattr(bridge, "run", launch)
    client, reconnect = object(), object()
    assert await desktop.run(tmp_path, session="s", client=client, reconnect=reconnect) == 0
    assert launch.await_args.args == (tmp_path.resolve(), "s", binary)
    assert launch.await_args.kwargs == {"client": client, "reconnect": reconnect, "desktop": True}


def test_freshest_desktop_source_binary_wins(tmp_path, monkeypatch):
    import os
    import nexus.ui.desktop.run as desktop

    root = tmp_path / "repo"
    monkeypatch.delenv("NEXUS_DESKTOP_BINARY", raising=False)
    monkeypatch.setattr(desktop, "__file__", str(root / "nexus/ui/desktop/run.py"))
    monkeypatch.setattr(desktop.shutil, "which", lambda _: None)
    monkeypatch.setattr(desktop.sys, "executable", str(tmp_path / "bin/python"))
    for profile, age in (("release", 100), ("debug", 0)):
        path = root / f"rust/desktop/target/{profile}/nexus-desktop"
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o700)
        os.utime(path, (path.stat().st_mtime - age, path.stat().st_mtime - age))
    assert binary_path() == (root / "rust/desktop/target/debug/nexus-desktop").resolve()
