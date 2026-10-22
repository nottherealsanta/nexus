"""Native reload is an outcome, not an action-owned process lifecycle."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.ui.ratatui.actions import ShellActions
from nexus.ui.ratatui import run as launcher


@pytest.mark.asyncio
async def test_reload_native_only_and_preserves_context(tmp_path):
    client = SimpleNamespace(enqueue=AsyncMock(), reload_extensions=AsyncMock(return_value={}))
    shell = ShellActions(SimpleNamespace(client=client, session="current"))
    shell.workspace = str(tmp_path)
    with pytest.raises(ValueError, match="only available in the native"):
        await shell.submit("/reload")
    shell.native_reload = True
    with pytest.raises(ValueError, match="no arguments"):
        await shell.submit("/reload extra")
    assert await shell.submit("/reload") is False
    assert shell.reload_request == launcher.ReloadRequest(tmp_path.resolve(), "current")
    client.enqueue.assert_not_awaited()
    client.reload_extensions.assert_not_awaited()
    assert await shell.submit("/reload-extensions") is True
    client.reload_extensions.assert_awaited_once_with(trigger="chat")


@pytest.mark.asyncio
async def test_cargo_artifact_used_explicitly(tmp_path, monkeypatch):
    executable = tmp_path / "actual-built-binary"
    executable.touch()
    message = {"reason": "compiler-artifact", "target": {"name": "nexus-ratatui"}, "executable": str(executable)}
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(message).encode(), None)))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", spawn)
    assert await launcher.rebuild() == executable
    argv = spawn.call_args.args
    assert argv[:5] == ("cargo", "build", "--release", "--locked", "--manifest-path")
    assert argv[5].endswith("rust/tui/Cargo.toml")
    assert "shell" not in spawn.call_args.kwargs
    await launcher.spawn(executable)
    assert spawn.call_args.args == (str(executable),)


@pytest.mark.asyncio
async def test_failed_build_reports_daemon_untouched(monkeypatch):
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b"", None)))
    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(RuntimeError, match="daemon was not stopped"):
        await launcher.rebuild()


@pytest.mark.asyncio
async def test_launcher_unwinds_before_build_and_restart(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from nexus import cli
    from nexus.ui import cli as ui_cli

    events = []
    clients = []

    async def open_client(workspace):
        events.append(("open", workspace))
        client = SimpleNamespace(aclose=AsyncMock(side_effect=lambda: events.append("close")))
        clients.append(client)
        return client

    async def spawn(binary=None):
        events.append(("spawn", binary))
        return object()

    outcomes = iter([launcher.ReloadRequest(tmp_path / "switched", "selected"), 0])

    async def run(client, **kwargs):
        events.append(("run", kwargs["session"]))
        return next(outcomes)

    @contextmanager
    def stderr_to(path):
        events.append("redirect")
        yield
        events.append("restore")

    async def release(process):
        events.append("release")

    async def rebuild():
        assert events[-3:] == ["close", "release", "restore"]
        events.append("build")
        return tmp_path / "built"

    async def restart(workspace):
        assert events[-1] == "build"
        events.append(("restart", workspace))

    monkeypatch.setattr(ui_cli, "open_client", open_client)
    for name, value in {"spawn": spawn, "run": run, "release": release,
                        "stderr_to": stderr_to, "rebuild": rebuild,
                        "restart_workspace": restart, "progress": AsyncMock()}.items():
        monkeypatch.setattr(launcher, name, value)
    assert await cli._chat(tmp_path, session="original") == 0
    assert ("spawn", tmp_path / "built") in events
    assert ("open", tmp_path / "switched") in events
    assert ("run", "selected") in events
    for client in clients:
        client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_checks_shutdown_before_reopen(tmp_path, monkeypatch):
    from nexus.host import daemon
    monkeypatch.setattr(daemon, "status", AsyncMock(return_value={"running": True, "pid": 123}))
    stop = AsyncMock(return_value=True)
    wait = AsyncMock(return_value=False)
    monkeypatch.setattr(daemon, "stop", stop)
    monkeypatch.setattr(daemon, "wait_stopped", wait)
    with pytest.raises(RuntimeError, match="timed out"):
        await launcher.restart_workspace(tmp_path)
    stop.assert_awaited_once_with(tmp_path)
    wait.assert_awaited_once_with(tmp_path, 123, polls=100, interval=0.1)


@pytest.mark.asyncio
async def test_release_kills_unresponsive_child():
    import asyncio
    from unittest.mock import Mock
    process = SimpleNamespace(returncode=None, stdin=SimpleNamespace(close=Mock()),
                              terminate=Mock(), kill=Mock(),
                              wait=AsyncMock(side_effect=[asyncio.TimeoutError, asyncio.TimeoutError, 0]))
    await launcher.release(process)
    process.stdin.close.assert_called_once()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
