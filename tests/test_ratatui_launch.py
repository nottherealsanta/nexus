"""Native launch/packaging seams stay lazy and preserve non-TUI imports."""
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from nexus.ui.ratatui.run import binary_path


def test_binary_override_is_explicit_and_executable(tmp_path, monkeypatch):
    path = tmp_path / "native"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    monkeypatch.setenv("NEXUS_TUI_BINARY", str(path))
    assert binary_path() == path
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="missing"):
        binary_path()


def test_native_import_does_not_import_textual():
    result = subprocess.run([sys.executable, "-c", "import sys; import nexus.ui.ratatui.run; assert 'textual' not in sys.modules"], cwd=Path(__file__).resolve().parents[1], env={**os.environ, "PYTHONPATH": "."}, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_cli_launch_routes_native_without_textual(monkeypatch, tmp_path):
    from nexus import cli
    import nexus.ui.cli
    import nexus.ui.ratatui.run
    client = AsyncMock()
    monkeypatch.setattr(nexus.ui.cli, "open_client", AsyncMock(return_value=client))
    launch = AsyncMock(return_value=0)
    monkeypatch.setattr(nexus.ui.ratatui.run, "run", launch)
    assert await cli._chat(tmp_path, session="s", renderer="ratatui") == 0
    assert launch.await_args.args == (client,)
    assert launch.await_args.kwargs["workspace"] == tmp_path
    client.aclose.assert_awaited_once()
