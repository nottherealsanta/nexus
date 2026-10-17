"""``/restart`` stops the workspace daemon from the client and reattaches."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nexus.host import daemon as daemon_mod
from nexus.ui.cli import commands
from nexus.ui.ratatui.actions import ShellActions


def test_restart_is_a_declared_command():
    parsed = commands.parse("/restart")
    assert parsed.name == "/restart" and parsed.spec is commands.BY_NAME["/restart"]
    assert not parsed.spec.hidden and "/restart" in commands.help_text()


def _shell(tmp_path, monkeypatch, *, reconnect=True):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = SimpleNamespace(aclose=AsyncMock())
    order = []
    controller = SimpleNamespace(client=old, session="s", switch_session=AsyncMock(), bootstrap=AsyncMock(), order=order)

    async def pause():
        order.append("pause")

    controller.pause = pause
    controller.unpause = lambda: order.append("unpause")
    controller.replace_client = lambda client: (setattr(controller, "client", client), old)[1]
    shell = ShellActions(controller)
    shell.workspace = str(tmp_path)
    new = SimpleNamespace(aclose=AsyncMock())
    shell.reconnect = AsyncMock(return_value=new) if reconnect else None
    return shell, controller, old, new


@pytest.mark.asyncio
async def test_restart_pauses_follow_waits_for_the_pid_and_reattaches(tmp_path, monkeypatch):
    shell, controller, old, new = _shell(tmp_path, monkeypatch)
    # status goes "not running" at socket removal, before the process exits: it must not be trusted.
    status = AsyncMock(return_value={"running": True, "pid": 7})
    monkeypatch.setattr(daemon_mod, "status", status)
    stop = AsyncMock(side_effect=lambda *a, **k: controller.order.append("stop") or True)
    monkeypatch.setattr(daemon_mod, "stop", stop)
    wait = AsyncMock(side_effect=lambda *a, **k: controller.order.append("wait") or True)
    monkeypatch.setattr(daemon_mod, "wait_stopped", wait)
    assert await shell.command("/restart", ()) is True
    assert status.await_count == 1
    assert wait.await_args.args[1] == 7
    shell.reconnect.assert_awaited_once()
    assert controller.client is new
    old.aclose.assert_awaited_once()
    controller.switch_session.assert_awaited_once_with("s")
    controller.bootstrap.assert_awaited_once()
    assert controller.order == ["pause", "stop", "wait", "unpause"]
    assert shell.notice.startswith("Restarted")


@pytest.mark.asyncio
async def test_restart_fails_when_stop_is_refused(tmp_path, monkeypatch):
    shell, controller, old, new = _shell(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "status", AsyncMock(return_value={"running": True, "pid": 7}))
    monkeypatch.setattr(daemon_mod, "stop", AsyncMock(return_value=False))
    wait = AsyncMock()
    monkeypatch.setattr(daemon_mod, "wait_stopped", wait)
    with pytest.raises(RuntimeError, match="did not accept"):
        await shell.command("/restart", ())
    wait.assert_not_awaited()
    shell.reconnect.assert_not_awaited()
    assert controller.client is old
    assert controller.order == ["pause", "unpause"]  # following resumes on the old client


@pytest.mark.asyncio
async def test_restart_starts_fresh_when_no_daemon_was_running(tmp_path, monkeypatch):
    shell, controller, old, new = _shell(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "status", AsyncMock(return_value={"running": False}))
    monkeypatch.setattr(daemon_mod, "stop", AsyncMock(return_value=False))
    wait = AsyncMock(return_value=True)
    monkeypatch.setattr(daemon_mod, "wait_stopped", wait)
    await shell.command("/restart", ())
    assert wait.await_args.args[1] is None  # no pid: the lock is probed instead
    assert controller.client is new


@pytest.mark.asyncio
async def test_restart_reports_a_daemon_that_will_not_exit(tmp_path, monkeypatch):
    shell, controller, old, new = _shell(tmp_path, monkeypatch)
    monkeypatch.setattr(daemon_mod, "status", AsyncMock(return_value={"running": True, "pid": 1}))
    monkeypatch.setattr(daemon_mod, "stop", AsyncMock(return_value=True))
    monkeypatch.setattr(daemon_mod, "wait_stopped", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="did not stop"):
        await shell.command("/restart", ())
    shell.reconnect.assert_not_awaited()
    assert controller.client is old
    assert controller.order[-1] == "unpause"


@pytest.mark.asyncio
async def test_wait_stopped_uses_the_pid_then_the_lock(tmp_path, monkeypatch):
    alive = iter([True, True, False])
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: next(alive))
    assert await daemon_mod.wait_stopped(tmp_path, 99, home=tmp_path, polls=5, interval=0)
    monkeypatch.setattr(daemon_mod, "_pid_alive", lambda pid: True)
    assert not await daemon_mod.wait_stopped(tmp_path, 99, home=tmp_path, polls=2, interval=0)

    import fcntl
    import os
    lock = daemon_mod.default_lock_path(tmp_path.resolve(), home=tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert not await daemon_mod.wait_stopped(tmp_path, None, home=tmp_path, polls=2, interval=0)
    finally:
        os.close(fd)
    assert await daemon_mod.wait_stopped(tmp_path, None, home=tmp_path, polls=2, interval=0)


@pytest.mark.asyncio
async def test_controller_pause_stops_follow_and_auto_reconnect():
    from nexus.client.protocol import ClientError
    from nexus.ui.ratatui.controller import NativeController

    async def lost(*args, **kwargs):
        raise ClientError("lost")
        yield

    async def idle(*args, **kwargs):
        await asyncio.sleep(60)
        yield

    client = SimpleNamespace(stream=idle, aclose=AsyncMock())
    controller = NativeController(client, "s")
    controller.reconnect = AsyncMock()
    post = AsyncMock()
    controller.resume(post)
    await asyncio.sleep(0)
    task = controller._task
    await controller.pause()
    assert task.cancelled() and controller._task is None
    controller.resume(post)  # a stray resume (action loop) must not re-arm while paused
    assert controller._task is None
    controller.unpause()
    assert controller._task is not None and not controller._task.done()
    await controller.pause()

    # A reconnect already in flight when paused is discarded without installing a second client.
    replacement = SimpleNamespace(aclose=AsyncMock())
    paused = NativeController(SimpleNamespace(stream=lost, aclose=AsyncMock()), "s")

    async def reconnect():
        paused._paused = True
        return replacement

    paused.reconnect = reconnect
    await paused._follow("s", AsyncMock())
    replacement.aclose.assert_awaited_once()
    assert paused.client is not replacement


@pytest.mark.asyncio
async def test_restart_validates_arguments_and_workspace(tmp_path, monkeypatch):
    shell, *_ = _shell(tmp_path, monkeypatch)
    stop = AsyncMock()
    monkeypatch.setattr(daemon_mod, "stop", stop)
    with pytest.raises(ValueError, match="Usage: /restart"):
        await shell.command("/restart", ("now",))
    shell.workspace = ""
    with pytest.raises(ValueError, match="workspace is unknown"):
        await shell.command("/restart", ())
    stop.assert_not_awaited()
